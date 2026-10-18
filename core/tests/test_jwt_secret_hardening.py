"""The key every dashboard session is signed with must not be public knowledge.

The defect these tests pin: the fallback secret was `sha256("atf-jwt::" + db
path)`.  The database path is in `docker-compose.yml`, in a public repository,
so anyone could compute the key and sign a token for any account — no exploit,
no timing, just arithmetic.  Verified live before the fix: a token signed that
way was accepted by `GET /api/rules` for two different accounts.

Three properties, in order of importance:

* a key derived from public facts is not accepted,
* admin is read from the account, never from the token's own claim,
* a signed token still has to describe a session we could have issued.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from core.infrastructure.grpc_server.account_servicer import (
    DEPLOYMENT_SECRET_MIN_BYTES,
    AccountControlServicer,
)
from core.infrastructure.persistence.database import SqliteDatabase

UID = 5094837833
OTHER_UID = 5989354154
DB_PATH = "/app/data/atf.db"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _sign(secret: bytes, claims: dict) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    sig = _b64url(hmac.new(secret, f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _claims(uid: int = UID, exp_delta: int = 7 * 24 * 3600, **extra) -> dict:
    now = int(time.time())
    return {"sub": str(uid), "username": f"tg_{uid}", "admin": False,
            "iat": now, "exp": now + exp_delta, **extra}


def _account(db, uid: int, is_admin: int = 0) -> None:
    db.execute(
        "INSERT OR REPLACE INTO web_accounts"
        " (user_id, username, password_hash, password_algo, password_iter, salt,"
        "  display_name, is_admin, plan, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, f"tg_{uid}", "hash", "pbkdf2_sha256", 240000, "salt",
         f"user {uid}", is_admin, "free", int(time.time()), int(time.time())),
    )


def _servicer(tmp_path, name="accounts.db", **kwargs):
    db = SqliteDatabase(str(tmp_path / name))
    _account(db, UID)
    _account(db, OTHER_UID)
    return AccountControlServicer(db, **kwargs)


# --------------------------------------------------------------------------- #
# the public-secret defect itself
# --------------------------------------------------------------------------- #

def test_the_database_path_is_no_longer_a_signing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ATF_JWT_SECRET", raising=False)
    svc = _servicer(tmp_path)
    for path in (DB_PATH, str(tmp_path / "accounts.db")):
        public = hashlib.sha256(f"atf-jwt::{path}".encode("utf-8")).digest()
        token = _sign(public, _claims())
        assert svc.jwt_validate(token) is None, "a key derived from the DB path still signs sessions"


def test_a_token_for_another_account_signed_with_a_guess_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("ATF_JWT_SECRET", raising=False)
    svc = _servicer(tmp_path)
    guesses = [
        hashlib.sha256(b"atf-jwt::/app/data/atf.db").digest(),
        hashlib.sha256(b"").digest(),
        b"changeme",
        b"secret",
    ]
    for guess in guesses:
        assert svc.jwt_validate(_sign(guess, _claims(OTHER_UID))) is None


def test_the_generated_secret_is_random_persistent_and_shared(tmp_path, monkeypatch):
    monkeypatch.delenv("ATF_JWT_SECRET", raising=False)
    first = _servicer(tmp_path)
    secret = first._get_secret()
    assert len(secret) >= DEPLOYMENT_SECRET_MIN_BYTES
    # Persisted: a redeploy must not log every dashboard out, and a second
    # process reading the same database must agree with the first.
    second = AccountControlServicer(SqliteDatabase(str(tmp_path / "accounts.db")))
    assert second._get_secret() == secret
    token = _sign(secret, _claims())
    assert first.jwt_validate(token) is not None
    assert second.jwt_validate(token) is not None


def test_a_configured_secret_wins_and_must_be_long_enough(tmp_path, monkeypatch):
    strong = "s" * DEPLOYMENT_SECRET_MIN_BYTES
    monkeypatch.setenv("ATF_JWT_SECRET", strong)
    svc = _servicer(tmp_path)
    assert svc._get_secret() == strong.encode()
    assert svc.jwt_validate(_sign(strong.encode(), _claims())) is not None

    monkeypatch.setenv("ATF_JWT_SECRET", "short")
    weak = _servicer(tmp_path, name="weak.db")
    with pytest.raises(RuntimeError):
        weak._get_secret()


def test_the_stored_secret_is_not_written_in_plain_sight_of_the_other_columns(tmp_path, monkeypatch):
    monkeypatch.delenv("ATF_JWT_SECRET", raising=False)
    svc = _servicer(tmp_path)
    secret = svc._get_secret()
    stored = svc._db.query_one("SELECT value FROM app_secrets WHERE key='jwt_signing'")["value"]
    assert bytes.fromhex(stored) == secret
    # It is the deployment's, not a hard-coded value any other deployment shares.
    other = _servicer(tmp_path, name="elsewhere.db")
    assert other._get_secret() != svc._get_secret()


# --------------------------------------------------------------------------- #
# admin is an account property, not a claim
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_an_admin_claim_does_not_make_an_admin(tmp_path, monkeypatch):
    monkeypatch.delenv("ATF_JWT_SECRET", raising=False)
    svc = _servicer(tmp_path)
    token = _sign(svc._get_secret(), _claims(uid=OTHER_UID, admin=True))
    resp = await svc.ValidateToken(type("R", (), {"token": token})(), None)
    assert resp.valid is True
    assert resp.is_admin is False, "the token promoted its own holder"


@pytest.mark.asyncio
async def test_the_accounts_own_admin_flag_still_counts(tmp_path, monkeypatch):
    monkeypatch.delenv("ATF_JWT_SECRET", raising=False)
    svc = _servicer(tmp_path)
    svc._db.execute("UPDATE web_accounts SET is_admin=1 WHERE user_id=?", (UID,))
    token = _sign(svc._get_secret(), _claims(uid=UID))
    resp = await svc.ValidateToken(type("R", (), {"token": token})(), None)
    assert (resp.valid, resp.is_admin) == (True, True)


@pytest.mark.asyncio
async def test_a_token_for_a_deleted_account_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("ATF_JWT_SECRET", raising=False)
    svc = _servicer(tmp_path)
    token = _sign(svc._get_secret(), _claims(uid=999999))
    resp = await svc.ValidateToken(type("R", (), {"token": token})(), None)
    assert (resp.valid, resp.error) == (False, "ACCOUNT_NOT_FOUND")


# --------------------------------------------------------------------------- #
# a signed token still has to be a session we could have issued
# --------------------------------------------------------------------------- #

def test_a_decade_long_session_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("ATF_JWT_SECRET", raising=False)
    svc = _servicer(tmp_path)
    secret = svc._get_secret()
    assert svc.jwt_validate(_sign(secret, _claims(exp_delta=10 * 365 * 24 * 3600))) is None
    # The seven days we actually issue are still fine.
    assert svc.jwt_validate(_sign(secret, _claims(exp_delta=7 * 24 * 3600))) is not None


def test_a_token_issued_in_the_future_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("ATF_JWT_SECRET", raising=False)
    svc = _servicer(tmp_path)
    future = int(time.time()) + 3600
    claims = _claims()
    claims["iat"] = future
    claims["exp"] = future + 7 * 24 * 3600
    assert svc.jwt_validate(_sign(svc._get_secret(), claims)) is None
