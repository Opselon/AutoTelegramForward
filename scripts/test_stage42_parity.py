#!/usr/bin/env python3
"""Stage 4.2 cross-language differential parity harness.

Runs the SAME cases against the real Python production engines and the real
Go production engines, and compares the outputs byte for byte. This is the
"Python versus Go differential test" the migration contract requires — not
a fixture file read by both sides, but one driver that executes both
engines and asserts they agree.

Python engines exercised (the real production modules, never reimplemented):
    core.domain.entities.CleanerRule.clean
    core.domain.services.LinkManagementEngine
    core.domain.services.FilterEngine.remove_urls / replace_links / remove_emojis
    core.domain.text_replace.apply_replacements
    core.domain.services.AdvancedSettingsEngine.apply_content_transforms
    core.domain.content_policy.ContentPolicy.transform

Go engines exercised (the Stage 4.2 Worker side, compiled once):
    atf/worker/internal/domain/cleaning (CleanerRule, Pipeline, links)

The Go side runs through a small generated main that reads a JSON case
list on stdin and writes a JSON result list on stdout, so the harness has
one subprocess for the whole suite instead of one per case.

Exit code 0 = every case agrees. Anything else prints the divergences.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.domain.entities import CleanerRule, ForwardRule, MessagePayload  # noqa: E402
from core.domain.services import (  # noqa: E402
    AdvancedSettingsEngine,
    FilterEngine,
    LinkManagementEngine,
)
from core.domain.text_replace import apply_replacements  # noqa: E402
from core.domain.value_objects import MediaType  # noqa: E402

GO_DRIVER = ROOT / "worker" / "tests" / "parity_driver_main.go"

# ---------------------------------------------------------------------------
# Python engine adapters
# ---------------------------------------------------------------------------

_ENGINE = FilterEngine()


def _cleaner(spec: Dict[str, Any]) -> CleanerRule:
    rule = CleanerRule()
    for key, value in spec.items():
        setattr(rule, key, value)
    return rule


def py_cleaner(case: Dict[str, Any]) -> str:
    return _cleaner(case["cleaner"]).clean(case["text"])


def py_emoji(case: Dict[str, Any]) -> str:
    return _ENGINE.remove_emojis(case["text"])


def py_link_policy(case: Dict[str, Any]) -> str:
    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    rule.link_policy = case["policy"]
    rule.domain_allowlist = case.get("allowlist", [])
    rule.domain_blocklist = case.get("blocklist", [])
    rule.link_rewrite_map = case.get("rewrite_map", {})
    payload = MessagePayload(message_id=1, chat_id="-1", text=case["text"],
                             media_type=MediaType.TEXT)
    text, _ents, _altered = LinkManagementEngine.apply_to_payload(payload, rule, text=case["text"])
    return text


def py_remove_urls(case: Dict[str, Any]) -> str:
    return _ENGINE.remove_urls(case["text"])


def py_replace_links(case: Dict[str, Any]) -> str:
    return _ENGINE.replace_links(case["text"], case["replacement"])


def py_replacements(case: Dict[str, Any]) -> str:
    return apply_replacements(case["text"], case["replacements"])


def _rule_with(**props: Any) -> ForwardRule:
    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    for key, value in props.items():
        setattr(rule, key, value)
    return rule


def py_advanced(case: Dict[str, Any]) -> str:
    rule = _rule_with()
    rule.metadata = dict(case.get("metadata", {}))
    text, _applied = AdvancedSettingsEngine.apply_content_transforms(case["text"], rule)
    return text


def py_header_footer(case: Dict[str, Any]) -> str:
    return _ENGINE.apply_header_footer(case["text"], header=case.get("header", ""),
                                       footer=case.get("footer", ""))


ADAPTERS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "cleaner": py_cleaner,
    "emoji": py_emoji,
    "link_policy": py_link_policy,
    "remove_urls": py_remove_urls,
    "replace_links": py_replace_links,
    "replacements": py_replacements,
    "advanced": py_advanced,
    "header_footer": py_header_footer,
}

# ---------------------------------------------------------------------------
# The case list — every entry executed by BOTH engines.
# ---------------------------------------------------------------------------


def cases() -> List[Dict[str, Any]]:
    return [
        # --- CleanerRule.clean (CLN-FULL-01, CLN-PHONE-01, CLN-MENTION-01,
        # --- CLN-HASHTAG-01)
        {"engine": "cleaner", "text": "@bob see https://a.io/x #news 5% OFF\n\n\n@alice call +1 415 555 2671 now",
         "cleaner": {"remove_urls": True, "remove_mentions": True, "remove_hashtags": True,
                     "remove_phone_numbers": True, "remove_empty_lines": True}},
        {"engine": "cleaner", "text": "order 1234567890 done",
         "cleaner": {"remove_phone_numbers": True}},
        {"engine": "cleaner", "text": "ping @abc now", "cleaner": {"remove_mentions": True}},
        {"engine": "cleaner", "text": "ping @alice now", "cleaner": {"remove_mentions": True}},
        {"engine": "cleaner", "text": "trend #hello-world now", "cleaner": {"remove_hashtags": True}},
        {"engine": "cleaner", "text": "gold #signal #vip now", "cleaner": {"remove_hashtags": True}},
        {"engine": "cleaner", "text": "call +989123456789 today", "cleaner": {"remove_phone_numbers": True}},
        {"engine": "cleaner", "text": "Buy now this promo deal",
         "cleaner": {"patterns": ["Buy now", "promo"]}},
        {"engine": "cleaner", "text": "first\n\n\n\nsecond", "cleaner": {"remove_empty_lines": True}},
        {"engine": "cleaner", "text": "hello", "cleaner": {"use_regex": True, "patterns": ["(unclosed"]}},

        # --- emoji (UNI-EMOJI-01..05)
        {"engine": "emoji", "text": "\U0001F468\u200d\U0001F469\u200d\U0001F467"},
        {"engine": "emoji", "text": "5\ufe0f\u20e3"},
        {"engine": "emoji", "text": "\U00012000 cuneiform"},
        {"engine": "emoji", "text": "سلام 🌟 دنیا"},
        {"engine": "emoji", "text": "a ← b, á"},
        {"engine": "emoji", "text": "hello 🚀 world"},

        # --- link policies
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_ALL_URLS"},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_EXTERNAL_URLS", "allowlist": ["t.me"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "ALLOWLIST_ONLY", "allowlist": ["t.me"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REWRITE_LINKS", "rewrite_map": {"a.io": "b.io"}},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_SELECTED_DOMAINS", "blocklist": ["evil.com"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "CUSTOM_LINK_POLICY", "allowlist": ["t.me"], "blocklist": ["evil.com"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_BUTTON_URLS"},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "PRESERVE_ALL"},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_ALL"},  # unknown value → fail closed to preserve
        # LNK-OVERRIDE-01: a mention survives link removal.
        {"engine": "link_policy", "text": "see @bob and https://a.io", "policy": "REMOVE_ALL_URLS"},

        # --- FilterEngine.remove_urls (the content-policy rewrite path)
        {"engine": "remove_urls", "text": "see @bob and https://a.io"},
        {"engine": "remove_urls", "text": "visit https://a.io now"},
        {"engine": "remove_urls", "text": "markdown [anchor](https://a.io) link"},
        {"engine": "remove_urls", "text": "t.me/x and www.example.com/y"},

        # --- replace_links (the durable-path legacy rewrite)
        {"engine": "replace_links", "text": "visit https://a.io now", "replacement": "[LINK]"},
        {"engine": "replace_links", "text": "markdown [anchor](https://a.io) link", "replacement": "X"},

        # --- apply_replacements (RPL-* family)
        {"engine": "replacements", "text": "# sale",
         "replacements": {"#": "##", "##": "x"}},
        # The same entries in the opposite storage order compose the other
        # way: same-priority insertion order IS the observable contract
        # (RPL-PRIORITY-01's notes), and a Go map decode would silently
        # re-order it.
        {"engine": "replacements", "text": "# sale",
         "replacements": {"##": "x", "#": "##"}},
        {"engine": "replacements", "text": "discount 10% DISCOUNT 20%",
         "replacements": {"DISCOUNT": "SALE"}},
        {"engine": "replacements", "text": "discount 10% DISCOUNT 20%",
         "replacements": {"Discount": "Sale"}},
        {"engine": "replacements", "text": "hello world",
         "replacements": {"world": ""}},
        {"engine": "replacements", "text": "item: apple",
         "replacements": {r"(\w+): (\w+)": {"to": r"\1 is \2", "mode": "regex"}}},
        {"engine": "replacements", "text": "hello",
         "replacements": {r"(unclosed": {"to": "x", "mode": "regex"}}},
        {"engine": "replacements", "text": "a\\1b",
         "replacements": {"a\\1b": "KEPT"}},
        {"engine": "replacements", "text": "promo price today",
         "replacements": {"promo": {"to": "sale", "whole_word": True}}},

        # --- advanced content transforms (watermark / link buttons / affiliate)
        {"engine": "advanced", "text": "body",
         "metadata": {"watermark_text": "© me", "paid_addons": {"watermark": True}}},
        {"engine": "advanced", "text": "body",
         "metadata": {"link_buttons": [{"text": "Join", "url": "https://t.me/x"}],
                      "paid_addons": {"link_buttons": True}}},
        {"engine": "advanced", "text": "see https://a.io/x now",
         "metadata": {"affiliate_kit": {"enabled": True, "prefix": "https://aff.io/"},
                      "paid_addons": {"affiliate_kit": True}}},
        # The add-on gate: paid_addons off means the transform never runs.
        {"engine": "advanced", "text": "body",
         "metadata": {"watermark_text": "© me", "paid_addons": {}}},

        # --- header / footer
        {"engine": "header_footer", "text": "body", "header": "HDR"},
        {"engine": "header_footer", "text": "HDR\n\nbody", "header": "HDR"},
        {"engine": "header_footer", "text": "body", "footer": "FTR"},
        {"engine": "header_footer", "text": "body", "header": "H", "footer": "F"},
        {"engine": "header_footer", "text": "body", "header": "", "footer": ""},
    ]


# ---------------------------------------------------------------------------
# The Go driver
# ---------------------------------------------------------------------------

# The driver source is a committed file (worker/tests/parity_driver.go), kept
# in the repo so the Go toolchain vets it on every build instead of the
# harness generating unvetted code at run time. The harness copies it into
# a main package under worker/tests, runs it once over the case list, and
# removes the copy.
DRIVER_SOURCE_FILE = ROOT / "worker" / "tests" / "parity_driver.go"


def run_go(cases_json: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Run the Go driver once over the whole case list."""
    driver_dir = ROOT / "worker" / "tests"
    driver_dir.mkdir(parents=True, exist_ok=True)
    driver = driver_dir / "parity_driver_main.go"
    source = DRIVER_SOURCE_FILE.read_text(encoding="utf-8")
    # Strip the build tag from the copy: the tag keeps the committed file
    # out of the normal build (it declares package main inside a library
    # tree), and the copy must compile when the harness runs it.
    lines = source.splitlines()
    while lines and (lines[0].startswith("//go:build") or
                     lines[0].startswith("// +build") or lines[0].strip() == ""):
        lines = lines[1:]
    driver.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            ["go", "run", "./tests"],
            cwd=str(ROOT / "worker"),
            input=json.dumps(cases_json).encode("utf-8"),
            capture_output=True,
            timeout=240,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
            return None
        return json.loads(proc.stdout.decode("utf-8"))
    finally:
        driver.unlink(missing_ok=True)


def main() -> int:
    case_list = cases()
    go_results = run_go(case_list)
    if go_results is None:
        print("PARITY: could not run the Go driver", file=sys.stderr)
        return 2

    by_index = {r["index"]: r for r in go_results}
    divergent = 0
    for index, case in enumerate(case_list):
        adapter = ADAPTERS[case["engine"]]
        py_out = adapter(case)
        go_out = by_index[index]["output"]
        if py_out != go_out:
            divergent += 1
            print(f"DIVERGENT case {index} ({case['engine']}):")
            print(f"  input : {case['text']!r}")
            print(f"  python: {py_out!r}")
            print(f"  go    : {go_out!r}")
    total = len(case_list)
    print(f"Stage 4.2 differential parity: {total - divergent}/{total} cases agree")
    if divergent:
        print(f"PARITY FAILED: {divergent} divergences", file=sys.stderr)
        return 1
    print("PARITY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
