"""Compatibility shim: the rules live in :mod:`atfcli.domain.validators`.

The validators are pure functions over strings — no I/O, no knowledge of HTTP —
so they belong in the domain layer. This module keeps the historical import path
working for command modules written before the move.
"""

from __future__ import annotations

from .domain.validators import *  # noqa: F401,F403
from .domain.validators import (  # noqa: F401  (names the wildcard skips)
    LEVELS,
    OUTCOMES,
    ROUTE_PATHS,
    SEVERITIES,
    VALIDATORS,
    argparse_type,
    check,
    coerce,
    describe_choices,
    suggest,
    validate,
)
