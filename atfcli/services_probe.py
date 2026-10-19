"""Compatibility alias: the service probe now lives in
:mod:`atfcli.infrastructure.host.services`.
"""

from __future__ import annotations

from .infrastructure.host.services import *  # noqa: F401,F403
from .infrastructure.host import services as _services

__all__ = [name for name in dir(_services) if not name.startswith("_")]
