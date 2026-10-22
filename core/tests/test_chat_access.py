"""Access planning: what a route may contain once Telegram has been asked.

Shape-valid is not permission-verified. These cases pin the contract between
the picker and the creation step: every source fans out to all sendable
targets, a duplicate rule is reported rather than re-created, a chat the
account cannot reach is excluded from the plan, and one failing create must
not cost the others.
"""

from __future__ import annotations

import pytest

from core.application.chat_access import (
    CODE_CREATED,
    CODE_DUPLICATE,
    CODE_FAILED,
    plan_forward_rules,
    summarize,
    validate_selection,
)
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
from core.infrastructure.telegram.chat_probe import StaticChatProbe


class RecordingRules:
    """A use case double that answers every create it is asked to run."""

    def __init__(self, results=None, fail_for=()):
        self.created = []
        self.boom = set(fail_for)
        self._results = results

    async def create(self, rule):
        self.created.append(rule)
        if rule.source_chat_id in self.boom:
            raise RuntimeError("session dead")
        if self._results:
            return self._results.pop(0)
        return rule


def _pick(selection: Selection, role: str, chat_id: str, title: str = "") -> None:
    selection.toggle(role, PickedChat(chat_id=chat_id, title=title))


def _two_by_one() -> Selection:
    selection = Selection()
    _pick(selection, ROLE_SOURCE, "-100111", "First")
    _pick(selection, ROLE_SOURCE, "-100222", "Second")
    for chat_id in ("-100aaa", "-100bbb"):
        _pick(selection, ROLE_TARGET, chat_id)
    for chat_id in selection.ids(ROLE_SOURCE) + selection.ids(ROLE_TARGET):
        selection.set_state(
            ROLE_SOURCE if selection.has(ROLE_SOURCE, chat_id) else ROLE_TARGET,
            chat_id,
            STATE_OK,
        )
    return selection


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_answers_are_absorbed_not_trusted_blindly():
    probe = StaticChatProbe()
    probe.seed("-100aaa", STATE_OK, title="News")
    probe.seed("-100bbb", STATE_UNKNOWN)
    selection = Selection()
    _pick(selection, ROLE_TARGET, "-100aaa")
    _pick(selection, ROLE_TARGET, "-100bbb")
    report = await validate_selection(selection, probe, owner_user_id=7)
    assert report.ok and report.blocked
    assert report.ok[0].title == "News"
    # The verdict was written back: the same pick now carries its state.
    assert [chat.state for chat in selection.of(ROLE_TARGET)] == [STATE_OK, STATE_UNKNOWN]


@pytest.mark.asyncio
async def test_unreachable_probe_is_reported_unchecked_not_guessed():
    probe = StaticChatProbe(default=STATE_UNCHECKED)
    selection = Selection()
    _pick(selection, ROLE_TARGET, "-100zzz")
    report = await validate_selection(selection, probe, owner_user_id=7)
    assert report.blocked == () and report.ok == ()
    assert len(report.unchecked) == 1


@pytest.mark.asyncio
async def test_probe_is_not_asked_for_a_second_verdict_on_the_same_chat():
    probe = StaticChatProbe()
    probe.seed("-100aaa", STATE_OK)
    selection = Selection()
    _pick(selection, ROLE_SOURCE, "-100aaa")
    await validate_selection(selection, probe, owner_user_id=7)
    # The pick now carries STATE_OK from the first probe, so the second call
    # must not probe again - a probe can flip and would rewrite the answer.
    first = selection.of(ROLE_SOURCE)[0].state
    await validate_selection(selection, probe, owner_user_id=7)
    assert first == STATE_OK
    assert selection.of(ROLE_SOURCE)[0].state == STATE_OK


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def test_every_source_fans_out_to_all_sendable_targets():
    plan = plan_forward_rules(_two_by_one())
    assert len(plan.plans) == 2
    assert all(len(p.targets) == 2 for p in plan.plans)
    assert plan.links == 4


def test_entities_are_read_and_written_through_per_ids_only(reader=None):
    plan = plan_forward_rules(_two_by_one())
    assert [p.source.chat_id for p in plan.plans] == ["-100111", "-100222"]


def test_a_source_without_access_is_skipped_and_named():
    selection = _two_by_one()
    selection.set_state(ROLE_SOURCE, "-100111", STATE_UNKNOWN)
    plan = plan_forward_rules(selection)
    assert plan.skipped_sources == ("-100111",)
    assert [p.source.chat_id for p in plan.plans] == ["-100222"]


def test_a_chat_picked_on_both_sides_is_skipped_not_looped():
    selection = _two_by_one()
    _pick(selection, ROLE_TARGET, "-100111")
    selection.set_state(ROLE_TARGET, "-100111", STATE_OK)
    plan = plan_forward_rules(selection, overlap=selection.overlap())
    # The overlap chat is a source; linking it to itself is banned.
    assert plan.skipped_sources == ("-100111",)
    assert len(plan.plans) == 1


def test_an_identical_existing_rule_is_a_duplicate_not_a_second_rule():
    class Existing:
        source_chat_id = "-100111"
        target_chat_id = "-100aaa"
        target_chat_ids = ["-100aaa", "-100bbb"]

    plan = plan_forward_rules(_two_by_one(), existing=[Existing()])
    duplicates = plan.duplicates
    assert [p.source.chat_id for p in duplicates] == ["-100111"]
    assert duplicates[0].links == 0
    # The other source is still planned as a fresh rule.
    assert [p.source.chat_id for p in plan.creates] == ["-100222"]


def test_a_rule_with_one_other_target_is_not_a_duplicate():
    class Differing:
        source_chat_id = "-100111"
        target_chat_id = "-100ccc"
        target_chat_ids = ["-100ccc"]

    plan = plan_forward_rules(_two_by_one(), existing=[Differing()])
    assert plan.duplicates == ()
    assert len(plan.plans) == 2


def test_targets_that_failed_the_check_never_enter_the_plan():
    selection = _two_by_one()
    selection.set_state(ROLE_TARGET, "-100bbb", STATE_READ_ONLY)
    plan = plan_forward_rules(selection)
    assert all(len(p.targets) == 1 for p in plan.plans)
    assert plan.links == 2


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_create_reports_each_source_separately():
    rules = RecordingRules()
    plan = plan_forward_rules(_two_by_one())
    outcomes = await apply_plan(plan, rules)
    assert [o.code for o in outcomes] == [CODE_CREATED, CODE_CREATED]
    assert len(rules.created) == 2
    assert rules.created[0].target_chat_ids == ["-100aaa", "-100bbb"]


async def apply_plan(plan, rules, **kwargs):
    from core.application.chat_access import apply_forward_plan
    return await apply_forward_plan(plan, rules, session_id="sess-1",
                                    owner_user_id=7, **kwargs)


@pytest.mark.asyncio
async def test_one_failing_source_does_not_cost_the_others():
    rules = RecordingRules(fail_for={"-100111"})
    plan = plan_forward_rules(_two_by_one())
    outcomes = await apply_plan(plan, rules)
    codes = {o.source_id: o.code for o in outcomes}
    assert codes["-100111"] == CODE_FAILED
    assert outcomes[0].detail == "session dead"
    assert codes["-100222"] == CODE_CREATED
    assert len(rules.created) == 2, "the healthy source must still be created"


@pytest.mark.asyncio
async def test_duplicates_are_reported_without_a_write():
    rules = RecordingRules()
    selection = _two_by_one()
    selection.set_state(ROLE_SOURCE, "-100111", STATE_OK)
    plan = plan_forward_rules(selection)

    class Twin:
        source_chat_id = "-100111"
        target_chat_id = "-100aaa"
        target_chat_ids = ["-100aaa", "-100bbb"]

    plan = plan_forward_rules(selection, existing=[Twin()])
    outcomes = await apply_plan(plan, rules)
    assert len(rules.created) == 1
    assert len(outcomes) == 2
    assert [o.code for o in outcomes] == [CODE_DUPLICATE, CODE_CREATED]


@pytest.mark.asyncio
async def test_the_session_scoped_owner_travels_into_the_rule():
    rules = RecordingRules()
    plan = plan_forward_rules(_two_by_one())
    await apply_plan(plan, rules, mode=None, routing=None)
    for rule in rules.created:
        assert rule.session_id == "sess-1"
        assert rule.owner_user_id == 7


def test_summarize_counts_each_outcome_bucket():
    from core.application.chat_access import RuleOutcome
    outcomes = [RuleOutcome("a", code=CODE_CREATED), RuleOutcome("b", code=CODE_DUPLICATE),
                RuleOutcome("c", code=CODE_FAILED), RuleOutcome("d", code=CODE_CREATED)]
    assert summarize(outcomes) == {CODE_CREATED: 2, CODE_DUPLICATE: 1, CODE_FAILED: 1}
