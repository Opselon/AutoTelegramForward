"""The admin screens must not assert a healthy stack they never probed.

Both admin bodies used to print a literal green dot in front of every service
and pass one shared ``{service_status}`` for all three, so a stopped logger or
an unreachable web gateway rendered exactly like a healthy one — the single
question an operator opens the diagnostics screen to answer.

These tests pin the contract that replaced it:

* the body carries one placeholder per service;
* each placeholder is filled from something this process can observe;
* the web gateway is reported "off" unless its health URL actually answers;
* the dead-letter count comes from the delivery queue, not from the error log.

The catalog half (every language carrying the same three placeholders) is
asserted in ``test_admin_panel_service_placeholders_are_translated``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.pro_bot_ui import ProBotUI

UID = 5094837833


def _catalog() -> Dict[str, Dict[str, str]]:
    here = Path(__file__).resolve()
    p = here.parents[2] / "core" / "infrastructure" / "telegram" / "locales" / "translations.json"
    if not p.exists():  # pragma: no cover - alternate cwd
        p = Path("core/infrastructure/telegram/locales/translations.json")
    return json.loads(p.read_text(encoding="utf-8"))


class _Rules:
    async def list_all(self):
        return []

    async def list_by_owner(self, uid):
        return []


class _Users:
    async def count_all(self):
        return 3


class _Queue:
    """The delivery queue's owner-scoped dead-letter contract."""

    def __init__(self, entries: int = 0) -> None:
        self.entries = entries

    async def count_dead_letters(self, scope: Optional[List[str]] = None) -> int:
        return self.entries

    async def list_dead_letters(self, scope=None, limit: int = 50):
        return []


class _ErrorLog:
    """Every read raises: the DLQ number must never come from here."""

    async def counts_by_severity(self, since_ts: int = 0):
        raise AssertionError("the dead-letter count must come from the delivery queue")


def _ui(web_url: str = "", queue: Optional[_Queue] = None, log: Any = object()) -> ProBotUI:
    return ProBotUI(
        bot=MagicMock(),
        i18n=I18n(),
        sessions=MagicMock(),
        rules=_Rules(),
        filter_rules=MagicMock(),
        ai_configs=MagicMock(),
        pipeline=MagicMock(),
        pool=MagicMock(),
        login=MagicMock(),
        log_client=log,
        admin_ids=[UID],
        error_log=_ErrorLog(),
        delivery_queue=queue,
        users=_Users(),
        web_url=web_url,
    )


@pytest.mark.asyncio
async def test_web_gateway_is_off_when_nothing_is_configured():
    ui = _ui(web_url="")
    state = await ui._probe_web_gateway_state("en")
    assert "OFF" in state


@pytest.mark.asyncio
async def test_web_gateway_is_off_when_the_probe_cannot_connect(monkeypatch):
    ui = _ui(web_url="http://127.0.0.1:9")  # discard port: nothing listens
    monkeypatch.setenv("ATF_WEB_PROBE_TIMEOUT", "1")
    state = await ui._probe_web_gateway_state("en")
    assert "OFF" in state, "an unreachable gateway must not render as healthy"


@pytest.mark.asyncio
async def test_web_gateway_is_on_when_healthz_answers(monkeypatch):
    """A real HTTP server on a real socket: no mocked urlopen."""

    def _ok_handler(reader, writer) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        writer.close()

    async def _serve():
        server = await asyncio.start_server(
            _ok_handler,
            "127.0.0.1",
            0,
        )
        return server

    server = await _serve()
    port = server.sockets[0].getsockname()[1]
    ui = _ui(web_url=f"http://127.0.0.1:{port}")
    try:
        state = await ui._probe_web_gateway_state("en")
    finally:
        server.close()
        await server.wait_closed()
    assert "ON" in state


@pytest.mark.asyncio
async def test_dead_letter_count_comes_from_the_queue_not_the_error_log():
    ui = _ui(queue=_Queue(entries=4))
    assert await ui._dlq_count() == 4


@pytest.mark.asyncio
async def test_admin_panel_body_fills_each_service_separately():
    ui = _ui(queue=_Queue(entries=2), log=None)  # log=None: the logger is not wired
    text, _kbd = await ui._render_admin_panel(UID)
    # core is running (this call is served by it), the logger is not wired, and
    # no web URL is configured — three different verdicts, never one shared one.
    assert text.count("OFF") >= 2, text
    assert "ON" in text, text


def test_admin_panel_service_placeholders_are_translated():
    """Every language fills the three per-service placeholders."""
    catalog = _catalog()
    for lang, values in catalog.items():
        body = values["ui_admin_panel_body"]
        for name in ("{core_status}", "{logger_status}", "{web_status}"):
            assert name in body, f"{lang} lost {name}"
        assert "{service_status}" not in body, f"{lang} still uses one shared status"
        stats = values["ui_admin_stats_body"]
        for name in ("{core_status}", "{logger_status}", "{web_status}"):
            assert name in stats, f"{lang} stats lost {name}"
        assert "{service_status}" not in stats, f"{lang} stats still uses one shared status"
