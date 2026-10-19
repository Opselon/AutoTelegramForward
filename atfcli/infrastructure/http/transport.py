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

Three rules protect the token once it exists:

* **It only travels over a URL the CLI validated** — `http(s)` with a real host.
  A `file://` or `ftp://` base URL is a configuration mistake that turns a
  network client into a file reader, so it is refused, not coerced.
* **It never follows a redirect to another origin.** urllib copies every header
  (Authorization included) onto a redirect it follows, which would hand the
  token to whatever host the URL replies with. Same-host hops are followed —
  including the `http` -> `https` upgrade — and anything else is refused with
  the address to use instead.
* **It is retried only when retrying is safe.** A GET/HEAD/PUT/DELETE that hit a
  connection reset or a 5xx is attempted again with bounded exponential
  backoff (honouring `Retry-After`); a POST is not, because a retried write is a
  second write.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from ...console import Console
from ..host.repo import RepoContext
from ...domain.errors import AuthError, AtfError, TimeoutError_, UnreachableError, http_status_error
from ...domain.validators import normalise_base_url

TOKEN_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_URLS = ("http://127.0.0.1:8088", "http://127.0.0.1:8080")

#: Extra attempts after the first one. Two means "a blip is not a failure" while
#: keeping the worst case (all attempts time out) to three timeouts, not ten.
DEFAULT_RETRIES = 2
#: Exponential backoff: 0.25s, 0.5s, 1s … capped, with jitter so a fleet of cron
#: jobs hitting a restarting API does not synchronise into a thundering herd.
BACKOFF_BASE = 0.25
BACKOFF_CAP = 2.0
#: A server may ask for a longer pause than our backoff; honour it, but never
#: let a hostile or misconfigured `Retry-After: 86400` stall a terminal.
RETRY_AFTER_CAP = 5.0
#: Statuses worth another attempt. 408/425/429 are explicitly "try again";
#: 500/502/503/504 are what a booting or restarting gateway answers.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Methods that may be replayed without changing the outcome.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
#: Redirect budget for one request.
MAX_REDIRECTS = 5


class CrossOriginRedirect(AtfError):
    """The API answered with a redirect that would carry the token elsewhere."""

    exit_code = 5  # unreachable: the URL the operator wants is not the one they gave


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _origin(url: str) -> tuple[str, int | None]:
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return (parts.hostname or "").lower(), port


def default_user_agent() -> str:
    """Identify the CLI (and its version) to the gateway, for its access log."""
    import platform

    from ... import __version__

    return (
        f"atf-cli/{__version__} ({platform.system()} {platform.machine() or 'unknown'}; "
        f"Python {platform.python_version()})"
    )


def _parse_retry_after(value: str) -> float | None:
    """`Retry-After` in either form the RFC allows: delay-seconds or HTTP-date."""
    text = (value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, moment.timestamp() - time.time())


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only while the credential cannot leave its origin.

    urllib's default handler copies the request headers — `Authorization`
    included — onto every hop it follows, so a 302 to an attacker's host hands
    over the bearer token. Here a hop is allowed when the host:port is unchanged
    (same host, any path) or when the same host upgrades `http` -> `https`
    (a strictly better channel). Everything else raises, and `raw()` turns that
    into the address the operator should have configured.
    """

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = req.full_url
        old_host, old_port = _origin(old)
        new_host, new_port = _origin(newurl)
        same_host = old_host == new_host
        upgrade = urllib.parse.urlsplit(old).scheme == "http" and urllib.parse.urlsplit(newurl).scheme == "https"
        if same_host and (old_port == new_port or upgrade):
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        raise CrossOriginRedirect(
            f"refused a redirect from {old} to {newurl}",
            hint=(
                "a bearer token must never be sent to another origin — point "
                "--url (or ATF_CLI_URL) at the address the API redirects to"
            ),
        )



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
    """`~/.atf/cli.json`, mode 0600, one entry per server URL.

    The file also carries the operator's default server (`default_url`), so a
    host that talks to a remote dashboard does not need `ATF_CLI_URL` exported in
    every shell — see `atf auth use`.

    File permissions are part of the contract, not an implementation detail: a
    token file that another local user can read is already compromised, so a
    loose mode is reported and tightened to 0600 before the token is used. On
    Windows `chmod` only toggles the read-only bit, so the check is POSIX-only
    and the warning says what it can.
    """

    def __init__(self, path: Path | None = None) -> None:
        home = os.environ.get("ATF_CLI_HOME") or str(Path.home() / ".atf")
        self.path = path or Path(home) / "cli.json"
        self._warned = False

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            # A corrupt store must not take the CLI down, but it must not be
            # silently ignored either: the next `set` overwrites it, so the
            # operator needs to know the old tokens are gone.
            print(
                f"warning: ignoring unreadable token store {self.path} "
                "(re-run `atf auth login` to rewrite it)",
                file=sys.stderr,
            )
            return {}

    def _check_permissions(self) -> None:
        if os.name != "posix" or self._warned:
            return
        try:
            mode = self.path.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                print(
                    f"warning: {self.path} is readable by other users (mode {mode:o})",
                    file=sys.stderr,
                )
            else:
                print(
                    f"warning: {self.path} was readable by other users (mode {mode:o}); "
                    "tightened it to 600",
                    file=sys.stderr,
                )
        self._warned = True

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        # Atomic replace: a reader (or a concurrent writer) sees either the old
        # document or the new one, never a half-written file.
        tmp.replace(self.path)

    def get(self, url: str) -> Credential | None:
        entry = self._load().get("servers", {}).get(url)
        if not isinstance(entry, dict) or not entry.get("token"):
            return None
        self._check_permissions()
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
        if data.get("default_url") == url:
            data.pop("default_url", None)
        self._save(data)

    def servers(self) -> list[str]:
        return sorted(self._load().get("servers", {}).keys())

    # ------------------------------------------------------------ default host
    def default_url(self) -> str:
        value = self._load().get("default_url", "")
        return value if isinstance(value, str) else ""

    def set_default_url(self, url: str) -> str:
        """Persist (and return) the canonical default server URL; "" clears it."""
        data = self._load()
        canonical = normalise_base_url(url, source="default server") if url else ""
        if canonical:
            data["default_url"] = canonical
        else:
            data.pop("default_url", None)
        self._save(data)
        return canonical

    def entries(self) -> list[dict[str, Any]]:
        """Every saved server with its non-secret metadata, for `atf auth servers`."""
        saved = self._load().get("servers", {})
        rows: list[dict[str, Any]] = []
        for url, entry in sorted(saved.items()):
            if not isinstance(entry, dict) or not entry.get("token"):
                continue
            cred = self.get(url)
            if cred is None:
                continue
            rows.append({**cred.safe(), "default": url == self.default_url()})
        return rows


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
        retries: int = DEFAULT_RETRIES,
        backoff_base: float = BACKOFF_BASE,
        sleep: Callable[[float], None] = time.sleep,
        agent: str = "",
    ) -> None:
        self.out = console
        self.repo = context
        self.url = self._base_url(url, source="--url") if url else ""
        self.token = token
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.retries = max(0, int(retries))
        self.backoff_base = max(0.0, float(backoff_base))
        self.sleep = sleep
        self.user_agent = agent or default_user_agent()
        self.store = TokenStore()
        self._minted: Credential | None = None
        self._opener_cache: urllib.request.OpenerDirector | None = None
        # The credential resolved for this client, so `load_credential` is
        # idempotent and the reported source stays truthful.
        self._credential: Credential | None = None

    @staticmethod
    def _base_url(value: str, *, source: str) -> str:
        """Validate and canonicalise a base URL, as a usage error when it is wrong."""
        from ...domain.errors import UsageError

        try:
            return normalise_base_url(value, source=source)
        except ValueError as exc:
            raise UsageError(str(exc), hint="e.g. --url https://atf.example.com") from exc

    # -------------------------------------------------------------- discovery
    def resolve_url(self, *, probe: bool = True) -> str:
        """The API base URL: flag, then environment, then the saved default, then probe.

        The saved default (`atf auth use`) sits below the environment on purpose:
        an exported `ATF_CLI_URL` is a statement about *this* shell, while the
        stored value is the machine's habit. Both are validated before a token
        is ever attached to them.
        """
        if self.url:
            return self.url
        for source in ("ATF_CLI_URL", "ATF_API_URL"):
            raw = os.environ.get(source, "")
            if raw:
                self.url = self._base_url(raw, source=source)
                return self.url
        stored = self.store.default_url()
        if stored:
            self.url = stored
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
            status, _ = self.raw(
                "GET", f"{base}/healthz", with_auth=False, timeout=3.0, retries=0
            )
            return status < 500
        except AtfError:
            return False

    # ------------------------------------------------------------ credentials
    def load_credential(self, *, allow_mint: bool = True) -> Credential:
        """Resolve the bearer token once per client, cheapest tier first.

        The result is cached: without this, a second call would see the token
        this method just installed on `self.token` and re-classify a locally
        minted credential as an explicitly passed one — which is how a
        "minted" token ends up labelled `--token/env` in `atf auth token`.
        """
        if self._credential is not None and not self._credential.expired:
            return self._credential
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
            self._credential = cred
            return cred

        stored = self.store.get(url)
        if stored and not stored.expired:
            self.out.register_secret(stored.token)
            self.token = stored.token
            self._credential = stored
            return stored

        if stored and stored.expired:
            self.out.warn(
                f"the saved token for {url} expired — minting a local admin token"
            )

        if allow_mint:
            cred = self.mint_local_admin(url)
            self.token = cred.token
            self._credential = cred
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
        if not user_id:
            # A token is only as good as the identity it claims. Minting one for
            # "user 0" would authenticate as an admin nobody can audit, so this
            # refuses instead of quietly writing traffic to a phantom account.
            raise AuthError(
                "no admin user id to mint a token for",
                hint=(
                    "set ADMIN_USER_ID in .env, pass --token/ATF_TOKEN, or run "
                    "`atf auth login --username <user>`"
                ),
            )
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

    def _opener(self) -> urllib.request.OpenerDirector:
        """One opener per client: guarded redirects, optional unverified TLS.

        Built once so the redirect guard cannot be forgotten on a code path that
        constructs its own opener, and so connection setup is reused across the
        retries of one request.
        """
        if self._opener_cache is None:
            handlers: list[Any] = [_GuardedRedirects()]
            context = self._ssl_context()
            if context is not None:
                handlers.append(urllib.request.HTTPSHandler(context=context))
            self._opener_cache = urllib.request.build_opener(*handlers)
        return self._opener_cache

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
        retries: int | None = None,
        idempotent: bool | None = None,
        request_id: str = "",
    ) -> tuple[int, bytes]:
        """Send one request, retrying only what is safe to retry.

        `retries` counts *extra* attempts (0 = one shot). Writes are not replayed
        unless the caller passes `idempotent=True`: a retried POST is a second
        POST, and a duplicated retry of a DLQ item is a duplicated message.
        """
        verb = method.upper()
        attempts = self.retries if retries is None else max(0, int(retries))
        replayable = verb in IDEMPOTENT_METHODS if idempotent is None else bool(idempotent)
        correlation = request_id or uuid.uuid4().hex
        for attempt in range(attempts + 1):
            try:
                status, data, retry_after = self._send(
                    verb,
                    url,
                    body=body,
                    headers=headers,
                    with_auth=with_auth,
                    timeout=timeout,
                    accept=accept,
                    request_id=correlation,
                )
            except (UnreachableError, TimeoutError_) as exc:
                if not replayable or attempt >= attempts:
                    raise
                self.out.debug(
                    f"{verb} {url} failed ({exc.message}); attempt "
                    f"{attempt + 1}/{attempts + 1}"
                )
                self.sleep(self._retry_delay(attempt, None))
                continue
            if status in RETRY_STATUS and replayable and attempt < attempts:
                self.out.debug(
                    f"{verb} {url} -> HTTP {status}; attempt {attempt + 1}/{attempts + 1}"
                )
                self.sleep(self._retry_delay(attempt, retry_after))
                continue
            return status, data
        # The loop either returns or raises; this keeps type checkers honest.
        raise UnreachableError(f"{verb} {url} failed after {attempts + 1} attempts")

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        """Seconds to wait before attempt `attempt + 1`.

        `Retry-After` wins when the server sent one (capped: a bad header must
        not hang the terminal), otherwise bounded exponential backoff with
        jitter, so parallel cron jobs do not retry in lockstep.
        """
        if retry_after:
            seconds = _parse_retry_after(retry_after)
            if seconds is not None:
                return max(0.0, min(seconds, RETRY_AFTER_CAP))
        delay = min(BACKOFF_CAP, self.backoff_base * (2**attempt))
        return delay * random.uniform(0.8, 1.2) if delay else 0.0

    def _send(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str] | None,
        with_auth: bool,
        timeout: float | None,
        accept: str,
        request_id: str,
    ) -> tuple[int, bytes, str | None]:
        """One HTTP exchange: (status, body, Retry-After)."""
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Accept", accept)
        request.add_header("User-Agent", self.user_agent)
        # Correlates every retry of one logical request in the server's log, and
        # lets an operator grep for it from the error message.
        request.add_header("X-Request-Id", request_id)
        if body is not None and not (headers or {}).get("Content-Type"):
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if with_auth and self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._opener().open(request, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read(), resp.headers.get("Retry-After")
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is an answer, not a transport failure: the caller decides
            # what it means (and `raw` decides whether it is worth retrying).
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            return exc.code, exc.read(), retry_after
        except CrossOriginRedirect:
            raise
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                raise TimeoutError_(
                    f"{method} {url} timed out after {timeout or self.timeout}s "
                    f"(request {request_id})",
                    hint="raise --timeout, or check the gateway's upstream latency",
                ) from exc
            raise UnreachableError(
                f"cannot reach {url}: {reason} (request {request_id})",
                hint="check the gateway is up (`atf doctor`) or pass --url",
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError_(
                f"{method} {url} timed out after {timeout or self.timeout}s",
                hint="raise --timeout, or check the gateway's upstream latency",
            ) from exc
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
        retries: int | None = None,
        idempotent: bool | None = None,
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
        status, data = self.raw(
            method,
            url,
            body=body,
            timeout=timeout,
            accept=accept,
            with_auth=with_auth,
            retries=retries,
            idempotent=idempotent,
        )
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
        if status == 403 and path.rstrip("/").endswith("/logs/purge"):
            # The log archive is one shared store with no tenant dimension on
            # its purge path, so the installation-wide purge is
            # administrator-only. "Log in again" would be actively misleading
            # here: the token is fine, the scope is not.
            return (
                "purge needs a task scope: pass --rule <task id> to purge one of "
                "your tasks (the installation-wide purge is administrator-only)"
            )
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
