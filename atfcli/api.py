"""HTTP client + credential handling for the ATF CLI.

The CLI never bundles credentials. A token comes from, in order of precedence:

1. `--token` (or `ATF_TOKEN`)          — explicit, for CI one-liners
2. the token file (`~/.atf/cli.json`)  — written by `atf auth login`
3. a locally minted admin token        — when the repo/containers are on this host

The third path is what makes the CLI usable before anyone has a password: on the
machine that runs the stack, an operator already has the access needed to read
the signing material, so the CLI can derive the same HMAC key core derives and
issue itself a short-lived admin token. It is local-machine trust, not a
backdoor: without filesystem/container access to this host, it cannot be done.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .console import Console
from .context import RepoContext
from .errors import AuthError, AtfError, TimeoutError_, UnreachableError, http_status_error

TOKEN_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_URLS = ("http://127.0.0.1:8088", "http://127.0.0.1:8080")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class Credential:
    token: str
    url: str
    exp: int = 0
    source: str = "unknown"
    username: str = ""
    user_id: int = 0
    admin: bool = False

    @property
    def expired(self) -> bool:
        return self.exp > 0 and self.exp < int(time.time())

    @property
    def seconds_left(self) -> int:
        return max(0, self.exp - int(time.time())) if self.exp else 0

    def safe(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "source": self.source,
            "username": self.username,
            "user_id": self.user_id,
            "admin": self.admin,
            "expires_in_s": self.seconds_left,
        }


class TokenStore:
    """`~/.atf/cli.json`, mode 0600, one entry per server URL."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.environ.get("ATF_CLI_HOME", Path.home() / ".atf")) / "cli.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def get(self, url: str) -> Credential | None:
        entry = self._load().get("servers", {}).get(url)
        if not isinstance(entry, dict) or not entry.get("token"):
            return None
        return Credential(
            token=entry["token"],
            url=url,
            exp=int(entry.get("exp", 0)),
            source="token file",
            username=entry.get("username", ""),
            user_id=int(entry.get("user_id", 0)),
            admin=bool(entry.get("admin", False)),
        )

    def set(self, cred: Credential) -> None:
        data = self._load()
        data.setdefault("servers", {})[cred.url] = {
            "token": cred.token,
            "exp": cred.exp,
            "username": cred.username,
            "user_id": cred.user_id,
            "admin": cred.admin,
            "saved_at": int(time.time()),
        }
        self._save(data)

    def clear(self, url: str | None = None) -> None:
        if url is None:
            if self.path.exists():
                self.path.unlink()
            return
        data = self._load()
        servers = data.get("servers", {})
        servers.pop(url, None)
        data["servers"] = servers
        self._save(data)

    def servers(self) -> list[str]:
        return sorted(self._load().get("servers", {}).keys())


def jwt_payload(token: str) -> dict:
    """Decode a JWT payload without verifying it — for display only."""
    try:
        _, payload_b64, _ = token.split(".")
        data = json.loads(_b64url_decode(payload_b64))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class ApiClient:
    """Thin, explicit HTTP client. One method per verb, no magic."""

    def __init__(
        self,
        console: Console,
        *,
        url: str = "",
        token: str = "",
        timeout: float = 20.0,
        verify_tls: bool = True,
        context: RepoContext | None = None,
    ) -> None:
        self.out = console
        self.repo = context
        self.url = (url or "").rstrip("/")
        self.token = token
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.store = TokenStore()
        self._minted: Credential | None = None

    # -------------------------------------------------------------- discovery
    def resolve_url(self, *, probe: bool = True) -> str:
        if self.url:
            return self.url
        env_url = os.environ.get("ATF_CLI_URL") or os.environ.get("ATF_API_URL")
        if env_url:
            self.url = env_url.rstrip("/")
            return self.url
        for candidate in DEFAULT_URLS:
            if not probe or self._alive(candidate):
                self.url = candidate
                return self.url
        # Nothing answered; keep the first default so the error message names a
        # concrete address instead of "no URL".
        self.url = DEFAULT_URLS[0]
        return self.url

    def _alive(self, base: str) -> bool:
        try:
            status, _ = self.raw("GET", f"{base}/healthz", with_auth=False, timeout=3.0)
            return status < 500
        except AtfError:
            return False

    # ------------------------------------------------------------ credentials
    def load_credential(self, *, allow_mint: bool = True) -> Credential:
        explicit = self.token or os.environ.get("ATF_TOKEN", "")
        url = self.resolve_url()
        if explicit:
            self.out.register_secret(explicit)
            payload = jwt_payload(explicit)
            cred = Credential(
                token=explicit,
                url=url,
                exp=int(payload.get("exp", 0)),
                source="--token/env",
                username=str(payload.get("username", "")),
                user_id=int(payload.get("sub", 0) or 0),
                admin=bool(payload.get("admin", False)),
            )
            self.token = cred.token
            return cred

        stored = self.store.get(url)
        if stored and not stored.expired:
            self.out.register_secret(stored.token)
            self.token = stored.token
            return stored

        if stored and stored.expired:
            self.out.debug(f"stored token for {url} expired; re-minting")

        if allow_mint:
            cred = self.mint_local_admin(url)
            self.token = cred.token
            return cred

        raise AuthError(
            "no credential available",
            hint="run `atf auth login --username <user>` or pass --token",
        )

    def mint_local_admin(
        self,
        url: str = "",
        *,
        user_id: int = 0,
        username: str = "cli-admin",
        ttl: int = TOKEN_TTL_SECONDS,
    ) -> Credential:
        """Issue an admin token signed with the same key core derives.

        The key comes from `ATF_JWT_SECRET` when set, else from the DB path the
        core container was started with — which is how core itself falls back.
        The secret is never printed and is registered with the console so it
        cannot leak into output.
        """
        url = url or self.resolve_url()
        secret, how = self._signing_key()
        if not secret:
            raise AuthError(
                "cannot derive a local signing key",
                hint=(
                    "set ATF_JWT_SECRET, or run this on the host that runs the stack "
                    "(so the core container's ATF_DB_PATH is readable)"
                ),
            )

        if user_id == 0:
            user_id = self._default_admin_id()
        if not username or username == "cli-admin":
            username = os.environ.get("ATF_CLI_USERNAME", "") or f"cli-{user_id}"

        now = int(time.time())
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = _b64url(
            json.dumps(
                {
                    "sub": str(user_id),
                    "username": username,
                    "admin": True,
                    "iat": now,
                    "exp": now + ttl,
                    "cli": True,
                }
            ).encode()
        )
        signing_input = f"{header}.{payload}".encode("ascii")
        sig = _b64url(hmac.new(secret, signing_input, hashlib.sha256).digest())
        token = f"{header}.{payload}.{sig}"
        self.out.register_secret(token)
        self.out.debug(f"minted a local admin token via {how}")
        return Credential(
            token=token,
            url=url,
            exp=now + ttl,
            source=f"local mint ({how})",
            username=username,
            user_id=user_id,
            admin=True,
        )

    def _signing_key(self) -> tuple[bytes, str]:
        env_secret = os.environ.get("ATF_JWT_SECRET") or (
            self.repo.env("ATF_JWT_SECRET") if self.repo else ""
        )
        if env_secret:
            self.out.register_secret(env_secret)
            return env_secret.encode("utf-8"), "ATF_JWT_SECRET"

        if self.repo:
            # Prefer what the running container actually has, then what compose
            # would inject: the derivation is keyed on that exact string.
            for candidate in (
                self.repo.container_env("core", "ATF_DB_PATH"),
                self.repo.env("ATF_DB_PATH"),
                "/app/data/atf.db",
            ):
                if candidate:
                    seed = f"atf-jwt::{candidate}".encode("utf-8")
                    return hashlib.sha256(seed).digest(), f"db path {candidate}"
        return b"", ""

    def _default_admin_id(self) -> int:
        for key in ("ADMIN_USER_ID", "ATF_ADMIN_USER_ID"):
            if self.repo:
                raw = self.repo.env(key) or self.repo.container_env("core", key)
            else:
                raw = os.environ.get(key, "")
            digits = "".join(ch for ch in str(raw) if ch.isdigit())
            if digits:
                return int(digits)
        return 0

    # -------------------------------------------------------------- transport
    def _ssl_context(self):
        if self.url.startswith("https") and not self.verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    def raw(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        with_auth: bool = True,
        timeout: float | None = None,
        accept: str = "application/json",
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Accept", accept)
        if body is not None and not (headers or {}).get("Content-Type"):
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if with_auth and self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout or self.timeout,
                context=self._ssl_context(),
            ) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if "timed out" in str(reason).lower():
                raise TimeoutError_(
                    f"{method} {url} timed out after {timeout or self.timeout}s"
                ) from exc
            raise UnreachableError(
                f"cannot reach {url}: {reason}",
                hint="check the gateway is up (`atf doctor`) or pass --url",
            ) from exc
        except TimeoutError as exc:
            raise TimeoutError_(f"{method} {url} timed out") from exc
        except ssl.SSLError as exc:
            raise UnreachableError(
                f"TLS failure talking to {url}: {exc}",
                hint="pass --insecure for a self-signed certificate",
            ) from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Any = None,
        raw_body: bytes | None = None,
        timeout: float | None = None,
        accept: str = "application/json",
        with_auth: bool = True,
        expect: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        url = f"{self.resolve_url()}{path}"
        if params:
            clean = {
                k: v
                for k, v in params.items()
                if v is not None and v != "" and v is not False
            }
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"
        body = raw_body
        if payload is not None and raw_body is None:
            body = json.dumps(payload).encode("utf-8")
        status, data = self.raw(method, url, body=body, timeout=timeout, accept=accept)
        if expect and status not in expect:
            message = self._error_message(data, status)
            raise http_status_error(status, message, hint=self._hint_for(status, path))
        if accept == "application/json" and data:
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return {"raw": data.decode("utf-8", "replace")}
        return data.decode("utf-8", "replace") if data else ""

    @staticmethod
    def _error_message(data: bytes, status: int) -> str:
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                for key in ("error", "message", "detail"):
                    if parsed.get(key):
                        return str(parsed[key])
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        text = data.decode("utf-8", "replace").strip()
        return text[:400] if text else f"HTTP {status}"

    @staticmethod
    def _hint_for(status: int, path: str) -> str:
        if status in (401, 403):
            return "run `atf auth login` or `atf auth token --save`"
        if status == 404:
            return f"no such endpoint or object: {path}"
        if status in (502, 503, 504):
            return "a backend service is down: `atf doctor`"
        return ""

    # ------------------------------------------------------------- vocabulary
    def get(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw) -> Any:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw) -> Any:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw) -> Any:
        return self.request("DELETE", path, **kw)

    def stream(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str = "text/csv",
        timeout: float | None = None,
    ) -> bytes:
        """Fetch a downloadable body (log exports)."""
        return self.request(
            "GET", path, params=params, accept=accept, timeout=timeout or 120.0
        )

    # ------------------------------------------------------------------- auth
    def login(self, username: str, password: str) -> Credential:
        self.out.register_secret(password)
        url = self.resolve_url()
        body = json.dumps({"username": username, "password": password}).encode()
        status, data = self.raw(
            "POST",
            f"{url}/api/auth/login",
            body=body,
            with_auth=False,
        )
        if status not in (200, 201):
            raise AuthError(
                self._error_message(data, status),
                hint="check the username/password; the dashboard login is the same credential",
            )
        parsed = json.loads(data or b"{}")
        token = (
            parsed.get("token")
            or parsed.get("access_token")
            or (parsed.get("session") or {}).get("token", "")
        )
        if not token:
            raise AuthError("the server did not return a token")
        self.out.register_secret(token)
        payload = jwt_payload(token)
        return Credential(
            token=token,
            url=url,
            exp=int(payload.get("exp", 0)) or int(time.time()) + 7 * 24 * 3600,
            source="password login",
            username=str(payload.get("username", username)),
            user_id=int(payload.get("sub", parsed.get("user_id", 0)) or 0),
            admin=bool(payload.get("admin", parsed.get("is_admin", False))),
        )

    def whoami(self) -> dict:
        """Confirm the credential against the server, not just locally."""
        try:
            data = self.get("/api/auth/me")
            if isinstance(data, dict):
                return data
        except AtfError:
            pass
        payload = jwt_payload(self.token)
        return {
            "user_id": int(payload.get("sub", 0) or 0),
            "username": payload.get("username", ""),
            "is_admin": bool(payload.get("admin", False)),
            "source": "local token payload",
        }
