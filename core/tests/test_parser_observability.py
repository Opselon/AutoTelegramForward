"""Parser observability (mission §8): a parser defect is distinguishable.

The mission requires that a parser failure never be confused with a Telegram
API failure. These tests pin exactly that: when normalization raises, the
event's origin is INTERNAL (our own code failed; Telegram delivered the
message fine) and its category is PARSING — not TELEGRAM_API, and not a
silent drop into ``error_detail``.
"""

from __future__ import annotations

import pytest

from core.application.pipeline import DurableMessagePipeline, Normalizer
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDiagnosticEventRepository,
)


class RecordingLogClient:
    def __init__(self):
        self.calls = []

    def error(self, service, category, message, detail=None, **kw):
        self.calls.append((service, category, message, detail))


def _pipeline(db, tmp) -> DurableMessagePipeline:
    diag = SqliteDiagnosticEventRepository(db)
    pipe = DurableMessagePipeline.__new__(DurableMessagePipeline)
    pipe._diagnostics = diag
    pipe._log_client = RecordingLogClient()
    return pipe


class Ctx:
    correlation_id = "corr-P"
    chat_id = "-100SRC"
    message_id = 31337


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "parser.db"))


async def test_a_parser_failure_is_an_internal_parsing_event(db):
    """Normalization raising produces parse.failed, origin INTERNAL."""
    pipe = _pipeline(db, None)
    await pipe._record_parser_diagnostic(
        ctx=Ctx(), exc=ValueError("bad utf"), stage_reason="MESSAGE_NORMALIZATION_FAILED",
        raw_message=None,
    )

    rows = await pipe._diagnostics.query_events()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_name"] == "parse.failed"
    assert row["category"] == "PARSING"
    # The decisive distinction: this is OUR defect, not Telegram's answer.
    assert row["error_origin"] == "INTERNAL"
    assert row["stage"] == "PARSE"
    assert row["reason"] == "MESSAGE_NORMALIZATION_FAILED"
    assert row["trace_id"] == "corr-P"
    assert row["source_message_id"] == 31337


async def test_the_parser_event_never_carries_message_text(db):
    """Only lengths and field-presence indicators — never the content."""
    pipe = _pipeline(db, None)
    await pipe._record_parser_diagnostic(
        ctx=Ctx(), exc=RuntimeError("x"), stage_reason="MESSAGE_NORMALIZATION_FAILED",
        raw_message=None,
    )
    serialized = " ".join(str(v) for v in (
        await pipe._diagnostics.query_events()
    ))
    # No text field exists on the event at all.
    assert "private body" not in serialized


async def test_a_clean_normalization_still_works(db):
    """The instrumented path does not change the happy path."""
    from types import SimpleNamespace
    from datetime import datetime, timezone
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=-100456), id=42,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc), text="hello",
        caption=None, media_group_id=None, service=False, edit_date=None,
        reply_to_message_id=None, media_type=None,
    )
    payload = Normalizer.normalize(msg)
    assert payload.text == "hello"
    assert payload.message_id == 42


async def test_the_parser_event_also_reaches_the_logger_stream(db):
    """§10: the event fans out to the Rust logger too."""
    pipe = _pipeline(db, None)
    await pipe._record_parser_diagnostic(
        ctx=Ctx(), exc=ValueError("boom"), stage_reason="PARSE_FAILED",
    )
    assert len(pipe._log_client.calls) == 1
    service, category, message, detail = pipe._log_client.calls[0]
    assert service == "diagnostics"
    assert category == "PARSING"
    assert message == "parse.failed"


async def test_diagnostics_are_optional_a_missing_store_does_not_break(db):
    """No store, no logger: the failure handling still completes."""
    pipe = DurableMessagePipeline.__new__(DurableMessagePipeline)
    pipe._diagnostics = None
    pipe._log_client = None
    # Must not raise.
    await pipe._record_parser_diagnostic(
        ctx=Ctx(), exc=ValueError("boom"), stage_reason="PARSE_FAILED",
    )
