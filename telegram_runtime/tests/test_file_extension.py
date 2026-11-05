"""Contract tests for ``get_file_extension`` — the RPC the Go Worker's
re-upload staging depends on.

The staged scratch file must carry the source media's extension, because
Telegram's ``messages.SendMedia`` infers the MIME type from the staged
file's name: a bare ``job-<id>-media`` dies with PHOTO_EXT_INVALID before
the upload can ever succeed. The Go side normalizes the answer and falls
back to the ingest classification when this RPC cannot answer, but the
contract it trusts is defined HERE — these tests pin it:

* photos have no file_name in MTProto — the answer is always ``.jpg``;
* media with an original file_name answers with that name's raw suffix
  (normalization is the Go side's job, never a second opinion here);
* media without any inferable name (voice notes, video notes) answers
  ``""`` — the Go fallback owns the default;
* a failed source read is a typed error, never a raw exception.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_runtime.adapters.pyrogram.client import PyrogramAdapter  # noqa: E402
from telegram_runtime import errors  # noqa: E402


class _StubClient:
    """The smallest pyrogram-shaped client get_file_extension can probe."""

    def __init__(self, message: Any = None, exc: Exception | None = None) -> None:
        self._message = message
        self._exc = exc

    async def get_messages(self, chat_id: Any, message_ids: Any) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._message


def _ext(client: _StubClient, chat_id: Any = "c", message_id: int = 1) -> tuple[str, Any]:
    adapter = PyrogramAdapter(client)
    return asyncio.run(adapter.get_file_extension(chat_id, message_id))


def test_photo_answers_jpg() -> None:
    # A Telegram photo is always JPEG — it has no file_name to consult.
    msg = SimpleNamespace(photo=SimpleNamespace())
    ext, ferr = _ext(_StubClient(msg))
    assert ferr is None
    assert ext == ".jpg"


def test_document_answers_with_its_real_suffix() -> None:
    msg = SimpleNamespace(document=SimpleNamespace(file_name="report.pdf"))
    ext, ferr = _ext(_StubClient(msg))
    assert ferr is None
    assert ext == ".pdf"


def test_suffix_is_reported_raw() -> None:
    # Normalization is the Go side's job: an uppercase suffix rides as-is.
    msg = SimpleNamespace(video=SimpleNamespace(file_name="clip.MP4"))
    ext, ferr = _ext(_StubClient(msg))
    assert ferr is None
    assert ext == ".MP4"


def test_document_without_name_answers_empty() -> None:
    msg = SimpleNamespace(document=SimpleNamespace(file_name=None))
    ext, ferr = _ext(_StubClient(msg))
    assert ferr is None
    assert ext == ""


def test_voice_note_answers_empty() -> None:
    # Voice notes carry no file_name; the Go fallback supplies .ogg.
    msg = SimpleNamespace(voice=SimpleNamespace(file_name=None))
    ext, ferr = _ext(_StubClient(msg))
    assert ferr is None
    assert ext == ""


def test_bare_message_answers_empty() -> None:
    ext, ferr = _ext(_StubClient(SimpleNamespace()))
    assert ferr is None
    assert ext == ""


def test_failed_source_read_is_a_typed_error() -> None:
    ext, ferr = _ext(_StubClient(exc=RuntimeError("source chat gone")))
    assert ext == ""
    # Typed vocabulary, never a raw exception: an unrecognized failure maps
    # to the shared taxonomy (UNKNOWN for a plain Python error) so the Go
    # side can always react to a typed code instead of crashing.
    assert ferr is not None
    assert ferr.code == errors.UNKNOWN_DELIVERY_OUTCOME


def test_unreadable_media_never_raises() -> None:
    # getattr-based probing: any object shape must degrade to "" or a typed
    # error, never an AttributeError escaping the adapter.
    ext, ferr = _ext(_StubClient("not even a message"))
    assert ferr is None
    assert ext == ""
