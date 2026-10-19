"""Compatibility shim: host detection lives in :mod:`atfcli.infrastructure.host.platforms`.

Platform probing shells out (`uname`, `docker info`) and reads `/proc`, so it is
infrastructure, not domain. The historical import path is kept so command modules
written before the move keep working.
"""

from __future__ import annotations

from .infrastructure.host import platforms as _platforms
from .infrastructure.host.platforms import *  # noqa: F401,F403

__all__ = [name for name in dir(_platforms) if not name.startswith("_")]
