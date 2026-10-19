"""Adapters: the outside world behind the ports the application layer declares.

Two sub-packages, because the CLI has exactly two kinds of outside:

``http``  the ATF API (tasks, logs, sessions, admin, ...) — one adapter per port
``host``  the machine the stack runs on: files, env, docker, subprocesses

Nothing in ``domain`` or ``application`` may import from here; the direction is
infrastructure -> application -> domain, never back.
"""

from __future__ import annotations

__all__: list[str] = []
