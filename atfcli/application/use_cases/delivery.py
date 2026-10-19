"""Delivery: the retry queue and its dead-letter pile.

The queue is the one place where "it looked fine" and "it was delivered" differ,
so this module keeps both numbers (queued *and* delivered) and never reports a
depth without saying when it was measured. Dead-letter retries are addressed by
message id with the same prefix matching as tasks, because the ids in
`atf delivery queue` are long and the operator is copying them by eye.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...domain.errors import NotFoundError, UsageError
from ...domain.models import DeadLetterItem, QueueSnapshot
from . import _payload


class Delivery:
    """Use cases over the forwarding queue."""

    def __init__(self, gateway) -> None:
        self.gateway = gateway

    def queue(self) -> QueueSnapshot:
        return self.gateway.queue()

    def dead_letters(self) -> list[DeadLetterItem]:
        return self.gateway.dead_letters()

    def find_dead_letter(self, ref: str) -> DeadLetterItem:
        if not ref:
            raise UsageError("a dead-letter message id or prefix is required")
        items = self.gateway.dead_letters()
        exact = [item for item in items if item.id == ref]
        if exact:
            return exact[0]
        prefix = [item for item in items if item.id.startswith(ref) and len(ref) >= 4]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise UsageError(
                f"{ref!r} matches {len(prefix)} dead letters; use a longer prefix",
                hint=", ".join(item.id[:12] for item in prefix[:5]),
            )
        raise NotFoundError(
            f"no dead letter matches {ref!r}",
            hint="`atf delivery dead-letters` lists them",
        )

    def retry(self, ref: str) -> _payload.Outcome:
        item = self.find_dead_letter(ref)
        document = _payload.mapping(self.gateway.retry_dead_letter(item.id))
        detail = f"requeued {item.id[:8]}"
        attempts = document.get("attempts") or document.get("attempt_count")
        if attempts:
            detail += f" (attempt {attempts})"
        return _payload.Outcome(document, detail)

    def retry_all(self, *, confirm: bool) -> _payload.Outcome:
        items = self.gateway.dead_letters()
        if not items:
            return _payload.Outcome({"ok": True, "retried": 0}, "nothing to retry — the pile is empty")
        if not confirm:
            raise UsageError(
                f"this requeues {len(items)} message(s)",
                hint="pass --yes to confirm; requeued messages are re-sent to their targets",
            )
        results: list[Mapping[str, Any]] = []
        failures = 0
        for item in items:
            try:
                results.append(_payload.mapping(self.gateway.retry_dead_letter(item.id)))
            except Exception as exc:  # one bad message must not abandon the rest
                failures += 1
                results.append({"message_id": item.id, "error": str(exc)})
        detail = f"requeued {len(results) - failures} of {len(items)} dead letter(s)"
        if failures:
            detail += f"; {failures} refused"
        return _payload.Outcome({"ok": not failures, "results": results}, detail)

    def purge(self, *, confirm: bool) -> _payload.Outcome:
        items = self.gateway.dead_letters()
        if not confirm:
            raise UsageError(
                f"this discards {len(items)} message(s) permanently",
                hint="pass --yes to confirm",
            )
        document = _payload.mapping(self.gateway.purge_dead_letters())
        removed = document.get("purged", document.get("removed", len(items)))
        return _payload.Outcome(document, f"discarded {removed} dead letter(s)")
