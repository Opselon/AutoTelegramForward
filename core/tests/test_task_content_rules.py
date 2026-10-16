"""Per-task content rules, end to end through the real pipeline.

Where ``test_content_policy.py`` pins the matching engine, this file pins the
*behaviour a user configures on a task*: which messages a task lets through,
what it rewrites, and how per-source and per-target overrides compose — for one
task routing many sources to many targets, with no cap on how many.

Everything here goes through ``DurableMessagePipeline.process`` (the same entry
the poller calls) or ``_apply_transforms`` (the same transform the delivery
text is built from), so a passing test is a statement about production
behaviour, not about a helper.
"""

from __future__ import annotations

import pytest

from core.application.pipeline import DurableMessagePipeline, PipelineStage
from core.domain import rule_settings as RS
from core.domain.entities import EvaluationResult, ForwardRule, MessagePayload
from core.domain.value_objects import FilterAction, ForwardMode, RoutingType

from .test_p1_durable_pipeline import FakeFilterRepo


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class MultiSourceRuleRepo:
    """Rule lookup by *any* listed source chat, like the real repository."""

    def __init__(self, rules):
        self.rules = list(rules)

    async def list_active_by_source(self, source_chat_id):
        out = []
        for rule in self.rules:
            ids = {str(rule.source_chat_id)}
            ids.update(str(x) for x in (rule.source_chat_ids or []))
            if str(source_chat_id) in ids and rule.is_active:
                out.append(rule)
        return out


class RecordingQueue:
    """Captures (target, text) for every primary forward job."""

    def __init__(self):
        self.jobs = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        if job_kind == "forward":
            self.jobs.append({"target": str(target_chat_id), "text": text,
                              "msg": payload.message_id, "branding": branding})
        return True

    async def get_pending_count(self):
        return 0


def make_rule(**kw):
    """A task, `multi_route=True` by default.

    The routing engine is single-match by design: the highest-priority matching
    task consumes a message unless it opts into multi-route. Several of these
    tests assert that *two* tasks did different things with the same message, so
    both opt in.
    """
    rule = ForwardRule(
        id=kw.pop("id", "task-1"),
        session_id="sess-1",
        source_chat_id=kw.pop("source_chat_id", "-1001"),
        target_chat_id=kw.pop("target_chat_id", "-2001"),
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        is_active=True,
        multi_route=kw.pop("multi_route", True),
        **kw,
    )
    return rule


def make_pipeline(rules, queue=None):
    queue = queue or RecordingQueue()
    pipeline = DurableMessagePipeline(
        rule_repo=MultiSourceRuleRepo(rules),
        filter_repo=FakeFilterRepo({}),
        queue_manager=queue,
    )
    return pipeline, queue


async def run(pipeline, chat="-1001", msg=1, text="hello"):
    return await pipeline.process({"chat_id": chat, "message_id": msg, "text": text})


def filter_stages(ctx):
    """Every FILTER outcome for this message — one per task that looked at it."""
    return [r for r in ctx.stage_results if r.stage == PipelineStage.FILTER]


def filter_stage(ctx):
    """The single FILTER outcome, for the single-task cases."""
    stages = filter_stages(ctx)
    assert len(stages) == 1, f"expected one task to act, got {len(stages)}"
    return stages[0]


def blocked(ctx, reason_part=""):
    """True when some task dropped the message, optionally for a given reason."""
    return any((not r.success) and reason_part in (r.reason or "") for r in filter_stages(ctx))


# --------------------------------------------------------------------------
# Blacklist / whitelist-only, per task
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blacklist_words_block_only_that_task():
    """'never forward if this word appears' — and the sibling task is untouched."""
    blocking_task = make_rule(id="t-block", target_chat_id="-2001")
    blocking_task.blacklist_words = ["casino", "تبلیغ"]
    open_task = make_rule(id="t-open", target_chat_id="-2002")

    pipeline, queue = make_pipeline([blocking_task, open_task])

    ctx = await run(pipeline, msg=11, text="Big casino news today")
    assert blocked(ctx, "blacklist:casino")
    # The task without the rule still forwarded it.
    assert [j["target"] for j in queue.jobs] == ["-2002"]

    queue.jobs.clear()
    ctx2 = await run(pipeline, msg=12, text="این یک تبلیغ است")
    assert blocked(ctx2, "blacklist")

    queue.jobs.clear()
    ctx3 = await run(pipeline, msg=13, text="Ordinary update")
    assert not blocked(ctx3)
    assert sorted(j["target"] for j in queue.jobs) == ["-2001", "-2002"]


@pytest.mark.asyncio
async def test_whitelist_only_forwards_a_matching_message():
    rule = make_rule(id="t-only")
    rule.whitelist_words = ["فوری", "urgent"]
    rule.content_policy_enabled = True
    pipeline, queue = make_pipeline([rule])

    ctx = await run(pipeline, msg=21, text="خبر فوری درباره بازار")
    assert filter_stage(ctx).success is True
    assert len(queue.jobs) == 1

    queue.jobs.clear()
    ctx2 = await run(pipeline, msg=22, text="خبر معمولی درباره بازار")
    assert filter_stage(ctx2).success is False
    assert filter_stage(ctx2).reason == "whitelist_mismatch"
    assert queue.jobs == []


@pytest.mark.asyncio
async def test_the_task_switch_turns_the_rules_off():
    """A configured-but-disabled task behaves as if the rules were not there."""
    rule = make_rule(id="t-off")
    rule.blacklist_words = ["casino"]
    rule.content_policy_enabled = False
    pipeline, queue = make_pipeline([rule])

    ctx = await run(pipeline, msg=31, text="casino")
    assert filter_stage(ctx).success is True
    assert len(queue.jobs) == 1


# --------------------------------------------------------------------------
# Rewrites
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_strip_and_remove_links_rewrite_the_text():
    rule = make_rule(id="t-rw")
    rule.content_policy_config = {
        "replacements": {"برند قدیمی": "برند جدید", "old brand": "new brand"},
        "strip_words": ["@spamchannel"],
        "remove_links": True,
    }
    pipeline, _ = make_pipeline([rule])

    payload = MessagePayload(message_id=41, chat_id="-1001",
                             text="buy old brand now https://x.com/a @spamchannel")
    evaluation = EvaluationResult(action=FilterAction.ALLOW)
    from core.application.pipeline import PipelineContext
    ctx = PipelineContext(payload=payload, correlation_id="rw-1")

    out = await pipeline._apply_transforms(ctx, payload, rule, evaluation)
    assert "new brand" in out
    assert "old brand" not in out
    assert "https://x.com/a" not in out
    assert "@spamchannel" not in out
    assert evaluation.links_removed is True


@pytest.mark.asyncio
async def test_blacklist_is_a_gate_and_strip_is_a_rewrite():
    """Blacklist drops the message; strip only edits it. Confusing the two is
    how a user ends up losing posts they meant to publish."""
    gate = make_rule(id="gate")
    gate.blacklist_words = ["promo"]
    editor = make_rule(id="editor", target_chat_id="-2002")
    editor.strip_words = ["promo"]

    pipeline, queue = make_pipeline([gate, editor])
    await run(pipeline, msg=51, text="promo code inside")

    by_target = {j["target"]: j["text"] for j in queue.jobs}
    assert "-2001" not in by_target          # dropped
    assert "promo" not in by_target["-2002"]  # rewritten
    assert "code inside" in by_target["-2002"]


# --------------------------------------------------------------------------
# Per-source and per-target overrides
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_source_override_applies_only_to_that_source():
    """One task, two sources: source B is stricter than source A."""
    rule = make_rule(id="t-src", source_chat_id="-1001")
    rule.source_chat_ids = ["-1001", "-1002"]
    rule.source_policies = {"-1002": {"blacklist_words": ["leak"]}}
    pipeline, queue = make_pipeline([rule])

    ctx_a = await run(pipeline, chat="-1001", msg=61, text="nothing to leak here")
    assert not blocked(ctx_a)

    ctx_b = await run(pipeline, chat="-1002", msg=62, text="nothing to leak here")
    assert blocked(ctx_b, "blacklist:leak")

    # Same message, same task: the difference is the source, nothing else.
    assert [j["target"] for j in queue.jobs] == ["-2001"]


@pytest.mark.asyncio
async def test_per_target_override_skips_one_target_and_keeps_the_others():
    """A,B -> X,Y where Y refuses this message: X still gets it."""
    rule = make_rule(id="t-tgt")
    rule.target_chat_ids = ["-2001", "-2002", "-2003"]
    rule.target_policies = {"-2002": {"blacklist_words": ["spam"]}}
    pipeline, queue = make_pipeline([rule])

    await run(pipeline, msg=71, text="spam inside this one")
    assert sorted(j["target"] for j in queue.jobs) == ["-2001", "-2003"]

    queue.jobs.clear()
    await run(pipeline, msg=72, text="clean text")
    assert sorted(j["target"] for j in queue.jobs) == ["-2001", "-2002", "-2003"]


@pytest.mark.asyncio
async def test_per_target_rewrite_lets_each_target_get_its_own_text():
    rule = make_rule(id="t-tgt-rw")
    rule.target_chat_ids = ["-2001", "-2002"]
    rule.target_policies = {
        "-2002": {"remove_links": True, "replacements": {"secret": "***"}},
    }
    pipeline, queue = make_pipeline([rule])

    await run(pipeline, msg=81, text="visit https://x.com/a for the secret deal")
    by_target = {j["target"]: j["text"] for j in queue.jobs}
    assert "https://x.com/a" in by_target["-2001"]      # untouched
    assert "https://x.com/a" not in by_target["-2002"]  # stripped for this target
    assert "***" in by_target["-2002"]
    assert "secret" in by_target["-2001"]


@pytest.mark.asyncio
async def test_overrides_cannot_relax_the_task_policy():
    """An override adds constraints. It cannot whitelist a blacklisted word."""
    rule = make_rule(id="t-relax")
    rule.blacklist_words = ["scam"]
    rule.target_chat_ids = ["-2001", "-2002"]
    # The override tries to allow everything for -2002: it is still blocked,
    # because the task's own policy is the first link in the chain.
    rule.target_policies = {"-2002": {"enabled": False}}
    pipeline, queue = make_pipeline([rule])

    await run(pipeline, msg=91, text="scam link")
    assert queue.jobs == []


# --------------------------------------------------------------------------
# No limits
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_task_can_route_many_sources_to_many_targets():
    """x,y,z -> a,b,c: every target gets the message, in one task."""
    rule = make_rule(id="t-mn", source_chat_id="-1001")
    rule.source_chat_ids = ["-1001", "-1002", "-1003"]
    rule.target_chat_ids = ["-2001", "-2002", "-2003"]
    pipeline, queue = make_pipeline([rule])

    for i, chat in enumerate(["-1001", "-1002", "-1003"]):
        await run(pipeline, chat=chat, msg=100 + i, text=f"post {i}")

    assert len(queue.jobs) == 9
    assert sorted({j["target"] for j in queue.jobs}) == ["-2001", "-2002", "-2003"]


@pytest.mark.asyncio
async def test_no_cap_on_targets_sources_or_words():
    targets = [f"-2{i:04d}" for i in range(40)]
    rule = make_rule(id="t-big", source_chat_id="-1001", target_chat_id=targets[0])
    rule.source_chat_ids = ["-1001"] + [f"-1{i:04d}" for i in range(1, 50)]
    rule.target_chat_ids = targets
    rule.blacklist_words = [f"word{i}" for i in range(500)]
    pipeline, queue = make_pipeline([rule])

    ctx = await run(pipeline, msg=200, text="ordinary message")
    assert filter_stage(ctx).success is True
    assert len(queue.jobs) == 40

    ctx2 = await run(pipeline, msg=201, text="this one says word499")
    assert blocked(ctx2, "blacklist:word499")


# --------------------------------------------------------------------------
# Settings surface (what the bot, dashboard and app write)
# --------------------------------------------------------------------------


def test_settings_surface_round_trips_the_content_rules():
    """The registry is the single write path for every UI: keys must exist,
    validate, and read back as the same values."""
    rule = make_rule(id="t-set")
    changed = RS.apply_settings(rule, {
        "content_policy_enabled": True,
        "blacklist_words": ["a", "b"],
        "whitelist_words": ["c"],
        "whitelist_mode": "all",
        "remove_links": True,
        "strip_words": ["x"],
        "regex_block": [r"\bword\b"],
        "source_policies": {"-1001": {"blacklist_words": ["z"]}},
        "target_policies": {"-2001": {"remove_links": True}},
    })
    assert set(changed) >= {
        "content_policy_enabled", "blacklist_words", "whitelist_words",
        "whitelist_mode", "remove_links", "strip_words", "regex_block",
        "source_policies", "target_policies",
    }

    read = RS.read_settings(rule)
    assert read["blacklist_words"] == ["a", "b"]
    assert read["whitelist_mode"] == "all"
    assert read["source_policies"] == {"-1001": {"blacklist_words": ["z"]}}

    pol = rule.content_policy
    assert pol.evaluate("a").allowed is False        # blacklist honoured
    assert pol.evaluate("d").allowed is False        # whitelist "all" not satisfied
    assert pol.evaluate("c").allowed is True         # the one allowed word
    assert pol.transform("x marks it")[0] == "marks it"


def test_settings_validation_rejects_a_bogus_mode():
    rule = make_rule(id="t-val")
    with pytest.raises(RS.SettingError):
        RS.apply_settings(rule, {"whitelist_mode": "banana"})


@pytest.mark.asyncio
async def test_settings_written_through_the_registry_are_enforced():
    """What a UI writes is what the pipeline does — no second code path."""
    rule = make_rule(id="t-enforce")
    RS.apply_settings(rule, {
        "content_policy_enabled": True,
        "blacklist_words": ["forbidden"],
    })
    pipeline, queue = make_pipeline([rule])

    ctx = await run(pipeline, msg=300, text="this is forbidden")
    assert blocked(ctx, "blacklist:forbidden")

    ctx2 = await run(pipeline, msg=301, text="this is fine")
    assert not blocked(ctx2)


def test_content_rules_survive_the_draft_round_trip():
    """The bot and the app both persist through to_draft/apply_draft."""
    rule = make_rule(id="t-draft")
    rule.blacklist_words = ["x"]
    rule.target_policies = {"-2002": {"remove_links": True}}

    fresh = make_rule(id="t-draft")
    fresh.apply_draft(rule.to_draft())

    assert fresh.blacklist_words == ["x"]
    assert fresh.target_policies == {"-2002": {"remove_links": True}}
    assert fresh.content_policy.evaluate("x").allowed is False

    # A draft written by a UI that only sends the nested blob also works.
    other = make_rule(id="t-draft2")
    other.apply_draft({"content_policy": {"blacklist_words": ["y"], "remove_links": True}})
    assert other.blacklist_words == ["y"]
    assert other.content_policy.remove_links is True

# --------------------------------------------------------------------------
# One fact, two spellings: the target list and the mirrored primary
# --------------------------------------------------------------------------


def test_a_target_list_always_mirrors_its_first_entry():
    """Every documented write path leaves `target_chat_id` = first target.

    The dispatcher reads `all_targets` (scalar first, then the list), so a task
    whose list is set but whose primary is empty would route correctly while
    every legacy reader — and `destination_chat_id` — saw nothing.
    """
    built = ForwardRule(id="t", name="n", target_chat_ids=["-2001", "-2002"])
    assert built.target_chat_ids == ["-2001", "-2002"]
    assert built.target_chat_id == "-2001"
    assert built.destination_chat_id == "-2001"

    via_method = ForwardRule(id="t2", name="n", source_chat_id="-1001")
    via_method.set_targets(["-3001", "-3001", "-3002"], names=["First", "Second"])
    assert via_method.target_chat_ids == ["-3001", "-3002"], "deduped, order kept"
    assert via_method.target_chat_id == "-3001"
    assert via_method.target_chat_name == "First"

    via_draft = ForwardRule(id="t3", name="n")
    via_draft.apply_draft({"target_chat_ids": ["-4001", "-4002"]})
    assert via_draft.target_chat_id == "-4001"
    assert via_draft.all_targets == ["-4001", "-4002"]

    # the draft is authoritative: naming only the list makes its first entry the
    # primary (that list *is* the whole route)...
    routed = ForwardRule(id="t4", name="n", target_chat_id="-5000")
    routed.apply_draft({"target_chat_ids": ["-5001", "-5002"]})
    assert routed.target_chat_id == "-5001"
    assert routed.all_targets == ["-5001", "-5002"]

    # ...and a client that sends both (as the app and the API do) keeps the
    # scalar it sent, with the extra targets after it.
    kept = ForwardRule(id="t5", name="n", target_chat_id="-6000")
    kept.apply_draft({"target_chat_id": "-6000", "target_chat_ids": ["-6000", "-6001"]})
    assert kept.target_chat_id == "-6000"
    assert kept.all_targets == ["-6000", "-6001"]

    # A bare assignment of the list cannot be intercepted on a plain field, so
    # the destination read falls back to the list rather than going empty.
    bare = ForwardRule(id="t6", name="n", source_chat_id="-1001")
    bare.target_chat_ids = ["-7001", "-7002"]
    assert bare.destination_chat_id == "-7001"
    assert bare.all_targets == ["-7001", "-7002"]
