"""The exchange RPC: a code, not a user id, is what opens a dashboard session.

`POST /api/auth/exchange-bot-token` used to be handed a Telegram user id and
answered with a 7-day session for that id. Telegram ids are public, so anyone
could open anyone's dashboard — the reported "other users' rules show up in my
account". The exchange now takes a code the bot minted for a user it had already
authenticated, and these tests pin that the code — and only the code — decides
whose session is issued.
"""

from __future__ import annotations

import asyncio
import time

from core.infrastructure.grpc_server.account_servicer import AccountControlServicer
from core.infrastructure.persistence.database import SqliteDatabase
from core.proto import autoforward_pb2 as pb

TENANT_A = 5_094_837_833
TENANT_B = 5_989_354_154


def _servicer(tmp_path, name="accounts.db"):
    db = SqliteDatabase(str(tmp_path / name))
    return db, AccountControlServicer(db, users=None, is_admin_user=[], admin_service=None)


def _claims(token: str) -> dict:
    import base64
    import json

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def test_exchange_returns_a_session_for_the_codes_account(tmp_path):
    db, srv = _servicer(tmp_path)
    # Provision the account the way the bot does, then mint a code for it.
    reg = asyncio.run(srv.IssueWebToken(pb.IssueWebTokenRequest(user_id=TENANT_A), None))
    assert reg.success
    code, expires_at = srv.issue_login_code(TENANT_A)
    assert expires_at > int(time.time())

    resp = asyncio.run(srv.ExchangeWebLoginCode(pb.ExchangeWebLoginCodeRequest(login_code=code), None))

    assert resp.success, resp.message
    assert _claims(resp.token)["sub"] == str(TENANT_A)
    assert resp.account.user_id == TENANT_A


def test_a_used_code_cannot_open_a_second_session(tmp_path):
    _, srv = _servicer(tmp_path)
    asyncio.run(srv.IssueWebToken(pb.IssueWebTokenRequest(user_id=TENANT_A), None))
    code, _ = srv.issue_login_code(TENANT_A)

    first = asyncio.run(srv.ExchangeWebLoginCode(pb.ExchangeWebLoginCodeRequest(login_code=code), None))
    second = asyncio.run(srv.ExchangeWebLoginCode(pb.ExchangeWebLoginCodeRequest(login_code=code), None))

    assert first.success
    assert not second.success
    assert second.error_code == "INVALID_LOGIN_CODE"
    assert second.token == ""


def test_unknown_or_blank_code_is_refused_without_a_token(tmp_path):
    _, srv = _servicer(tmp_path)
    asyncio.run(srv.IssueWebToken(pb.IssueWebTokenRequest(user_id=TENANT_A), None))

    for value in ("", "   ", "made-up-code"):
        resp = asyncio.run(
            srv.ExchangeWebLoginCode(pb.ExchangeWebLoginCodeRequest(login_code=value), None)
        )
        assert not resp.success
        assert resp.token == ""
        assert resp.error_code in {"INVALID_LOGIN_CODE", "LOGIN_CODE_REQUIRED"}


def test_two_tenants_never_receive_each_others_session(tmp_path):
    _, srv = _servicer(tmp_path)
    asyncio.run(srv.IssueWebToken(pb.IssueWebTokenRequest(user_id=TENANT_A), None))
    asyncio.run(srv.IssueWebToken(pb.IssueWebTokenRequest(user_id=TENANT_B), None))

    code_b, _ = srv.issue_login_code(TENANT_B)
    resp = asyncio.run(srv.ExchangeWebLoginCode(pb.ExchangeWebLoginCodeRequest(login_code=code_b), None))

    assert resp.success
    assert _claims(resp.token)["sub"] == str(TENANT_B)
    assert resp.account.username != f"tg_{TENANT_A}"


def test_issuing_a_code_needs_an_existing_account(tmp_path):
    """No account, no code: the bot provisions first, then mints."""
    _, srv = _servicer(tmp_path)
    try:
        srv.issue_login_code(TENANT_A)
    except ValueError as exc:
        assert "no account" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("issuing a code for an unknown account must fail")


def test_the_exchange_never_reads_a_user_id_from_the_request(tmp_path):
    """The request message has no user_id field at all — the code is the only input."""
    fields = {f.name for f in pb.ExchangeWebLoginCodeRequest.DESCRIPTOR.fields}
    assert fields == {"login_code"}
    assert "user_id" not in fields


def test_issue_login_code_mints_for_the_caller_and_exchanges_cleanly(tmp_path):
    """IssueLoginCode: a minted code belongs to the account the API verified.

    The Go API calls this RPC only after ValidateToken proved the caller's
    JWT names the id it passes on; the tenancy is decided by that chain, so
    the core itself is the one that must refuse unknown ids.
    """
    db, ctx = _servicer(tmp_path)
    base = AccountControlServicer(db, users=None, is_admin_user=[], admin_service=None)
    # Seed the account TENANT_B will later be minted for.
    reg = asyncio.run(base.IssueWebToken(pb.IssueWebTokenRequest(user_id=TENANT_B), None))
    assert reg.success

    class Ctx:
        async def abort(self, *a, **k):
            raise AssertionError("IssueLoginCode must not abort")

    resp = asyncio.run(base.IssueLoginCode(pb.IssueLoginCodeRequest(user_id=TENANT_B), Ctx()))
    assert resp.login_code, "a known user must get a code"
    # the minted code immediately exchanges for that exact account
    ex = asyncio.run(base.ExchangeWebLoginCode(pb.ExchangeWebLoginCodeRequest(login_code=resp.login_code), None))
    assert ex.success
    assert _claims(ex.token)["sub"] == str(TENANT_B)

    # an unknown account (no user) must NOT mint
    resp2 = asyncio.run(base.IssueLoginCode(pb.IssueLoginCodeRequest(user_id=1122334455), Ctx()))
    assert resp2.login_code == "", "unknown account must not be minted a code"


def test_issue_login_code_cooldown_prevents_duplicate_links(tmp_path):
    """Two rapid mints for one account must not create two live links.

    The first still-valid code stays usable; the second mint inside the
    cooldown is refused so a double click cannot invalidate what the user
    already received.
    """
    db, srv = _servicer(tmp_path)
    base = AccountControlServicer(db, users=None, is_admin_user=[], admin_service=None)
    reg = asyncio.run(base.IssueWebToken(pb.IssueWebTokenRequest(user_id=TENANT_A), None))
    assert reg.success

    class Ctx:
        async def abort(self, *a, **k):
            raise AssertionError("cooldown must not abort")

    first = asyncio.run(base.IssueLoginCode(pb.IssueLoginCodeRequest(user_id=TENANT_A), Ctx()))
    assert first.login_code, "first mint succeeds"
    # Second attempt inside the cooldown answers empty code + retry time.
    second = asyncio.run(base.IssueLoginCode(pb.IssueLoginCodeRequest(user_id=TENANT_A), Ctx()))
    assert second.login_code == ""
    assert second.expires_at > int(time.time()), "expires_at carries the retry time"

    # The FIRST code still exchanges cleanly — the cooldown never broke it.
    ex = asyncio.run(base.ExchangeWebLoginCode(pb.ExchangeWebLoginCodeRequest(login_code=first.login_code), None))
    assert ex.success
    assert _claims(ex.token)["sub"] == str(TENANT_A)


def test_try_issue_reuses_clock_injection_for_deterministic_cooldown(tmp_path):
    """Cooldown math is driven by the injected clock, so tests are exact."""
    from core.application.web_login_codes import WebLoginCodeService

    db, srv = _servicer(tmp_path)
    clock = [1_000_000]
    codes = WebLoginCodeService(db, clock=lambda: clock[0])
    got = codes.try_issue(TENANT_A, cooldown_seconds=60)
    assert got is not None, "first mint within cooldown succeeds"
    code, _exp = got
    clock[0] += 30
    assert codes.try_issue(TENANT_A, cooldown_seconds=60) is None, "second mint inside 60s refused"
    assert codes.cooldown_remaining(TENANT_A, cooldown_seconds=60) == 30
    clock[0] += 61
    assert codes.cooldown_remaining(TENANT_A, cooldown_seconds=60) == 0
    again = codes.try_issue(TENANT_A, cooldown_seconds=60)
    assert again is not None and again[0] != code
    # Burn the first, then confirm the second still burns: single use holds.
    assert codes.burn(code) == TENANT_A
    assert codes.burn(code) is None, "already-used code must not redeem twice"
