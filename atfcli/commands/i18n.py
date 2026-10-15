"""Internationalization & localization parity audits across 10 locales."""

from __future__ import annotations

from . import _shared as sh

NAME = "i18n"
HELP = "Audit and enforce strict 1:1 translation catalog parity across all 10 locales"


def _audit(ctx, args) -> int:
    ctx.out.step("Running cross-language parity audit...")
    res1 = ctx.repo.run_streaming([ctx.repo.python(), "scripts/audit_cross_language.py"], check=False)

    ctx.out.step("Running Telegram i18n audit...")
    res2 = ctx.repo.run_streaming([ctx.repo.python(), "scripts/audit_telegram_i18n.py"], check=False)

    if res1 != 0 or res2 != 0:
        ctx.out.fail("i18n audit found parity mismatches")
        return 1
    ctx.out.ok("All 10 locale catalogs strictly match 1:1 with English baseline")
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=_audit)
    parser.add_argument("--strict", action="store_true", help="fail on any warning")
