"""The branding hop (A -> C -> B) composed with the rest of the pipeline.

The hop is only useful if it composes: the message that lands in C must be the
*processed* message (cleaner, Replace rules, content policies, AI rewrite,
header/footer, watermark), and the worker must reproduce C without applying any
of those transforms a second time.

These tests therefore run the real production path end to end —

    pipeline (transforms + hop)  ->  durable queue (job state)  ->  worker (stage 2)

— with only the Telegram client and the repositories faked, and pin the four
failure modes that composition can have:

* the hop posts the *raw* text (transforms skipped for branded rules),
* the worker re-applies a transform (Replace/AI run twice),
* the hop changes the deliverable (media added to a TEXT_ONLY rule, caption
  added to a MEDIA_ONLY one),
* a hop miss drops the message instead of falling back to a direct delivery.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any, Dict, List, Optional

import pytest

from core.application.branding_hop import post_to_channel
from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import DeliveryJob, ForwardRule, MessagePayload
from core.domain.services import FilterEngine, RoutingPolicy
from core.application.pipeline import PipelineStage
from core.domain.value_objects import ContentMode, ForwardMode
from core.infrastructure.telegram.durable_queue import DurableQueueManager

C_CHANNEL = "-1001789"
B_TARGET = "-1007777"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class FakeClient:
    """Records every call Telegram would have received."""

    def __init__(self) -> None:
        self.forwards: List[Dict[str, Any]] = []
        self.copies: List[Dict[str, Any]] = []
        self.sends: List[Dict[str, Any]] = []
        self.groups: List[Dict[str, Any]] = []
        self.fail_copy_with: Optional[Exception] = None
        self.fail_group_with: Optional[Exception] = None
        self._next_id = 900

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send_message(self, chat_id: Any, text: str, **kwargs) -> Any:
        mid = self._id()
        self.sends.append({"chat_id": str(chat_id), "text": text, "id": mid, "kwargs": kwargs})
        return types.SimpleNamespace(id=mid)

    async def copy_message(self, chat_id: Any, from_chat_id: Any, message_id: int, **kwargs) -> Any:
        if self.fail_copy_with is not None and str(chat_id) == C_CHANNEL:
            raise self.fail_copy_with
        mid = self._id()
        self.copies.append({
            "chat_id": str(chat_id), "from_chat_id": str(from_chat_id),
            "message_id": int(message_id), "id": mid, "kwargs": kwargs,
        })
        return types.SimpleNamespace(id=mid)

    async def copy_media_group(self, chat_id: Any, from_chat_id: Any, message_id: int, **kwargs) -> Any:
        if self.fail_group_with is not None and str(chat_id) == C_CHANNEL:
            raise self.fail_group_with
        mids = [self._id() for _ in range(3)]
        self.groups.append({
            "chat_id": str(chat_id), "from_chat_id": str(from_chat_id),
            "message_id": int(message_id), "ids": mids, "kwargs": kwargs,
        })
        return [types.SimpleNamespace(id=i) for i in mids]

    async def forward_messages(self, chat_id: Any, from_chat_id: Any, message_ids: List[int], **kwargs) -> Any:
        mid = self._id()
        self.forwards.append({
            "chat_id": str(chat_id), "from_chat_id": str(from_chat_id),
            "message_ids": [int(m) for m in message_ids], "id": mid,
        })
        return [types.SimpleNamespace(id=mid)]

    @property
    def c_posts(self) -> List[Dict[str, Any]]:
        return [c for c in self.copies + self.sends if c["chat_id"] == C_CHANNEL]

    @property
    def c_groups(self) -> List[Dict[str, Any]]:
        return [g for g in self.groups if g["chat_id"] == C_CHANNEL]


class FakePool:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def get(self, session_id: str) -> FakeClient:
        return self.client


class FakeRuleRepo:
    def __init__(self, rule: ForwardRule) -> None:
        self.rule = rule

    async def list_active_by_source(self, chat_id: str) -> List[ForwardRule]:
        return [self.rule]


class FakeFilterRepo:
    async def get_by_id(self, rule_id: str):  # noqa: D102 - unused in these tests
        return None


class FakeProcessedRepo:
    async def was_processed(self, *args, **kwargs) -> bool:  # noqa: D102
        return False

    async def mark_processed(self, *args, **kwargs) -> None:  # noqa: D102
        return None


class FakeMapRepo:
    def __init__(self, already_seen: bool = False) -> None:
        self.seen = already_seen

    async def was_content_forwarded(self, *args, **kwargs) -> bool:  # noqa: D102
        return self.seen

    async def record_content_hash(self, *args, **kwargs) -> None:  # noqa: D102
        return None

    async def claim_content_hash(self, *args, **kwargs) -> bool:  # noqa: D102
        # Pre-fix semantics: the pipeline still consults the old check; a store
        # that has only the read-only interface is honoured by the pipeline.
        return not self.seen

    async def release_content_hash(self, *args, **kwargs) -> None:  # noqa: D102
        return None


class FakeQueue:
    """The durable queue's enqueue interface, capturing the hop job state."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text, evaluation=None,
                               job_kind="forward", correlation_id="", branding=None) -> bool:
        self.calls.append({
            "target": str(target_chat_id), "text": text, "job_kind": job_kind,
            "branding": dict(branding) if branding else None,
            "source_chat_id": str(payload.chat_id),
        })
        return True

    def branding_for(self, target: str) -> Optional[Dict[str, Any]]:
        for call in self.calls:
            if call["target"] == target:
                return call["branding"]
        return None

    def text_for(self, target: str) -> str:
        for call in self.calls:
            if call["target"] == target:
                return call["text"]
        return ""


class FakeAIRepo:
    """Mirrors ``IAIConfigRepository``: the engine reads a config owner-scoped.

    ``get_owned`` is part of the interface (Part 2 STEER 10), so a double that
    only implements ``get_by_id`` makes the pipeline's owner-scoped read raise,
    which its handler correctly treats as "AI unavailable" and drops the
    message. That is the fail-closed path working — and it means the test would
    no longer be testing the hop at all.
    """

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg

    async def get_by_id(self, cfg_id: str) -> Any:
        return self.cfg

    async def get_owned(self, cfg_id: str, owner_user_id: int = 0) -> Any:
        return self.cfg


class FakeAIProvider:
    """Counts rewrites: the pipeline must call it exactly once per message."""

    def __init__(self, output: str = "REWRITTEN", calls: Optional[List[str]] = None) -> None:
        self.output = output
        self.calls = calls if calls is not None else []

    async def rewrite(self, cfg: Any, text: str) -> str:
        self.calls.append(text)
        return self.output


class FakeAIFactory:
    def __init__(self, provider: FakeAIProvider) -> None:
        self.provider = provider

    def get(self, cfg: Any) -> FakeAIProvider:
        return self.provider


def _rule(**kw) -> ForwardRule:
    base = dict(
        id="rule-bh", session_id="sess-1",
        source_chat_id="-1001234", target_chat_id=B_TARGET,
        use_intermediate=True, intermediate_channel_id=C_CHANNEL,
    )
    base.update(kw)
    return ForwardRule(**base)


def _media_payload(text: str = "hello world", caption: Optional[str] = None,
                   message_id: int = 5, entities: Optional[List[Any]] = None) -> MessagePayload:
    payload = MessagePayload(chat_id="-1001234", message_id=message_id, text=text)
    payload.has_media = True
    payload.media_type = "photo"
    if caption is not None:
        payload.caption = caption
    if entities is not None:
        payload.caption_entities = entities
    return payload


def _album_payload(caption: str = "album caption", message_id: int = 20,
                   count: int = 3) -> MessagePayload:
    """What the album aggregator hands downstream: the first message plus the rest."""
    payload = MessagePayload(chat_id="-1001234", message_id=message_id, text=caption)
    payload.has_media = True
    payload.media_type = "photo"
    payload.caption = caption
    payload.media_group_id = f"g{message_id}"
    payload.album_messages = [
        types.SimpleNamespace(message_id=message_id + i) for i in range(count)
    ]
    return payload


def _client() -> FakeClient:
    return FakeClient()


def _performer(client: FakeClient):
    """The real dispatcher — the production hop performer."""
    from core.infrastructure.telegram.dispatcher import MessageDispatcher
    from core.domain.services import SmartRoutingEngine

    use_case = types.SimpleNamespace(sender=None, _routing=SmartRoutingEngine())
    return MessageDispatcher(pool=FakePool(client), use_case=use_case)


def _pipeline(rule: ForwardRule, client: FakeClient, queue: Optional[FakeQueue] = None,
              seen: bool = False, **kw):
    pipeline = DurableMessagePipeline(
        rule_repo=FakeRuleRepo(rule),
        filter_repo=FakeFilterRepo(),
        queue_manager=queue if queue is not None else FakeQueue(),
        msg_map_repo=FakeMapRepo(already_seen=seen),
        processed_repo=FakeProcessedRepo(),
        filter_engine=FilterEngine(),
        routing_policy=RoutingPolicy(),
        pool=FakePool(client),
        **kw,
    )
    pipeline.set_branding_hop(_performer(client))
    return pipeline


class WorkerStub(DurableQueueManager):
    """The worker's send path without spinning up the whole manager."""

    def __init__(self) -> None:  # noqa: D107 - deliberately no base init
        pass


async def _run(pipeline, payload):
    from core.application.pipeline import Normalizer

    return await pipeline.process(Normalizer.normalize(payload), trigger="NEW_MESSAGE")


async def _drain(worker: WorkerStub, client: FakeClient, job: DeliveryJob) -> int:
    return await worker._dispatch_to_telegram(client, job)


def _job_for(rule: ForwardRule, payload: MessagePayload, queue: FakeQueue, worker_text: str) -> DeliveryJob:
    """Rebuild the job the durable queue would have persisted for target B."""
    branding = queue.branding_for(B_TARGET)
    return DeliveryJob(
        id=f"{rule.id}:{payload.chat_id}:{payload.message_id}:{B_TARGET}",
        rule_id=rule.id,
        source_chat_id=str(payload.chat_id),
        source_message_id=int(payload.message_id),
        target_chat_id=B_TARGET,
        payload_data={
            "forward_mode": ForwardMode.COPY_MESSAGE.value,
            "content_mode": "AUTO",
            "text": worker_text,
            "has_media": bool(payload.has_media),
            "media_type": "photo",
            "branding": branding,
        },
    )


# --------------------------------------------------------------------------- #
# The hop carries the processed message, and only once
# --------------------------------------------------------------------------- #
class TestHopCarriesTheProcessedText:
    @pytest.mark.asyncio
    async def test_replace_rules_are_visible_in_c(self):
        """A branded rule must not deliver raw text: C holds the replaced text."""
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        rule.replacements = {"world": "moon"}
        payload = _media_payload("hello world")
        pipeline = _pipeline(rule, client, queue)

        await _run(pipeline, payload)

        assert len(client.c_posts) == 1, "exactly one post into C"
        post = client.c_posts[0]
        assert post["kwargs"].get("caption") == "hello moon", \
            "C must hold the *processed* caption, not the raw one"
        assert post["from_chat_id"] == "-1001234", "C's copy is taken from the source message"
        # ... and the queue carries the same processed text for B.
        assert queue.text_for(B_TARGET) == "hello moon"

    @pytest.mark.asyncio
    async def test_ai_rewrite_output_is_what_c_holds_and_it_runs_once(self):
        ai_calls: List[str] = []
        client = _client()
        queue = FakeQueue()
        rule = _rule(ai_config_id="cfg-1")
        payload = MessagePayload(chat_id="-1001234", message_id=7, text="original body")
        provider = FakeAIProvider(output="REWRITTEN BODY", calls=ai_calls)
        pipeline = _pipeline(rule, client, queue,
                             ai_repo=FakeAIRepo(types.SimpleNamespace(is_enabled=True)),
                             ai_factory=FakeAIFactory(provider))

        await _run(pipeline, payload)

        assert ai_calls == ["original body"], "the AI must run once, on the original text"
        post = client.c_posts[0]
        assert post["text"] == "REWRITTEN BODY", "C must hold the rewritten text"
        assert queue.text_for(B_TARGET) == "REWRITTEN BODY"

        # The worker reproduces C and never touches the AI again.
        job = _job_for(rule, payload, queue, queue.text_for(B_TARGET))
        await _drain(WorkerStub(), client, job)
        assert ai_calls == ["original body"], "the worker must not rewrite a second time"
        assert [s for s in client.sends if s["chat_id"] == B_TARGET] == [], \
            "the worker must forward C's post, not re-send the text"
        assert client.forwards[-1]["from_chat_id"] == C_CHANNEL

    @pytest.mark.asyncio
    async def test_header_and_replacements_are_applied_exactly_once_end_to_end(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        rule.header = "[MY BRAND]"
        rule.replacements = {"old": "new"}
        payload = MessagePayload(chat_id="-1001234", message_id=8, text="old body")
        pipeline = _pipeline(rule, client, queue)

        await _run(pipeline, payload)

        expected = "[MY BRAND]\n\nnew body"
        assert client.c_posts[0]["text"] == expected
        assert queue.text_for(B_TARGET) == expected

        job = _job_for(rule, payload, queue, expected)
        await _drain(WorkerStub(), client, job)

        # Stage 2 is a forward of C, so the header/body is not emitted again.
        assert [s for s in client.sends if s["chat_id"] == B_TARGET] == []
        forwarded = [f for f in client.forwards if f["chat_id"] == B_TARGET]
        assert forwarded and forwarded[0]["from_chat_id"] == C_CHANNEL

    @pytest.mark.asyncio
    async def test_entities_survive_when_no_transform_changed_the_text(self):
        entity = types.SimpleNamespace(type="bold", offset=0, length=5)
        client = _client()
        rule = _rule()
        payload = _media_payload("hello world", caption="hello world", entities=[entity])
        pipeline = _pipeline(rule, client)

        await _run(pipeline, payload)

        post = client.c_posts[0]
        assert post["kwargs"].get("caption_entities") == [entity], \
            "unchanged text keeps its formatting in C (and therefore in B's forward)"

    @pytest.mark.asyncio
    async def test_entities_are_dropped_when_a_transform_invalidates_the_offsets(self):
        entity = types.SimpleNamespace(type="bold", offset=0, length=5)
        client = _client()
        rule = _rule()
        rule.replacements = {"hello": "greetings"}
        payload = _media_payload("hello world", caption="hello world", entities=[entity])

        await _run(_pipeline(rule, client), payload)

        post = client.c_posts[0]
        assert post["kwargs"].get("caption") == "greetings world"
        assert "caption_entities" not in post["kwargs"], \
            "offsets from the old text would bold the wrong characters"


# --------------------------------------------------------------------------- #
# The hop must not change *what* the target receives
# --------------------------------------------------------------------------- #
class TestHopKeepsTheDeliverable:
    @pytest.mark.asyncio
    async def test_text_only_rule_posts_text_into_c_even_for_a_media_source(self):
        client = _client()
        rule = _rule(content_mode=ContentMode.TEXT_ONLY)
        payload = _media_payload("caption text")

        await _run(_pipeline(rule, client), payload)

        assert client.copies == [], "a TEXT_ONLY task must not put media in C"
        assert [s["text"] for s in client.sends] == ["caption text"]

    @pytest.mark.asyncio
    async def test_media_only_rule_posts_media_without_a_caption_into_c(self):
        client = _client()
        rule = _rule(content_mode=ContentMode.MEDIA_ONLY)
        payload = _media_payload("caption text")

        await _run(_pipeline(rule, client), payload)

        assert client.sends == []
        assert client.copies[0]["kwargs"].get("caption") == "", \
            "MEDIA_ONLY means C carries no caption either"

    @pytest.mark.asyncio
    async def test_caption_overflow_travels_as_a_second_post(self):
        """A caption longer than Telegram's media limit is split, not truncated."""
        client = _client()
        rule = _rule()
        payload = _media_payload("x" * 1500)

        plan = await post_to_channel(client, payload, rule, "x" * 1500)

        assert len(client.c_posts) == 2, "the caption tail is a follow-up post in C"
        assert len(plan["message_ids"]) == 2, "both posts are handed to the queue"
        # And the worker forwards both of them.
        job = _job_for(rule, payload, FakeQueue(), "x" * 1024)
        job.payload_data["branding"] = plan
        await _drain(WorkerStub(), client, job)
        assert client.forwards[-1]["message_ids"] == plan["message_ids"]

    @pytest.mark.asyncio
    async def test_pipeline_truncates_a_media_caption_before_the_hop(self):
        """Telegram rejects captions over 1024, so the hop never posts one."""
        client = _client()
        queue = FakeQueue()
        payload = _media_payload("y" * 1500)

        await _run(_pipeline(_rule(), client, queue), payload)

        assert len(client.c_posts) == 1
        posted = client.c_posts[0]["kwargs"].get("caption") or ""
        assert len(posted) <= 1024


# --------------------------------------------------------------------------- #
# The other gates: a message that is not delivered is not branded either
# --------------------------------------------------------------------------- #
class TestGatesRunBeforeTheHop:
    @pytest.mark.asyncio
    async def test_blacklisted_message_is_never_posted_into_c(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        rule.blacklist_words = ["casino"]

        ctx = await _run(_pipeline(rule, client, queue), MessagePayload(
            chat_id="-1001234", message_id=30, text="Big casino news today"))

        blocked = [r for r in ctx.stage_results
                   if r.stage == PipelineStage.FILTER and not r.success]
        assert blocked and "blacklist" in (blocked[0].reason or ""), "the filter must have stopped it"
        assert client.c_posts == [], "a blocked message must not appear in our channel"
        assert queue.calls == []

    @pytest.mark.asyncio
    async def test_whitelist_miss_is_never_posted_into_c(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        rule.whitelist_words = ["urgent"]
        rule.content_policy_enabled = True

        await _run(_pipeline(rule, client, queue), MessagePayload(
            chat_id="-1001234", message_id=31, text="ordinary market update"))

        assert client.c_posts == []
        assert queue.calls == []

    @pytest.mark.asyncio
    async def test_a_duplicate_is_not_posted_into_c_twice(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        rule.duplicate_filters = True

        await _run(_pipeline(rule, client, queue, seen=True), MessagePayload(
            chat_id="-1001234", message_id=32, text="hello world"))

        assert client.c_posts == [], "dedupe happened first, so C never saw it"
        assert queue.calls == []

    @pytest.mark.asyncio
    async def test_a_paused_rule_posts_nothing_into_c(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule(is_paused=True)

        await _run(_pipeline(rule, client, queue), MessagePayload(
            chat_id="-1001234", message_id=33, text="hello world"))

        assert client.c_posts == []
        assert queue.calls == []


# --------------------------------------------------------------------------- #
# Media handling: an album stays an album (hop and worker alike)
# --------------------------------------------------------------------------- #
class TestAlbumComposition:
    @pytest.mark.asyncio
    async def test_pipeline_hops_an_album_as_a_group(self):
        client = _client()
        queue = FakeQueue()
        payload = _album_payload()

        await _run(_pipeline(_rule(), client, queue), payload)

        assert len(client.c_groups) == 1, "an album is one grouped post into C"
        group = client.c_groups[0]
        assert group["message_id"] == payload.message_id
        assert group["kwargs"].get("captions") is None, "an unchanged caption is kept as-is"
        plan = queue.branding_for(B_TARGET)
        assert len(plan["message_ids"]) == 3, "every item of the group is handed to the queue"

    @pytest.mark.asyncio
    async def test_transformed_caption_reaches_c_and_keeps_the_other_items(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        rule.replacements = {"album": "gallery"}
        payload = _album_payload("album caption")

        await _run(_pipeline(rule, client, queue), payload)

        captions = client.c_groups[0]["kwargs"].get("captions")
        assert captions == ["gallery caption", None, None], \
            "the first item carries the processed caption, the rest keep their own"
        assert queue.text_for(B_TARGET) == "gallery caption"

    @pytest.mark.asyncio
    async def test_media_only_album_loses_every_caption_in_c(self):
        client = _client()
        rule = _rule(content_mode=ContentMode.MEDIA_ONLY)

        await _run(_pipeline(rule, client), _album_payload())

        assert client.c_groups[0]["kwargs"].get("captions") == "", \
            "MEDIA_ONLY means the group carries no caption either"

    @pytest.mark.asyncio
    async def test_text_only_album_is_text_in_c_with_no_media(self):
        client = _client()
        rule = _rule(content_mode=ContentMode.TEXT_ONLY)

        await _run(_pipeline(rule, client), _album_payload("album caption"))

        assert client.c_groups == [] and client.copies == []
        assert [s["text"] for s in client.sends] == ["album caption"]

    @pytest.mark.asyncio
    async def test_album_copy_refusal_falls_back_to_the_album_re_upload(self):
        client = _client()
        client.fail_group_with = RuntimeError("CHAT_FORWARDS_RESTRICTED")
        rule = _rule()
        calls: List[tuple] = []

        async def _reupload_album(target, caption, mode_val):
            calls.append((target, caption, mode_val))
            return [501, 502, 503]

        plan = await post_to_channel(
            client, _album_payload(), rule, "album caption", reupload_album=_reupload_album,
        )

        assert calls and calls[0][1] == "album caption"
        assert plan["message_ids"] == [501, 502, 503], "the whole album travels to stage 2"

    @pytest.mark.asyncio
    async def test_album_degrades_to_the_first_item_when_nothing_album_aware_works(self):
        client = _client()
        client.fail_group_with = RuntimeError("CHAT_FORWARDS_RESTRICTED")
        plan = await post_to_channel(client, _album_payload(), _rule(), "album caption")

        assert plan is not None, "a hop miss must never become a dropped album"
        assert len(client.copies) == 1 and client.copies[0]["from_chat_id"] == "-1001234"

    @pytest.mark.asyncio
    async def test_worker_forwards_the_whole_album_from_c(self):
        client = _client()
        job = DeliveryJob(
            id="j5", rule_id="rule-bh", source_chat_id="-1001234", source_message_id=20,
            target_chat_id=B_TARGET,
            payload_data={
                "forward_mode": ForwardMode.COPY_MESSAGE.value, "content_mode": "AUTO",
                "text": "album caption", "has_media": True, "media_type": "photo",
                "media_group_id": "g20", "album_message_ids": [20, 21, 22],
                "branding": {"chat_id": C_CHANNEL, "message_id": 88,
                             "message_ids": [88, 89, 90], "force_forward": True},
            },
        )

        await _drain(WorkerStub(), client, job)

        assert client.forwards[-1]["from_chat_id"] == C_CHANNEL
        assert client.forwards[-1]["message_ids"] == [88, 89, 90], \
            "stage 2 forwards C's group, not the source album"


# --------------------------------------------------------------------------- #
# Triggers, fallbacks, and never delivering to C twice
# --------------------------------------------------------------------------- #
class TestHopsTriggersAndFallbacks:
    @pytest.mark.asyncio
    async def test_gated_out_message_is_delivered_directly(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        rule.branding_triggers_enabled = True
        rule.branding_keywords = ["promo"]
        payload = MessagePayload(chat_id="-1001234", message_id=9, text="nothing to see")

        await _run(_pipeline(rule, client, queue), payload)

        assert client.c_posts == [], "a trigger miss must post nothing into C"
        assert queue.branding_for(B_TARGET) is None
        assert queue.calls and queue.calls[0]["target"] == B_TARGET

    @pytest.mark.asyncio
    async def test_matching_trigger_takes_the_hop(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        rule.branding_triggers_enabled = True
        rule.branding_keywords = ["promo"]
        payload = MessagePayload(chat_id="-1001234", message_id=10, text="PROMO today")

        await _run(_pipeline(rule, client, queue), payload)

        assert len(client.c_posts) == 1
        assert queue.branding_for(B_TARGET)["chat_id"] == C_CHANNEL

    @pytest.mark.asyncio
    async def test_c_is_never_delivered_to_as_a_target(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule(target_chat_ids=[B_TARGET, C_CHANNEL, "-1008888"])
        payload = MessagePayload(chat_id="-1001234", message_id=11, text="hello")

        await _run(_pipeline(rule, client, queue), payload)

        assert len(client.c_posts) == 1, "C gets the hop post, not a second copy as a target"
        assert [c["target"] for c in queue.calls] == [B_TARGET, "-1008888"]

    @pytest.mark.asyncio
    async def test_two_targets_share_one_post_into_c(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule(target_chat_ids=[B_TARGET, "-1008888"])
        payload = MessagePayload(chat_id="-1001234", message_id=12, text="hello")

        await _run(_pipeline(rule, client, queue), payload)

        assert len(client.c_posts) == 1, "identical deliveries must not post twice into C"
        plans = {c["target"]: c["branding"] for c in queue.calls}
        assert plans[B_TARGET]["message_id"] == plans["-1008888"]["message_id"]

    @pytest.mark.asyncio
    async def test_hop_failure_falls_back_to_a_direct_delivery(self):
        """A Telegram failure in C must never drop the message."""
        client = _client()
        client.fail_copy_with = RuntimeError("CHAT_FORWARDS_RESTRICTED: copy blocked")
        queue = FakeQueue()
        rule = _rule()
        payload = _media_payload("hello world")

        await _run(_pipeline(rule, client, queue), payload)

        # The performer's re-upload layer may still have posted into C; either
        # way the message is delivered and never dropped.
        assert queue.calls and queue.calls[0]["target"] == B_TARGET

    @pytest.mark.asyncio
    async def test_no_live_client_falls_back_to_a_direct_delivery(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        pipeline = _pipeline(rule, client, queue)
        pipeline._branding_hop = _performer(client)
        pipeline._branding_hop._pool = types.SimpleNamespace(get=lambda sid: None)

        await _run(pipeline, MessagePayload(chat_id="-1001234", message_id=13, text="hello"))

        assert queue.calls and queue.calls[0]["branding"] is None
        assert queue.calls[0]["target"] == B_TARGET

    @pytest.mark.asyncio
    async def test_hop_is_skipped_when_c_is_the_only_or_own_target(self):
        client = _client()
        queue = FakeQueue()
        rule = _rule(target_chat_id=C_CHANNEL, target_chat_ids=[C_CHANNEL])

        await _run(_pipeline(rule, client, queue), MessagePayload(chat_id="-1001234", message_id=14, text="hi"))

        assert client.c_posts == [], "no stage 2 exists, so C must not be posted into"
        assert [c["target"] for c in queue.calls] == [C_CHANNEL]

    @pytest.mark.asyncio
    async def test_unwired_pipeline_delivers_directly(self):
        """Without the performer a branded rule must still deliver (never drop)."""
        client = _client()
        queue = FakeQueue()
        rule = _rule()
        pipeline = DurableMessagePipeline(
            rule_repo=FakeRuleRepo(rule), filter_repo=FakeFilterRepo(),
            queue_manager=queue, msg_map_repo=FakeMapRepo(), processed_repo=FakeProcessedRepo(),
        )

        await _run(pipeline, MessagePayload(chat_id="-1001234", message_id=15, text="hello"))

        assert client.c_posts == []
        assert queue.calls and queue.calls[0]["branding"] is None


    @pytest.mark.asyncio
    async def test_a_keyword_reply_is_not_posted_into_c(self):
        """A reply is our own plain text — it has no forward header to brand."""
        client = _client()
        queue = FakeQueue()
        rule = _rule(target_chat_ids=[B_TARGET, C_CHANNEL])
        rule.keyword_confirmation = True
        rule.keyword_reply = "thanks for reading"
        rule.whitelist = ["urgent"]
        rule.metadata["paid_addons"] = {"keyword_reply": True}

        await _run(_pipeline(rule, client, queue), MessagePayload(
            chat_id="-1001234", message_id=40, text="urgent market update"))

        replies = [c for c in queue.calls if c["job_kind"] == "keyword_reply"]
        assert [c["target"] for c in replies] == [B_TARGET], \
            "the reply follows the forward's destinations minus C"
        forwards = [c for c in queue.calls if c["job_kind"] == "forward"]
        assert [c["target"] for c in forwards] == [B_TARGET]
        assert len(client.c_posts) == 1, "only the hop post lands in C, never the reply"


# --------------------------------------------------------------------------- #
# Worker: reproduce C, never re-process
# --------------------------------------------------------------------------- #
class TestWorkerReproducesCWithoutReprocessing:
    @pytest.mark.asyncio
    async def test_native_forward_carries_every_post_of_the_hop(self):
        client = _client()
        worker = WorkerStub()
        job = DeliveryJob(
            id="j1", rule_id="rule-bh", source_chat_id="-1001234", source_message_id=5,
            target_chat_id=B_TARGET,
            payload_data={
                "forward_mode": ForwardMode.COPY_MESSAGE.value, "content_mode": "AUTO",
                "text": "processed", "has_media": True, "media_type": "photo",
                "branding": {"chat_id": C_CHANNEL, "message_id": 77,
                             "message_ids": [77, 78], "force_forward": True},
            },
        )

        await _drain(worker, client, job)

        assert client.forwards == [{
            "chat_id": B_TARGET, "from_chat_id": C_CHANNEL,
            "message_ids": [77, 78], "id": client.forwards[0]["id"],
        }]

    @pytest.mark.asyncio
    async def test_copy_fallback_keeps_c_caption_and_entities(self):
        """Re-sending `text` here would apply the content a second time."""
        client = _client()
        worker = WorkerStub()
        job = DeliveryJob(
            id="j2", rule_id="rule-bh", source_chat_id="-1001234", source_message_id=5,
            target_chat_id=B_TARGET,
            payload_data={
                "forward_mode": ForwardMode.COPY_MESSAGE.value, "content_mode": "AUTO",
                "text": "processed", "has_media": True, "media_type": "photo",
                "branding": {"chat_id": C_CHANNEL, "message_id": 77, "force_forward": False},
            },
        )

        await _drain(worker, client, job)

        assert [c["from_chat_id"] for c in client.copies] == [C_CHANNEL]
        assert "caption" not in client.copies[0]["kwargs"], \
            "no caption override: C's caption and entities are reproduced verbatim"
        assert [s for s in client.sends if s["chat_id"] == B_TARGET] == []

    @pytest.mark.asyncio
    async def test_copy_fallback_honours_media_only(self):
        client = _client()
        worker = WorkerStub()
        job = DeliveryJob(
            id="j3", rule_id="rule-bh", source_chat_id="-1001234", source_message_id=5,
            target_chat_id=B_TARGET,
            payload_data={
                "forward_mode": ForwardMode.COPY_MESSAGE.value, "content_mode": "MEDIA_ONLY",
                "text": "processed", "has_media": True, "media_type": "photo",
                "branding": {"chat_id": C_CHANNEL, "message_id": 77, "force_forward": False},
            },
        )

        await _drain(worker, client, job)

        assert client.copies[0]["kwargs"].get("caption") == ""

    @pytest.mark.asyncio
    async def test_branded_text_job_copies_c_not_the_source(self):
        client = _client()
        worker = WorkerStub()
        job = DeliveryJob(
            id="j4", rule_id="rule-bh", source_chat_id="-1001234", source_message_id=5,
            target_chat_id=B_TARGET,
            payload_data={
                "forward_mode": ForwardMode.COPY_MESSAGE.value, "content_mode": "TEXT_ONLY",
                "text": "processed", "has_media": False,
                "branding": {"chat_id": C_CHANNEL, "message_id": 77, "force_forward": False},
            },
        )

        await _drain(worker, client, job)

        assert client.sends == [s for s in client.sends if s["chat_id"] != B_TARGET]
        assert client.copies and client.copies[0]["from_chat_id"] == C_CHANNEL


# --------------------------------------------------------------------------- #
# The posting routine itself
# --------------------------------------------------------------------------- #
class TestPostToChannel:
    @pytest.mark.asyncio
    async def test_nothing_to_post_returns_none(self):
        client = _client()
        rule = _rule()
        payload = MessagePayload(chat_id="-1001234", message_id=5, text="")

        assert await post_to_channel(client, payload, rule, "") is None
        assert client.c_posts == []

    @pytest.mark.asyncio
    async def test_the_protection_fallback_is_used_when_a_copy_is_refused(self):
        client = _client()
        client.fail_copy_with = RuntimeError("CHAT_FORWARDS_RESTRICTED")
        rule = _rule()
        payload = _media_payload("hello world")
        calls: List[tuple] = []

        async def _reupload(target, caption, mode_val):
            calls.append((target, caption, mode_val))
            return types.SimpleNamespace(id=4242)

        plan = await post_to_channel(client, payload, rule, "hello world", reupload=_reupload)

        assert calls and calls[0][1] == "hello world", "the processed caption is re-uploaded"
        assert plan["message_id"] == 4242

    @pytest.mark.asyncio
    async def test_hop_never_posts_back_into_its_own_source(self):
        client = _client()
        rule = _rule(source_chat_id=C_CHANNEL)
        payload = MessagePayload(chat_id=C_CHANNEL, message_id=5, text="hi")

        assert await post_to_channel(client, payload, rule, "hi") is None
        assert client.c_posts == []


# --------------------------------------------------------------------------- #
# Wiring: the hop must survive being deployed, not just tested
# --------------------------------------------------------------------------- #
def test_main_wires_the_hop_into_the_durable_pipeline():
    """The three halves existed before and never met: guard the wiring itself."""
    from pathlib import Path

    source = Path("core/main.py").read_text(encoding="utf-8")
    assert "set_branding_hop(dispatcher)" in source, \
        "main.py must wire the dispatcher into the durable pipeline"
    assert source.index("set_branding_hop(dispatcher)") > source.index("dispatcher = MessageDispatcher("), \
        "the performer must be wired after the dispatcher exists"
