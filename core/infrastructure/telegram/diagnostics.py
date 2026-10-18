"""What the bot can tell a user about their own forwarding, without a dashboard.

The server already answers all of this — `GET /api/v1/diagnostics` for the
dashboard, `metrics_hourly`, the error log, the delivery queue's own counters —
but a user whose only client is Telegram could see none of it. This module turns
those same stores into one screen: is anything stuck, what failed recently and
under which diagnostic id, what the queue is retrying, and how much actually went
through.

Two rules hold this together:

* **Owner scope, fail-closed.** The error log and the per-task counters are
  global tables. A row is shown only when its rule belongs to the caller; a row
  that names a rule the caller does not own is dropped, not summarised. Rows with
  no rule at all are account-level and are counted but never attributed to a task.
* **Reading never raises.** A diagnostics screen that cannot render is a second
  failure. Every source is optional and its absence degrades to "not reported",
  so a missing store shows a shorter screen rather than an error.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Queue states that mean "something is waiting or going wrong".
_ATTENTION_STATES = ("RETRY", "FAILED", "UNKNOWN", "PENDING", "PROCESSING")

#: Counters a rule-stats row may carry; summed defensively because the store is
#: allowed to grow new ones without this screen breaking.
_STAT_KEYS = ("forwarded", "filtered", "errors", "sent", "skipped", "retried")


async def _call(source: Any, name: str, *args, **kwargs) -> Any:
    """Await ``source.name(...)`` when it exists, else ``None``."""
    if source is None:
        return None
    method = getattr(source, name, None)
    if method is None:
        return None
    try:
        return await method(*args, **kwargs)
    except Exception:
        # A convenience read must never take the screen down with it.
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def collect_report(
    *,
    queue: Any = None,
    errors: Any = None,
    metrics: Any = None,
    rule_stats: Any = None,
    owned_rule_ids: Sequence[str] = (),
    recent_limit: int = 5,
) -> Dict[str, Any]:
    """Gather one diagnostics report, scoped to the rules ``owned_rule_ids`` names."""
    owned = {str(r) for r in owned_rule_ids}
    report: Dict[str, Any] = {
        "queue": {},
        "errors": {"counts": {}, "recent": [], "total": 0},
        "tasks": [],
        "throughput": {},
        "owned_rules": len(owned),
    }

    age = await _call(queue, "get_queue_age_metrics")
    if not isinstance(age, dict):
        # Older stores only report the raw count; degrade rather than show 0.
        pending = await _call(queue, "get_pending_count")
        age = {"pending_count": pending} if pending is not None else None
    if isinstance(age, dict):
        report["queue"]["pending"] = _as_int(age.get("pending_count"))
        report["queue"]["oldest_age_seconds"] = _as_int(
            age.get("oldest_job_age_seconds")
        )

    stats = await _call(queue, "get_stats")
    if isinstance(stats, dict):
        # Keys are DeliveryStatus values; anything that is not a known state is
        # still shown, because an unrecognised state is exactly what a
        # diagnostics screen should surface.
        report["queue"]["states"] = {
            str(state): _as_int(count)
            for state, count in sorted(stats.items())
            if _as_int(count) > 0 or str(state) in _ATTENTION_STATES
        }
        report["queue"]["needs_attention"] = sum(
            _as_int(count)
            for state, count in stats.items()
            if str(state).upper() in _ATTENTION_STATES
        )

    counts = await _call(errors, "counts_by_severity")
    if isinstance(counts, dict):
        report["errors"]["counts"] = {
            str(sev).upper(): _as_int(n) for sev, n in counts.items()
        }
        report["errors"]["total"] = sum(report["errors"]["counts"].values())

    recent = await _call(errors, "recent", max(int(recent_limit), 1) * 4)
    if isinstance(recent, Iterable) and not isinstance(recent, (str, bytes, dict)):
        kept: List[Dict[str, Any]] = []
        for row in recent:
            if not isinstance(row, dict):
                continue
            rule_id = str(row.get("rule_id") or "")
            if rule_id and rule_id not in owned:
                # Someone else's task: never shown, never attributed.
                continue
            kept.append(
                {
                    "category": str(row.get("category") or ""),
                    "error_name": str(row.get("error_name") or ""),
                    "severity": str(row.get("severity") or "").upper(),
                    "rule_id": rule_id,
                    "detail": str(row.get("detail") or ""),
                    "recoverable": bool(row.get("recoverable")),
                    # The one handle a user can quote when asking for help.
                    "correlation_id": str(
                        row.get("diagnostic_id")
                        or row.get("correlation_id")
                        or row.get("id")
                        or ""
                    ),
                }
            )
            if len(kept) >= int(recent_limit):
                break
        report["errors"]["recent"] = kept

    for rule_id in sorted(owned):
        row = await _call(rule_stats, "get", rule_id)
        if not isinstance(row, dict):
            continue
        counters = {
            key: _as_int(row.get(key)) for key in _STAT_KEYS if row.get(key) is not None
        }
        total = sum(
            _as_int(row.get(key)) for key in ("forwarded", "filtered", "errors")
        )
        if not counters and not total:
            continue
        report["tasks"].append({"rule_id": rule_id, "counters": counters, "total": total})
    report["tasks"].sort(key=lambda t: (-t["total"], t["rule_id"]))

    totals = await _call(metrics, "totals")
    if isinstance(totals, dict):
        report["throughput"]["totals"] = {
            key: _as_int(totals.get(key))
            for key in ("forwarded", "filtered", "errors")
            if totals.get(key) is not None
        }
    hourly = await _call(metrics, "hourly", 24)
    if isinstance(hourly, Iterable) and not isinstance(hourly, (str, bytes, dict)):
        rows = [r for r in hourly if isinstance(r, dict)]
        if rows:
            report["throughput"]["last_24h"] = {
                "forwarded": sum(_as_int(r.get("forwarded")) for r in rows),
                "filtered": sum(_as_int(r.get("filtered")) for r in rows),
                "errors": sum(_as_int(r.get("errors")) for r in rows),
                "hours": len(rows),
            }
    return report


def _age(seconds: int) -> str:
    """A duration a person can read: 45s, 12m, 3h, 2d."""
    seconds = max(_as_int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def render_report(t, report: Dict[str, Any]) -> str:
    """The report as a Telegram message, in the caller's language.

    ``t`` is the bot's translator. Only lines with something to say are emitted:
    a healthy account gets a short screen, not twelve zeros.
    """
    lines: List[str] = [t("ui_diag_title"), ""]

    queue = report.get("queue") or {}
    if queue.get("pending") is not None or queue.get("states"):
        detail = ""
        if queue.get("oldest_age_seconds"):
            detail = t("ui_diag_oldest", age=_age(queue["oldest_age_seconds"]))
        lines.append(
            t("ui_diag_queue", pending=_as_int(queue.get("pending")), detail=detail)
        )
        states = queue.get("states") or {}
        # Only states that have something in them. A store that reports its
        # failures as zeroes is a healthy queue, and "FAILED 0, RETRY 0" reads
        # like a problem report when there is none.
        attention = {
            state: count
            for state, count in states.items()
            if str(state).upper() in ("RETRY", "FAILED", "UNKNOWN")
            and _as_int(count) > 0
        }
        if attention:
            lines.append(
                t(
                    "ui_diag_states",
                    states=", ".join(f"{k} {v}" for k, v in sorted(attention.items())),
                )
            )
        elif _as_int(queue.get("needs_attention")) == 0:
            lines.append(t("ui_diag_clear"))

    errors = report.get("errors") or {}
    if errors.get("total"):
        by_sev = ", ".join(
            f"{sev} {n}" for sev, n in sorted((errors.get("counts") or {}).items()) if n
        )
        lines.append(t("ui_diag_errors", by_severity=by_sev))
    recent = errors.get("recent") or []
    if recent:
        lines += ["", t("ui_diag_recent")]
        for item in recent:
            label = item.get("category") or item.get("error_name") or "?"
            if item.get("error_name") and item.get("category"):
                label = f"{item['category']} · {item['error_name']}"
            suffix = ""
            if item.get("rule_id"):
                suffix = t("ui_diag_on_task", task=str(item["rule_id"])[:8])
            cid = item.get("correlation_id") or ""
            lines.append(f"• {label}{suffix}")
            if cid:
                # Quotable when the user asks for help, and the only stable way
                # to find this exact failure in the logger.
                lines.append(t("ui_diag_correlation", id=cid))

    throughput = report.get("throughput") or {}
    day = throughput.get("last_24h")
    if day:
        lines += [
            "",
            t(
                "ui_diag_throughput",
                forwarded=_as_int(day.get("forwarded")),
                filtered=_as_int(day.get("filtered")),
                errors=_as_int(day.get("errors")),
            ),
        ]
    totals = throughput.get("totals")
    if totals:
        lines.append(
            t(
                "ui_diag_totals",
                forwarded=_as_int(totals.get("forwarded")),
                filtered=_as_int(totals.get("filtered")),
                errors=_as_int(totals.get("errors")),
            )
        )

    tasks = report.get("tasks") or []
    if tasks:
        lines += ["", t("ui_diag_tasks")]
        for task in tasks[:5]:
            counters = task.get("counters") or {}
            detail = ", ".join(f"{k} {v}" for k, v in sorted(counters.items()))
            lines.append(
                f"• `{str(task['rule_id'])[:8]}` — {detail}"
                if detail
                else f"• `{str(task['rule_id'])[:8]}`"
            )

    if len(lines) <= 2:
        lines.append(t("ui_diag_nothing_yet"))
    return "\n".join(lines)


async def diagnostics_message(
    t, *, queue: Any = None, errors: Any = None, metrics: Any = None,
    rule_stats: Any = None, owned_rule_ids: Sequence[str] = (),
) -> str:
    """Collect and render in one step, for a callback handler."""
    report = await collect_report(
        queue=queue,
        errors=errors,
        metrics=metrics,
        rule_stats=rule_stats,
        owned_rule_ids=owned_rule_ids,
    )
    return render_report(t, report)


def owned_ids_of(rules: Optional[Iterable[Any]]) -> List[str]:
    """The rule ids a caller owns, as strings, ignoring anything malformed."""
    out: List[str] = []
    for rule in rules or ():
        rule_id = getattr(rule, "id", None) or (
            rule.get("id") if isinstance(rule, dict) else None
        )
        if rule_id:
            out.append(str(rule_id))
    return out


# ------------------------------------------------------------------------- #
# Dead-letter queue: the one queue state a user can act on
#
# A dead letter waits for a person: nothing else will touch a delivery that
# exhausted its budget. The screen below reads it through the repository's
# owner-scoped contract (`list_dead_letters`/`replay_dead_letter`/
# `purge_dead_letters`), never raw SQL — the column names are that repository's
# business, and having them here is how two backends drifted apart before.
# ------------------------------------------------------------------------- #

#: Error text shown per entry, truncated like the API's own projection.
_DEAD_LETTER_ERROR_MAX = 96

_RESULT_CODE_KEYS = {
    "requeued": "ui_dlq_requeued",
    "not_found": "ui_dlq_not_found",
    "already_pending": "ui_dlq_already_pending",
    "already_sent": "ui_dlq_already_sent",
    "replay_failed": "ui_dlq_replay_failed",
}


async def collect_dead_letters(
    *,
    queue: Any = None,
    owned_rule_ids: Sequence[str] = (),
    limit: int = 8,
) -> Dict[str, Any]:
    """The caller's dead letters, newest first, plus whether the list went on.

    ``owned_rule_ids`` is the tenant scope passed straight to the repository: an
    empty sequence means this user has no tasks, so they own no dead letters —
    never "everyone's".
    """
    scope = [str(r) for r in owned_rule_ids]
    # One row past the page declares "more instead of a false total".
    rows = await _call(queue, "list_dead_letters", scope, limit=limit + 1) or []
    entries = [r for r in (dict(r) for r in rows)][:limit]
    return {
        "total": None if len(rows) > limit else len(entries),
        "more": len(rows) > limit,
        "entries": entries,
    }


def render_dead_letters(t, collected: Dict[str, Any]) -> str:
    """One entry per line: what failed, where it was going, how often."""
    entries = list(collected.get("entries") or [])
    if not entries:
        return t("ui_dlq_empty")

    lines = [t("ui_dlq_title", count=len(entries))]
    for row in entries:
        rule_id = str(row.get("rule_id") or "")[:8]
        target = str(row.get("target_chat_id") or "")
        error = str(row.get("last_error") or "")[:_DEAD_LETTER_ERROR_MAX]
        lines.append(
            t(
                "ui_dlq_entry",
                rule=rule_id or "?",
                target=target or "?",
                attempts=_as_int(row.get("attempts")),
                error=error or "?",
            )
        )
    if collected.get("more"):
        lines.append(t("ui_dlq_more"))
    return "\n".join(lines)


async def count_dead_letters(
    *, queue: Any = None, owned_rule_ids: Sequence[str] = ()
) -> int:
    """How many entries the scope holds — the number the badge shows.

    Same scope as the list, from the same repository, so the badge and the list
    cannot disagree.
    """
    scope = [str(r) for r in owned_rule_ids]
    return _as_int(await _call(queue, "count_dead_letters", scope))


async def replay_dead_letter(
    *, queue: Any, entry_id: str, owned_rule_ids: Sequence[str] = ()
) -> str:
    """Re-queue one entry through the repository's scoped contract.

    Returns one of the repository's documented result codes so the caller can
    translate it (`_RESULT_CODE_KEYS`); unknown codes pass through as-is.
    """
    scope = [str(r) for r in owned_rule_ids]
    result = await _call(queue, "replay_dead_letter", str(entry_id), scope)
    if not isinstance(result, str) or not result:
        return "replay_failed"
    return result


async def purge_dead_letters(
    *, queue: Any, owned_rule_ids: Sequence[str] = ()
) -> int:
    """Remove every entry in the scope; the repository returns how many."""
    scope = [str(r) for r in owned_rule_ids]
    removed = await _call(queue, "purge_dead_letters", scope)
    return _as_int(removed)


def result_code_key(result: str) -> Optional[str]:
    """The i18n key for a repository result code, else None."""
    return _RESULT_CODE_KEYS.get(result)
