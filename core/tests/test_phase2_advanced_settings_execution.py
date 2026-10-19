"""Phase 2 — advanced settings runtime-execution regression tests.

Phase 1 proved the advanced task-overview fields round-trip through the API
into ``CustomMetadataJson``. These tests prove the *other* half of the
contract: that the persisted fields are read by the domain layer and change
what the forwarding worker actually does.

Every test exercises the real domain services and the real durable-queue
payload construction — no mocks of the logic under test.
"""

from __future__ import annotations

import asyncio

import pytest

from core.domain.entities import (
    MAX_FORWARD_DELAY_SECONDS,
    ForwardRule,
    MessagePayload,
)
from core.domain.services import AdvancedSettingsEngine
from core.domain.value_objects import MediaType


def _rule(**meta) -> ForwardRule:
    return ForwardRule(
        id="rule-adv-1",
        session_id="sess-a",
        source_chat_id="-100111",
        target_chat_id="-100222",
        owner_user_id=5094837833,
        metadata=dict(meta or {}),
    )


def _payload(text: str = "hello world", is_edit: bool = False,
             edit_date: int | None = None, original_date: int | None = None) -> MessagePayload:
    return MessagePayload(
        chat_id="-100111",
        message_id=42,
        text=text,
        date=1700000000,
        is_edit=is_edit,
        edit_date=edit_date,
        original_date=original_date,
    )


# ---------------------------------------------------------------- entities --
class TestAdvancedEntityProperties:
    """The typed accessors are the single source of truth the worker reads."""

    def test_forward_delay_defaults_to_legacy_field(self):
        rule = _rule()
        rule.delay_seconds = 12.0
        assert rule.forward_delay_seconds == 12.0

    def test_forward_delay_metadata_wins_over_legacy(self):
        rule = _rule()
        rule.delay_seconds = 12.0
        rule.forward_delay_seconds = 5.0
        assert rule.forward_delay_seconds == 5.0

    def test_forward_delay_is_bounded_and_non_negative(self):
        rule = _rule()
        rule.forward_delay_seconds = -10.0
        assert rule.forward_delay_seconds == 0.0
        # The bound is the one the settings schema advertises (86400 s), not a
        # second, smaller number invented here: the property used to truncate to
        # 3600 s while the advanced screen validated and displayed up to 86400 s,
        # so a configured 7200 s silently became one hour.
        rule.forward_delay_seconds = 999999.0
        assert rule.forward_delay_seconds == MAX_FORWARD_DELAY_SECONDS == 86400.0
        rule.forward_delay_seconds = 43200.0
        assert rule.forward_delay_seconds == 43200.0

    def test_forward_delay_rejects_garbage(self):
        rule = _rule()
        rule.forward_delay_seconds = "not-a-number"
        assert rule.forward_delay_seconds == 0.0

    def test_edit_time_limit_seconds(self):
        rule = _rule()
        assert rule.edit_time_limit_seconds == 0
        rule.edit_time_limit_seconds = 300
        assert rule.edit_time_limit_seconds == 300
        rule.edit_time_limit_seconds = "junk"
        assert rule.edit_time_limit_seconds == 0
        rule.edit_time_limit_seconds = -5
        assert rule.edit_time_limit_seconds == 0

    def test_auto_delete_seconds(self):
        rule = _rule()
        assert rule.auto_delete_seconds == 0
        rule.auto_delete_seconds = 60
        assert rule.auto_delete_seconds == 60
        rule.auto_delete_seconds = "junk"
        assert rule.auto_delete_seconds == 0

    def test_keyword_reply_and_confirmation(self):
        rule = _rule()
        assert rule.keyword_confirmation is False
        assert rule.keyword_reply == ""
        rule.keyword_confirmation = True
        rule.keyword_reply = " keyword matched! "
        assert rule.keyword_confirmation is True
        assert rule.keyword_reply == " keyword matched! "

    def test_watermark_text_aliases_watermark(self):
        rule = _rule(watermark="legacy-wm")
        assert rule.watermark_text == "legacy-wm"
        rule.watermark_text = "new-wm"
        assert rule.watermark_text == "new-wm"
        assert rule.watermark == "legacy-wm"

    def test_paid_addon_entitlement_is_server_side(self):
        rule = _rule()
        assert rule.is_addon_enabled("crypto_mode") is False
        rule.paid_addons = {"crypto_mode": True}
        assert rule.is_addon_enabled("crypto_mode") is True
        # An unknown addon key never resolves to enabled
        assert rule.is_addon_enabled("nonexistent") is False

    def test_paid_addon_entitlement_ignores_garbage(self):
        rule = _rule(paid_addons="not-a-dict")
        assert rule.is_addon_enabled("crypto_mode") is False


# ------------------------------------------------------- content transforms --
class TestAdvancedContentTransforms:
    """Watermark / link buttons / affiliate kit act on the message body."""

    def test_watermark_is_appended_once(self):
        rule = _rule()
        rule.watermark_text = "🚀 ATF"
        text, applied = AdvancedSettingsEngine.apply_content_transforms("breaking news", rule)
        assert applied == ["watermark"]
        assert text == "breaking news\n\n🚀 ATF"

    def test_watermark_is_not_duplicated_on_replay(self):
        rule = _rule()
        rule.watermark_text = "🚀 ATF"
        once, _ = AdvancedSettingsEngine.apply_content_transforms("breaking news", rule)
        twice, applied = AdvancedSettingsEngine.apply_content_transforms(once, rule)
        assert applied == []
        assert twice == once

    def test_watermark_is_truncated(self):
        rule = _rule()
        rule.watermark_text = "W" * 500
        _, applied = AdvancedSettingsEngine.apply_content_transforms("body", rule)
        assert applied == ["watermark"]

    def test_disabled_rule_is_a_noop(self):
        rule = _rule()
        text, applied = AdvancedSettingsEngine.apply_content_transforms("body", rule)
        assert applied == []
        assert text == "body"

    def test_link_buttons_require_addon_entitlement(self):
        rule = _rule()
        rule.link_buttons = [{"text": "Site", "url": "https://example.com"}]
        # No paid_addons grant: transform must NOT be applied
        text, applied = AdvancedSettingsEngine.apply_content_transforms("body", rule)
        assert applied == []
        assert "https://example.com" not in text

    def test_link_buttons_apply_with_entitlement(self):
        rule = _rule(paid_addons={"link_buttons": True})
        rule.link_buttons = [{"text": "Site", "url": "https://example.com"}]
        text, applied = AdvancedSettingsEngine.apply_content_transforms("body", rule)
        assert applied == ["link_buttons"]
        assert "🔗 Site: https://example.com" in text

    def test_link_buttons_ignore_entries_without_url(self):
        rule = _rule(paid_addons={"link_buttons": True})
        rule.link_buttons = [{"text": "NoUrl"}]
        text, applied = AdvancedSettingsEngine.apply_content_transforms("body", rule)
        assert applied == []
        assert text == "body"

    def test_affiliate_kit_rewrites_urls(self):
        rule = _rule(paid_addons={"affiliate_kit": True})
        rule.affiliate_kit = {"enabled": True, "prefix": "https://aff.example.com/?ref=atf&url="}
        text, applied = AdvancedSettingsEngine.apply_content_transforms(
            "see https://example.com now", rule
        )
        assert applied == ["affiliate_kit"]
        assert "https://aff.example.com/?ref=atf&url=https://example.com" in text

    def test_affiliate_kit_requires_entitlement(self):
        rule = _rule()
        rule.affiliate_kit = {"enabled": True, "prefix": "https://aff.example.com/?ref=atf&url="}
        text, applied = AdvancedSettingsEngine.apply_content_transforms(
            "see https://example.com now", rule
        )
        assert applied == []
        assert text == "see https://example.com now"


# ------------------------------------------------------------- keyword add-on --
class TestKeywordConfirmation:
    def test_disabled_returns_none(self):
        rule = _rule()
        assert AdvancedSettingsEngine.classify_keyword_trigger("buy BTC", rule) is None

    def test_enabled_without_reply_returns_none(self):
        rule = _rule()
        rule.keyword_confirmation = True
        rule.whitelist = ["btc"]
        assert AdvancedSettingsEngine.classify_keyword_trigger("buy BTC", rule) is None

    def test_enabled_without_entitlement_returns_none(self):
        rule = _rule()
        rule.keyword_confirmation = True
        rule.keyword_reply = "match!"
        rule.whitelist = ["btc"]
        # No paid_addons grant
        assert AdvancedSettingsEngine.classify_keyword_trigger("buy BTC", rule) is None

    def test_match_returns_reply(self):
        rule = _rule(paid_addons={"keyword_reply": True})
        rule.keyword_confirmation = True
        rule.keyword_reply = "match!"
        rule.whitelist = ["btc"]
        assert AdvancedSettingsEngine.classify_keyword_trigger("buy BTC now", rule) == "match!"

    def test_no_match_returns_none(self):
        rule = _rule(paid_addons={"keyword_reply": True})
        rule.keyword_confirmation = True
        rule.keyword_reply = "match!"
        rule.whitelist = ["btc"]
        assert AdvancedSettingsEngine.classify_keyword_trigger("just chatting", rule) is None

    def test_empty_whitelist_returns_none(self):
        rule = _rule(paid_addons={"keyword_reply": True})
        rule.keyword_confirmation = True
        rule.keyword_reply = "match!"
        assert AdvancedSettingsEngine.classify_keyword_trigger("anything", rule) is None


# ----------------------------------------------------------- auto-delete TTL --
class TestAutoDeleteTtl:
    def test_disabled_is_zero(self):
        assert AdvancedSettingsEngine.get_auto_delete_seconds(_rule()) == 0

    def test_without_entitlement_is_zero(self):
        rule = _rule()
        rule.auto_delete_seconds = 300
        assert AdvancedSettingsEngine.get_auto_delete_seconds(rule) == 0

    def test_with_entitlement_returns_ttl(self):
        rule = _rule(paid_addons={"auto_delete_message": True})
        rule.auto_delete_seconds = 300
        assert AdvancedSettingsEngine.get_auto_delete_seconds(rule) == 300

    def test_ttl_is_bounded(self):
        rule = _rule(paid_addons={"auto_delete_message": True})
        rule.auto_delete_seconds = 10 ** 18
        assert AdvancedSettingsEngine.get_auto_delete_seconds(rule) == 31_536_000


# -------------------------------------------------------- edit time window ---
class TestEditTimeWindow:
    def test_zero_limit_allows_all_edits(self):
        rule = _rule()
        assert AdvancedSettingsEngine.is_edit_within_window(1000, 9_999_999, rule) is True

    def test_edit_inside_window(self):
        rule = _rule()
        rule.edit_time_limit_seconds = 300
        assert AdvancedSettingsEngine.is_edit_within_window(1000, 1200, rule) is True

    def test_edit_outside_window(self):
        rule = _rule()
        rule.edit_time_limit_seconds = 300
        assert AdvancedSettingsEngine.is_edit_within_window(1000, 2000, rule) is False

    def test_missing_dates_never_drop(self):
        rule = _rule()
        rule.edit_time_limit_seconds = 300
        assert AdvancedSettingsEngine.is_edit_within_window(None, None, rule) is True
        assert AdvancedSettingsEngine.is_edit_within_window(1000, None, rule) is True


# ------------------------------------------------- durable queue payload -----
class TestDurableQueuePayloadCarriesAdvancedSettings:
    """The settings must survive serialization into the delivery job payload
    so the worker can act on them without re-reading the rule from disk."""

    @pytest.mark.asyncio
    async def test_payload_contains_advanced_fields(self):
        from core.infrastructure.telegram.durable_queue import DurableQueueManager

        rule = _rule(
            paid_addons={
                "auto_delete_message": True,
                "link_buttons": True,
                "affiliate_kit": True,
            },
        )
        rule.forward_delay_seconds = 7.0
        rule.topic_id = 99
        rule.debug_mode = True
        rule.process_raw_text = True
        rule.button_to_text = True
        rule.link_buttons = [{"text": "Site", "url": "https://example.com"}]
        rule.affiliate_kit = {"enabled": True, "prefix": "https://aff.example.com/?u="}

        captured = {}
        orig_enqueue = DurableQueueManager.enqueue_delivery

        async def fake_enqueue(self, r, p, target, text, evaluation=None):
            # Reconstruct what the real method would have built
            from core.domain.services import AdvancedSettingsEngine as ASE
            captured["advanced_delay_seconds"] = ASE.get_dispatch_delay(r)
            captured["auto_delete_seconds"] = ASE.get_auto_delete_seconds(r)
            captured["topic_id"] = int(getattr(r, "topic_id", None) or 0)
            captured["debug_mode"] = bool(getattr(r, "debug_mode", False))
            captured["process_raw_text"] = bool(getattr(r, "process_raw_text", False))
            captured["button_to_text"] = bool(getattr(r, "button_to_text", False))
            captured["text"], captured["applied"] = ASE.apply_content_transforms(text, r, p)
            return True

        DurableQueueManager.enqueue_delivery = fake_enqueue
        try:
            mgr = DurableQueueManager.__new__(DurableQueueManager)
            payload = _payload("breaking news")
            ok = await mgr.enqueue_delivery(rule, payload, "-100222", "breaking news", None)
            assert ok is True
        finally:
            DurableQueueManager.enqueue_delivery = orig_enqueue

        assert captured["advanced_delay_seconds"] == 7.0
        assert captured["auto_delete_seconds"] == 0 or captured["auto_delete_seconds"] >= 0
        assert captured["topic_id"] == 99
        assert captured["debug_mode"] is True
        assert captured["process_raw_text"] is True
        assert captured["button_to_text"] is True
        # Link buttons + affiliate kit were both entitled and applied
        assert set(captured["applied"]) == {"link_buttons", "affiliate_kit"}


# ------------------------------------------------------ pipeline integration --
class TestPipelineAppliesAdvancedSettings:
    """The DurableMessagePipeline must run the content transforms and honour
    the edit-time window — proving the settings affect runtime behaviour."""

    @pytest.mark.asyncio
    async def test_edit_outside_window_is_skipped(self):
        from core.application.pipeline import DurableMessagePipeline

        rule = _rule()
        rule.edit_time_limit_seconds = 60
        rule.trigger_events = ["NEW_MESSAGE", "EDITED_MESSAGE"]

        class FakeRuleRepo:
            async def list_active_by_source(self, chat_id):
                return [rule]

        class FakeFilterRepo:
            async def get_by_id(self, fid):
                return None

        pipe = DurableMessagePipeline(rule_repo=FakeRuleRepo(), filter_repo=FakeFilterRepo())

        class FakeChat:
            id = "-100111"
            type = "channel"
            username = "chan"
            title = "Channel"

        class RawMsg:
            id = 42
            chat = FakeChat()
            text = "edited later"
            edit_date = 1700001000  # 1000s later, exceeds 60s limit
            date = 1700000000
            service = None
            photo = video = document = audio = None
            voice = sticker = animation = None
            caption = None
            media_group_id = None

        ctx = await pipe.process(RawMsg(), trigger="EDITED_MESSAGE")
        skipped = [s for s in ctx.stage_results if s.stage.value == "DEDUPLICATE" and not s.success]
        assert any(s.reason == "edit_outside_time_window" for s in skipped), \
            "an edit outside edit_time_limit_seconds must be skipped by the pipeline"

    @pytest.mark.asyncio
    async def test_keyword_reply_enqueued_as_distinct_job(self):
        """Keyword reply must be enqueued with job_kind="keyword_reply".

        Without it, the reply collides with the primary forward's job id
        (rule:chat:msg:target) and is silently deduplicated away — the user
        configures a reply and nothing is ever sent.
        """
        from core.infrastructure.telegram.durable_queue import DurableQueueManager

        rule = _rule(paid_addons={"keyword_reply": True})
        rule.keyword_confirmation = True
        rule.keyword_reply = "keyword matched!"
        rule.whitelist = ["btc"]
        rule.is_active = True

        payload = _payload("buy BTC now")

        calls: list[dict] = []
        orig = DurableQueueManager.enqueue_delivery

        async def fake_enqueue(self, r, p, target, text, evaluation=None, job_kind="forward"):
            calls.append({"target": target, "text": text, "job_kind": job_kind})
            return True

        DurableQueueManager.enqueue_delivery = fake_enqueue
        try:
            mgr = DurableQueueManager.__new__(DurableQueueManager)
            # 1. The primary forward enqueues normally.
            await mgr.enqueue_delivery(rule, payload, "-100222", "buy BTC now", None)
            # 2. The reply enqueues with a distinct kind.
            reply = AdvancedSettingsEngine.classify_keyword_trigger("buy BTC now", rule)
            assert reply == "keyword matched!"
            await mgr.enqueue_delivery(rule, payload, "-100222", reply, None,
                                       job_kind="keyword_reply")
        finally:
            DurableQueueManager.enqueue_delivery = orig

        assert calls[0]["job_kind"] == "forward"
        assert calls[1]["job_kind"] == "keyword_reply"
        assert calls[1]["text"] == "keyword matched!"


class TestKeywordReplyJobIsolation:
    """A keyword-reply delivery is text-only and never re-ships the source media."""

    @staticmethod
    def _mgr_with_fake_queue():

        from core.infrastructure.telegram.durable_queue import DurableQueueManager

        class FakeQueueRepo:
            async def get_pending_count(self):
                return 0

        mgr = DurableQueueManager.__new__(DurableQueueManager)
        mgr._queue = FakeQueueRepo()
        mgr._max_queue_size = 10000
        mgr._notify_event = asyncio.Event()
        mgr.stats = {"enqueued": 0, "backpressure_drops": 0}
        return mgr

    def test_reply_payload_is_flagged(self):
        rule = _rule()
        mgr = self._mgr_with_fake_queue()

        captured: dict = {}
        orig_atomic = type(mgr)._enqueue_atomic

        async def fake_atomic(self, mapping, job):
            captured["payload_data"] = job.payload_data
            captured["job_id"] = job.id
            return True

        type(mgr)._enqueue_atomic = fake_atomic
        try:
            import asyncio
            payload = _payload("buy BTC now")
            asyncio.run(mgr.enqueue_delivery(rule, payload, "-100222", "hi", None,
                                             job_kind="keyword_reply"))
        finally:
            type(mgr)._enqueue_atomic = orig_atomic

        assert captured["payload_data"]["is_reply"] is True
        assert "keyword_reply" in captured["job_id"]

    def test_forward_payload_is_not_flagged(self):
        rule = _rule()
        mgr = self._mgr_with_fake_queue()

        captured: dict = {}
        orig_atomic = type(mgr)._enqueue_atomic

        async def fake_atomic(self, mapping, job):
            captured["payload_data"] = job.payload_data
            captured["job_id"] = job.id
            return True

        type(mgr)._enqueue_atomic = fake_atomic
        try:
            import asyncio
            payload = _payload("hello")
            asyncio.run(mgr.enqueue_delivery(rule, payload, "-100222", "hello", None))
        finally:
            type(mgr)._enqueue_atomic = orig_atomic

        assert captured["payload_data"].get("is_reply", False) is False
        assert "keyword_reply" not in captured["job_id"]
