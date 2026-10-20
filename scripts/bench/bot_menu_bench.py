#!/usr/bin/env python3
"""Measure the bot's menu render path (the §5 "menu build ≤ 2 ms" budget).

Builds the real main menu — real catalog, real style funnel, real language
resolution — for a client and an admin, in every supported locale, and reports
median / p95 over ``--repeats`` builds per case.

Run it from a checkout whose ``core`` is importable::

    python scripts/bench/bot_menu_bench.py
    python scripts/bench/bot_menu_bench.py --json

It is a measurement tool, not a test: the regression gate is
``core/tests/test_bot_menu_render_perf.py``.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.infrastructure.telegram.i18n import SUPPORTED_LANGUAGES, I18n  # noqa: E402
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState  # noqa: E402

ADMIN_UID = 5094837833
CLIENT_UID = 5989354154


class _CountingDB:
    def __init__(self, language: str = "en") -> None:
        self.language = language
        self.reads = 0

    def query_one(self, sql, params=()):  # noqa: ANN001
        self.reads += 1
        return {"language": self.language}


class _Users:
    def __init__(self, db: _CountingDB) -> None:
        self._db = db


def make_ui(lang: str, *, admin: bool, with_db: bool, prewarm_state: bool) -> tuple[ProBotUI, _CountingDB]:
    ui = object.__new__(ProBotUI)
    ui.i18n = I18n()
    ui._states = {}
    ui._db = None
    ui._admin_ids = {ADMIN_UID} if admin else set()
    ui._admin_service = None
    db = _CountingDB(lang)
    ui._users = _Users(db) if with_db else None
    if prewarm_state:
        uid = ADMIN_UID if admin else CLIENT_UID
        st = UiState()
        st.lang = lang
        ui._states[uid] = st
    return ui, db


def measure(ui: ProBotUI, uid: int, repeats: int) -> dict:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        ui._main_menu(uid)
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    return {
        "p50_ms": round(statistics.median(samples), 4),
        "p95_ms": round(samples[int(len(samples) * 0.95) - 1], 4),
        "max_ms": round(samples[-1], 4),
    }


#: A representative screen's worth of catalog keys: plain labels, labels with
#: placeholders (the formatting path), and button keys that go on to be
#: decorated. Roughly the mix a settings/rule panel renders per edit.
SCREEN_KEYS = [
    "ui_cancel", "ui_back", "ui_done", "ui_refresh", "ui_main_menu_forward",
    "ui_main_menu_filters", "ui_main_menu_settings", "ui_main_menu_stats",
    "ui_main_menu_help", "ui_main_menu_lang", "ui_main_menu_sessions",
    "ui_main_menu_publish", "ui_main_menu_clone", "ui_main_menu_web",
    "ui_main_menu_diag", "ui_main_menu_upgrade", "choose_lang", "start",
]
FORMATTED_KEYS = [
    ("login_success", {"phone": "+15551234567"}),
    ("ui_page_of", {"page": "2", "total": "7", "count": "1284"}),
    ("cooldown", {"error": "30"}),
    ("ui_admin_activate_user", {"uid": "5094837833"}),
]


def measure_text(repeats: int) -> dict:
    """µs per ``I18n.t`` call over a screen's worth of keys, all locales."""
    i18n = I18n()
    # Warm every language so the measurement is the steady state a running
    # bot is in, not the first-ever call.
    for lang in SUPPORTED_LANGUAGES:
        for key in SCREEN_KEYS:
            i18n.t(key, lang=lang)
        for key, kw in FORMATTED_KEYS:
            i18n.t(key, lang=lang, **kw)

    calls = 0
    started = time.perf_counter()
    for _ in range(repeats):
        for lang in SUPPORTED_LANGUAGES:
            for key in SCREEN_KEYS:
                i18n.t(key, lang=lang)
                calls += 1
            for key, kw in FORMATTED_KEYS:
                i18n.t(key, lang=lang, **kw)
                calls += 1
    elapsed = time.perf_counter() - started
    return {
        "calls": calls,
        "us_per_call": round(elapsed / calls * 1e6, 3),
        "calls_per_second": int(calls / elapsed),
    }


def measure_buttons(repeats: int) -> dict:
    """µs per ``_btn`` (catalog lookup + style glyph + colour class)."""
    ui, _ = make_ui("en", admin=False, with_db=False, prewarm_state=False)
    keys = [key for key in SCREEN_KEYS if key.startswith("ui_main_menu_")] or SCREEN_KEYS
    for lang in SUPPORTED_LANGUAGES:
        for key in keys:
            ui._btn(key, lang=lang)

    calls = 0
    started = time.perf_counter()
    for _ in range(repeats):
        for lang in SUPPORTED_LANGUAGES:
            for key in keys:
                ui._btn(key, lang=lang)
                calls += 1
    elapsed = time.perf_counter() - started
    return {
        "calls": calls,
        "us_per_call": round(elapsed / calls * 1e6, 3),
        "calls_per_second": int(calls / elapsed),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=300)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--section", choices=("menu", "text", "buttons", "all"), default="all")
    args = ap.parse_args()

    results = {}
    for lang in SUPPORTED_LANGUAGES:
        for admin in (False, True):
            uid = ADMIN_UID if admin else CLIENT_UID
            # Menu built from a bare uid: no in-memory state, so the language
            # has to be resolved — this is the shape the old code paid a
            # database read for on every render.
            ui, db = make_ui(lang, admin=admin, with_db=True, prewarm_state=False)
            ui._main_menu(uid)  # warm: catalog table + style memo
            db.reads = 0
            timing = measure(ui, uid, args.repeats)
            timing["db_reads_per_build"] = round(db.reads / args.repeats, 3)
            results[f"{lang}{'/admin' if admin else ''}"] = timing

    if args.section == "menu":
        worst = max(results.items(), key=lambda kv: kv[1]["p50_ms"])
        if args.json:
            print(json.dumps({"cases": results, "worst": worst[0], **worst[1]}, indent=2))
        else:
            print(f"{'case':<10} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8} {'db/build':>9}")
            for name, row in results.items():
                print(
                    f"{name:<10} {row['p50_ms']:>8.3f} {row['p95_ms']:>8.3f} "
                    f"{row['max_ms']:>8.3f} {row['db_reads_per_build']:>9.3f}"
                )
            print(f"\nworst p50: {worst[1]['p50_ms']:.3f} ms ({worst[0]}) — budget 2.000 ms")
        return 0

    # The menu is measured for every section: it is the published budget.
    client_en = results.get("en") or next(iter(results.values()))
    if args.section in ("text", "all"):
        results["text"] = measure_text(args.repeats)
    if args.section in ("buttons", "all"):
        results["buttons"] = measure_buttons(args.repeats)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"menu build (en, client): p50 {client_en['p50_ms']:.3f} ms, "
              f"p95 {client_en['p95_ms']:.3f} ms, "
              f"{client_en['db_reads_per_build']:.2f} db reads/build — budget 2.000 ms")
        for name in ("text", "buttons"):
            row = results.get(name)
            if row:
                print(f"{name:<8} {row['us_per_call']:>8.3f} µs/call  "
                      f"({row['calls_per_second']:,} calls/s, n={row['calls']:,})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
