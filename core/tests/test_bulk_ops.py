"""Bulk wiring: selection state and per-item execution, independent of surfaces.

The selection must be safe against everything Telegram can send and everything
a shared server can do between presses: garbage in the persisted buffer, ids of
rules deleted elsewhere, offline clocks, a batch retried after a timeout. The
execution must report every rule separately — one failure never hides the rest.
"""

from __future__ import annotations

import pytest

from core.application import bulk_ops


@pytest.fixture
def now() -> float:
    return 1_000_000.0


def test_toggle_adds_deduplicates_and_removes(now):
    sel = bulk_ops.Selection()
    sel = sel.toggle("a", now)
    sel = sel.toggle("a", now + 1)          # a second tap unselects
    assert not sel
    sel = sel.toggle("b", now).toggle("b", now).toggle("b", now)
    assert tuple(sel.ids) == ("b",)


def test_toggle_stamps_the_change_time_for_expiry(now):
    sel = bulk_ops.Selection(("a",), now)
    assert not sel.expired(now, ttl=1800)
    assert sel.expired(now + 1900)
    assert not bulk_ops.Selection().expired(now + 10_000)


def test_toggle_refuses_the_cap_and_select_all_fits_instead(now):
    sel = bulk_ops.Selection(tuple(f"r{i:02d}" for i in range(bulk_ops.MAX_SELECTION)), now)
    with pytest.raises(ValueError):
        sel.toggle("overflow", now)
    grown = sel.select_all(("extra-a", "extra-b"), now)   # no raise, keeps the cap
    assert len(grown) == bulk_ops.MAX_SELECTION
    assert "extra-a" not in grown


def test_select_all_dedupes_and_keeps_listing_order(now):
    sel = bulk_ops.Selection((), now).select_all(["b", "a", "b", "c"], now)
    assert tuple(sel.ids) == ("b", "a", "c")


def test_prune_drops_ids_that_left_account_and_keeps_untouched_selection(now):
    sel = bulk_ops.Selection(("live", "deleted", "live"), now)
    kept = sel.prune(["live"])
    assert tuple(kept.ids) == ("live", "live")
    assert sel.prune(["live", "deleted"]) is sel   # no change → same object


def test_buffer_roundtrip_survives_garbage(now):
    stored = bulk_ops.Selection(("a", "b"), now).to_buffer()
    restored = bulk_ops.Selection.from_buffer(stored)
    assert tuple(restored.ids) == ("a", "b")
    assert restored.ts == now

    for garbage in (None, {}, [], "ids", 4, {"ids": "ab", "ts": 1},
                    {"ids": [1, None, "ok", "  x "], "ts": "no"}):
        sel = bulk_ops.Selection.from_buffer(garbage)
        assert all(isinstance(r, str) and r for r in sel.ids)


def test_buffer_bounds_untrusted_input(now):
    overflow = [f"r{i:02d}" + "x" * 70 for i in range(300)]
    sel = bulk_ops.Selection.from_buffer({"ids": overflow, "ts": now})
    assert len(sel) == bulk_ops.MAX_SELECTION
    assert all(len(r) <= 64 for r in sel.ids)


async def test_apply_each_preserves_order_and_reports_per_item():
    async def act(rid: str) -> str:
        if rid == "req":
            raise RuntimeError("boom")
        if rid == "gone":
            return bulk_ops.MISSING
        return bulk_ops.OK

    outcomes = await bulk_ops.apply_each(["gone", "ok-a", "req", "ok-b", ""], act)
    assert [o.rule_id for o in outcomes] == ["gone", "ok-a", "req", "ok-b"]


async def test_apply_each_isolates_failures_and_skips_blank_ids():
    calls: list = []

    async def act(rid: str) -> str:
        calls.append(rid)
        if rid == "bad":
            raise ValueError("database down")
        return bulk_ops.OK

    outcomes = await bulk_ops.apply_each(["a", "bad", "c"], act)
    assert calls == ["a", "bad", "c"]
    failure = outcomes[1]
    assert not failure.ok
    assert failure.code == bulk_ops.FAILED
    assert failure.detail == "ValueError"       # code, never a raw message
    assert [o.code for o in (outcomes[0], outcomes[2])] == [bulk_ops.OK, bulk_ops.OK]


def test_summary_counts_codes_without_losing_rows():
    outcomes = [
        bulk_ops.Outcome("a", bulk_ops.OK),
        bulk_ops.Outcome("b", bulk_ops.UNCHANGED),
        bulk_ops.Outcome("c", bulk_ops.MISSING),
        bulk_ops.Outcome("d", bulk_ops.FAILED),
        bulk_ops.Outcome("e", "weird"),
    ]
    summary = bulk_ops.summarize(outcomes)
    assert (summary.total, summary.ok, summary.unchanged, summary.missing, summary.failed) == \
        (5, 1, 1, 1, 2)
    assert not summary.all_ok


async def test_a_batch_is_safe_to_retry_now_that_codes_are_stable():
    """Idempotency contract: enable/enable is ok/unchanged, delete/delete is ok/missing."""
    stored = {"a": False, "b": True}

    async def enable(rid: str) -> str:
        if rid not in stored:
            return bulk_ops.MISSING
        if stored[rid]:
            return bulk_ops.UNCHANGED
        stored[rid] = True
        return bulk_ops.OK

    first = await bulk_ops.apply_each(["a", "gone"], enable)
    second = await bulk_ops.apply_each(["a", "gone"], enable)
    assert [o.code for o in first] == [bulk_ops.OK, bulk_ops.MISSING]
    assert [o.code for o in second] == [bulk_ops.UNCHANGED, bulk_ops.MISSING]
    assert stored == {"a": True, "b": True}
