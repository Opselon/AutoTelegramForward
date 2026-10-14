"""v2.0 app update — regression tests for the seven marquee features.

1. Show Forward Header  (forward_mode mapping)
2. Duplicate Filters    (content-hash dedup at pipeline level)
3. Cleaner              (content stripping transform)
4. Filters              (media-type / content filters — existing, checked for parity)
5. Set Delay Forward    (bounded dispatch delay)
6. Edit / Reply / Delete (sync propagation)
7. Multi-Language       (mobile l10n — covered in Dart, contract checked here)

Each test exercises the real domain services and pipeline stages.
"""

from __future__ import annotations

import asyncio

import pytest

from core.domain.entities import CleanerRule, ForwardRule, MessagePayload
from core.domain.services import AdvancedSettingsEngine
from core.domain.value_objects import ForwardMode, MediaType


def _rule(**meta) -> ForwardRule:
    return ForwardRule(
        id="rule-v2-1",
        session_id="sess-a",
        source_chat_id="-100111",
        target_chat_id="-100222",
        owner_user_id=5094837833,
        metadata=dict(meta or {}),
    )


def _payload(text: str = "hello", message_id: int = 1,
             reply_to_message_id: int | None = None) -> MessagePayload:
    return MessagePayload(
        chat_id="-100111",
        message_id=message_id,
        text=text,
        date=1700000000,
        reply_to_message_id=reply_to_message_id,
    )


# ------------------------------------------------- 1. Show Forward Header ----
class TestShowForwardHeader:
    """The toggle maps to Telegram's native forward (keeps sender header)."""

    def test_toggle_on_maps_to_direct_forward(self):
        rule = _rule()
        assert rule.show_forward_header is False
        rule.show_forward_header = True
        assert rule.show_forward_header is True
        assert rule.forward_mode == ForwardMode.DIRECT_FORWARD

    def test_toggle_off_maps_to_copy_message(self):
        rule = _rule()
        rule.forward_mode = ForwardMode.DIRECT_FORWARD
        rule.show_forward_header = False
        assert rule.forward_mode == ForwardMode.COPY_MESSAGE

    def test_default_is_clean_copy(self):
        rule = _rule()
        # The engine default must be a clean copy, not a header-preserving
        # native forward — that is the safer default for a forwarding product.
        assert rule.show_forward_header is False


# ------------------------------------------------- 2. Duplicate Filters -----

class TestDuplicateFilterFingerprint:
    """Content-level dedup: identical normalized text is collapsed."""

    def test_same_text_different_message_id_matches(self):
        from core.application.pipeline import _content_fingerprint

        p1 = _payload("Breaking news", message_id=1)
        p2 = _payload("Breaking news", message_id=2)
        assert _content_fingerprint(p1) == _content_fingerprint(p2)

    def test_whitespace_normalization(self):
        from core.application.pipeline import _content_fingerprint

        p1 = _payload("Breaking   news\n\nnow", message_id=1)
        p2 = _payload("breaking news now", message_id=2)
        assert _content_fingerprint(p1) == _content_fingerprint(p2)

    def test_different_text_does_not_match(self):
        from core.application.pipeline import _content_fingerprint

        p1 = _payload("Breaking news", message_id=1)
        p2 = _payload("Other news", message_id=2)
        assert _content_fingerprint(p1) != _content_fingerprint(p2)

    def test_media_type_differs_fingerprint(self):
        from core.application.pipeline import _content_fingerprint

        p1 = _payload("caption", message_id=1)
        p2 = MessagePayload(chat_id="-100111", message_id=2, text="caption",
                            media_type=MediaType.PHOTO, has_media=True)
        assert _content_fingerprint(p1) != _content_fingerprint(p2)

    def test_album_grouping_differs_from_single(self):
        from core.application.pipeline import _content_fingerprint

        p1 = _payload("caption", message_id=1)
        p2 = MessagePayload(chat_id="-100111", message_id=2, text="caption",
                            media_group_id="album-1")
        assert _content_fingerprint(p1) != _content_fingerprint(p2)


# ------------------------------------------------- 3. Cleaner ---------------
class TestCleaner:
    def test_literal_pattern_removed(self):
        c = CleanerRule(patterns=["spam"])
        assert c.clean("hello spam world") == "hello world"

    def test_case_insensitive_by_default(self):
        c = CleanerRule(patterns=["spam"])
        assert c.clean(" Spam and SPAM ") == "and"

    def test_case_sensitive_respects_case(self):
        c = CleanerRule(patterns=["Spam"], case_sensitive=True)
        assert c.clean("Spam spam") == "spam"

    def test_regex_pattern(self):
        c = CleanerRule(patterns=[r"\d+"], use_regex=True)
        assert c.clean("order 42 now") == "order now"

    def test_malformed_regex_does_not_raise(self):
        c = CleanerRule(patterns=["([invalid"], use_regex=True)
        assert c.clean("unchanged") == "unchanged"

    def test_remove_urls(self):
        c = CleanerRule(remove_urls=True)
        assert "https" not in c.clean("see https://x.com/a now")
        assert "t.me" not in c.clean("join https://t.me/c/123")

    def test_remove_mentions(self):
        c = CleanerRule(remove_mentions=True)
        out = c.clean("hey @adminpage look")
        assert "@" not in out

    def test_remove_hashtags(self):
        c = CleanerRule(remove_hashtags=True)
        assert "#" not in c.clean("news #breaking #tech")

    def test_remove_emojis(self):
        c = CleanerRule(remove_emojis=True)
        assert "🚀" not in c.clean("launch 🚀 now")

    def test_whitespace_holes_collapsed(self):
        c = CleanerRule(patterns=["middle"])
        assert c.clean("a middle b") == "a b"

    def test_empty_text_is_safe(self):
        assert CleanerRule(patterns=["x"]).clean("") == ""

    def test_no_patterns_is_noop(self):
        assert CleanerRule().clean("plain text") == "plain text"


class TestCleanerOnRule:
    def test_disabled_rule_builds_none(self):
        rule = _rule(cleaner_enabled=False,
                     cleaner_config={"patterns": ["spam"]})
        assert rule.build_cleaner() is None

    def test_empty_config_builds_none(self):
        rule = _rule(cleaner_enabled=True, cleaner_config={})
        assert rule.build_cleaner() is None

    def test_enabled_rule_materializes_cleaner(self):
        rule = _rule(
            cleaner_enabled=True,
            cleaner_config={
                "patterns": ["spam", "  "],
                "remove_urls": True,
                "use_regex": False,
            },
        )
        cleaner = rule.build_cleaner()
        assert cleaner is not None
        assert cleaner.remove_urls is True
        assert cleaner.clean("spam https://x.com") == ""

    def test_malformed_config_builds_none(self):
        rule = _rule(cleaner_enabled=True, cleaner_config="not-a-dict")
        assert rule.build_cleaner() is None

    def test_garbage_patterns_are_dropped(self):
        rule = _rule(cleaner_enabled=True,
                     cleaner_config={"patterns": ["ok", 42, None, ""]})
        cleaner = rule.build_cleaner()
        assert cleaner is not None
        assert cleaner.patterns == ["ok"]


# ------------------------------------------------- 4. Filters ---------------
class TestFiltersParity:
    """The Filters card in the reference UI maps to per-type media filters."""

    def test_block_text(self):
        rule = _rule()
        rule.block_text = True
        assert rule.block_text is True

    def test_block_links(self):
        rule = _rule()
        rule.block_links = True
        assert rule.block_links is True

    def test_block_voice(self):
        rule = _rule()
        rule.block_voice = True
        assert rule.block_voice is True

    def test_blacklist_keywords(self):
        rule = _rule()
        rule.blacklist = ["scam"]
        assert rule.blacklist == ["scam"]


# ------------------------------------------------- 5. Set Delay Forward -----
class TestSetDelayForward:
    def test_metadata_delay_wins(self):
        rule = _rule()
        rule.delay_seconds = 12.0
        rule.forward_delay_seconds = 5.0
        assert AdvancedSettingsEngine.get_dispatch_delay(rule) == 5.0

    def test_falls_back_to_legacy_delay(self):
        rule = _rule()
        rule.delay_seconds = 12.0
        assert AdvancedSettingsEngine.get_dispatch_delay(rule) == 12.0

    def test_delay_is_bounded(self):
        rule = _rule()
        rule.forward_delay_seconds = 10_000.0
        assert AdvancedSettingsEngine.get_dispatch_delay(rule) == 3600.0

    def test_negative_delay_is_zero(self):
        rule = _rule()
        rule.forward_delay_seconds = -5.0
        assert AdvancedSettingsEngine.get_dispatch_delay(rule) == 0.0


# ------------------------------------------------- 6. Edit/Reply/Delete -----
class TestSyncFlags:
    def test_sync_edits_flag(self):
        rule = _rule()
        assert rule.sync_edits in (True, False)
        rule.metadata["sync_edits"] = True
        assert rule.sync_edits is True

    def test_sync_deletes_flag(self):
        rule = _rule()
        rule.metadata["sync_deletes"] = True
        assert rule.sync_deletes is True

    def test_sync_replies_flag(self):
        rule = _rule()
        assert rule.sync_replies is False
        rule.sync_replies = True
        assert rule.sync_replies is True
        assert rule.metadata["sync_replies"] is True


class TestReplyPropagationContract:
    """handle_reply must be gated, mapped, and safe."""

    @pytest.mark.asyncio
    async def test_reply_without_reply_target_is_noop(self):
        from core.infrastructure.telegram.sync_engine import SyncEngine

        engine = SyncEngine.__new__(SyncEngine)
        engine._map = _NoopMap()
        n = await engine.handle_reply(_payload("a reply", message_id=9))
        assert n == 0

    @pytest.mark.asyncio
    async def test_reply_with_no_mapping_is_noop(self):
        from core.infrastructure.telegram.sync_engine import SyncEngine

        class EmptyMap:
            async def get_by_source(self, chat, mid):
                return []

        engine = SyncEngine.__new__(SyncEngine)
        engine._map = EmptyMap()
        n = await engine.handle_reply(_payload("a reply", message_id=9,
                                               reply_to_message_id=5))
        assert n == 0

    @pytest.mark.asyncio
    async def test_reply_propagates_when_enabled(self):
        from core.domain.entities import DeliveryStatus, MessageMapping
        from core.infrastructure.telegram.sync_engine import SyncEngine

        mapping = MessageMapping(
            id="m1", rule_id="rule-v2-1", source_chat_id="-100111",
            source_message_id=5, target_chat_id="-100222",
            target_message_id=77, delivery_status=DeliveryStatus.SENT,
        )

        class FakeMap:
            async def get_by_source(self, chat, mid):
                return [mapping] if mid == 5 else []

        class FakeRuleRepo:
            async def get_by_id(self, rid):
                rule = _rule()
                rule.sync_replies = True
                rule.id = "rule-v2-1"
                return rule

        sent: list[dict] = []

        class FakeClient:
            async def send_message(self, chat_id, text, reply_to_message_id=None):
                sent.append({"chat_id": chat_id, "text": text,
                             "reply_to": reply_to_message_id})
                return object()

        class FakePool:
            def get(self, sid):
                return FakeClient()

        engine = SyncEngine.__new__(SyncEngine)
        engine._map = FakeMap()
        engine._rules = FakeRuleRepo()
        engine._pool = FakePool()

        n = await engine.handle_reply(_payload("nice post!", message_id=9,
                                               reply_to_message_id=5))
        assert n == 1
        assert sent[0]["chat_id"] == -100222
        assert sent[0]["reply_to"] == 77
        assert sent[0]["text"] == "nice post!"

    @pytest.mark.asyncio
    async def test_reply_skipped_when_flag_off(self):
        from core.domain.entities import DeliveryStatus, MessageMapping
        from core.infrastructure.telegram.sync_engine import SyncEngine

        mapping = MessageMapping(
            id="m1", rule_id="rule-v2-1", source_chat_id="-100111",
            source_message_id=5, target_chat_id="-100222",
            target_message_id=77, delivery_status=DeliveryStatus.SENT,
        )

        class FakeMap:
            async def get_by_source(self, chat, mid):
                return [mapping]

        class FakeRuleRepo:
            async def get_by_id(self, rid):
                rule = _rule()
                rule.sync_replies = False  # user disabled reply sync
                rule.id = "rule-v2-1"
                return rule

        engine = SyncEngine.__new__(SyncEngine)
        engine._map = FakeMap()
        engine._rules = FakeRuleRepo()
        engine._pool = _NoopPool()

        n = await engine.handle_reply(_payload("reply", message_id=9,
                                               reply_to_message_id=5))
        assert n == 0


class _NoopMap:
    async def get_by_source(self, chat, mid):
        return []


class _NoopPool:
    def get(self, sid):
        return None


# ------------------------------------------------- 7. Multi-Language --------
class TestMultiLanguageContract:
    """The core stores language as a plain preference; rendering lives in the
    clients. The backend contract: a language code round-trips through the
    rule metadata without affecting any worker behaviour."""

    def test_language_round_trips(self):
        rule = _rule()
        rule.metadata["language"] = "pt"
        assert rule.metadata["language"] == "pt"

    def test_language_does_not_change_forwarding(self):
        rule_a = _rule()
        rule_b = _rule(language_hint="ru") if False else _rule()
        rule_b.metadata["language"] = "ru"
        assert rule_a.forward_mode == rule_b.forward_mode
        assert AdvancedSettingsEngine.get_dispatch_delay(rule_a) == \
            AdvancedSettingsEngine.get_dispatch_delay(rule_b)


# ------------------------------------------------- Normalizer ----------------
class TestNormalizerPopulatesReplyTarget:
    def test_pyrogram_style_raw(self):
        from core.application.pipeline import Normalizer

        class FakeChat:
            id = "-100111"
            type = "channel"

        class Raw:
            id = 9
            chat = FakeChat()
            text = "a reply"
            date = 1700000000
            reply_to_message_id = 5

        p = Normalizer.normalize(Raw())
        assert p.reply_to_message_id == 5

    def test_missing_reply_target_is_none(self):
        from core.application.pipeline import Normalizer

        class FakeChat:
            id = "-100111"
            type = "channel"

        class Raw:
            id = 9
            chat = FakeChat()
            text = "plain"
            date = 1700000000

        p = Normalizer.normalize(Raw())
        assert p.reply_to_message_id is None
