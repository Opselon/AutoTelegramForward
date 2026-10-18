"""Guard: a dashboard session is opened by a single-use code, never by a user id.

The defect this pins shut: `POST /api/auth/exchange-bot-token` accepted a
Telegram user id and answered with a 7-day session for that account. Telegram
ids are public, so anyone could sign in as anyone — which is how one tenant's
tasks, replacements and AI keys showed up in another tenant's dashboard.

Every client on that path is checked here, because the hole only stays shut if
none of them can mint or send a bare user id again:

* the Go handler must exchange a code and must not call IssueWebToken,
* the web bundle must send `login_code`, and must read `?code=` from the URL,
* the bot must put a code in the link, not a session token,
* the proto message must have no `user_id` field to smuggle one through.

The rubric is deliberately about *source text*, not behaviour: a behavioural
test proves the current server refuses, while this proves no client reintroduces
the request shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    path = ROOT / rel
    assert path.exists(), f"missing {rel}"
    return path.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Strip comments so the fix's own explanation cannot satisfy a check."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r'(?m)^\s*//.*$', "", text)
    text = re.sub(r'(?m)^\s*#.*$', "", text)
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    return text


def _func_body(source: str, name: str) -> str:
    """Return the body of a Go method named ``name`` (brace-matched)."""
    start = source.index(f"func (h *Handlers) {name}(")
    open_brace = source.index("{", start)
    depth = 0
    for i in range(open_brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : i + 1]
    raise AssertionError(f"unterminated body for {name}")


# --------------------------------------------------------------------- Go side


def test_go_exchange_requires_a_code_and_never_mints_from_a_user_id():
    body = _func_body(_code_only(_read("api/internal/handlers/auth.go")), "AuthBotExchange")

    assert "ExchangeWebLoginCode" in body, "the handler must exchange a login code"
    assert "login_code" in body, "the handler must read login_code from the body"
    assert "IssueWebToken" not in body, (
        "minting a token from a posted user id is the cross-tenant hole"
    )
    # A refusal is a refusal on the wire, not a 200 with success=false.
    assert "StatusUnauthorized" in body, "a refused exchange must answer 401"
    assert "resp.Success" in body, "the handler must gate on the core's verdict"


def test_go_password_change_is_bound_to_the_caller():
    body = _func_body(_code_only(_read("api/internal/handlers/auth.go")), "AuthChangePassword")

    assert "ValidateToken" in body, "the caller's identity must come from the token"
    assert "who.UserId" in body, "the password change must target the token's subject"


def test_the_only_user_id_mint_is_the_signature_verified_tma_path():
    """A user id may mint a session only when Telegram signed it (Mini App).

    Everywhere else — including the exchange endpoint — the code decides.
    """
    allowed = {"AuthTMA"}
    verified = "hmac.Equal"
    offenders = []
    for path in (ROOT / "api").rglob("*.go"):
        if path.name.endswith("_test.go"):
            continue
        raw = path.read_text(encoding="utf-8")
        text = _code_only(raw)
        for match in re.finditer(r"IssueWebTokenRequest\{[^}]*UserId", text):
            head = text[: match.start()]
            owner = re.findall(r"func \(h \*Handlers\) (\w+)\(", head)
            enclosing = owner[-1] if owner else "<file scope>"
            body = _func_body(text, enclosing) if enclosing in allowed else ""
            if enclosing not in allowed or verified not in body:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{enclosing}")
    assert offenders == [], f"unverified user-id token minting is back in {offenders}"


# -------------------------------------------------------------------- web side


def test_web_sends_a_login_code():
    text = _code_only(_read("web/frontend/src/api.ts"))
    call = re.search(r"exchangeBotToken:\s*\(req:\s*\{[^}]*\}\)", text)
    assert call, "exchangeBotToken must exist"
    assert "login_code" in call.group(0)
    assert "user_id" not in call.group(0), "the web client must not post a user id"


def test_web_reads_the_code_from_the_url_and_strips_it():
    text = _read("web/frontend/src/main.tsx")
    assert "params.get('code')" in text, "the dashboard link carries ?code="
    assert "exchangeBotToken({ login_code:" in _code_only(text), (
        "the code must be exchanged, not stored as a session"
    )
    assert "replaceState" in text, "the credential must be removed from the address bar"


# -------------------------------------------------------------------- bot side


def test_the_bot_link_carries_a_single_use_code():
    text = _code_only(_read("core/infrastructure/telegram/pro_bot_ui.py"))
    assert "?code=" in text, "the dashboard button must hand out a login code"
    assert "?token=" not in text, "a long-lived session token must not travel in a link"
    assert "issue_login_code" in text, "the bot must mint the code through the account servicer"


def test_the_bot_no_longer_prints_a_credential():
    text = _code_only(_read("core/infrastructure/telegram/pro_bot_ui.py"))
    assert "ui_main_token" not in text, "the menu used to print 24 characters of a live token"
    assert "_mint_web_token" not in text, "the link helper minted a 7-day session per render"


# ------------------------------------------------------------------ proto side


def test_the_exchange_message_has_no_user_id_field():
    proto = _read("proto/autoforward.proto")
    block = re.search(
        r"message ExchangeWebLoginCodeRequest\s*\{(.*?)\}", proto, flags=re.S
    )
    assert block, "ExchangeWebLoginCodeRequest must exist"
    fields = re.findall(r"\b(?:string|int64|int32|bool|double)\s+(\w+)\s*=", block.group(1))
    assert fields == ["login_code"], f"unexpected fields: {fields}"


def test_the_exchange_rpc_is_exposed_by_the_account_service():
    proto = _read("proto/autoforward.proto")
    assert (
        "rpc ExchangeWebLoginCode(ExchangeWebLoginCodeRequest) returns (LoginAccountResponse);"
        in proto
    )


@pytest.mark.parametrize("client", ["api/proto/autoforward.pb.go", "core/proto/autoforward_pb2.py"])
def test_generated_stubs_carry_the_exchange(client):
    assert "ExchangeWebLoginCode" in _read(client), (
        "regenerate the stubs (scripts/gen_proto.py) after editing the proto"
    )
