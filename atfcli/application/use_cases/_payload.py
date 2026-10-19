"""Small shared helpers for the use cases.

Kept here rather than duplicated in nine modules: a mapping coercion that never
invents a shape, and the re-exports the use cases import from one place.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..results import Outcome, Warning_


def mapping(payload: Any) -> dict[str, Any]:
    """Coerce a response into a dict without inventing fields."""
    return dict(payload) if isinstance(payload, Mapping) else {"value": payload}


__all__ = ["Outcome", "Warning_", "mapping"]
