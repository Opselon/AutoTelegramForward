"""CRUD over the per-owner collections: AI configs, prompt templates, filters.

One use case set covers three endpoints because they are the same shape. What
this module adds over "call the endpoint" is the part an operator actually needs:

* **Local refusals for values that cannot work** — a 400 from the API for an
  empty name is a round trip spent teaching nothing.
* **An echo check.** The Go handlers bind these bodies with
  ``protojson.UnmarshalOptions{DiscardUnknown: true}``, so a mistyped field name
  is *silently dropped* and the request still succeeds. After a write, the fields
  that were sent are compared with the record that came back; anything missing is
  reported as a warning that names the likely cause. Without it, `--set modelName=…`
  looks like it worked and changes nothing.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ...domain.errors import AtfError, NotFoundError, NotSupportedError, UsageError
from ...domain.resources import Resource
from ...domain.secrets import is_secret_field
from ..gateways import ResourceGateway
from ..results import Outcome, Warning_


class Resources:
    """Use cases over one CRUD collection, driven by its :class:`ResourceSpec`."""

    def __init__(self, gateway: ResourceGateway) -> None:
        self.gateway = gateway
        self.spec = gateway.spec

    # ------------------------------------------------------------------ queries
    def list(self) -> list[Resource]:
        return self.gateway.list_resources()

    def find(self, ref: str) -> Resource:
        if not ref:
            raise UsageError(f"a {self.spec.name} id, id prefix or name is required")
        rows = self.gateway.list_resources()
        exact = [row for row in rows if row.id == ref]
        if exact:
            return exact[0]
        prefix = [row for row in rows if row.id.startswith(ref) and len(ref) >= 4]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise UsageError(
                f"{ref!r} matches {len(prefix)} {self.spec.plural}; use a longer prefix",
                hint=", ".join(row.id[:12] for row in prefix[:5]),
            )
        by_name = [row for row in rows if row.label.strip().lower() == ref.strip().lower()]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise UsageError(f"{ref!r} names {len(by_name)} {self.spec.plural}; use the id instead")
        raise NotFoundError(
            f"no {self.spec.name} matches {ref!r}",
            hint=f"`atf {self.spec.path.rsplit('/', 1)[-1]} list` shows the ids and names",
        )

    # ---------------------------------------------------------------- mutations
    def create(self, values: Mapping[str, Any], *, expected_version: int = 0) -> Outcome:
        payload = self._with_version(dict(values), expected_version, creating=True)
        document = _mapping(self.gateway.create_resource(payload))
        created_id = self.spec.id_of(document) or self.spec.id_of(_inner(document, self.spec))
        detail = f"created {self.spec.name}"
        if created_id:
            detail += f" {created_id[:8]}"
        return Outcome(
            payload=document,
            detail=detail,
            warnings=self._echo_warnings(payload, document),
        )

    def update(
        self, ref: str, values: Mapping[str, Any], *, expected_version: int = 0
    ) -> Outcome:
        if not values:
            raise UsageError(
                "nothing to update",
                hint="pass --set key=value or one of the documented flags",
            )
        row = self.find(ref)
        payload = self._with_version(dict(values), expected_version, creating=False, resource_id=row.id)
        document = _mapping(self.gateway.update_resource(row.id, payload))
        return Outcome(
            payload=document,
            detail=f"updated {self.spec.name} {row.id[:8]} ({', '.join(sorted(values))})",
            warnings=self._echo_warnings(payload, document),
        )

    def delete(self, ref: str) -> Outcome:
        row = self.find(ref)
        document = _mapping(self.gateway.delete_resource(row.id))
        return Outcome(
            payload={"ok": True, "deleted": row.id, **document},
            detail=f"deleted {self.spec.name} {row.id[:8]}",
        )

    def test_prompt(
        self, ref: str, ai_config_id: str, sample_text: str, *, version: int = 0
    ) -> Outcome:
        """Run a prompt template against a real AI config, without forwarding.

        The AI config is passed as an id: resolving it is the caller's job, which
        keeps this use case free of a second collection's lookup rules.
        """
        if not hasattr(self.gateway, "test_prompt"):
            raise NotSupportedError(f"{self.spec.plural} cannot be tested")
        template = self.find(ref)
        if not str(sample_text or "").strip():
            raise UsageError("nothing to test", hint="pass --text 'a sample message'")
        payload = _mapping(
            self.gateway.test_prompt(
                prompt_id=template.id,
                ai_config_id=ai_config_id,
                sample_text=sample_text,
                version=version,
            )
        )
        rendered = str(payload.get("output") or payload.get("rendered_text") or payload.get("text") or "")
        outcome = Outcome(
            payload=payload,
            detail=f"rendered {template.label or template.id[:8]}",
            follow_up={"output": rendered} if rendered else {},
        )
        if payload.get("error"):
            raise AtfError(str(payload["error"]))
        return outcome

    # ------------------------------------------------------------------ helpers
    def require_fields(self, values: Mapping[str, Any]) -> None:
        missing = [field for field in self.spec.required if not str(values.get(field, "")).strip()]
        if missing:
            flags = ", ".join(f"--{self.spec.flag_of(field)}" for field in missing)
            raise UsageError(
                f"{self.spec.name} needs {', '.join(missing)}",
                hint=f"pass {flags}",
            )

    def _with_version(
        self,
        values: dict[str, Any],
        expected_version: int,
        *,
        creating: bool,
        resource_id: str = "",
    ) -> dict[str, Any]:
        if not self.spec.wrapper:
            return values
        # Wrapped endpoints (prompt templates) send `{"template": {...}}` plus the
        # optimistic-concurrency version the server compares before writing.
        if resource_id:
            values.setdefault("id", resource_id)
        payload: dict[str, Any] = {self.spec.wrapper: values}
        if expected_version:
            payload["expected_version"] = expected_version
        if creating:
            values.pop("id", None)
        return payload

    def _echo_warnings(self, sent: Mapping[str, Any], document: Mapping[str, Any]) -> list[Warning_]:
        """Report fields the server silently discarded (`DiscardUnknown`)."""
        inner = _inner(document, self.spec)
        target = inner or document
        if not target:
            return []
        missing: list[str] = []
        for field in sent:
            if field in ("expected_version",) or is_secret_field(field):
                continue
            if field in target:
                continue
            missing.append(field)
        if not missing:
            return []
        return [
            Warning_(
                f"the server did not echo: {', '.join(sorted(missing))}",
                hint=(
                    "unknown fields are discarded silently — check the spelling "
                    "(the API uses the proto field names, e.g. apiKey, baseUrl, isEnabled)"
                ),
            )
        ]


def _inner(document: Mapping[str, Any], spec: Any) -> Mapping[str, Any]:
    """The record inside an envelope like `{"template": {...}}`."""
    wrapper = getattr(spec, "wrapper", "")
    if wrapper:
        inner = document.get(wrapper)
        if isinstance(inner, Mapping):
            return inner
    for key in ("config", "filter", "resource"):
        inner = document.get(key)
        if isinstance(inner, Mapping):
            return inner
    return {}


def _mapping(payload: Any) -> dict[str, Any]:
    return dict(payload) if isinstance(payload, Mapping) else {"value": payload}


def summarise(resources: Sequence[Resource], *, limit: int = 0) -> Iterable[Resource]:
    if limit > 0:
        return list(resources)[:limit]
    return resources
