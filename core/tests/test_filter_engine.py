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
