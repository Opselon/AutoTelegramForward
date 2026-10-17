"""In-bot diagnostics: what the screen may show, and to whom.

Two things are worth testing here and neither is cosmetic.

**Owner scope.** The error log and the per-task counters are global tables; the
screen is per-account. A row naming another tenant's task must be dropped, never
summarised, and rows that name no task at all must be counted without being
attributed to one — otherwise a user reading the screen learns what someone
else's forwarder is doing.

**Degradation.** This is a screen a user opens *because* something is wrong. A
missing store, an unreadable row or an unexpected shape has to shorten the report,
never raise: a diagnostics panel that returns an error is a second failure.
"""
from __future__ import annotations

import asyncio

import pytest

from core.infrastructure.telegram.diagnostics import (
    collect_report,
    diagnostics_message,
    owned_ids_of,
    render_report,
)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def t(key, **params):
    """The real catalog, so a missing key fails here rather than on a phone."""
    from core.infrastructure.telegram.i18n import I18n

    text = I18n().t(key, lang="en", **params)
    assert text and text != key, f"catalog key missing: {key}"
    return text


class Queue:
    """The queue's read surface, with one job stuck for eleven minutes."""

    def __init__(self, stats=None, age=None, fail_age=False):
        self._stats = stats if stats is not None else {
            "PENDING": 2, "SENT": 40, "RETRY": 1, "FAILED": 3, "PROCESSING": 1,
        }
        self._age = age if age is not None else {
            "pending_count": 4, "oldest_job_age_seconds": 660,
        }
        self._fail_age = fail_age
        self.asked_retry = False

    async def get_stats(self):
        if self._fail_age:
            raise RuntimeError("queue store busy")
        return dict(self._stats)

    async def get_queue_age_metrics(self):
        return dict(self._age)


class Errors:
    def __init__(self, rows, counts=None):
        self.rows = rows
        self.counts = counts if counts is not None else {"ERROR": 2, "WARNING": 1}

    async def recent(self, limit=10, severity=None):
        return list(self.rows)[: int(limit)]

    async def counts_by_severity(self, since_ts=0):
        return dict(self.counts)


class Metrics:
    def __init__(self, totals=None, hourly=None):
        self._totals = totals if totals is not None else {
            "forwarded": 120, "filtered": 9, "errors": 3,
        }
        self._hourly = hourly if hourly is not None else [
            {"ts_hour": 1, "forwarded": 60, "filtered": 4, "errors": 1},
            {"ts_hour": 2, "forwarded": 60, "filtered": 5, "errors": 2},
        ]

    async def totals(self):
        return dict(self._totals)

    async def hourly(self, hours=24):
        return list(self._hourly)


class RuleStats:
    def __init__(self, per_rule):
        self.per_rule = per_rule
        self.asked: list[str] = []

    async def get(self, rule_id):
        self.asked.append(str(rule_id))
        return self.per_rule.get(str(rule_id))


MINE = "aaaa1111-0000-0000-0000-000000000001"
THEIRS = "bbbb2222-0000-0000-0000-000000000002"


def error_row(rule_id="", message="FloodWait", category="rate_limit", **kw):
    row = {
        "id": 7,
        "ts": 1_700_000_000,
        "category": category,
        "error_name": message,
        "severity": "ERROR",
        "detail": "",
        "recoverable": True,
        "rule_id": rule_id,
    }
    row.update(kw)
    return row


# --------------------------------------------------------------------------- #
# Owner scope
# --------------------------------------------------------------------------- #


def test_a_failure_on_someone_elses_task_is_not_shown():
    report = run(
        collect_report(
            errors=Errors([error_row(THEIRS), error_row(MINE)]),
            owned_rule_ids=[MINE],
        )
    )

    shown = [item["rule_id"] for item in report["errors"]["recent"]]
    assert shown == [MINE], f"another tenant's task leaked into the screen: {shown}"


def test_a_failure_naming_no_task_is_counted_but_not_attributed():
    report = run(
        collect_report(
            errors=Errors([error_row(""), error_row(MINE)]),
            owned_rule_ids=[MINE],
        )
    )

    assert report["errors"]["total"] == 3  # the severity counts are account-wide
    assert len(report["errors"]["recent"]) == 2
    assert report["errors"]["recent"][0]["rule_id"] == ""


def test_per_task_counters_are_read_only_for_owned_tasks():
    stats = RuleStats({MINE: {"forwarded": 5}, THEIRS: {"forwarded": 99}})

    report = run(collect_report(rule_stats=stats, owned_rule_ids=[MINE]))

    assert stats.asked == [MINE], "a counter for an unowned task was read"
    assert [task["rule_id"] for task in report["tasks"]] == [MINE]
    assert report["tasks"][0]["counters"]["forwarded"] == 5


def test_the_screen_names_the_task_a_failure_belongs_to():
    report = run(
        collect_report(errors=Errors([error_row(MINE)]), owned_rule_ids=[MINE])
    )

    body = render_report(t, report)

    assert MINE[:8] in body
    assert "task " + MINE[:8] in body


# --------------------------------------------------------------------------- #
# What the screen reports
# --------------------------------------------------------------------------- #


def test_the_queue_reports_its_backlog_and_oldest_job():
    report = run(collect_report(queue=Queue()))

    body = render_report(t, report)

    assert "4" in body, "the pending count must be shown"
    assert "11m" in body, "the age must be human-readable, not raw seconds"
    assert "RETRY 1" in body and "FAILED 3" in body


def test_a_queue_that_is_only_busy_is_reported_as_clear():
    report = run(collect_report(queue=Queue(stats={"PENDING": 0, "SENT": 12})))

    body = render_report(t, report)

    assert t("ui_diag_clear") in body


def test_a_failure_carries_the_id_a_user_can_quote():
    report = run(
        collect_report(
            errors=Errors([error_row(MINE, diagnostic_id="5c1f2b7a-9d")]),
            owned_rule_ids=[MINE],
        )
    )

    body = render_report(t, report)

    assert "5c1f2b7a-9d" in body, "the correlation id is the only quotable handle"
    # The error's own name and category, so the user can tell what broke.
    assert "rate_limit" in body and "FloodWait" in body


def test_throughput_is_shown_for_the_day_and_all_time():
    report = run(collect_report(metrics=Metrics()))

    body = render_report(t, report)

    assert "120" in body and "4" not in body.split("24")[0]
    assert t("ui_diag_throughput").split("{")[0] in body
    assert t("ui_diag_totals").split("{")[0] in body


def test_a_healthy_account_gets_a_short_screen():
    report = run(
        collect_report(
            queue=Queue(stats={"PENDING": 0, "SENT": 3}, age={"pending_count": 0}),
            errors=Errors([], counts={}),
        )
    )

    body = render_report(t, report)

    assert body.count("\n") < 8, f"too many lines for a healthy account:\n{body}"
    assert t("ui_diag_recent") not in body


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


def test_a_missing_store_shortens_the_report_instead_of_raising():
    report = run(collect_report())
    body = render_report(t, report)

    assert report["queue"] == {}
    assert body.startswith(t("ui_diag_title"))
    assert t("ui_diag_nothing_yet") in body


def test_a_failing_store_is_not_reported_as_zero():
    report = run(collect_report(queue=Queue(fail_age=True)))

    # get_queue_age_metrics still answers, get_stats does not: the pending count
    # survives and the state breakdown is simply absent.
    assert report["queue"].get("pending") == 4
    assert "states" not in report["queue"]
    assert render_report(t, report)


def test_a_stat_row_of_an_unexpected_shape_is_skipped_not_fatal():
    report = run(
        collect_report(rule_stats=RuleStats({MINE: {"forwarded": "many"}}))
    )

    assert report["tasks"] == [] or report["tasks"][0]["counters"] == {"forwarded": 0}


def test_rows_that_are_not_dicts_are_ignored():
    report = run(
        collect_report(
            errors=Errors(["not-a-row", 42, error_row(MINE)]),
            owned_rule_ids=[MINE],
        )
    )

    assert [item["rule_id"] for item in report["errors"]["recent"]] == [MINE]


def test_the_translator_is_used_for_every_line():
    """A key that only exists in English would show as the key itself."""
    for lang in ("en", "fa", "ru", "zh"):
        from core.infrastructure.telegram.i18n import I18n

        i18n = I18n()
        report = run(
            collect_report(
                queue=Queue(),
                errors=Errors([error_row(MINE, diagnostic_id="abc")]),
                metrics=Metrics(),
                rule_stats=RuleStats({MINE: {"forwarded": 1}}),
                owned_rule_ids=[MINE],
            )
        )
        body = render_report(
            lambda key, **params: i18n.t(key, lang=lang, **params), report
        )
        for line in body.splitlines():
            if line.startswith(("•", "Σ", "📊")):
                continue
            assert "ui_diag_" not in line, f"{lang}: untranslated key in {line!r}"


def test_owned_ids_ignores_rows_without_an_id():
    class Rule:
        def __init__(self, rid):
            self.id = rid

    assert owned_ids_of([Rule("a"), Rule(""), {"id": "b"}, object()]) == ["a", "b"]


def test_the_message_helper_is_the_collector_and_the_renderer():
    body = run(
        diagnostics_message(
            t, queue=Queue(), errors=Errors([error_row(MINE)]), owned_rule_ids=[MINE]
        )
    )

    assert t("ui_diag_title") in body and MINE[:8] in body
