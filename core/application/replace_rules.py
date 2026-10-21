"""The replace-rule manager: one rule, many tasks, one owner.

What a "replace rule" is
------------------------
A find-and-replace pair that runs on the text of a forwarded message. It has a
name, a match mode (exact text or a regular expression), optional flags, an
on/off switch, an execution priority, and a set of forwarding tasks it applies
to.

Where it is stored
------------------
In the tasks themselves: ``forward_rules.metadata["replacements"]``, the field
the engine already reads for every message. The manager does not add a table,
because the forwarder would still have to consult that field on the hot path —
a second store would be a second truth to keep in sync, and the first one to
disagree would be the one that decides what a user's message looks like.

The shape, per task::

    {"<rule id>": {"id": "<rule id>", "source": "old", "to": "new",
                   "mode": "regex", "flags": "i", "name": "Promo",
                   "enabled": true, "priority": 100}}

A rule assigned to three tasks is three entries that share one id. The manager
groups them into one row, and a write goes to every copy — so "assign this rule
to a task" is adding a copy, and "delete this rule" is removing all of them.
Entries written before the manager existed keep working: their key is the source
text and they carry no id, so one is derived deterministically
(:func:`core.domain.text_replace.legacy_rule_id`) and the entry is migrated to
the id-keyed shape the first time it is edited.

Isolation
---------
Every read is ``list_by_owner`` and every write is ``update_owned``; a rule id
arriving from a client is a selector, never proof of ownership. A task that is
not the caller's answers exactly like one that does not exist.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, replace as dataclass_replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..domain.text_replace import (
    DEFAULT_PRIORITY,
    MAX_ENTRIES,
    MODE_LITERAL,
    MODE_REGEX,
    MODES,
    ReplaceEntry,
    ReplaceError,
    apply_entry,
    clamp_priority,
    clean_flags,
    count_matches,
    entry_from_value,
    legacy_rule_id,
    stored_entries,
    validate_entry,
)

logger = logging.getLogger("atf.replace_rules")

#: Page size bounds. A client may ask for a page, not for the whole table.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

STATUS_ALL = "all"
STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"
STATUSES: Tuple[str, ...] = (STATUS_ALL, STATUS_ENABLED, STATUS_DISABLED)


class ReplaceRuleError(ValueError):
    """A refused operation, carrying a stable machine-readable code.

    ``key`` is the catalog key that explains it to a user; the manager's three
    surfaces all render from it, so a new refusal cannot be added without an
    explanation in every language.
    """

    def __init__(self, code: str, key: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.key = key
        self.detail = detail or code


#: code -> catalog key. Kept next to the codes for the same reason
#: ``text_replace._ERROR_KEYS`` is: a refusal and its explanation travel together.
_ERROR_KEYS = {
    "not_found": "ui_rpl_err_not_found",
    "task_required": "ui_rpl_err_task_required",
    "task_not_found": "ui_rpl_err_task_not_found",
    "no_tasks": "ui_rpl_err_no_tasks",
    "too_many_entries": "ui_rep_err_too_many",
    "invalid_entry": "ui_rpl_err_invalid_entry",
    "invalid_json": "ui_rpl_err_invalid_entry",
}


def error_key(code: str) -> str:
    return _ERROR_KEYS.get(code, "ui_rpl_err_invalid_entry")


def _truthy(value: Any) -> bool:
    """A form value as a boolean (a JSON ``"false"`` must not read as true)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ReplaceRule:
    """One rule as the manager sees it, across every task it is assigned to."""

    id: str
    name: str
    source: str
    to: str
    mode: str = MODE_LITERAL
    flags: str = ""
    whole_word: bool = False
    enabled: bool = True
    priority: int = DEFAULT_PRIORITY
    task_ids: Tuple[str, ...] = ()
    task_names: Tuple[str, ...] = ()
    version: int = 1
    updated_at: int = 0

    @property
    def is_regex(self) -> bool:
        return self.mode == MODE_REGEX

    @property
    def badge(self) -> str:
        """Short technical marker, the same one the per-task panel renders."""
        return self.to_entry().badge()

    def to_entry(self) -> ReplaceEntry:
        return ReplaceEntry(
            source=self.source, to=self.to, mode=self.mode, flags=self.flags,
            whole_word=self.whole_word, name=self.name, enabled=self.enabled,
            priority=self.priority, rid=self.id,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "to": self.to,
            "mode": self.mode,
            "flags": self.flags,
            "whole_word": self.whole_word,
            "enabled": self.enabled,
            "priority": self.priority,
            "task_ids": list(self.task_ids),
            "task_names": list(self.task_names),
            "version": self.version,
            "updated_at": self.updated_at,
            "badge": self.badge,
        }


@dataclass
class _Group:
    """The copies of one rule, gathered while reading the owner's tasks."""

    entry: ReplaceEntry
    task_ids: List[str]
    task_names: List[str]
    updated_at: int = 0
    version: int = 1


class ReplaceRuleService:
    """Owner-scoped management of replace rules, stored on their tasks."""

    def __init__(self, rules: Any) -> None:
        #: ``ForwardRuleUseCases`` (or anything with the same owner-scoped API).
        self._rules = rules

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    async def _owned_rules(self, owner: int) -> List[Any]:
        if not owner:
            return []
        try:
            return list(await self._rules.list_by_owner(int(owner)) or [])
        except Exception as exc:
            logger.warning("[PIPELINE:REPLACE] list_by_owner(%s) failed: %s", owner, exc)
            return []

    @staticmethod
    def _task_name(task: Any) -> str:
        name = str(getattr(task, "name", "") or "").strip()
        if name:
            return name
        source = str(getattr(task, "source_chat_name", "") or "").strip()
        return source or str(getattr(task, "id", "") or "")

    def _groups(self, tasks: Sequence[Any]) -> Dict[str, _Group]:
        """Every stored entry, grouped by rule id across the owner's tasks."""
        groups: Dict[str, _Group] = {}
        for task in tasks:
            task_id = str(getattr(task, "id", "") or "")
            if not task_id:
                continue
            task_name = self._task_name(task)
            updated = int(getattr(task, "updated_at", 0) or 0)
            version = int(getattr(task, "version", 1) or 1)
            replacements = getattr(task, "metadata", {})
            replacements = replacements.get("replacements") if isinstance(replacements, dict) else {}
            for rid, entry in stored_entries(replacements, task_id):
                group = groups.get(rid)
                if group is None:
                    groups[rid] = _Group(
                        entry=entry, task_ids=[task_id], task_names=[task_name],
                        updated_at=updated, version=version,
                    )
                    continue
                group.task_ids.append(task_id)
                group.task_names.append(task_name)
                # Copies should be identical; if they are not (a hand-edited
                # row, or a write that failed halfway), the most recently
                # updated task wins, so the screen shows what the engine most
                # recently applied rather than an arbitrary copy.
                if updated >= group.updated_at:
                    group.entry = entry
                    group.updated_at = updated
                    group.version = version
        return groups

    @staticmethod
    def _view(rid: str, group: _Group) -> ReplaceRule:
        entry = group.entry
        return ReplaceRule(
            id=rid,
            name=entry.name,
            source=entry.source,
            to=entry.to,
            mode=entry.mode,
            flags=entry.flags,
            whole_word=entry.whole_word,
            enabled=entry.enabled,
            priority=entry.priority,
            task_ids=tuple(group.task_ids),
            task_names=tuple(group.task_names),
            version=group.version,
            updated_at=group.updated_at,
        )

    @staticmethod
    def _matches_query(view: ReplaceRule, needle: str) -> bool:
        if not needle:
            return True
        low = needle.casefold()
        return any(
            low in str(value or "").casefold()
            for value in (view.name, view.source, view.to)
        )

    async def list(
        self,
        owner: int,
        *,
        query: str = "",
        status: str = STATUS_ALL,
        task_id: str = "",
        page: int = 1,
        per_page: int = DEFAULT_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """One page of the owner's rules, with the counts the header shows."""
        tasks = await self._owned_rules(owner)
        groups = self._groups(tasks)
        views = [self._view(rid, group) for rid, group in groups.items()]

        # The task filter narrows the *set*: the counts describe the tasks the
        # user is looking at, not the whole account, so "3 enabled" next to a
        # task filter never counts a rule that task does not run.
        wanted_task = str(task_id or "").strip()
        if wanted_task:
            views = [v for v in views if wanted_task in v.task_ids]

        counts = {
            "total": len(views),
            "enabled": sum(1 for v in views if v.enabled),
            "disabled": sum(1 for v in views if not v.enabled),
        }

        needle = str(query or "").strip()
        if needle:
            views = [v for v in views if self._matches_query(v, needle)]
        state = str(status or STATUS_ALL).strip().lower()
        if state == STATUS_ENABLED:
            views = [v for v in views if v.enabled]
        elif state == STATUS_DISABLED:
            views = [v for v in views if not v.enabled]

        # Execution order, then a stable human order. The list is a preview of
        # what runs first, which is the one thing a user cannot infer from a
        # card that only shows the text.
        views.sort(key=lambda v: (-v.priority, (v.name or v.source).casefold(), v.id))

        size = max(1, min(int(per_page or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        total = len(views)
        pages = max(1, (total + size - 1) // size)
        current = max(1, min(int(page or 1), pages))
        start = (current - 1) * size
        page_items = views[start:start + size]

        return {
            "items": [v.to_dict() for v in page_items],
            "page": current,
            "per_page": size,
            "total": total,
            "pages": pages,
            "counts": counts,
            "status": state if state in STATUSES else STATUS_ALL,
            "query": needle,
            "task_id": wanted_task,
            "tasks": [
                {"id": str(getattr(t, "id", "")), "name": self._task_name(t)}
                for t in tasks
            ],
        }

    async def get(self, owner: int, rule_id: str) -> ReplaceRule:
        groups = self._groups(await self._owned_rules(owner))
        group = groups.get(str(rule_id or ""))
        if group is None:
            raise ReplaceRuleError("not_found", error_key("not_found"))
        return self._view(str(rule_id), group)

    async def tasks(self, owner: int) -> List[Dict[str, str]]:
        return [
            {"id": str(getattr(t, "id", "")), "name": self._task_name(t)}
            for t in await self._owned_rules(owner)
        ]

    # ------------------------------------------------------------------ #
    # Payload -> entry
    # ------------------------------------------------------------------ #
    @staticmethod
    def _payload_entry(data: Mapping[str, Any], *, rid: str = "") -> ReplaceEntry:
        """Validate a create/update body into an entry.

        Every field is optional except the source, so a PATCH-shaped body that
        only flips ``enabled`` works; the caller merges it onto the stored entry
        first (see :meth:`update`).
        """
        mode = str(data.get("mode") or MODE_LITERAL).strip().lower()
        if mode not in MODES:
            raise ReplaceError("unknown_mode", f"mode '{mode}' is not supported")
        flags = clean_flags(data.get("flags", ""), mode)
        if "ignore_case" in data and not flags:
            flags = clean_flags("i", mode) if data.get("ignore_case") else ""
        whole = bool(data.get("whole_word", "w" in flags)) or ("w" in flags)
        if whole and "w" not in flags:
            flags = clean_flags(flags + "w", mode)
        entry = ReplaceEntry(
            source=str(data.get("source") or "").strip(),
            to="" if data.get("to") is None else str(data.get("to")),
            mode=mode,
            flags=flags,
            whole_word=whole,
            name=str(data.get("name") or "").strip()[:40],
            enabled=_truthy(data.get("enabled", True)),
            priority=clamp_priority(data.get("priority", DEFAULT_PRIORITY)),
            rid=rid,
        )
        validate_entry(entry)
        return entry

    @staticmethod
    def _entry_or_refuse(data: Mapping[str, Any], *, rid: str = "") -> ReplaceEntry:
        try:
            return ReplaceRuleService._payload_entry(data, rid=rid)
        except ReplaceError as exc:
            raise ReplaceRuleError("invalid_entry", error_key("invalid_entry"),
                                   detail=exc.detail) from exc

    @staticmethod
    def _task_ids(data: Mapping[str, Any]) -> List[str]:
        raw = data.get("task_ids")
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = [part for part in re.split(r"[,\s]+", raw) if part]
        if not isinstance(raw, Iterable):
            return []
        out: List[str] = []
        for value in raw:
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
        return out

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _replacements(task: Any) -> Dict[str, Any]:
        metadata = getattr(task, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            task.metadata = metadata
        current = metadata.get("replacements")
        return dict(current) if isinstance(current, dict) else {}

    @staticmethod
    def _put_copy(replacements: Dict[str, Any], rid: str, task_id: str,
                  entry: ReplaceEntry) -> None:
        """Write one copy under ``rid``, dropping the legacy key it came from."""
        key = ReplaceRuleService._copy_key(replacements, rid, task_id)
        if key is not None and key != rid:
            replacements.pop(key, None)
        replacements[rid] = entry.to_rule_value(rid)

    @staticmethod
    def _copy_key(replacements: Mapping[str, Any], rid: str, task_id: str) -> Optional[str]:
        """The key a rule's copy is stored under in one task, if it is there.

        Usually the rule id; a legacy entry is keyed by its source text, so the
        key is resolved through the same derivation :func:`stored_entries` uses.
        """
        if rid in replacements:
            return rid
        for key, value in replacements.items():
            entry = entry_from_value(key, value)
            if (entry.rid or legacy_rule_id(task_id, str(key))) == rid:
                return str(key)
        return None

    async def _save_task(self, task: Any, owner: int, replacements: Dict[str, Any]) -> None:
        metadata = getattr(task, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            task.metadata = metadata
        metadata["replacements"] = replacements
        # update_owned re-checks ownership against the stored row: the task was
        # read through list_by_owner, and this closes the window where it could
        # have changed hands between the read and the write.
        await self._rules.update_owned(task, int(owner))

    async def create(self, owner: int, data: Mapping[str, Any]) -> ReplaceRule:
        tasks = await self._owned_rules(owner)
        if not tasks:
            raise ReplaceRuleError("no_tasks", error_key("no_tasks"))
        entry = self._entry_or_refuse(data)
        wanted = self._task_ids(data)
        if not wanted:
            raise ReplaceRuleError("task_required", error_key("task_required"))

        by_id = {str(getattr(t, "id", "")): t for t in tasks}
        missing = [tid for tid in wanted if tid not in by_id]
        if missing:
            # Same answer for "not yours" and "does not exist".
            raise ReplaceRuleError("task_not_found", error_key("task_not_found"))

        rid = uuid.uuid4().hex[:16]
        entry = dataclass_replace(entry, rid=rid)
        for tid in wanted:
            task = by_id[tid]
            replacements = self._replacements(task)
            if self._copy_key(replacements, rid, tid) is None and len(replacements) >= MAX_ENTRIES:
                raise ReplaceRuleError("too_many_entries", error_key("too_many_entries"))
            self._put_copy(replacements, rid, tid, entry)
            await self._save_task(task, owner, replacements)
        logger.info(
            "[PIPELINE:REPLACE] owner=%s created rule=%s tasks=%d mode=%s",
            owner, rid, len(wanted), entry.mode,
        )
        return await self.get(owner, rid)

    async def update(self, owner: int, rule_id: str, data: Mapping[str, Any]) -> ReplaceRule:
        tasks = await self._owned_rules(owner)
        rid = str(rule_id or "")
        groups = self._groups(tasks)
        group = groups.get(rid)
        if group is None:
            raise ReplaceRuleError("not_found", error_key("not_found"))

        # Merge onto the stored entry: a body that only says {"enabled": false}
        # must not blank the pattern it is switching off.
        merged: Dict[str, Any] = {
            "source": group.entry.source,
            "to": group.entry.to,
            "mode": group.entry.mode,
            "flags": group.entry.flags,
            "whole_word": group.entry.whole_word,
            "name": group.entry.name,
            "enabled": group.entry.enabled,
            "priority": group.entry.priority,
        }
        for field in ("source", "to", "mode", "flags", "whole_word", "name",
                      "enabled", "priority", "ignore_case"):
            if field in data:
                merged[field] = data[field]
        entry = self._entry_or_refuse(merged, rid=rid)

        wanted = self._task_ids(data)
        targets = wanted if wanted else list(group.task_ids)
        if not targets:
            raise ReplaceRuleError("task_required", error_key("task_required"))
        by_id = {str(getattr(t, "id", "")): t for t in tasks}
        missing = [tid for tid in targets if tid not in by_id]
        if missing:
            raise ReplaceRuleError("task_not_found", error_key("task_not_found"))

        assigned = set(targets)
        for tid, task in by_id.items():
            replacements = self._replacements(task)
            key = self._copy_key(replacements, rid, tid)
            if tid in assigned:
                if key is None and len(replacements) >= MAX_ENTRIES:
                    raise ReplaceRuleError("too_many_entries", error_key("too_many_entries"))
                self._put_copy(replacements, rid, tid, entry)
                await self._save_task(task, owner, replacements)
            elif key is not None:
                replacements.pop(key, None)
                await self._save_task(task, owner, replacements)
        logger.info(
            "[PIPELINE:REPLACE] owner=%s updated rule=%s tasks=%d enabled=%s",
            owner, rid, len(targets), entry.enabled,
        )
        return await self.get(owner, rid)

    async def delete(self, owner: int, rule_id: str) -> Dict[str, Any]:
        rid = str(rule_id or "")
        tasks = await self._owned_rules(owner)
        removed = 0
        for task in tasks:
            task_id = str(getattr(task, "id", "") or "")
            replacements = self._replacements(task)
            key = self._copy_key(replacements, rid, task_id)
            if key is None:
                continue
            replacements.pop(key, None)
            await self._save_task(task, owner, replacements)
            removed += 1
        if not removed:
            raise ReplaceRuleError("not_found", error_key("not_found"))
        logger.info("[PIPELINE:REPLACE] owner=%s deleted rule=%s copies=%d", owner, rid, removed)
        return {"ok": True, "removed": removed}

    async def toggle(self, owner: int, rule_id: str) -> ReplaceRule:
        current = await self.get(owner, rule_id)
        return await self.update(owner, rule_id, {"enabled": not current.enabled})

    async def duplicate(self, owner: int, rule_id: str, name: str = "") -> ReplaceRule:
        current = await self.get(owner, rule_id)
        data: Dict[str, Any] = {
            "source": current.source,
            "to": current.to,
            "mode": current.mode,
            "flags": current.flags,
            "whole_word": current.whole_word,
            "enabled": current.enabled,
            "priority": current.priority,
            "name": (str(name or "").strip() or current.name or current.source)[:40],
            "task_ids": list(current.task_ids),
        }
        return await self.create(owner, data)

    # ------------------------------------------------------------------ #
    # Preview
    # ------------------------------------------------------------------ #
    async def test(self, owner: int, data: Mapping[str, Any]) -> Dict[str, Any]:
        """Apply one rule (or a draft) to a sample string.

        Uses the same ``apply_entry`` the forwarder uses, so the preview cannot
        promise an output the pipeline would not produce — including the legacy
        literal's smart case and the guarded regex compiler.
        """
        sample = "" if data.get("sample") is None else str(data.get("sample"))
        rid = str(data.get("rule_id") or "").strip()
        if rid:
            current = await self.get(owner, rid)
            base: Dict[str, Any] = {
                "source": current.source, "to": current.to, "mode": current.mode,
                "flags": current.flags, "whole_word": current.whole_word,
                "enabled": current.enabled, "priority": current.priority,
                "name": current.name,
            }
            for field in ("source", "to", "mode", "flags", "whole_word"):
                if field in data:
                    base[field] = data[field]
            entry = self._entry_or_refuse(base, rid=rid)
        else:
            entry = self._entry_or_refuse(data)
        output = apply_entry(sample, entry)
        return {
            "ok": True,
            "input": sample,
            "output": output,
            "matches": count_matches(sample, entry),
            "changed": output != sample,
        }


__all__ = [
    "ReplaceRule",
    "ReplaceRuleError",
    "ReplaceRuleService",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "STATUS_ALL",
    "STATUS_ENABLED",
    "STATUS_DISABLED",
    "error_key",
]
