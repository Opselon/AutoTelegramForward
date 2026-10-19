"""Compatibility alias: the self-update engine now lives in
:mod:`atfcli.infrastructure.host.upgrade`.

The module object is aliased rather than re-exported, so a caller that reaches for
a private helper (or patches one, as the upgrade tests do) still sees the same
module the command modules use. The self-update engine is host work — git, `/proc`, sockets — so it belongs in the
infrastructure ring, not at the package root.
"""

from __future__ import annotations

import sys

from .infrastructure.host import upgrade as _impl

sys.modules[__name__] = _impl
