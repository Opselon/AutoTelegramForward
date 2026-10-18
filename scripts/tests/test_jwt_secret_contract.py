"""The session signing key: not derivable, not shipped, and carried to the core.

`POST /api/auth/exchange-bot-token` used to mint a session for a bare Telegram
id (#151).  Fixing that closed one door; this guard covers the other one found
the same day: the fallback key was `sha256("atf-jwt::" + db_path)`, and the path
is in the compose file, so a token for any account could be signed offline.

These checks are source-level on purpose: the property is about what the
deployment does when nobody configured anything, which no unit test of a running
process can see.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_no_source_derives_the_signing_key_from_something_public():
    servicer = _read("core/infrastructure/grpc_server/account_servicer.py")
    assert "atf-jwt::" not in servicer, "the signing key is derived again"
    # The old seed used the database path; nothing may hash a path into a key.
    assert not re.search(r"hashlib\.sha256\(\s*f?\"atf-jwt", servicer)
    assert "_load_or_create_secret" in servicer


def test_the_core_receives_the_secret_from_the_environment():
    compose = _read("docker-compose.yml")
    core = compose.split("  core:")[1].split("\n  api:")[0]
    assert "ATF_JWT_SECRET" in core, (
        "the core container is not handed ATF_JWT_SECRET, so a configured "
        "secret in .env never reaches the process that signs sessions"
    )


def test_the_example_env_ships_no_usable_secret():
    example = _read(".env.example")
    live = [line for line in example.splitlines()
            if line.strip().startswith("ATF_JWT_SECRET=")]
    assert live == [], f".env.example ships a signing key: {live}"
    assert "# ATF_JWT_SECRET=" in example, "the variable is not documented"


def test_a_guessable_secret_is_refused_rather_than_trusted():
    servicer = _read("core/infrastructure/grpc_server/account_servicer.py")
    assert "DEPLOYMENT_SECRET_MIN_BYTES" in servicer
    assert "_PLACEHOLDER_SECRETS" in servicer
    # The check must run before the value is accepted as the key.
    check = servicer.index("marker in lowered for marker in _PLACEHOLDER_SECRETS")
    accept = servicer.index("self._secret = raw")
    assert check < accept, "a short secret is stored before it is validated"


def test_admin_never_comes_from_the_token_claim():
    servicer = _read("core/infrastructure/grpc_server/account_servicer.py")
    validate = servicer.split("async def ValidateToken", 1)[1].split("def generate_secure_password", 1)[0]
    assert 'payload.get("admin"' not in validate, (
        "ValidateToken trusts the token's own admin claim; a leaked key would "
        "then also be a promotion"
    )
    assert 'row["is_admin"]' in validate


def test_the_docs_say_what_the_key_is_for():
    docs = _read("docs/security.md") if (REPO / "docs/security.md").exists() else _read(".env.example")
    assert "ATF_JWT_SECRET" in docs
