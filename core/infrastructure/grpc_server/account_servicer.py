"""AccountControlServicer — unified per-user identity for bot + web dashboard.

Passwords: PBKDF2-SHA256 (240k iterations, per-user salt, constant-time verify).
Tokens: JWT HS256 signed with the account service secret (ATF_JWT_SECRET or
derived from the DB encryption key), 7-day expiry.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import os
import re
import secrets
import string
import time
from typing import Any, Optional

import grpc

from ...proto import pb
from ...proto import autoforward_pb2_grpc as pb_grpc

_JWT_ITER = 240_000
_TOKEN_TTL = 7 * 24 * 3600
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{4,32}$")
_MIN_PASSWORD_LEN = 6


def _hash_password(password: str, salt: bytes, iterations: int = _JWT_ITER) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return base64.urlsafe_b64encode(dk).decode("ascii").rstrip("=")


def _verify_password(password: str, salt: bytes, expected: str, iterations: int) -> bool:
    got = _hash_password(password, salt, iterations)
    return hmac.compare_digest(got, expected)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class AccountControlServicer(pb_grpc.AccountControlServiceServicer):
    def __init__(self, db, users=None, is_admin_user=None) -> None:
        self._db = db
        self._users = users  # optional SqliteUserRepository for plan/language
        self._admin_ids = set(is_admin_user or [])
        secret = os.environ.get("ATF_JWT_SECRET", "")
        self._secret = secret.encode("utf-8") if secret else None

    # ------------------------------------------------------------------ jwt
    def _get_secret(self) -> bytes:
        if self._secret:
            return self._secret
        # Derive a stable secret from the DB path + salt table when env is absent
        seed = f"atf-jwt::{self._db._db_path}".encode("utf-8")
        self._secret = hashlib.sha256(seed).digest()
        return self._secret

    def _jwt_issue(self, user_id: int, username: str, is_admin: bool) -> tuple[str, int]:
        now = int(time.time())
        exp = now + _TOKEN_TTL
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = _b64url(json.dumps({
            "sub": str(user_id), "username": username,
            "admin": is_admin, "iat": now, "exp": exp,
        }).encode())
        signing_input = f"{header}.{payload}".encode("ascii")
        sig = _b64url(hmac.new(self._get_secret(), signing_input, hashlib.sha256).digest())
        return f"{header}.{payload}.{sig}", exp

    def jwt_validate(self, token: str) -> Optional[dict]:
        try:
            header_b64, payload_b64, sig_b64 = token.split(".")
            signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
            expected = _b64url(hmac.new(self._get_secret(), signing_input, hashlib.sha256).digest())
            if not hmac.compare_digest(expected, sig_b64):
                return None
            payload = json.loads(_b64url_decode(payload_b64))
            if int(payload.get("exp", 0)) < int(time.time()):
                return None
            return payload
        except Exception:
            return None

    # -------------------------------------------------------------- helpers
    def _account_pb(self, row) -> pb.WebAccount:
        return pb.WebAccount(
            user_id=int(row["user_id"]),
            username=row["username"],
            display_name=row["display_name"] or "",
            is_admin=bool(row["is_admin"]),
            plan=row["plan"] or "free",
            created_at=int(row["created_at"]),
        )

    def _find_by_username(self, username: str):
        return self._db.query_one(
            "SELECT * FROM web_accounts WHERE username=?", (username,)
        )

    def _find_by_uid(self, user_id: int):
        return self._db.query_one(
            "SELECT * FROM web_accounts WHERE user_id=?", (int(user_id),)
        )

    def _auto_username(self, user_id: int, display_name: str) -> str:
        base = re.sub(r"[^a-zA-Z0-9_]", "", display_name or "")[:20]
        base = base if len(base) >= 4 else f"user{user_id}"
        candidate = base
        i = 0
        while self._find_by_username(candidate):
            i += 1
            candidate = f"{base}{i}"
            if i > 500:
                candidate = f"user{user_id}_{secrets.token_hex(2)}"
                break
        return candidate

    # ----------------------------------------------------------------- rpcs
    async def RegisterAccount(self, request, context):
        uid = int(request.user_id or 0)
        if uid <= 0:
            min_row = self._db.query_one("SELECT MIN(user_id) as min_id FROM web_accounts WHERE user_id < 0")
            cur_min = (min_row["min_id"] if min_row and min_row["min_id"] is not None else 0)
            uid = min(cur_min, 0) - 1
        username = (request.username or "").strip()
        password = request.password or ""
        if username and not _USERNAME_RE.match(username):
            return pb.RegisterAccountResponse(
                success=False, message="Username must be 4-32 chars (letters, digits, _)",
                error_code="INVALID_USERNAME")
        if password and len(password) < _MIN_PASSWORD_LEN:
            return pb.RegisterAccountResponse(
                success=False, message=f"Password must be at least {_MIN_PASSWORD_LEN} characters",
                error_code="WEAK_PASSWORD")

        existing = self._find_by_uid(uid) if uid else None
        if existing:
            return pb.RegisterAccountResponse(
                success=False, message="Account already exists for this Telegram user",
                error_code="ALREADY_REGISTERED", account=self._account_pb(existing))
        if username and self._find_by_username(username):
            return pb.RegisterAccountResponse(
                success=False, message="Username is already taken",
                error_code="USERNAME_TAKEN")

        username = username or self._auto_username(uid, request.display_name)
        if not username:  # no user id, no name -> generate fully random
            username = f"user_{secrets.token_hex(4)}"
            while self._find_by_username(username):
                username = f"user_{secrets.token_hex(4)}"

        salt = secrets.token_bytes(16)
        now = int(time.time())
        is_admin = int(bool(request.is_admin) or uid in self._admin_ids)
        plan = getattr(request, "plan", None) or "free"
        self._db.execute(
            "INSERT INTO web_accounts (user_id, username, password_hash, password_algo,"
            " password_iter, salt, display_name, is_admin, plan, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid, username,
                _hash_password(password, salt) if password else "",
                "pbkdf2_sha256", _JWT_ITER, salt.hex(),
                request.display_name or "", is_admin,
                plan, now, now,
            ),
        )
        row = self._find_by_uid(uid)
        return pb.RegisterAccountResponse(success=True, message="Account created",
                                          account=self._account_pb(row))

    async def LoginAccount(self, request, context):
        username = (request.username or "").strip()
        password = request.password or ""
        row = self._find_by_username(username) if username else None

        # Telegram-linked login without password (bot-issued one-time proof)
        if row is None and request.telegram_user_id:
            row = self._find_by_uid(request.telegram_user_id)
            if row and not row["password_hash"]:
                return self._login_success(row)
            if row:
                return pb.LoginAccountResponse(
                    success=False, message="Password required",
                    error_code="PASSWORD_REQUIRED")
            return pb.LoginAccountResponse(
                success=False, message="Invalid username or password",
                error_code="INVALID_CREDENTIALS")

        if row is None or not row["password_hash"]:
            # Constant-time-ish dummy verify to reduce user enumeration
            _verify_password(password, b"atf-dummy-salt", "x" * 43, 1000)
            return pb.LoginAccountResponse(
                success=False, message="Invalid username or password",
                error_code="INVALID_CREDENTIALS")

        if not _verify_password(password, bytes.fromhex(row["salt"]),
                                row["password_hash"], int(row["password_iter"])):
            return pb.LoginAccountResponse(
                success=False, message="Invalid username or password",
                error_code="INVALID_CREDENTIALS")

        # Optional proof-link: bind the web account to the telegram user
        if request.telegram_user_id and not row["user_id"]:
            self._db.execute(
                "UPDATE web_accounts SET user_id=?, updated_at=? WHERE username=?",
                (int(request.telegram_user_id), int(time.time()), username))
            row = self._find_by_username(username)

        return self._login_success(row)

    def _login_success(self, row) -> pb.LoginAccountResponse:
        token, exp = self._jwt_issue(int(row["user_id"]), row["username"], bool(row["is_admin"]))
        return pb.LoginAccountResponse(
            success=True, message="Login successful", token=token, expires_at=exp,
            account=self._account_pb(row))

    async def ValidateToken(self, request, context):
        payload = self.jwt_validate(request.token or "")
        if not payload:
            return pb.ValidateTokenResponse(valid=False, error="INVALID_OR_EXPIRED")
        return pb.ValidateTokenResponse(
            valid=True,
            user_id=int(payload.get("sub", 0)),
            username=payload.get("username", ""),
            is_admin=bool(payload.get("admin", False)),
        )

    def generate_secure_password(self, length: int = 14) -> str:
        upper = secrets.choice(string.ascii_uppercase)
        lower = secrets.choice(string.ascii_lowercase)
        digit = secrets.choice(string.digits)
        punct = secrets.choice("!@#$%&*")
        all_chars = string.ascii_letters + string.digits + "!@#$%&*"
        rest = [secrets.choice(all_chars) for _ in range(max(length - 4, 1))]
        res = list(upper + lower + digit + punct + "".join(rest))
        secrets.SystemRandom().shuffle(res)
        return "".join(res)

    async def get_or_create_credentials(
        self, user_id: int, display_name: str = "", force_reset: bool = False
    ) -> tuple[str, str, str]:
        """Returns (username, plain_secret, token) for Telegram bot user integration."""
        uid = int(user_id)
        row = self._find_by_uid(uid)
        plain_secret = ""

        if row is None:
            plain_secret = self.generate_secure_password()
            username = f"tg_{uid}"
            reg = await self.RegisterAccount(
                pb.RegisterAccountRequest(
                    user_id=uid,
                    username=username,
                    password=plain_secret,
                    display_name=display_name or f"User {uid}",
                    is_admin=uid in self._admin_ids,
                ),
                None,
            )
            row = self._find_by_uid(uid)
        elif force_reset or not row["password_hash"]:
            plain_secret = self.generate_secure_password()
            salt = secrets.token_bytes(16)
            now = int(time.time())
            self._db.execute(
                "UPDATE web_accounts SET password_hash=?, salt=?, updated_at=? WHERE user_id=?",
                (_hash_password(plain_secret, salt), salt.hex(), now, uid),
            )
            row = self._find_by_uid(uid)
        else:
            plain_secret = "(Password unchanged)"

        token, _ = self._jwt_issue(int(row["user_id"]), row["username"], bool(row["is_admin"]))
        return (row["username"], plain_secret, token)

    async def IssueWebToken(self, request, context):
        """Bot -> core: mint a dashboard token for an authenticated Telegram user.
        Auto-provisions the account on first use (passwordless until they set one)."""
        uid = int(request.user_id or 0)
        if uid <= 0:
            return pb.LoginAccountResponse(success=False, message="user_id required",
                                           error_code="INVALID_USER")
        row = self._find_by_uid(uid)
        if row is None:
            reg = await self.RegisterAccount(
                pb.RegisterAccountRequest(user_id=uid, username="", password="",
                                          display_name="", is_admin=False),
                context)
            if not reg.success:
                return pb.LoginAccountResponse(success=False, message=reg.message,
                                               error_code=reg.error_code)
            row = self._find_by_uid(uid)
        return self._login_success(row)

    async def ChangePassword(self, request, context):
        uid = int(request.user_id or 0)
        row = self._find_by_uid(uid)
        if row is None:
            return pb.StatusResponse(success=False, message="Account not found",
                                     error_code="NOT_FOUND")
        if row["password_hash"]:
            if not request.old_password or not _verify_password(
                    request.old_password, bytes.fromhex(row["salt"]),
                    row["password_hash"], int(row["password_iter"])):
                return pb.StatusResponse(success=False, message="Current password incorrect",
                                         error_code="INVALID_CREDENTIALS")
        new_password = request.new_password or ""
        if len(new_password) < _MIN_PASSWORD_LEN:
            return pb.StatusResponse(
                success=False,
                message=f"Password must be at least {_MIN_PASSWORD_LEN} characters",
                error_code="WEAK_PASSWORD")
        salt = secrets.token_bytes(16)
        self._db.execute(
            "UPDATE web_accounts SET password_hash=?, password_algo='pbkdf2_sha256',"
            " password_iter=?, salt=?, updated_at=? WHERE user_id=?",
            (_hash_password(new_password, salt), _JWT_ITER, salt.hex(),
             int(time.time()), uid))
        return pb.StatusResponse(success=True, message="Password updated")

    async def GetAccount(self, request, context):
        row = None
        if request.user_id:
            row = self._find_by_uid(request.user_id)
        elif request.username:
            row = self._find_by_username(request.username.strip())
        if row is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "account not found")
        return self._account_pb(row)
