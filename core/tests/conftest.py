"""LOCAL-ONLY shim — NOT committed.

Python 3.14 removed the implicit event loop that pyrogram 2.0.106's sync.py
needs at import time. CI runs 3.11 where this is unnecessary. This conftest
creates a loop before pyrogram is imported so the local test run works.
"""
import asyncio
import sys

if sys.version_info >= (3, 12):
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
