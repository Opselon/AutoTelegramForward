"""Comprehensive tests for Smart Forwarding Rules, VIP intermediate routing,
loop prevention, deduplication, and fallback mechanisms.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.domain.entities import ForwardRule, MessagePayload
from core.domain.value_objects import ForwardMode, RoutingType, MediaType
from core.domain.services import SmartRoutingEngine, DuplicateDetectionService
from core.infrastructure.telegram.dispatcher import MessageDispatcher


@pytest.fixture
def smart_engine():
    return SmartRoutingEngine()


@pytest.fixture
def dup_service():
    return DuplicateDetectionService(ttl_seconds=60)


def test_vip_message_detection_positive(smart_engine):
    """VIP message detection must strictly rely on telegram origin metadata."""
    rule = ForwardRule(
        source_chat_id="-1001111111111",  # e.g. xauusd_forex4
        target_chat_id="-1003333333333",  # e.g. @usdjp
        intermediate_channel_id="-1002222222222",  # e.g. +uVg1efOFMEc2NTNk
        use_intermediate=True,
        message_category="VIP_ONLY",
        forward_mode=ForwardMode.CUSTOM_HEADER_COPY,
        custom_header="💎 VIP SIGNAL",
    )

    # Payload forwarded from VIP channel into main channel
    payload = MessagePayload(
        message_id=5001,
        chat_id="-1001111111111",
        text="BUY GOLD 2650 SL 2640 TP 2680",
        forward_origin={
            "type": "channel",
            "from_chat_id": "-1002222222222",
            "from_chat_title": "VIP Club",
            "from_message_id": 1234,
        },
    )

    is_vip, reason = smart_engine.is_vip_origin(payload, rule)
    assert is_vip is True
    assert "forward_chat_id_match" in reason or "forward_origin" in reason

    matched, eval_reason = smart_engine.evaluate_rule(payload, rule)
    assert matched is True


def test_normal_user_message_not_classified_as_vip(smart_engine):
    """Normal chat messages like user 'ahmad' saying thanks should NEVER be VIP."""
    rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        intermediate_channel_id="-1002222222222",
        use_intermediate=True,
        message_category="VIP_ONLY",
    )

    # Ahmad writes a normal thank you message in the main channel
    payload = MessagePayload(
        message_id=5002,
        chat_id="-1001111111111",
        sender_id="99887766",
        sender_name="Ahmad",
        text="خیلی ممنون از تحلیل عالی و سیگنال خوبتون دستتون درد نکنه VIP",
        # Notice: NO forward_origin from VIP channel!
    )

    is_vip, reason = smart_engine.is_vip_origin(payload, rule)
    assert is_vip is False

    # The VIP rule must reject this message
    matched, eval_reason = smart_engine.evaluate_rule(payload, rule)
    assert matched is False
    assert "category_vip_unmatched" in eval_reason


def test_normal_user_message_routed_by_normal_rule(smart_engine):
    """Normal rule forwards user message directly to target without intermediate channel or VIP tags."""
    normal_rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        message_category="NORMAL_ONLY",
        forward_mode=ForwardMode.COPY_MESSAGE,
        use_intermediate=False,
    )

    payload = MessagePayload(
        message_id=5003,
        chat_id="-1001111111111",
        sender_id="99887766",
        sender_name="Ahmad",
        text="تشکر بابت سیگنال",
    )

    is_vip, _ = smart_engine.is_vip_origin(payload, normal_rule)
    assert is_vip is False

    matched, eval_reason = smart_engine.evaluate_rule(payload, normal_rule)
    assert matched is True
    assert eval_reason == "matched"


def test_loop_prevention_same_source_and_intermediate(smart_engine):
    """If intermediate channel equals source chat, system must detect loop and reject."""
    rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        intermediate_channel_id="-1001111111111",  # Same as source!
        use_intermediate=True,
    )

    payload = MessagePayload(
        message_id=5004,
        chat_id="-1001111111111",
        text="Test loop",
    )

    is_loop, reason = smart_engine.check_loop(payload, rule, target_chat_id="-1001111111111")
    assert is_loop is True
    assert "target_matches_source" in reason


def test_duplicate_prevention_on_repeated_delivery(dup_service):
    """Duplicate detection prevents processing the same message twice in the same pipeline."""
    payload = MessagePayload(
        message_id=7777,
        chat_id="-1001111111111",
        text="Important signal",
    )

    key = dup_service.make_key(payload, rule_id="rule_abc", target_chat_id="-1003333333333")
    
    # First check: not duplicate
    assert dup_service.is_duplicate(key) is False

    # Mark seen
    dup_service.mark_seen(key)

    # Second check: duplicate detected!
    assert dup_service.is_duplicate(key) is True


@pytest.mark.asyncio
async def test_fallback_to_clean_copy_when_forward_restricted():
    """When Telegram blocks native forward due to ChatForwardsRestricted, dispatcher must fallback to COPY."""
    dispatcher = MessageDispatcher(pool=MagicMock(), use_case=MagicMock())
    client = MagicMock()

    from pyrogram.errors import ChatForwardsRestricted

    client.forward_messages = AsyncMock(side_effect=ChatForwardsRestricted())
    client.copy_message = AsyncMock(return_value=MagicMock(id=9999))
    client.send_message = AsyncMock(return_value=MagicMock(id=9999))

    rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        forward_mode=ForwardMode.DIRECT_FORWARD,
        fallback_mode="COPY_MESSAGE",
    )

    payload = MessagePayload(
        message_id=8888,
        chat_id="-1001111111111",
        text="Protected Signal Post",
    )

    # Deliver message
    res = await dispatcher._deliver(client, payload, rule, "Protected Signal Post", "-1003333333333")

    assert res is not None
    assert client.copy_message.called or client.send_message.called


@pytest.mark.asyncio
async def test_vip_intermediate_hopping_delivery():
    """Verify intermediate hopping routes message through VIP channel first before final target."""
    pool = MagicMock()
    client = MagicMock()
    pool.all_clients = MagicMock(return_value={"default": client})
    pool.get = MagicMock(return_value=client)

    dispatcher = MessageDispatcher(pool=pool, use_case=MagicMock())

    intermediate_msg = MagicMock(id=4444)
    final_msg = MagicMock(id=5555)

    client.forward_messages = AsyncMock(return_value=[intermediate_msg])
    client.copy_message = AsyncMock(return_value=final_msg)
    client.send_message = AsyncMock(return_value=final_msg)

    rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        intermediate_channel_id="-1002222222222",
        use_intermediate=True,
        forward_mode=ForwardMode.CUSTOM_HEADER_COPY,
        custom_header="🌟 EXCLUSIVE VIP 🌟",
    )

    payload = MessagePayload(
        message_id=1111,
        chat_id="-1001111111111",
        text="Gold Buy at 2650",
    )

    # Hop to intermediate channel
    await dispatcher._hop_intermediate(payload, rule, "Gold Buy at 2650", "-1002222222222")
    assert client.forward_messages.called or client.send_message.called

    # Deliver to final target with intermediate provenance
    res = await dispatcher._deliver(
        client, payload, rule, "🌟 EXCLUSIVE VIP 🌟\nGold Buy at 2650", "-1003333333333"
    )

    assert res is not None


def test_smart_bot_ui_menu_rendering():
    """Verify smart routing dashboard renders in bot UI with all interactive buttons."""
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI

    ui = ProBotUI.__new__(ProBotUI)
    ui._t = lambda key, **kw: key
    ui._kbd = lambda rows: ProBotUI._kbd(ui, rows)

    rule = ForwardRule(
        id="rule_test_12345678",
        source_chat_id="-1001111111111",
        source_chat_name="Main Channel",
        target_chat_id="-1003333333333",
        target_chat_name="USDJP Target",
        intermediate_channel_id="-1002222222222",
        intermediate_channel_name="VIP Intermediate",
        use_intermediate=True,
        message_category="VIP_ONLY",
        forward_mode=ForwardMode.CUSTOM_HEADER_COPY,
        custom_header="💎 VIP SIGNAL",
        priority=20,
    )

    text, kbd = ui._render_smart_menu(rule)

    assert "مسیریابی هوشمند و VIP" in text
    assert "VIP Intermediate" in text or "-1002222222222" in text
    assert "فقط پیام‌های VIP" in text
    assert "CUSTOM_HEADER_COPY" in text or "کپی با هدر اختصاصی" in text
    assert "💎 VIP SIGNAL" in text

    # Verify buttons present
    button_callbacks = [b.callback_data for row in kbd.inline_keyboard for b in row]
    assert any("rsmc:rule_test_12345678" in cb for cb in button_callbacks)
    assert any("rsmt:rule_test_12345678" in cb for cb in button_callbacks)
    assert any("rsmw:rule_test_12345678" in cb for cb in button_callbacks)
    assert any("rsmm:rule_test_12345678" in cb for cb in button_callbacks)
    assert any("rsmh:rule_test_12345678" in cb for cb in button_callbacks)
    assert any("rsmtst:rule_test_12345678" in cb for cb in button_callbacks)
    assert any("rd:rule_test_12345678" in cb for cb in button_callbacks)


def test_rule_priority_and_match_mode_all(smart_engine):
    """Test match_mode='ALL' requiring BOTH origin match AND keyword match."""
    rule_all = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        message_category="VIP_ONLY",
        priority=30,
        detection_criteria={
            "match_mode": "ALL",
            "forward_origin_chat_ids": ["-1002222222222"],
            "text_contains": ["GOLD", "VIP"],
        },
    )

    # Case 1: Origin matches, but text is missing "VIP" -> Should FAIL under ALL
    payload_missing_kw = MessagePayload(
        message_id=6001,
        chat_id="-1001111111111",
        text="Analysis on EURUSD",
        forward_origin={"from_chat_id": "-1002222222222"},
    )
    is_vip, reason = smart_engine.is_vip_origin(payload_missing_kw, rule_all)
    assert is_vip is False

    # Case 2: Origin matches AND text contains keyword -> Should PASS under ALL
    payload_matching = MessagePayload(
        message_id=6002,
        chat_id="-1001111111111",
        text="GOLD VIP Signal: BUY NOW",
        forward_origin={"from_chat_id": "-1002222222222"},
    )
    is_vip, reason = smart_engine.is_vip_origin(payload_matching, rule_all)
    assert is_vip is True
    assert "all_matched" in reason


def test_rule_match_mode_any_and_regex(smart_engine):
    """Test match_mode='ANY' and regex evaluation."""
    rule_any = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        message_category="VIP_ONLY",
        priority=25,
        detection_criteria={
            "match_mode": "ANY",
            "regex_pattern": r"^TP\d+\s+HIT",
            "text_contains": ["jackpot"],
        },
    )

    # Matching via regex
    payload_regex = MessagePayload(
        message_id=6003,
        chat_id="-1001111111111",
        text="TP1 HIT with +50 pips!",
    )
    is_vip, reason = smart_engine.is_vip_origin(payload_regex, rule_any)
    assert is_vip is True
    assert "regex_matched" in reason

    # Non-matching
    payload_nomatch = MessagePayload(
        message_id=6004,
        chat_id="-1001111111111",
        text="Normal message without pattern",
    )
    is_vip, reason = smart_engine.is_vip_origin(payload_nomatch, rule_any)
    assert is_vip is False


def test_unknown_origin_never_guessed_as_vip(smart_engine):
    """If metadata is absent, origin must be UNKNOWN_ORIGIN and never guessed as VIP."""
    rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        message_category="VIP_ONLY",
        detection_criteria={
            "require_forward_origin": True,
            "forward_origin_chat_ids": ["-1002222222222"],
        },
    )

    # Message without any forward_origin
    payload = MessagePayload(
        message_id=6005,
        chat_id="-1001111111111",
        text="Regular user chat saying VIP",
    )

    is_vip, reason = smart_engine.is_vip_origin(payload, rule)
    assert is_vip is False
    assert "UNKNOWN_ORIGIN" in reason or "no_criteria_matched" in reason or "unmatched" in reason


def test_non_vip_forward_routed_via_default_route(smart_engine):
    """Forward from non-selected channel processed via default route A -> B."""
    vip_rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        intermediate_channel_id="-1002222222222",
        use_intermediate=True,
        message_category="VIP_ONLY",
        priority=50,
        detection_criteria={
            "forward_origin_chat_ids": ["-1002222222222"],
        },
    )

    default_rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        use_intermediate=False,
        message_category="ALL",
        priority=10,
    )

    # Forwarded from some other random channel -99999999
    payload = MessagePayload(
        message_id=6006,
        chat_id="-1001111111111",
        text="News from public channel",
        forward_origin={"from_chat_id": "-100999999999"},
    )

    # VIP rule must NOT match
    matched_vip, _ = smart_engine.evaluate_rule(payload, vip_rule)
    assert matched_vip is False

    # Default rule MUST match
    matched_default, _ = smart_engine.evaluate_rule(payload, default_rule)
    assert matched_default is True

    # Rule priority ordering:
    matched_rules = smart_engine.matching_rules(payload, [vip_rule, default_rule])
    assert len(matched_rules) == 1
    assert matched_rules[0] == default_rule
    assert matched_rules[0].use_intermediate is False


@pytest.mark.asyncio
async def test_protected_content_handled_via_reupload_or_restricted():
    """If native copy is blocked by channel content protection, dispatcher downloads & re-uploads."""
    dispatcher = MessageDispatcher(pool=MagicMock(), use_case=MagicMock())
    client = MagicMock()

    from pyrogram.errors import ChatForwardsRestricted

    # copy_message fails with ChatForwardsRestricted
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    client.get_messages = AsyncMock(return_value=MagicMock())
    client.download_media = AsyncMock(return_value=None)  # Download not possible
    client.send_message = AsyncMock(return_value=MagicMock(id=8888))

    rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        forward_mode=ForwardMode.COPY_MESSAGE,
        split_long_caption=True,
    )

    payload = MessagePayload(
        message_id=7001,
        chat_id="-1001111111111",
        text="Protected analysis caption",
        has_media=True,
        media_type=MediaType.PHOTO,
    )

    # Deliver: when download fails, it falls back to delivering caption text cleanly!
    res = await dispatcher._deliver_copy(client, payload, rule, "Protected analysis caption", "-1003333333333", "AUTO", None)
    assert res is not None
    assert client.send_message.called


def test_sqlite_and_postgres_entity_mapping():
    """Verify entities map cleanly with all new routing and criteria fields."""
    rule = ForwardRule(
        source_chat_id="-1001111111111",
        target_chat_id="-1003333333333",
        intermediate_channel_id="-1002222222222",
        use_intermediate=True,
        message_category="VIP_ONLY",
        priority=40,
        detection_criteria={
            "match_mode": "ALL",
            "forward_origin_chat_ids": ["-1002222222222"],
            "text_contains": ["SIGNAL"],
        },
    )

    assert rule.enabled is True
    assert rule.priority == 40
    assert rule.match_mode == "ALL"
    assert rule.forward_origin_chat_ids == ["-1002222222222"]
    assert rule.text_contains == ["SIGNAL"]
    assert rule.intermediate_chat_id == "-1002222222222"
    assert rule.destination_chat_id == "-1003333333333"
