"""Chat access: prove a peer before a route is allowed to point at it.

A numeric id is a shape, not a permission. This module answers the two
questions the picker must not guess:

* **can this account reach the chat at all** (``UNKNOWN`` when Telegram says
  no, ``UNCHECKED`` when there was nobody to ask);
* **can the bot post there** (``READ_ONLY`` for a channel where the account
  is a plain member or an admin without post rights).

Probing is a port — :class:`ChatAccessProbe` — because the honest answer comes
from Telegram, and tests need a probe that says exactly what they mean. The
planning step (:func:`plan_forward_rules`) and the creation step
(:func:`apply_forward_plan`) are pure/application-side: the handler only
renders their outcome, and a failure on one source never hides the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from core.application.chat_selection import (
    ROLE_SOURCE,
    ROLE_TARGET,
    STATE_OK,
    STATE_READ_ONLY,
    STATE_UNCHECKED,
    STATE_UNKNOWN,
    PickedChat,
    Selection,
)
from core.domain.peer_ids import unique_peer_key

CODE_CREATED = "created"
CODE_DUPLICATE = "duplicate"
CODE_FAILED = "failed"


@dataclass(frozen=True)
class Probe:
    """What the account can do with one chat, as Telegram reported it."""

    chat_id: str
    state: str = STATE_UNCHECKED
    title: str = ""
    reason: str = ""

    @property
    def sendable(self) -> bool:
        return self.state == STATE_OK


class ChatAccessProbe(Protocol):
    """The port: an adapter asks Telegram, tests answer directly."""

    async def probe(self, owner_user_id: int,
                    chat_ids: Sequence[str]) -> Dict[str, Probe]:
        ...


@dataclass(frozen=True)
class ValidationReport:
    """The per-chat verdict after probing both roles."""

    probes: Tuple[Probe, ...] = ()
    ok: Tuple[Probe, ...] = ()
    blocked: Tuple[Probe, ...] = ()
    unchecked: Tuple[Probe, ...] = ()
    overlap: Tuple[str, ...] = ()

    @property
    def sendable_ids(self) -> Tuple[str, ...]:
        return tuple(probe.chat_id for probe in self.ok)

    def probe_for(self, chat_id: str) -> Optional[Probe]:
        key = unique_peer_key(chat_id)
        for probe in self.probes:
            if unique_peer_key(probe.chat_id) == key:
                return probe
        return None


async def validate_selection(
    selection: Selection,
    probe: ChatAccessProbe,
    owner_user_id: int,
) -> ValidationReport:
    """Probe every picked chat and write the verdict back into the selection.

    A chat that was already probed in this flow is not asked again: the second
    probe would only add a network round-trip and a chance to flip the answer.
    """
    wanted: List[str] = []
    seen: set = set()
    for role in (ROLE_SOURCE, ROLE_TARGET):
        for chat in selection.of(role):
            if chat.state in (STATE_OK, STATE_READ_ONLY, STATE_UNKNOWN):
                continue
            key = chat.key
            if key in seen:
                continue
            seen.add(key)
            wanted.append(chat.chat_id)

    found: Dict[str, Probe] = {}
    if wanted:
        found = await probe.probe(owner_user_id, wanted)

    probes: List[Probe] = []
    ok: List[Probe] = []
    blocked: List[Probe] = []
    unchecked: List[Probe] = []
    for role in (ROLE_SOURCE, ROLE_TARGET):
        for chat in selection.of(role):
            result = found.get(chat.chat_id) or found.get(unique_peer_key(chat.chat_id))
            if result is None:
                # Already-probed picks keep their earlier verdict.
                result = Probe(chat_id=chat.chat_id, state=chat.state, title=chat.title)
            selection.set_state(role, chat.chat_id, result.state, result.title)
            probes.append(result)
            if result.state == STATE_OK:
                ok.append(result)
            elif result.state == STATE_UNCHECKED:
                unchecked.append(result)
            else:
                blocked.append(result)
    return ValidationReport(
        probes=tuple(probes),
        ok=tuple(ok),
        blocked=tuple(blocked),
        unchecked=tuple(unchecked),
        overlap=selection.overlap(),
    )


@dataclass(frozen=True)
class RulePlan:
    """One rule to create: a single source and all sendable targets."""

    source: PickedChat
    targets: Tuple[PickedChat, ...] = ()
    duplicate: bool = False
    rule_id: str = ""

    @property
    def links(self) -> int:
        return 0 if self.duplicate else len(self.targets)


@dataclass(frozen=True)
class PlanReport:
    """The whole route as it would be created."""

    plans: Tuple[RulePlan, ...] = ()
    skipped_sources: Tuple[str, ...] = ()
    overlap: Tuple[str, ...] = ()

    @property
    def links(self) -> int:
        return sum(plan.links for plan in self.plans)

    @property
    def duplicates(self) -> Tuple[RulePlan, ...]:
        return tuple(plan for plan in self.plans if plan.duplicate)

    @property
    def creates(self) -> Tuple[RulePlan, ...]:
        return tuple(plan for plan in self.plans if not plan.duplicate)


def _target_keys(rule: Any) -> set:
    ids = list(getattr(rule, "target_chat_ids", None) or [])
    primary = str(getattr(rule, "target_chat_id", "") or "")
    if primary:
        ids.insert(0, primary)
    return {unique_peer_key(item) for item in ids if str(item or "").strip()}


def plan_forward_rules(
    selection: Selection,
    existing: Iterable[Any] = (),
    rule_ids: Optional[Sequence[str]] = None,
    overlap: Sequence[str] = (),
) -> PlanReport:
    """Fan the selection out into one rule per source, all targets attached.

    The rule's own shape is single-source by design (a source chat and its
    fan-out list), so a multi-source route is N rules. A source whose targets
    already exist unchanged in one of the caller's rules is planned as
    ``duplicate`` instead of being silently created twice.

    ``overlap`` lists chats picked as both a source and a target: creating
    them would build a self-loop, so those sources are skipped and reported.
    """
    targets = tuple(
        chat for chat in selection.of(ROLE_TARGET) if chat.state == STATE_OK
    )
    target_keys = {chat.key for chat in targets}
    blocked_targets = tuple(
        chat.chat_id for chat in selection.of(ROLE_TARGET) if chat.state != STATE_OK
    )
    overlap_keys = {unique_peer_key(item) for item in overlap}

    by_source: Dict[str, List[Any]] = {}
    for rule in existing or ():
        by_source.setdefault(unique_peer_key(getattr(rule, "source_chat_id", "")), []).append(rule)

    plans: List[RulePlan] = []
    skipped: List[str] = []
    minted = list(rule_ids or [])
    for index, source in enumerate(selection.of(ROLE_SOURCE)):
        if source.state != STATE_OK:
            skipped.append(source.chat_id)
            continue
        if source.key in overlap_keys or source.key in target_keys:
            skipped.append(source.chat_id)
            continue
        if not targets:
            # A rule with no reachable target is not a rule: the use case would
            # reject it as "target empty" after the user approved the card.
            skipped.append(source.chat_id)
            continue
        duplicate = any(
            _target_keys(rule) == target_keys for rule in by_source.get(source.key, [])
        )
        rule_id = minted[index] if index < len(minted) else ""
        plans.append(RulePlan(source=source, targets=targets,
                              duplicate=duplicate, rule_id=rule_id))
    _ = blocked_targets  # reported by the caller from the validation report
    return PlanReport(tuple(plans), tuple(skipped), tuple(overlap))


@dataclass(frozen=True)
class RuleOutcome:
    """The per-source result the confirmation card reports line by line."""

    source_id: str
    source_label: str = ""
    code: str = CODE_CREATED
    rule_id: str = ""
    detail: str = ""
    links: int = 0

    @property
    def ok(self) -> bool:
        return self.code == CODE_CREATED


async def apply_forward_plan(
    report: PlanReport,
    rules_use_case: Any,
    session_id: str,
    owner_user_id: int,
    mode: Any = None,
    routing: Any = None,
    build_rule: Any = None,
) -> List[RuleOutcome]:
    """Create the planned rules, one ``RuleOutcome`` per plan.

    Each create runs in its own try: a rejected source (a dead session, a
    domain rule, a database error) is reported for that source and the rest of
    the plan still runs. Duplicates are reported without a write.
    """
    outcomes: List[RuleOutcome] = []
    for plan in report.plans:
        if plan.duplicate:
            outcomes.append(RuleOutcome(
                source_id=plan.source.chat_id,
                source_label=plan.source.label,
                code=CODE_DUPLICATE,
                links=0,
            ))
            continue
        try:
            if build_rule is not None:
                rule = build_rule(plan, session_id, owner_user_id, mode, routing)
            else:
                from core.domain.entities import ForwardMode, ForwardRule, RoutingType

                # An empty id here would be *stored* as an empty primary key and
                # the second source of the same route would collide with the
                # first, so the field is omitted and the aggregate mints one.
                fields: Dict[str, Any] = {}
                if plan.rule_id:
                    fields["id"] = plan.rule_id
                rule = ForwardRule(
                    session_id=session_id,
                    source_chat_id=plan.source.chat_id,
                    source_chat_name=plan.source.title,
                    target_chat_id=plan.targets[0].chat_id if plan.targets else "",
                    target_chat_name=plan.targets[0].title if plan.targets else "",
                    target_chat_ids=[chat.chat_id for chat in plan.targets],
                    routing_type=routing or RoutingType.CHANNEL_TO_CHANNEL,
                    forward_mode=mode or ForwardMode.COPY_MESSAGE,
                    owner_user_id=owner_user_id,
                    **fields,
                )
            created = await rules_use_case.create(rule)
            outcomes.append(RuleOutcome(
                source_id=plan.source.chat_id,
                source_label=plan.source.label,
                code=CODE_CREATED,
                rule_id=str(getattr(created, "id", "") or plan.rule_id),
                links=len(plan.targets),
            ))
        except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
            outcomes.append(RuleOutcome(
                source_id=plan.source.chat_id,
                source_label=plan.source.label,
                code=CODE_FAILED,
                detail=str(exc)[:160],
            ))
    return outcomes


def summarize(outcomes: Sequence[RuleOutcome]) -> Dict[str, int]:
    """Counts for the result header: created / duplicate / failed."""
    summary = {CODE_CREATED: 0, CODE_DUPLICATE: 0, CODE_FAILED: 0}
    for outcome in outcomes:
        summary[outcome.code] = summary.get(outcome.code, 0) + 1
    return summary
