#!/usr/bin/env python3
"""Audit the API contract against every consumer, statically.

The failure this exists to prevent is silent: the server renames a JSON field,
Go keeps compiling, the dashboard and the app keep running, and a panel just
renders blank. Nothing in a unit test on either side notices, because each side
is self-consistent.

So this script reads one source of truth - `contracts/fixtures/*.json`, captured
from a live `atf-api` - and asserts that the producer (the Go handler structs)
and the consumers (Dart models, TypeScript types) all speak about the same
fields:

  * every field the API really sends is described by the Go struct that emits it
    and parsed by each client (a missing one is a blank field in the UI);
  * every field a client parses is a field the API sends (a phantom parse is a
    typo that silently keeps a default forever);
  * contract payloads stay booleans-only: no client may read a value field for a
    configuration key, so a token can never be rendered;
  * the fixtures themselves carry no credential-shaped values.

Run it in CI before tests; exit code 1 means the two ends have drifted.

    python scripts/audit_api_contract.py            # human table
    python scripts/audit_api_contract.py --json     # one JSON object
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "contracts" / "fixtures"
GO_HANDLER = ROOT / "api" / "internal" / "handlers" / "system.go"
DART_MODELS = ROOT / "mobile" / "lib" / "core" / "models.dart"
TS_TYPES = ROOT / "web" / "frontend" / "src" / "types.ts"

# Fields a contract payload may legitimately never carry, so their absence from
# a capture is not drift.
OPTIONAL_FIELDS = {"hint", "commit_sha"}

# A configuration contract is booleans-only. If a client ever reads a field with
# one of these names out of a contract payload, a secret can reach a screen.
FORBIDDEN_FIELD_RE = re.compile(
    r"^(value|secret|password|token|api_key|apikey|passphrase|dsn|url|conn.*)$",
    re.I,
)

CREDENTIAL_SHAPE_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.checked: list[tuple[str, int, int]] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def table(self, name: str, expected: set[str], seen: set[str]) -> None:
        self.checked.append((name, len(expected), len(seen)))


def read(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"✗ required source file missing: {path}")
    return path.read_text(encoding="utf-8")


def go_struct_tags(source: str) -> dict[str, set[str]]:
    """Map struct name -> set of json field names."""
    structs: dict[str, set[str]] = {}
    for match in re.finditer(r"type\s+(\w+)\s+struct\s*\{(.*?)\n\}", source, re.S):
        name, body = match.group(1), match.group(2)
        tags = {
            tag.split(",")[0]
            for tag in re.findall(r'json:"([^"]+)"', body)
            if tag.split(",")[0] not in ("", "-")
        }
        if tags:
            structs[name] = tags
    return structs


def go_response_keys(source: str, func_name: str) -> set[str]:
    """Keys of the response map a handler writes, e.g. `"ready":`.

    These two envelopes are built as `map[string]any` rather than a struct, so
    the compiler cannot catch a renamed field - which is exactly why this audit
    drives them off the captured payload instead of trusting either side.
    """
    match = re.search(
        rf"func \(h \*Handlers\) {func_name}\(w http\.ResponseWriter.*?\n\}}",
        source,
        re.S,
    )
    if not match:
        raise SystemExit(f"✗ Go handler not found: {func_name} in {GO_HANDLER}")
    return set(re.findall(r'"([a-z0-9_]+)":', match.group(0)))


def dart_class_keys(source: str, class_name: str) -> set[str]:
    """Keys a Dart model reads out of JSON, e.g. `json['latency_ms']`."""
    match = re.search(rf"\nclass {class_name} \{{(.*?)\n\}}", source, re.S)
    if not match:
        raise SystemExit(f"✗ Dart class not found: {class_name} in {DART_MODELS}")
    return set(re.findall(r"json\['([a-z0-9_]+)'\]", match.group(1)))


def ts_interface_fields(source: str, interface: str) -> set[str]:
    match = re.search(rf"export interface {interface} \{{(.*?)\n\}}", source, re.S)
    if not match:
        raise SystemExit(f"✗ TS interface not found: {interface} in {TS_TYPES}")
    body = re.sub(r"//.*", "", match.group(1))
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return {
        field.rstrip("?").strip()
        for field in re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\??\s*:", body, re.M)
    }


def load_fixture(name: str) -> dict:
    path = FIXTURES / f"{name}.json"
    if not path.is_file():
        raise SystemExit(
            f"✗ fixture missing: {path}\n"
            "  capture it with: python scripts/capture_contract_fixtures.py"
        )
    raw = path.read_text(encoding="utf-8")
    if (found := CREDENTIAL_SHAPE_RE.search(raw)):
        raise SystemExit(
            f"✗ {path.name} looks like it captured a credential "
            f"(match at offset {found.start()}); fixtures must be shape-only"
        )
    return json.loads(raw)


def compare(
    report: Report,
    label: str,
    fixture_fields: set[str],
    consumers: dict[str, set[str]],
) -> None:
    """Every fixture field must be described by the producer and each consumer."""
    report.table(label, fixture_fields, set())
    for name, fields in consumers.items():
        missing = sorted(f for f in fixture_fields if f not in fields)
        if missing:
            report.error(
                f"{label}: {name} does not read field(s) the API sends: "
                f"{', '.join(missing)}"
            )
        phantom = sorted(f for f in fields if f not in fixture_fields)
        phantom = [f for f in phantom if f not in OPTIONAL_FIELDS]
        if phantom:
            report.warn(
                f"{label}: {name} reads field(s) absent from the capture "
                f"(typo, or optional at runtime): {', '.join(phantom)}"
            )
        forbidden = sorted(f for f in fields if FORBIDDEN_FIELD_RE.match(f))
        if forbidden:
            report.error(
                f"{label}: {name} reads value-bearing field(s) from a contract "
                f"payload: {', '.join(forbidden)} - the contract is booleans only"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    args = parser.parse_args()

    go = go_struct_tags(read(GO_HANDLER))
    dart_src = read(DART_MODELS)
    ts_src = read(TS_TYPES)

    setup = load_fixture("system_setup")
    env = load_fixture("system_env")
    health = load_fixture("health_services")

    setup_go = go_response_keys(read(GO_HANDLER), "SetupReport")
    env_go = go_response_keys(read(GO_HANDLER), "EnvContractHandler")
    check_go = go.get("EnvCheck", set())
    service_go = go.get("ServiceProbe", set())

    report = Report()

    # --- the element shapes both payloads share ------------------------------
    check_fixture = set((setup.get("checks") or [{}])[0].keys())
    compare(
        report,
        "checks[]",
        check_fixture,
        {
            "api/internal/handlers/system.go:EnvCheck": check_go,
            "mobile/lib/core/models.dart:EnvCheck": dart_class_keys(dart_src, "EnvCheck"),
            "web/frontend/src/types.ts:EnvCheck": ts_interface_fields(ts_src, "EnvCheck"),
        },
    )
    if set((env.get("checks") or [{}])[0].keys()) != check_fixture:
        report.error("checks[] differs between /api/system/setup and /api/system/env")

    service_fixture = set((setup.get("services") or [{}])[0].keys())
    compare(
        report,
        "services[]",
        service_fixture,
        {
            "api/internal/handlers/system.go:ServiceProbe": service_go,
            "mobile/lib/core/models.dart:ServiceHealth": dart_class_keys(
                dart_src, "ServiceHealth"
            ),
            "web/frontend/src/types.ts:ServiceHealth": ts_interface_fields(
                ts_src, "ServiceHealth"
            ),
        },
    )
    if set((health.get("services") or [{}])[0].keys()) != service_fixture:
        report.error(
            "the services[] shape differs between /api/system/setup and "
            "/api/v1/health/services - one client would render blanks"
        )

    # --- the envelopes -------------------------------------------------------
    compare(
        report,
        "system_setup",
        set(setup.keys()),
        {
            "api/internal/handlers/system.go:SetupReport": setup_go,
            "mobile/lib/core/models.dart:SetupReport": dart_class_keys(
                dart_src, "SetupReport"
            ),
            "web/frontend/src/types.ts:SetupReport": ts_interface_fields(
                ts_src, "SetupReport"
            ),
        },
    )
    compare(
        report,
        "system_env",
        set(env.keys()),
        {
            "api/internal/handlers/system.go:EnvContractHandler": env_go,
            "web/frontend/src/types.ts:EnvContract": ts_interface_fields(
                ts_src, "EnvContract"
            ),
        },
    )
    compare(
        report,
        "health_services",
        set(health.keys()),
        {
            "web/frontend/src/types.ts:ServicesHealthResponse": ts_interface_fields(
                ts_src, "ServicesHealthResponse"
            ),
        },
    )

    # --- the contract must stay value-free -----------------------------------
    for label, payload in (("system_setup", setup), ("system_env", env)):
        for entry in payload.get("checks") or []:
            if not isinstance(entry.get("set"), bool):
                report.error(f"{label}: checks[].set must be a boolean, got {entry!r}")
            extra = set(entry.keys()) - check_fixture
            if extra:
                report.error(f"{label}: checks[] carries unexpected keys {sorted(extra)}")

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not report.errors,
                    "errors": report.errors,
                    "warnings": report.warnings,
                    "fixtures": {
                        "system_setup": sorted(setup.keys()),
                        "system_env": sorted(env.keys()),
                        "health_services": sorted(health.keys()),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("API contract audit\n")
        for name, expected, _ in report.checked:
            print(f"  checked  {name}")
        print(
            f"\n  fixtures: system_setup={len(setup)} keys, system_env={len(env)} keys, "
            f"health_services={len(health)} keys"
        )
        for warning in report.warnings:
            print(f"  ⚠ {warning}")
        for error in report.errors:
            print(f"  ✗ {error}")
        print(
            f"\n{'✓ contract in sync' if not report.errors else '✗ contract drift'}"
            f" ({len(report.errors)} error(s), {len(report.warnings)} warning(s))"
        )

    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
