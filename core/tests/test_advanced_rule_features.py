import pytest
from core.domain.entities import ForwardRule, MessagePayload
from core.domain.services import FilterEngine
from core.domain.value_objects import ForwardMode, MediaType
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAIConfigRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.application.use_cases import MessageForwardingUseCase


def test_rule_metadata_properties():
    rule = ForwardRule(
        session_id="sess_123",
        source_chat_id="-1001",
        target_chat_id="-1002",
    )
    # Defaults
    assert rule.replacements == {}
    assert rule.header == ""
    assert rule.footer == ""
    assert rule.remove_links is False
    assert rule.block_voice is False
    assert rule.block_stickers is False
    assert rule.remove_emojis is False
    assert rule.ignore_edits is False
    assert rule.sync_deletes is False
    assert rule.album_mode == "album"

    # Set values via metadata and fields
    rule.metadata = {
        "replacements": {"old": "new", "del_me": ""},
        "header": "📢 BREAKING:",
        "footer": "👉 @mychan",
        "block_voice": True,
        "block_stickers": True,
        "remove_emojis": True,
        "sync_deletes": True,
        "album_mode": "first",
    }
    rule.ignore_edits = True
    rule.remove_links = True

    assert rule.replacements == {"old": "new", "del_me": ""}
    assert rule.header == "📢 BREAKING:"
    assert rule.footer == "👉 @mychan"
    assert rule.remove_links is True
    assert rule.block_voice is True
    assert rule.block_stickers is True
    assert rule.remove_emojis is True
    assert rule.ignore_edits is True
    assert rule.sync_deletes is True
    assert rule.album_mode == "first"


def test_filter_engine_emoji_and_text_transforms():
    fe = FilterEngine()

    # 1. Emoji stripping
    text_with_emojis = "Hello 🔥🚀 world! 😊 Welcome ⭐️"
    clean_emojis = fe.remove_emojis(text_with_emojis)
    assert "🔥" not in clean_emojis
    assert "🚀" not in clean_emojis
    assert "😊" not in clean_emojis
    assert "Hello  world!  Welcome" in clean_emojis or "Hello world!" in clean_emojis

    # 2. Text replacements
    text_to_replace = "Follow us at @OldChannel and get 10% discount now."
    replacements = {
        "@OldChannel": "@NewChannel",
        "10%": "50%",
        "now": "today",
    }
    replaced = fe.replace_text(text_to_replace, replacements)
    assert "@NewChannel" in replaced
    assert "@OldChannel" not in replaced
    assert "50% discount today." in replaced

    # 3. Header & Footer
    base_text = "Main announcement here"
    result = fe.apply_header_footer(base_text, header="🔝 HEADER", footer="🔻 FOOTER")
    assert result == "🔝 HEADER\n\nMain announcement here\n\n🔻 FOOTER"

    # 4. Links replacement / removal
    link_text = "Check out https://google.com and https://t.me/example for details"
    no_links = fe.remove_links(link_text)
    assert "https://google.com" not in no_links
    assert "https://t.me/example" not in no_links

    replaced_links = fe.replace_links(link_text, "https://mychannel.ir")
    assert "https://google.com" not in replaced_links
    assert "https://mychannel.ir" in replaced_links


@pytest.mark.asyncio
async def test_pipeline_voice_and_sticker_filtering():
    db = SqliteDatabase(":memory:")
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id, username,"
        " first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES (?, ?, '', '', '', '', 1, 1, 0, 0)",
        ("s1", "+1000"),
    )
    crypto = CryptoService("testkey")
    rule_repo = SqliteForwardRuleRepository(db)
    filter_repo = SqliteFilterRuleRepository(db)
    ai_repo = SqliteAIConfigRepository(db, crypto)

    sent_messages = []

    async def fake_sender(payload, rule, text, eval_res):
        sent_messages.append((payload, text))
        return True

    uc = MessageForwardingUseCase(rule_repo, filter_repo, ai_repo, None)
    uc.sender = fake_sender

    rule = ForwardRule(
        session_id="s1",
        source_chat_id="-1001",
        target_chat_id="-1002",
        forward_mode=ForwardMode.COPY_MESSAGE,
        metadata={
            "block_voice": True,
            "block_stickers": True,
            "remove_emojis": True,
            "replacements": {"bad": "good"},
            "header": "HEAD",
            "footer": "FOOT",
        },
    )
    await rule_repo.add(rule)

    # 1. Voice message should be dropped
    voice_msg = MessagePayload(
        message_id=1,
        chat_id="-1001",
        text="",
        media_type=MediaType.VOICE,
    )
    res_voice = await uc.process_message(voice_msg)
    assert res_voice.forwarded is False
    assert len(sent_messages) == 0

    # 2. Sticker message should be dropped
    sticker_msg = MessagePayload(
        message_id=2,
        chat_id="-1001",
        text="",
        media_type=MediaType.STICKER,
    )
    res_sticker = await uc.process_message(sticker_msg)
    assert res_sticker.forwarded is False
    assert len(sent_messages) == 0

    # 3. Regular text with emoji and replacement
    text_msg = MessagePayload(
        message_id=3,
        chat_id="-1001",
        text="This is bad 🔥 news!",
        media_type=MediaType.TEXT,
    )
    res_text = await uc.process_message(text_msg)
    assert res_text.forwarded is True
    assert len(sent_messages) == 1
    sent_text = sent_messages[0][1]
    assert "🔥" not in sent_text
    assert "good" in sent_text
    assert "bad" not in sent_text
    assert sent_text.startswith("HEAD")
    assert sent_text.endswith("FOOT")


@pytest.mark.asyncio
async def test_pipeline_edit_policy():
    db = SqliteDatabase(":memory:")
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id, username,"
        " first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES (?, ?, '', '', '', '', 1, 1, 0, 0)",
        ("s1", "+1000"),
    )
    crypto = CryptoService("testkey")
    rule_repo = SqliteForwardRuleRepository(db)
    filter_repo = SqliteFilterRuleRepository(db)
    ai_repo = SqliteAIConfigRepository(db, crypto)

    sent = []

    async def fake_sender(payload, rule, text, eval_res):
        sent.append(payload)
        return True

    uc = MessageForwardingUseCase(rule_repo, filter_repo, ai_repo, None)
    uc.sender = fake_sender

    rule = ForwardRule(
        session_id="s1",
        source_chat_id="-1001",
        target_chat_id="-1002",
        ignore_edits=True,
    )
    await rule_repo.add(rule)

    edit_msg = MessagePayload(
        message_id=10,
        chat_id="-1001",
        text="Edited message",
        is_edit=True,
    )
    res = await uc.process_message(edit_msg)
    assert res.forwarded is False
    assert len(sent) == 0

@pytest.mark.asyncio
async def test_bot_ui_rule_card_rendering():
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI

    ui = object.__new__(ProBotUI)
    ui._render_rule_detail = ProBotUI._render_rule_detail.__get__(ui, ProBotUI)
    ui._render_replace_menu = ProBotUI._render_replace_menu.__get__(ui, ProBotUI)
    ui._render_header_footer_menu = ProBotUI._render_header_footer_menu.__get__(ui, ProBotUI)
    ui._kbd = ProBotUI._kbd.__get__(ui, ProBotUI)
    ui._t = lambda key, **kw: key

    rule = ForwardRule(
        id="test-rule-id-12345",
        session_id="s1",
        source_chat_id="-1001",
        target_chat_id="-1002",
        source_chat_name="Channel Source",
        target_chat_name="Channel Target",
        metadata={
            "replacements": {"apple": "orange"},
            "header": "TOP_HEADER",
            "footer": "BOTTOM_FOOTER",
            "remove_links": True,
            "block_voice": True,
            "block_stickers": True,
            "remove_emojis": True,
            "sync_deletes": True,
            "album_mode": "first",
        },
    )

    # 1. Test Detail View
    text, kbd = ui._render_rule_detail(rule)
    assert "تنظیمات پیشرفته قانون" in text
    assert "Channel Source" in text
    assert "Channel Target" in text
    assert "🚫 حذف" in text  # links removed
    assert "🚫 مسدود" in text  # voice/stickers blocked
    assert "🧹 پاکسازی" in text  # emoji stripped
    assert "1️⃣ فقط اولین مدیا" in text  # album mode
    assert "تنظیم شده" in text
    assert "1 مورد فعال" in text

    # Check keyboard callbacks
    callbacks = [btn.callback_data for row in kbd.inline_keyboard for btn in row]
    assert "rtx:test-rule-id-12345" in callbacks  # text replace menu
    assert "rhf:test-rule-id-12345" in callbacks  # header/footer menu
    assert "rlk:test-rule-id-12345" in callbacks  # link toggle
    assert "rvc:test-rule-id-12345" in callbacks  # voice toggle
    assert "rst:test-rule-id-12345" in callbacks  # sticker toggle
    assert "rem:test-rule-id-12345" in callbacks  # emoji toggle
    assert "red:test-rule-id-12345" in callbacks  # edit toggle
    assert "rdl:test-rule-id-12345" in callbacks  # delete sync toggle
    assert "rmg:test-rule-id-12345" in callbacks  # album mode toggle
    assert "rrm:test-rule-id-12345" in callbacks  # remove rule

    # 2. Test Replace Menu View
    rep_text, rep_kbd = ui._render_replace_menu(rule)
    assert "جایگزینی متن (Text Replace)" in rep_text
    assert "apple" in rep_text
    assert "orange" in rep_text
    rep_cbs = [btn.callback_data for row in rep_kbd.inline_keyboard for btn in row]
    assert "rtxa:test-rule-id-12345" in rep_cbs  # add replacement
    assert "rtxc:test-rule-id-12345" in rep_cbs  # clear replacements
    assert "rtxd:test-rule-id-12345:0" in rep_cbs  # delete specific replacement

    # 3. Test Header/Footer Menu View
    hf_text, hf_kbd = ui._render_header_footer_menu(rule)
    assert "تنظیمات هدر و فوتر" in hf_text
    assert "TOP_HEADER" in hf_text
    assert "BOTTOM_FOOTER" in hf_text
    hf_cbs = [btn.callback_data for row in hf_kbd.inline_keyboard for btn in row]
    assert "rh_s:test-rule-id-12345" in hf_cbs  # set header
    assert "rh_d:test-rule-id-12345" in hf_cbs  # del header
    assert "rf_s:test-rule-id-12345" in hf_cbs  # set footer
    assert "rf_d:test-rule-id-12345" in hf_cbs  # del footer
