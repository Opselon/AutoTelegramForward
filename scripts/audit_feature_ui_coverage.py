#!/usr/bin/env python3
"""Feature-reachability audit: is every catalogued feature reachable from each UI?

`docs/feature-coverage-matrix.md` lists what the engine can do. That says nothing
about whether a user can reach it from the dashboard, the phone or the bot. This
tool maps each feature to the surfaces that expose it, by searching the actual UI
source for the feature's vocabulary, and prints the gaps.

    python3 scripts/audit_feature_ui_coverage.py            # matrix
    python3 scripts/audit_feature_ui_coverage.py --gaps     # only missing
    python3 scripts/audit_feature_ui_coverage.py --json     # machine-readable

Exit code is always 0: this is a report, not a gate (the matrix legitimately has
bot-only features). Wire --fail-if-empty=<surface> in CI if a surface must stay
at full parity.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "feature-coverage-matrix.md"

SURFACES = {
    "bot": [ROOT / "core" / "infrastructure" / "telegram" / "pro_bot_ui.py",
            ROOT / "core" / "infrastructure" / "telegram" / "ui_state.py",
            ROOT / "core" / "infrastructure" / "telegram" / "bot_ui"],
    "web": [ROOT / "web" / "frontend" / "src"],
    "mobile": [ROOT / "mobile" / "lib"],
}

# Feature id -> vocabulary that only appears if the surface actually exposes it.
# Keep terms distinctive: a generic word like "option" matches everywhere and
# makes the report lie.
TERMS: dict[str, list[str]] = {
    "F01": ["edit_sync", "editSync", "edit sync", "sync_edit", "syncEdit"],
    "F02": ["delete_sync", "deleteSync", "delete sync", "sync_delete", "syncDelete"],
    "F03": ["reply", "replyTo", "reply_to"],
    "F04": ["header", "footer"],
    "F05": ["duplicate_filter", "duplicateFilter", "dup_filter", "dupFilter", "duplicate filter"],
    "F06": ["replacement", "replacements", "text_replace", "textReplace", "replace_rule"],
    "F07": ["blacklist", "black_list", "blocklist"],
    "F08": ["whitelist", "white_list", "allowlist"],
    "F09": ["cleaner"],
    "F10": ["filter", "filters"],
    "F11": ["delay"],
    "F12": ["paused", "pause", "is_active", "isActive", "enabled"],
    "F13": ["duplicate", "clone", "copy_rule", "copyRule"],
    "F14": ["rename"],
    "F15": ["draft"],
    "F16": ["advanced"],
    "F17": ["settings", "all_settings", "allSettings"],
    "F18": ["delete_rule", "deleteRule", "remove_rule", "removeRule", "delete rule"],
    "F19": ["route", "routing"],
    "F20": ["bot_token", "botToken", "bot token"],
    "F21": ["category", "categories"],
    "F22": ["back"],
    "F23": ["comment", "comments"],
    "F24": ["every_n", "everyN", "forward_every", "forwardEvery"],
    "F25": ["keyword", "keywords"],
    "F26": ["keyword_reply", "keywordReply", "reply_threshold", "replyThreshold"],
    "F27": ["auto_delete", "autoDelete", "autodelete", "self_destruct", "selfDestruct"],
    "F28": ["webhook"],
    "X01": ["ai_rewrite", "aiRewrite", "rewrite"],
    "X02": ["watermark"],
    "X03": ["translat"],
    "X04": ["schedule", "cron"],
    "X05": ["bot_token", "botToken"],
    "X06": ["ai_config", "aiConfig", "ai config", "prompt"],
    "X07": ["session", "sessions", "account", "accounts"],
    "X08": ["backup", "restore", "migrate", "migration"],
    "X09": ["diagnostic", "dlq", "dead_letter", "deadLetter"],
    "X10": ["log", "logs", "audit"],
    "X11": ["admin"],
    "X12": ["dashboard"],
}

ID_RE = re.compile(r"^\|\s*((?:F|X)\d{2})\s*\|([^|]*)\|")


def features() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in MATRIX.read_text(encoding="utf-8", errors="replace").splitlines():
        match = ID_RE.match(line)
        if match:
            rows.append((match.group(1), match.group(2).strip()))
    return rows


def collect_files(paths: list[Path], suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.exists():
            for candidate in path.rglob("*"):
                if candidate.is_file() and candidate.suffix in suffixes:
                    files.append(candidate)
    return files


def scan(surface: str) -> dict[str, str]:
    suffixes = (".py",) if surface == "bot" else ((".tsx", ".ts") if surface == "web" else (".dart",))
    index: dict[str, str] = {}
    for path in collect_files(SURFACES[surface], suffixes):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        index[str(path.relative_to(ROOT))] = text
    return index


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gaps", action="store_true", help="print only features missing from a surface")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--surface", choices=sorted(SURFACES), help="restrict the report to one surface")
    args = parser.parse_args(argv)

    corpora = {name: scan(name) for name in SURFACES}
    report = []
    for feature_id, name in features():
        terms = TERMS.get(feature_id, [])
        row = {"id": feature_id, "feature": name, "surfaces": {}, "terms": terms}
        for surface, files in corpora.items():
            if args.surface and surface != args.surface:
                continue
            hits = []
            for relative, text in files.items():
                lowered = text.lower()
                for term in terms:
                    if term.lower() in lowered:
                        hits.append(relative)
                        break
            row["surfaces"][surface] = {"hit": bool(hits), "files": len(hits)}
        report.append(row)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    surfaces = [args.surface] if args.surface else list(SURFACES)
    header = f"{'ID':5} {'feature':38} " + " ".join(f"{s:8}" for s in surfaces)
    print(header)
    print("-" * len(header))
    for row in report:
        if args.gaps and all(row["surfaces"][s]["hit"] for s in surfaces):
            continue
        marks = " ".join(("yes" if row["surfaces"][s]["hit"] else "MISSING").ljust(8) for s in surfaces)
        print(f"{row['id']:5} {row['feature'][:37]:38} {marks}")

    print()
    for surface in surfaces:
        missing = [row["id"] for row in report if not row["surfaces"][surface]["hit"]]
        print(f"{surface}: {len(report) - len(missing)}/{len(report)} features reachable"
              + (f" — missing {', '.join(missing)}" if missing else ""))
    untracked = [row["id"] for row in report if not row["terms"]]
    if untracked:
        print(f"note: no vocabulary mapped for {', '.join(untracked)} (extend TERMS)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
