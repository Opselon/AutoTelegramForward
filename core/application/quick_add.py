"""One-line task creation: the application half of ``/forward add``.

The Telegram command is only a surface. Everything that decides whether a task
may exist lives here, so the bot, a future Mini App and an API endpoint create
the same task from the same rules:

* the label is validated once (the catalog of legal characters is a domain
  rule, not a regex copied into a handler);
* every chat must already be *resolved* by the caller — this layer never
  guesses a peer id from text;
* a user's tasks are looked up and written under that user's id only, and a
  zero/absent owner is refused rather than treated as an admin;
* a label that already names one of the caller's tasks is refused: ``add``
  creates, it never silently overwrites another task's route;
* the per-task content filters are applied through the entity's own setters,
  so the forwarder reads exactly the fields it already enforces.

Nothing in this module imports Pyrogram, SQL or a framework: it depends on the
application use cases and the domain entities only.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Sequence, Tuple

from ..domain.entities import ForwardRule
from ..domain.value_objects import ChannelReference, ForwardMode, RoutingType
from .use_cases import ForwardRuleUseCases, SessionUseCases

#: The label grammar promised by the wizard and the command help.
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,50}$")

#: What one ``--block=`` entry may name. Each one is a real gate the pipeline
#: reads from the rule (`ForwardRule.FILTER_OPTION_FLAGS`).
BLOCKABLE = ("links", "video", "audio", "voice", "text", "stickers")

#: ``--only=`` values mapped onto the entity's own media handling field.
ONLY_MODES = {"text": "TEXT_ONLY", "media": "MEDIA_ONLY", "all": "AUTO"}


class QuickAddError(ValueError):
    """A task cannot be created; ``code`` is what the surface localizes."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def validate_label(label: str) -> str:
    """Return the label trimmed, or raise ``QuickAddError('bad_label')``."""
    value = str(label or "").strip()
    if not value or not LABEL_PATTERN.match(value):
        raise QuickAddError("bad_label", value)
    return value


@dataclass(frozen=True)
class TaskFilters:
    """Per-task content filters carried by one quick-add command.

    Immutable and language-free: it says *what* to filter, the surface decides
    how to describe it. Empty means "no filter pins", i.e. the task inherits the
    bot defaults like every other task.
    """

    block: Tuple[str, ...] = ()
    only: str = ""
    keywords: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    delay: int = 0
    every: int = 0

    def __post_init__(self) -> None:
        for value in self.block:
            if value not in BLOCKABLE:
                raise QuickAddError("bad_filter_value", value)
        if self.only and self.only not in ONLY_MODES:
            raise QuickAddError("bad_filter_value", self.only)
        if self.delay < 0:
            raise QuickAddError("bad_filter_value", str(self.delay))
        if self.every < 0:
            raise QuickAddError("bad_filter_value", str(self.every))

    @property
    def is_empty(self) -> bool:
        return not (self.block or self.only or self.keywords or self.deny
                    or self.delay or self.every)

    def apply_to(self, rule: ForwardRule) -> None:
        """Write these filters onto ``rule`` through its own setters."""
        for name in self.block:
            setattr(rule, f"block_{name}", True)
        if self.only:
            rule.media_handling = ONLY_MODES[self.only]
        if self.keywords:
            criteria: dict = rule.detection_criteria if isinstance(rule.detection_criteria, dict) else {}
            criteria["text_contains"] = list(self.keywords)
            rule.detection_criteria = criteria
        if self.deny:
            rule.blacklist_words = list(self.deny)
        if self.delay:
            rule.forward_delay_seconds = float(self.delay)
        if self.every:
            rule.forward_every_n = int(self.every)

    def tokens(self) -> List[str]:
        """The canonical flag spelling, for the confirmation card."""
        out: List[str] = []
        if self.only:
            out.append(f"--only={self.only}")
        if self.block:
            out.append("--block=" + ",".join(self.block))
        if self.keywords:
            out.append("--keywords=" + ",".join(self.keywords))
        if self.deny:
            out.append("--deny=" + ",".join(self.deny))
        if self.delay:
            out.append(f"--delay={self.delay}")
        if self.every:
            out.append(f"--every={self.every}")
        return out


@dataclass(frozen=True)
class QuickAddRequest:
    """A validated *resolved* request: ids, never raw usernames."""

    label: str
    sources: Sequence[ChannelReference] = field(default_factory=tuple)
    targets: Sequence[ChannelReference] = field(default_factory=tuple)
    filters: TaskFilters = field(default_factory=TaskFilters)


class QuickAddService:
    """Create one owned forwarding task from a quick-add request.

    Same use cases the wizard already goes through, so endpoint validation,
    optimistic locking and session ownership are not re-implemented here.
    """

    def __init__(self, rules: ForwardRuleUseCases, sessions: SessionUseCases) -> None:
        self._rules = rules
        self._sessions = sessions

    # -- queries ---------------------------------------------------------

    async def find_by_label(self, owner_user_id: int, label: str) -> Any:
        """The caller's task named ``label`` (case-insensitive), or None."""
        if int(owner_user_id or 0) <= 0:
            return None
        needle = str(label or "").strip().lower()
        if not needle:
            return None
        for rule in await self._rules.list_by_owner(int(owner_user_id)):
            if (rule.name or "").strip().lower() == needle:
                return rule
        return None

    async def _owner_session(self, owner_user_id: int):
        sessions = await self._sessions.list_by_owner(owner_user_id)
        for session in sessions:
            if getattr(session, "is_active", True) and getattr(session, "is_authorized", True):
                return session
        return sessions[0] if sessions else None

    # -- command ---------------------------------------------------------

    async def create(self, owner_user_id: int, request: QuickAddRequest) -> ForwardRule:
        owner = int(owner_user_id or 0)
        if owner <= 0:
            # Fail closed: an anonymous caller is never the system scope.
            raise QuickAddError("no_owner")
        label = validate_label(request.label)
        sources = [ref for ref in request.sources if ref.is_resolved]
        targets = [ref for ref in request.targets if ref.is_resolved]
        if not sources:
            raise QuickAddError("empty_sources")
        if not targets:
            raise QuickAddError("empty_targets")
        overlap = {ref.chat_id for ref in sources} & {ref.chat_id for ref in targets}
        if overlap:
            raise QuickAddError("overlap", sorted(overlap)[0])
        duplicate = await self.find_by_label(owner, label)
        if duplicate is not None:
            raise QuickAddError("duplicate_label", duplicate.name or label)
        session = await self._owner_session(owner)
        if session is None:
            raise QuickAddError("no_session")

        rule = ForwardRule(
            id=str(uuid.uuid4()),
            session_id=str(getattr(session, "id", "") or ""),
            name=label,
            owner_user_id=owner,
            routing_type=RoutingType.CHANNEL_TO_CHANNEL,
            forward_mode=ForwardMode.COPY_MESSAGE,
        )
        rule.source_chat_ids = [ref.chat_id for ref in sources]
        rule.source_chat_names = [ref.title for ref in sources]
        rule.source_chat_name = sources[0].title or sources[0].chat_id
        rule.target_chat_ids = [ref.chat_id for ref in targets]
        rule.target_chat_id = targets[0].chat_id
        rule.target_chat_name = targets[0].title or targets[0].chat_id
        request.filters.apply_to(rule)
        await self._rules.create(rule)
        return rule
