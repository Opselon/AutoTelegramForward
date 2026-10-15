"""Small shared coercions for the persistence layer.

Kept out of the repository modules so SQLite and PostgreSQL backends agree on
how domain values are turned into column values.
"""

from typing import Any


def enum_str(value: Any, default: str = "") -> str:
    """Coerce an enum member — or its raw string value — to the string to persist.

    ``getattr(getattr(obj, "field", X), "value", Y)`` is a trap: a plain string
    has no ``.value``, so the hardcoded ``Y`` silently replaces the caller's
    value. Assigning the string ``"SEND_ORIGINAL"`` to ``ai_fallback_policy``
    therefore persisted ``"DROP"`` — a wrong value written without any error,
    which then drives real behaviour (dropping messages during an AI outage).
    Always fall back to the value itself, never to a hardcoded default.
    """
    if value is None:
        return default
    inner = getattr(value, "value", None)
    if inner is not None:
        return str(inner)
    text = str(value).strip()
    return text or default
