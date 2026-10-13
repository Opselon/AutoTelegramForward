from core.domain.entities import FilterRule, MessagePayload
from core.domain.services import FilterEngine
from core.domain.value_objects import MediaType


def payload(text="hello", media=MediaType.TEXT, service=False):
    return MessagePayload(
        message_id=1, chat_id="-100123", text=text,
        media_type=media, has_media=media is not MediaType.TEXT,
        is_service=service,
    )


def test_no_filter_allows():
    e = FilterEngine().evaluate(payload(), None)
    assert e.action.name == "ALLOW"


def test_service_message_dropped():
    e = FilterEngine().evaluate(payload(service=True), None)
    assert e.action.name == "DROP"


def test_blacklist_keyword():
    fr = FilterRule(blacklist_keywords=["spam"])
    assert FilterEngine().evaluate(payload("buy spam now"), fr).action.name == "DROP"
    assert FilterEngine().evaluate(payload("clean text"), fr).action.name == "ALLOW"


def test_whitelist_requires_match():
    fr = FilterRule(whitelist_keywords=["gold"])
    assert FilterEngine().evaluate(payload("gold signals"), fr).action.name == "ALLOW"
    assert FilterEngine().evaluate(payload("random"), fr).action.name == "DROP"


def test_regex_pattern():
    fr = FilterRule(regex_patterns=[r"price[:\s]+\d+"])
    assert FilterEngine().evaluate(payload("price: 100"), fr).action.name == "ALLOW"
    assert FilterEngine().evaluate(payload("no numbers"), fr).action.name == "DROP"


def test_invalid_regex_is_ignored_not_crash():
    fr = FilterRule(regex_patterns=["([unclosed"])
    assert FilterEngine().evaluate(payload("text"), fr).action.name == "ALLOW"


def test_media_type_blocking():
    fr = FilterRule(blocked_media_types=["video"])
    p = payload(media=MediaType.VIDEO)
    assert FilterEngine().evaluate(p, fr).action.name == "DROP"


def test_allowed_media_only():
    fr = FilterRule(allowed_media_types=["photo"])
    assert FilterEngine().evaluate(payload(media=MediaType.PHOTO), fr).action.name == "ALLOW"
    assert FilterEngine().evaluate(payload(media=MediaType.VIDEO), fr).action.name == "DROP"


def test_length_bounds():
    fr = FilterRule(min_message_length=5, max_message_length=10)
    assert FilterEngine().evaluate(payload("hi"), fr).action.name == "DROP"
    assert FilterEngine().evaluate(payload("x" * 20), fr).action.name == "DROP"
    assert FilterEngine().evaluate(payload("just right"), fr).action.name == "ALLOW"


def test_remove_links():
    out = FilterEngine().remove_links("check https://example.com/x and @channel now")
    assert "example.com" not in out and "@channel" not in out
    assert "check" in out and "now" in out


def test_cleaner_features():
    from core.domain.services import TextTransformer
    tt = TextTransformer()
    # Mention removal
    t1 = tt.remove_usernames("Join @mychannel and contact @admin_user now")
    assert "@mychannel" not in t1 and "@admin_user" not in t1
    assert "Join" in t1 and "now" in t1

    # Hashtag removal
    t2 = tt.remove_hashtags("Breaking news #bitcoin #ایران #crypto2026 update")
    assert "#bitcoin" not in t2 and "#ایران" not in t2
    assert "Breaking news" in t2 and "update" in t2

    # Phone removal
    t3 = tt.remove_phones("Call +1-800-555-0199 or 09123456789 for help")
    assert "+1-800-555-0199" not in t3 and "09123456789" not in t3
    assert "Call" in t3

    # Emoji removal
    t4 = tt.remove_emojis("Hello 😊 World 🔥🚀")
    assert "😊" not in t4 and "🔥" not in t4 and "🚀" not in t4
    assert "Hello" in t4 and "World" in t4

    # Custom words removal
    t5 = tt.remove_custom_words("Buy cheap watches today", ["cheap", "watches"])
    assert "cheap" not in t5 and "watches" not in t5
    assert "Buy" in t5 and "today" in t5


def test_link_filter_blocking():
    fr = FilterRule(blocked_media_types=["link"])
    p_link = payload("Check this out https://t.me/testchannel")
    assert FilterEngine().evaluate(p_link, fr).action.name == "DROP"

    p_clean = payload("Just regular discussion text")
    assert FilterEngine().evaluate(p_clean, fr).action.name == "ALLOW"


def test_header_footer_shortcodes():
    from core.domain.entities import ForwardRule
    from core.domain.services import HeaderFormatter

    p = MessagePayload(
        message_id=999,
        chat_id="-1001234567890",
        chat_title="My Source Channel",
        chat_username="mysource",
        sender_id="555",
        sender_name="Alice",
        text="Sample post text",
        date=1700000000,
    )
    r = ForwardRule(id="rule123", name="News Rule")

    template = (
        "Post [[ORIGIN_POST_ID]] by [[ORIGIN_NAME]] ([[ORIGIN_USERNAME]]):\n"
        "[[ORIGIN_TEXT]]\n"
        "From chat: [[ORIGIN_CHAT_ID]] / User: [[FROM_USER]]"
    )
    formatted = HeaderFormatter.format_text(template, r, p)
    assert "Post 999 by My Source Channel (@mysource):" in formatted
    assert "Sample post text" in formatted
    assert "From chat: -1001234567890 / User: Alice" in formatted

