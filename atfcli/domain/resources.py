"""The resource catalogue: which CRUD surfaces exist and how their payloads lie.

AI configurations, prompt templates and message filters are the same shape of
problem — a per-owner collection behind ``/api/<name>`` with an ``id`` and a
``name`` — so they share one generic use case and one generic command. The facts
that genuinely differ (path, envelope key, which field is the label) are stated
once, here, instead of being re-typed in each command module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .models import items_of


@dataclass(frozen=True)
class ResourceSpec:
    """Everything the generic CRUD use cases need to know about a collection."""

    name: str  # singular, used in messages: "AI config"
    plural: str  # envelope key and human plural: "configs"
    path: str  # API collection path, e.g. "/api/ai-configs"
    id_keys: tuple[str, ...] = ("id",)
    label_keys: tuple[str, ...] = ("name", "title", "label")
    #: Fields the server requires on create. Refused locally with a clear hint
    #: rather than sent as an empty string the server stores.
    required: tuple[str, ...] = ()
    #: Fields a human can override with a single flag, in display order.
    editable: tuple[str, ...] = ()
    #: `PUT` replaces the record; `PATCH` merges. Both are one call, but the
    #: message and the dry-run preview should say which one happened.
    update_method: str = "PUT"
    #: Some endpoints wrap the record (prompts send ``{"template": {...}}`` and
    #: an optimistic-concurrency version). Empty means "send the object itself".
    wrapper: str = ""

    def id_of(self, payload: Mapping[str, Any]) -> str:
        for key in self.id_keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    def label_of(self, payload: Mapping[str, Any]) -> str:
        for key in self.label_keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    def item_path(self, resource_id: str) -> str:
        return f"{self.path}/{resource_id}"

    def flag_of(self, field: str) -> str:
        """Wire field name -> human flag: ``apiKey`` becomes ``--api-key``.

        The API speaks protojson camelCase; a command line should not. Keeping
        both names in one place is what stops a typo'd flag from silently
        becoming a dropped field on the wire.
        """
        out: list[str] = []
        for index, char in enumerate(field):
            if char.isupper() and index:
                out.append("-")
            out.append(char)
        return "".join(out).replace("_", "-").lower()

    def flag_map(self) -> dict[str, str]:
        """argparse dest (underscored) -> wire field name, for every editable field."""
        return {self.flag_of(field).replace("-", "_"): field for field in self.editable}

    def list_of(self, payload: Any) -> list[dict[str, Any]]:
        return items_of(payload, self.plural, "items", "data")

    def envelope(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return {"count": len(rows), self.plural: list(rows)}


AI_CONFIGS = ResourceSpec(
    name="AI config",
    plural="configs",
    path="/api/ai-configs",
    id_keys=("id", "config_id"),
    label_keys=("name", "model", "provider"),
    required=("name", "provider", "model"),
    # protojson field names, taken from proto/autoforward.proto (AIConfig). The
    # API discards unknown fields, so a wrongly-cased key is not an error there —
    # which is why `Resources.create/update` echoes the fields back and warns.
    editable=(
        "name",
        "provider",
        "model",
        "apiKey",
        "baseUrl",
        "systemPrompt",
        "userPromptTemplate",
        "temperature",
        "isEnabled",
        "targetLanguage",
    ),
)

PROMPTS = ResourceSpec(
    name="prompt template",
    # The API answers `{"templates": [...]}`; the plural is the envelope key, so
    # it has to match the server, not the CLI group name.
    plural="templates",
    path="/api/prompts",
    id_keys=("id", "template_id"),
    label_keys=("name", "title"),
    required=("name",),
    editable=("name", "description", "system_prompt", "user_prompt_template", "target_language"),
    wrapper="template",
)

FILTERS = ResourceSpec(
    name="filter",
    plural="filters",
    path="/api/filters",
    id_keys=("id", "filter_id"),
    label_keys=("name", "pattern"),
    required=("name",),
    editable=(
        "name",
        "whitelistKeywords",
        "blacklistKeywords",
        "regexPatterns",
        "allowedMediaTypes",
        "blockedMediaTypes",
        "dropServiceMessages",
        "minMessageLength",
        "maxMessageLength",
    ),
)

#: CLI group name -> spec. The command registry is derived from this, so a new
#: resource is one line here plus one line in `COMMAND_MODULES`.
CATALOGUE: dict[str, ResourceSpec] = {
    "ai": AI_CONFIGS,
    "prompts": PROMPTS,
    "filters": FILTERS,
}


def spec_for(resource: str) -> ResourceSpec:
    try:
        return CATALOGUE[resource]
    except KeyError as exc:
        raise KeyError(
            f"unknown resource {resource!r}; catalogued: {', '.join(sorted(CATALOGUE))}"
        ) from exc


@dataclass(frozen=True)
class Resource:
    """One row of a CRUD collection, with the raw payload kept for `--json`."""

    id: str
    label: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], spec: ResourceSpec) -> "Resource":
        return cls(id=spec.id_of(payload), label=spec.label_of(payload), raw=dict(payload))

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)
