"""Pytest configuration for the logger tests.

pyrogram grabs the thread's event loop while being imported, and under Python
3.12+ `asyncio.get_event_loop()` raises when no loop is current — so importing
the bot UI inside a test module aborted collection of this whole directory
(`ERROR collecting logger/tests/test_pro_ui.py`). Install a current loop before
any test module in this directory is imported.
"""

from __future__ import annotations

import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:  # pragma: no cover - depends on interpreter/pytest version
    asyncio.set_event_loop(asyncio.new_event_loop())
