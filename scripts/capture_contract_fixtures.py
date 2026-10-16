#!/usr/bin/env python3
"""Re-capture the API contract fixtures that every client is tested against.

`contracts/fixtures/*.json` are the shared truth for `scripts/audit_api_contract.py`
and for the client contract tests (Go handler tests, `mobile/test/core/system_contract_test.dart`,
the web types). They are captured from a *running* API rather than hand-written,
so the tests exercise the bytes the server really sends.

Run this after changing a `/api/system/*` payload, then update the clients and
the audit in the same change:

    python scripts/capture_contract_fixtures.py                 # localhost:8080
    python scripts/capture_contract_fixtures.py --base https://api.example.com

Credentials are read from `--token`, `ATF_TOKEN`, or `~/.atf/cli.json` (minted by
`atf auth login`, keyed by server URL). No value is ever printed: the fixtures
carry the *shape* of the contract and the `set`-booleans, never a secret. The
script refuses to write a fixture that contains a credential-shaped string.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "contracts" / "fixtures"
CLI_CREDENTIALS = Path.home() / ".atf" / "cli.json"

ENDPOINTS = {
    "system_setup": "/api/system/setup",
    "system_env": "/api/system/env",
    "health_services": "/api/v1/health/services",
}

# `1234567:AA...` - a bot token shape. Fixtures are shape-only.
CREDENTIAL_SHAPE_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
SECRET_KEYS = {"value", "secret", "password", "token", "api_key", "passphrase"}


def resolve_token(base: str, explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    if env := os.environ.get("ATF_TOKEN", "").strip():
        return env
    if CLI_CREDENTIALS.is_file():
        data = json.loads(CLI_CREDENTIALS.read_text(encoding="utf-8"))
        # ~/.atf/cli.json is keyed by server URL, not by a top-level token.
        for url, entry in (data.get("servers") or {}).items():
            if base.rstrip("/").startswith(url.rstrip("/")) and entry.get("token"):
                return entry["token"]
        for entry in (data.get("servers") or {}).values():
            if entry.get("token"):
                return entry["token"]
    raise SystemExit(
        "✗ no credential found: pass --token, set ATF_TOKEN, or run `atf auth login`"
    )


def fetch(base: str, path: str, token: str) -> dict:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        headers={"authorization": f"Bearer {token}", "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - operator feedback
        raise SystemExit(f"✗ {path} answered HTTP {exc.code}")
    except urllib.error.URLError as exc:  # pragma: no cover - operator feedback
        raise SystemExit(f"✗ {path} unreachable: {exc.reason}")


def scrubbed(payload: dict, name: str) -> dict:
    """Assert the fixture is shape-only before it is written to the repo."""
    blob = json.dumps(payload, ensure_ascii=False)
    if (match := CREDENTIAL_SHAPE_RE.search(blob)):
        raise SystemExit(
            f"✗ {name} carries a credential-shaped value (offset {match.start()}); "
            "refusing to write it to the repository"
        )

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in SECRET_KEYS:
                    raise SystemExit(
                        f"✗ {name} contains a `{key}` field: the contract must stay "
                        "booleans-only so no value can reach a client"
                    )
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    for entry in payload.get("checks") or []:
        if not isinstance(entry.get("set"), bool):
            raise SystemExit(f"✗ {name}: checks[].set is not a boolean")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default=None)
    parser.add_argument("--dry-run", action="store_true", help="verify, write nothing")
    args = parser.parse_args()

    token = resolve_token(args.base, args.token)
    FIXTURES.mkdir(parents=True, exist_ok=True)

    for name, path in ENDPOINTS.items():
        payload = scrubbed(fetch(args.base, path, token), name)
        target = FIXTURES / f"{name}.json"
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.dry_run:
            print(f"  ok {name:16} {path} → {len(payload)} keys (dry-run)")
            continue
        changed = not target.is_file() or target.read_text(encoding="utf-8") != rendered
        target.write_text(rendered, encoding="utf-8")
        print(
            f"  {'updated' if changed else 'unchanged'} {name:16} {path} "
            f"→ {len(payload)} keys"
        )
    print("\nfixtures verified credential-free.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
