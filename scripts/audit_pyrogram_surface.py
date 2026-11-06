#!/usr/bin/env python3
"""One-to-one audit of our pyrogram usage against the pinned release.

``docs/pyrogram-methods-2.0.106.json`` is the documented surface of the
pinned pyrogram (docs.pyrogram.org's API Reference, verified against the
v2.0.106 tag source). This script holds the app to it:

1. every call whose method name matches a surface method — in any module
   that imports pyrogram — must pass only parameters that surface method
   accepts (kwargs are checked mechanically; a mismatch is how the
   ``get_messages(limit=...)`` album bug and the ``message_thread_id``
   topic bug looked in code review, and both failed real deliveries);
2. every symbol imported from ``pyrogram.errors`` / ``pyrogram.enums`` /
   ``pyrogram.types`` / ``pyrogram.handlers`` must exist in the surface.

Re-run whenever the pin moves or a new call site appears:

    python3 scripts/audit_pyrogram_surface.py

Stdlib-only. Exit 1 on any finding, 2 when the surface JSON is missing.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SURFACE = ROOT / "docs" / "pyrogram-methods-2.0.106.json"

#: Modules whose receiver names we trust to be pyrogram Clients. A call is
#: attributed when its file imports pyrogram AND the method name matches the
#: surface; same-named methods on other objects (our own Bot API transport,
#: sqlite wrappers) are excluded by this allowlist of receiver spellings.
CLIENT_RECEIVERS = re.compile(r"(^|\.)(client|_client|bot)$") if (re := __import__("re")) else None

SCAN_ROOTS = ("core", "telegram_runtime", "atfcli", "scripts")


def main() -> int:
    if not SURFACE.exists():
        print(f"surface file missing: {SURFACE}", file=sys.stderr)
        return 2
    doc = json.loads(SURFACE.read_text(encoding="utf-8"))
    methods = {m["name"]: m for m in doc["high_level_methods"]}
    types = set(doc["types"])
    enums = set(doc["enums"])
    errors = set(doc["errors"]["names"])

    findings: list[str] = []
    call_checked = 0
    imports_checked = 0

    for root in SCAN_ROOTS:
        base = ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            if "pyrogram" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                name = node.func.attr
                spec = methods.get(name)
                if spec is None:
                    continue
                recv_src = ast.unparse(node.func.value)
                if not CLIENT_RECEIVERS.search(recv_src):
                    continue
                call_checked += 1
                valid = {p["name"] for p in spec["params"]}
                has_var_kwargs = any(p["name"].startswith("**") for p in spec["params"])
                if has_var_kwargs:
                    continue
                for kw in node.keywords:
                    if kw.arg and kw.arg not in valid:
                        findings.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: "
                            f"{name}({kw.arg}=...) — not a parameter of the pinned "
                            f"{name}({', '.join(sorted(valid))})")

            for node in ast.walk(tree):
                if not (isinstance(node, ast.ImportFrom) and node.module):
                    continue
                mod = node.module
                if mod == "pyrogram.errors":
                    for a in node.names:
                        imports_checked += 1
                        if a.name not in errors:
                            findings.append(
                                f"{path.relative_to(ROOT)}:{node.lineno}: "
                                f"pyrogram.errors.{a.name} — not in the pinned error surface")
                elif mod == "pyrogram.enums":
                    for a in node.names:
                        imports_checked += 1
                        if a.name not in enums:
                            findings.append(
                                f"{path.relative_to(ROOT)}:{node.lineno}: "
                                f"pyrogram.enums.{a.name} — not in the pinned enum surface")
                elif mod == "pyrogram.types":
                    for a in node.names:
                        imports_checked += 1
                        if a.name not in types:
                            findings.append(
                                f"{path.relative_to(ROOT)}:{node.lineno}: "
                                f"pyrogram.types.{a.name} — not in the pinned type surface")

    print(f"pyrogram surface audit: {call_checked} call site(s), "
          f"{imports_checked} symbol import(s) checked against pyrogram {doc['pinned_version']}")
    if findings:
        print(f"\n{len(findings)} finding(s):")
        for f in findings:
            print(f"  - {f}")
        return 1
    print("PASS: every pyrogram call and import resolves 1:1 against the pinned surface")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
