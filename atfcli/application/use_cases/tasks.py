"""Forwarding tasks: find one, change it, and say what changed.

The rules encoded here are the ones an operator hits in practice:

* a task can be named by a full id, a unique id prefix, or its name — operators
  copy ids out of logs, and requiring the exact 36-character uuid makes every
  command harder than the task it performs;
* "enable a task that is already enabled" must not silently *disable* it (the API
  only offers `toggle`), so the desired state is compared with the state the
  server reports, and the flip is corrected when they disagree;
* a task must keep at least one source — removing the last one is refused with a
  way forward instead of producing a task that can never fire.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ...domain import validators
from ...domain.errors import AtfError, NotFoundError, UsageError
from ...domain.logs import LogPage, LogQuery
from ...domain.models import (
    RoutePath,
    SimulationResult,
    Task,
    TaskDraft,
    TaskState,
    items_of,
)
from ..gateways import TaskGateway
from ..results import Outcome, Warning_

#: Verb -> the API endpoint that performs it. Derived from one tuple so the help
#: text, the validator and the dispatch cannot disagree.
LIFECYCLE: dict[str, str] = {
    "enable": "toggle",
    "disable": "toggle",
    "toggle": "toggle",
    "pause": "pause",
    "resume": "resume",
    "stop": "stop",
    "restart": "restart",
}
#: The verbs that promise a state rather than a flip.
_DESIRED_STATE: dict[str, bool] = {"enable": True, "disable": False}
#: Endpoints that answer with the changed rule; the others answer `{success}`.
_RETURNS_RULE: frozenset[str] = frozenset({"toggle", "pause", "resume", "stop", "restart"})


def matches_id(candidate: str, wanted: str, *, min_prefix: int = 4) -> bool:
    if not candidate or not wanted:
        return False
    if candidate == wanted:
        return True
    return len(wanted) >= min_prefix and candidate.startswith(wanted)


class Tasks:
    """Use cases over the task aggregate."""

    def __init__(self, gateway: TaskGateway) -> None:
        self.gateway = gateway

    # ------------------------------------------------------------------ queries
    def list(self, *, state: str = "", source: str = "", enabled_only: bool = False) -> list[Task]:
        """Every task, optionally narrowed. Filtering never hits the network twice."""
        tasks = self.gateway.list_tasks()
        if enabled_only:
            tasks = [task for task in tasks if task.enabled]
        if state:
            wanted = state.strip().lower()
            problems = {"on": TaskState.ENABLED, "off": TaskState.DISABLED, "paused": TaskState.PAUSED}
            if wanted not in problems:
                raise UsageError(
                    f"unknown state {state!r}{validators.suggest(wanted, problems)}",
                    hint="states: on, off, paused",
                )
            tasks = [task for task in tasks if task.state is problems[wanted]]
        if source:
            needle = source.strip().lower()
            tasks = [task for task in tasks if _matches_source(task, needle)]
        return tasks

    def find(self, ref: str) -> Task:
        """Resolve a task by id, unique id prefix, or name."""
        if not ref:
            raise UsageError("a task id, id prefix or name is required")
        tasks = self.gateway.list_tasks()
        exact = [task for task in tasks if task.id == ref]
        if exact:
            return exact[0]
        prefix = [task for task in tasks if matches_id(task.id, ref)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise UsageError(
                f"{ref!r} matches {len(prefix)} tasks; use a longer prefix",
                hint=", ".join(task.id[:12] for task in prefix[:5]),
            )
        by_name = [task for task in tasks if task.name.strip().lower() == ref.strip().lower()]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise UsageError(f"{ref!r} names {len(by_name)} tasks; use the id instead")
        raise NotFoundError(
            f"no task matches {ref!r}",
            hint="`atf rules list` shows the ids and names",
        )

    # ---------------------------------------------------------------- mutations
    def create(self, draft: TaskDraft) -> Outcome:
        try:
            document = _mapping(self.gateway.create_task(draft.to_payload()))
        except AtfError as exc:
            raise _explain_create_refusal(exc, draft) from None
        task = _rule_of(document)
        detail = f"created task {task.id[:8]}" if task.id else "created task (id in response)"
        return Outcome(
            payload=document,
            detail=detail,
            follow_up={"source": task.source_chat_id, "target": task.target_chat_id},
        )

    def update(self, ref: str, changes: Mapping[str, Any]) -> Outcome:
        if not changes:
            raise UsageError(
                "nothing to update",
                hint="pass --set key=value, --name, --source or --target",
            )
        task = self.find(ref)
        payload = self.gateway.update_task(task.id, changes)
        return Outcome(
            payload=dict(payload) if isinstance(payload, Mapping) else {"value": payload},
            detail=f"updated task {task.id[:8]} ({', '.join(sorted(changes))})",
            follow_up={"changed": sorted(changes)},
        )

    def delete(self, ref: str) -> Outcome:
        task = self.find(ref)
        payload = self.gateway.delete_task(task.id)
        return Outcome(
            payload={"ok": True, "deleted": task.id, **_mapping(payload)},
            detail=f"deleted task {task.id[:8]}",
        )

    def set_state(self, ref: str, verb: str) -> Outcome:
        """`enable`/`disable` promise a state; `pause`/`resume`/`stop`/`restart` act."""
        if verb not in LIFECYCLE:
            raise UsageError(
                f"unknown lifecycle verb {verb!r}",
                hint=f"verbs: {', '.join(sorted(LIFECYCLE))}",
            )
        task = self.find(ref)
        wanted = _DESIRED_STATE.get(verb)
        if wanted is not None and task.enabled == wanted:
            # Already in the promised state. `toggle` is the only mutation the API
            # has, so asking would flip it the wrong way and then need flipping back.
            return Outcome(
                payload=dict(task.raw),
                detail=f"task {task.id[:8]} is already {'enabled' if wanted else 'disabled'}",
                follow_up={"enabled": wanted},
            )
        payload = self.gateway.task_action(task.id, LIFECYCLE[verb])
        state = _enabled_from(payload)
        if wanted is not None and state is not None and state != wanted:
            # `toggle` flipped the wrong way: ask once more so the promise holds.
            payload = self.gateway.task_action(task.id, "toggle")
            state = _enabled_from(payload)
        detail = f"{verb} task {task.id[:8]}"
        if state is not None:
            detail += f" → {'enabled' if state else 'disabled'}"
        elif verb in ("pause", "resume", "stop", "restart"):
            detail += f" ({LIFECYCLE[verb]})"
        return Outcome(payload=_mapping(payload), detail=detail, follow_up={"enabled": state})

    def duplicate(self, ref: str) -> Outcome:
        task = self.find(ref)
        payload = self.gateway.duplicate_task(task.id)
        clone = _rule_of(payload)
        detail = f"duplicated {task.id[:8]}"
        if clone.id:
            detail += f" → {clone.id[:8]}"
        return Outcome(payload=_mapping(payload), detail=detail, follow_up={"id": clone.id})

    def set_route_path(self, ref: str, path: RoutePath) -> Outcome:
        task = self.find(ref)
        payload = self.gateway.set_route_path(task.id, path)
        describe = path.kind if not path.intermediate else f"{path.kind} via {path.intermediate}"
        return Outcome(
            payload=_mapping(payload),
            detail=f"route path for {task.id[:8]} set to {describe}",
        )

    def simulate(self, ref: str, text: str, *, sender: str = "") -> SimulationResult:
        task = self.find(ref)
        return self.gateway.simulate(task.id, text, sender=sender)

    def logs(self, ref: str, query: LogQuery) -> LogPage:
        task = self.find(ref)
        return self.gateway.task_logs(task.id, query)

    # ----------------------------------------------------------------- settings
    def settings(self, ref: str) -> tuple[Task, dict[str, Any]]:
        task = self.find(ref)
        payload = self.gateway.get_settings(task.id)
        settings = payload.get("settings") if isinstance(payload, Mapping) else None
        return task, dict(settings) if isinstance(settings, Mapping) else {}

    def setting(self, ref: str, key: str) -> tuple[Task, Any]:
        task, settings = self.settings(ref)
        if key not in settings:
            raise NotFoundError(
                f"setting {key!r} is not set on this task",
                hint="`atf rules settings schema` lists every key the server accepts",
            )
        return task, settings[key]

    def settings_schema(self, *, group: str = "") -> Mapping[str, Any]:
        """The settings registry, narrowed to one group when asked.

        The API answers `{"success":…, "schema": {"settings": […], "groups": […]}}`,
        so the registry has to be unwrapped before it is filtered — reading
        `settings` off the envelope returned nothing, which made `--group` look
        like "no such settings" rather than "no matches".
        """
        payload = self.gateway.settings_schema()
        inner = payload.get("schema") if isinstance(payload.get("schema"), Mapping) else payload
        specs = items_of(inner, "settings")
        chosen = [spec for spec in specs if spec.get("group") == group] if group else specs
        groups = [str(item) for item in inner.get("groups") or []] if isinstance(inner, Mapping) else []
        return {
            "payload": dict(inner) if isinstance(inner, Mapping) else {},
            "settings": chosen,
            "groups": groups,
            "count": int(inner.get("count", len(specs)) or len(specs)) if isinstance(inner, Mapping) else len(specs),
            "total": len(specs),
        }

    def set_settings(self, ref: str, values: Mapping[str, Any], *, replace_all: bool = False) -> Outcome:
        if not values:
            raise UsageError("nothing to set", hint="pass key=value pairs, e.g. clean_buttons=true")
        task = self.find(ref)
        payload = _mapping(self.gateway.update_settings(task.id, values, replace_all=replace_all))
        outcome = Outcome(
            payload=payload,
            detail=f"applied {len(values)} setting(s) to {task.id[:8]}: {', '.join(sorted(values))}",
        )
        if outcome.failed:
            # The server names the offending key in `error_code`; repeating it is
            # how the operator learns which one to fix.
            raise AtfError(
                outcome.failure_reason,
                hint=str(payload.get("error_code", "") or "`atf rules settings schema` lists every valid key"),
            )
        return outcome

    def reset_settings(self, ref: str, keys: Sequence[str]) -> Outcome:
        if not keys:
            raise UsageError("nothing to clear", hint="name the settings to remove, e.g. clean_buttons")
        task = self.find(ref)
        payload = _mapping(self.gateway.update_settings(task.id, {}, resets=list(keys)))
        outcome = Outcome(payload, f"cleared {len(keys)} override(s) on {task.id[:8]}")
        if outcome.failed:
            raise AtfError(outcome.failure_reason, hint=str(payload.get("error_code", "") or ""))
        return outcome

    def effective_settings(self, ref: str) -> tuple[Task, dict[str, Any], dict[str, Any]]:
        """Stored overrides, plus where each effective value came from."""
        task = self.find(ref)
        payload = _mapping(self.gateway.get_settings(task.id))
        stored = payload.get("settings")
        effective = payload.get("effective")
        return (
            task,
            dict(stored) if isinstance(stored, Mapping) else {},
            dict(effective) if isinstance(effective, Mapping) else {},
        )

    # ------------------------------------------------------------------ sources
    def sources(self, ref: str) -> tuple[Task, list[str], list[str]]:
        task = self.find(ref)
        names = [str(name) for name in (task.raw.get("source_chat_names") or [])]
        return task, list(task.source_chat_ids), names

    def add_source(self, ref: str, chat: str, *, title: str = "") -> Outcome:
        problem = validators.valid_chat_ref(chat)
        if problem:
            raise UsageError(f"source: {problem}")
        task, sources, names = self.sources(ref)
        if chat in sources:
            return Outcome(
                payload={"ok": True, "sources": sources, "unchanged": True},
                detail=f"{chat} is already a source of this task",
                warnings=[Warning_("nothing changed")],
            )
        updated = [*sources, chat]
        changes: dict[str, Any] = {"source_chat_ids": updated}
        if title:
            changes["source_chat_names"] = [*names, title]
        payload = self.gateway.update_task(task.id, changes)
        return Outcome(
            payload={"ok": True, "sources": updated, "response": _mapping(payload)},
            detail=f"added {chat} — task now reads {len(updated)} source(s)",
        )

    def remove_source(self, ref: str, chat: str) -> Outcome:
        task, sources, names = self.sources(ref)
        if chat not in sources:
            raise NotFoundError(f"{chat} is not a source of this task")
        if len(sources) == 1:
            raise AtfError(
                "a task must keep at least one source",
                hint="add another source first, or delete the task",
            )
        # Titles are positional: dropping by value would remove an unrelated
        # source that happens to share the title.
        keep = [index for index, source in enumerate(sources) if source != chat]
        updated = [sources[index] for index in keep]
        changes: dict[str, Any] = {"source_chat_ids": updated}
        remaining_names = [names[index] for index in keep if index < len(names)]
        if remaining_names:
            changes["source_chat_names"] = remaining_names
        payload = self.gateway.update_task(task.id, changes)
        return Outcome(
            payload={"ok": True, "sources": updated, "response": _mapping(payload)},
            detail=f"removed {chat} — task now reads {len(updated)} source(s)",
        )

    def set_sources(self, ref: str, chats: Sequence[str]) -> Outcome:
        if not chats:
            raise UsageError("pass one or more chat ids")
        for chat in chats:
            problem = validators.valid_chat_ref(chat)
            if problem:
                raise UsageError(f"source {chat!r}: {problem}")
        task, _, _ = self.sources(ref)
        payload = self.gateway.update_task(
            task.id, {"source_chat_ids": list(chats), "source_chat_id": chats[0]}
        )
        return Outcome(
            payload={"ok": True, "sources": list(chats), "response": _mapping(payload)},
            detail=f"set {len(chats)} source(s)",
        )


# ---------------------------------------------------------------------- helpers
def _matches_source(task: Task, needle: str) -> bool:
    haystacks: Iterable[str] = (
        task.source_chat_id,
        task.source_title,
        *task.source_chat_ids,
    )
    return any(needle in str(value).lower() for value in haystacks if value)


def _mapping(payload: Any) -> dict[str, Any]:
    return dict(payload) if isinstance(payload, Mapping) else {"value": payload}


def _rule_of(payload: Any) -> Task:
    """Parse a rule out of ``{"rule": {...}}``, ``{"id": ...}`` or the rule itself."""
    if isinstance(payload, Mapping):
        inner = payload.get("rule")
        if isinstance(inner, Mapping):
            return Task.from_payload(inner)
        if payload.get("id") or payload.get("rule_id"):
            return Task.from_payload(payload)
        result = payload.get("result")
        if isinstance(result, Mapping):
            return Task.from_payload(result)
    return Task.from_payload({})


def _explain_create_refusal(exc: AtfError, draft: TaskDraft) -> AtfError:
    """Turn the server's terse reference error into the next step.

    The API refuses a source chat that no linked session can see with
    `invalid_reference`, which reads like a malformed id — it is not. The fix is a
    session, and saying so saves a round of guessing.
    """
    text = f"{exc.message} {exc.hint or ''}".lower()
    if "invalid_reference" in text or "does not exist" in text:
        return AtfError(
            exc.message,
            hint=(
                "the source chat is not visible to any linked session — pass "
                "--session <id> (see `atf sessions list`), or link an account first"
                + (f"; this attempt used session {draft.session_id}" if draft.session_id else "")
            ),
        )
    return exc


def _enabled_from(payload: Any) -> bool | None:
    """Read the enabled flag from a mutation response, or None when unstated.

    The rule DTO spells it `is_active` (proto field 9); older answers used
    `is_enabled`, so both are accepted. A field the server omits is not `False`:
    reading absence as a state is how a refusal looks like success.
    """
    if not isinstance(payload, Mapping):
        return None
    for key in ("is_active", "is_enabled"):
        if isinstance(payload.get(key), bool):
            return bool(payload[key])
    inner = payload.get("rule")
    if isinstance(inner, Mapping):
        for key in ("is_active", "is_enabled"):
            if isinstance(inner.get(key), bool):
                return bool(inner[key])
    return None
