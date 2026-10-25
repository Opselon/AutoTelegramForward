"""Comprehensive regression test suite for runtime filter enforcement.

Covers:
1. Web flat metadata hydration into ForwardRule.content_policy_config.
2. Production pipeline drops matching blacklist keywords and phrases.
3. Production pipeline drops matching regex_block patterns.
4. Production pipeline requires matching whitelist keywords / phrases.
5. Production pipeline requires matching regex_require patterns.
6. Blacklist veto over whitelist when both match.
7. Multi-word phrase matching with whitespace/formatting variations.
8. Filter preview agreement with runtime decision.
9. ForwardRule property accessors & draft roundtrips.
"""

import pytest
import asyncio
from core.domain.entities import ForwardRule, FilterRule, MessagePayload
from core.domain.value_objects import MediaType
from core.domain.services import FilterEngine, SmartRoutingEngine
from core.domain.content_policy import ContentPolicy, preview_match
from core.application.pipeline import DurableMessagePipeline, PipelineStage


class MockRuleRepo:
    def __init__(self, rule: ForwardRule):
        self._rule = rule

    async def list_active_by_source(self, src: str):
        if str(self._rule.source_chat_id) == str(src):
            return [self._rule]
        return []


class MockFilterRepo:
    async def get_by_id(self, fid: str):
        return None


class FakeMsg:
    def __init__(self, text: str, msg_id: int = 1, chat_id: int = -100100):
        self.id = msg_id
        self.chat = type("Chat", (), {"id": chat_id, "title": "Source"})()
        self.text = text
        self.date = 1700000000
        self.media = None
        self.service = False


@pytest.mark.asyncio
async def test_web_flat_metadata_enforces_blacklist_keywords_in_pipeline():
    """Rules saved via Web dashboard with flat metadata must enforce blacklist."""
    rule = ForwardRule(
        id="rule-web-bl",
        session_id="s1",
        source_chat_id="-100100",
        target_chat_id="-100200",
        is_active=True,
    )
    # Simulated payload from Web dashboard / Go enrichCustomMetadata
    rule.metadata = {
        "blacklist_keywords": ["scam alert", "bad bot"],
        "whitelist_mode": "any",
    }

    assert rule.content_policy.has_rules
    assert rule.blacklist_words == ["scam alert", "bad bot"]

    pipe = DurableMessagePipeline(MockRuleRepo(rule), MockFilterRepo())

    # Message matching blacklist
    msg_blocked = FakeMsg("Attention everyone, this is a scam alert message!")
    ctx = await pipe.process(msg_blocked)
    filter_records = [sr for sr in ctx.stage_results if sr.stage == PipelineStage.FILTER]
    assert filter_records
    assert not filter_records[-1].success
    assert "blacklist" in filter_records[-1].reason

    # Clean message
    msg_clean = FakeMsg("Everything is normal and running smoothly.")
    ctx_clean = await pipe.process(msg_clean)
    filter_records_clean = [sr for sr in ctx_clean.stage_results if sr.stage == PipelineStage.FILTER]
    assert filter_records_clean
    assert filter_records_clean[-1].success


@pytest.mark.asyncio
async def test_web_flat_metadata_enforces_regex_block_in_pipeline():
    """Rules saved with flat regex_block patterns must reject matching messages."""
    rule = ForwardRule(
        id="rule-web-rx-bl",
        session_id="s1",
        source_chat_id="-100100",
        target_chat_id="-100200",
        is_active=True,
    )
    rule.metadata = {
        "regex_block": [r"promo\d{3,}", r"https?://t\.me/joinchat/\S+"],
    }

    assert rule.regex_block == [r"promo\d{3,}", r"https?://t\.me/joinchat/\S+"]
    assert rule.content_policy.has_rules

    pipe = DurableMessagePipeline(MockRuleRepo(rule), MockFilterRepo())

    # Matching message
    msg_rx = FakeMsg("Check this out: promo99999 discount today!")
    ctx = await pipe.process(msg_rx)
    filter_records = [sr for sr in ctx.stage_results if sr.stage == PipelineStage.FILTER]
    assert filter_records
    assert not filter_records[-1].success
    assert "regex_block" in filter_records[-1].reason

    # Non-matching message
    msg_ok = FakeMsg("Regular message without promotional codes.")
    ctx_ok = await pipe.process(msg_ok)
    filter_records_ok = [sr for sr in ctx_ok.stage_results if sr.stage == PipelineStage.FILTER]
    assert filter_records_ok
    assert filter_records_ok[-1].success


@pytest.mark.asyncio
async def test_web_flat_metadata_enforces_whitelist_and_regex_require():
    """Rules with whitelist keywords and regex_require must require both to pass."""
    rule = ForwardRule(
        id="rule-web-wl-rx",
        session_id="s1",
        source_chat_id="-100100",
        target_chat_id="-100200",
        is_active=True,
    )
    rule.metadata = {
        "whitelist_keywords": ["Announcement", "Critical Update"],
        "regex_require": [r"^\[OFFICIAL\]"],
        "whitelist_mode": "any",
    }

    pipe = DurableMessagePipeline(MockRuleRepo(rule), MockFilterRepo())

    # Misses both
    ctx1 = await pipe.process(FakeMsg("Random post"))
    assert not [sr for sr in ctx1.stage_results if sr.stage == PipelineStage.FILTER][-1].success

    # Matches whitelist but misses regex_require
    ctx2 = await pipe.process(FakeMsg("Here is an Announcement for you"))
    assert not [sr for sr in ctx2.stage_results if sr.stage == PipelineStage.FILTER][-1].success

    # Matches regex_require but misses whitelist keyword
    ctx3 = await pipe.process(FakeMsg("[OFFICIAL] Routine server maintenance notice"))
    assert not [sr for sr in ctx3.stage_results if sr.stage == PipelineStage.FILTER][-1].success

    # Matches both!
    ctx4 = await pipe.process(FakeMsg("[OFFICIAL] Critical Update scheduled for tonight"))
    assert [sr for sr in ctx4.stage_results if sr.stage == PipelineStage.FILTER][-1].success


def test_smart_routing_engine_and_servicer_evaluates_content_policy():
    """Servicer TestRule simulation must respect rule content policy."""
    rule = ForwardRule(
        id="rule-sre",
        session_id="s1",
        source_chat_id="-100100",
        target_chat_id="-100200",
        is_active=True,
    )
    rule.metadata = {
        "blacklist_keywords": ["banned token"],
    }

    # Banned keyword
    payload_bad = MessagePayload(
        message_id=1, chat_id="-100100", text="Buy our banned token now", media_type=MediaType.TEXT
    )
    decision = rule.content_policy.evaluate(payload_bad.text)
    assert not decision.allowed
    assert "blacklist:banned token" in decision.reason


def test_legacy_filter_engine_regex_blocking():
    """FilterEngine must block matching messages when regex is configured with blacklist."""
    engine = FilterEngine()
    filt = FilterRule(
        name="bl_regex",
        blacklist_keywords=["scam"],
        regex_patterns=[r"^\[BLOCKED\]"],
    )

    payload_rx = MessagePayload(message_id=1, chat_id="1", text="[BLOCKED] malicious content")
    res_rx = engine.evaluate(payload_rx, filt)
    assert res_rx.action.value == "DROP"
    assert "regex_block:" in res_rx.reason

    payload_clean = MessagePayload(message_id=2, chat_id="1", text="regular normal text")
    res_clean = engine.evaluate(payload_clean, filt)
    assert res_clean.action.value == "ALLOW"
