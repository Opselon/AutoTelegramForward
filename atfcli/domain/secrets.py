"""Which names hold a secret — used to mask output and to skip echo checks.

Two callers, one list: the console/`admin settings` masking (a value whose *name*
says it is a token must never be printed) and the resource echo check (a field
the server masks on the way back must not be reported as "the server dropped
it").
"""

from __future__ import annotations

import re

SECRET_NAME_PATTERN = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API_HASH|API_ID|MASTER_KEY|PRIVATE|CVC|CARD|DSN|DATABASE_URL)",
    re.IGNORECASE,
)

#: The same idea for the API's camelCase field names (`apiKey`, `baseUrl` is not
#: one, `apiHash` is): used to decide what the echo check must not evaluate.
_SECRET_FIELD_PATTERN = re.compile(
    r"(token|secret|password|passwd|api_?key|api_?hash|api_?id|credential|private)",
    re.IGNORECASE,
)


def is_secret_name(name: str) -> bool:
    """True for env-style names that carry a credential."""
    return bool(SECRET_NAME_PATTERN.search(name or ""))


def is_secret_field(name: str) -> bool:
    """True for a JSON/API field name that carries a credential or is masked."""
    return bool(_SECRET_FIELD_PATTERN.search(name or ""))


def mask(label: str, value: object, *, marker: str = "[REDACTED]") -> object:
    return marker if is_secret_name(label) and value not in (None, "") else value
