"""What a use case hands back to the presentation layer.

Two things have to survive the trip out of the application layer: the document a
script parses (``--json``) and the sentence a human reads. Keeping both in one
object is deliberate — the bug this prevents is a command that prints a confident
sentence while ``--json`` carries a different story, or a refusal narrated with
``line`` that prints nothing at all in JSON mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Warning_:
    """A non-fatal observation: the call worked, but something needs saying."""

    message: str
    hint: str = ""

    def to_json(self) -> dict[str, str]:
        return {"message": self.message, "hint": self.hint} if self.hint else {"message": self.message}


@dataclass(frozen=True)
class Outcome:
    """The result of a mutating use case."""

    payload: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""
    warnings: Sequence[Warning_] = ()
    #: Some flows (task lifecycle, source edits) want to narrate the new state.
    follow_up: Mapping[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """A refusal the server expressed inside a 200 (``success: false``)."""
        return self.payload.get("success") is False

    @property
    def failure_reason(self) -> str:
        for key in ("message", "error", "detail"):
            value = self.payload.get(key)
            if value:
                return str(value)
        return "the server refused the change"

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = dict(self.payload)
        if self.warnings:
            document["warnings"] = [warning.to_json() for warning in self.warnings]
        return document
