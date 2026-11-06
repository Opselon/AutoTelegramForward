"""Replace maps (links & @mentions) + the channel-only branding gate.

These pin the three behaviours the branding/replace upgrade added:

* ``apply_mention_rewrites`` — the "@old -> @new" pass (exact handle wins over
  the ``*`` catch-all, one pass, standalone mentions only);
* the link engine's rewrite map — the ``*`` catch-all and the URL-prefix key
  that let a task rewrite "any link found" to one target;
* ``branding.require_channel`` — normal users keep their own "Forwarded from"
  header on a direct delivery while channel content takes the hop.

Plus the registry plumbing: the four new settings are exposed through the
schema the API and the generic settings UIs render.
"""

from __future__ import annotations

import pytest

from core.domain import branding, rule_settings as rs, services, text_replace
from core.domain.entities import ForwardRule, MessagePayload


# --------------------------------------------------------------------------- #
# @mention rewrites
# --------------------------------------------------------------------------- #
def test_exact_handle_rewrites_case_insensitively():
    out = text_replace.apply_mention_rewrites(
        "buy from @OldShop now", {"@oldshop": "@newshop"})
    assert out == "buy from @newshop now"


def test_short_handles_are_not_mentions():
    # Telegram usernames run 4..32 characters (the same alphabet the cleaner
    # strips): "@a" or "@old" is not a handle and must not be rewritten.
    out = text_replace.apply_mention_rewrites("a @a b @old", {"*": "@main"})
    assert out == "a @a b @old"


def test_catch_all_rewrites_every_mention():
    out = text_replace.apply_mention_rewrites(
        "a @first b @second", {"*": "@main"})
    assert out == "a @main b @main"


def test_exact_beats_catch_all():
    out = text_replace.apply_mention_rewrites(
        "@oldshop and @other", {"@oldshop": "@newshop", "*": "@main"})
    assert out == "@newshop and @main"


def test_handles_glued_to_words_or_emails_do_not_match():
    text = "mail me at bob@example.com or @valid_handle"
    out = text_replace.apply_mention_rewrites(text, {"*": "@main"})
    # bob@example.com must survive; the standalone mention is rewritten.
    assert "bob@example.com" in out
    assert "@valid_handle" not in out


def test_single_pass_output_is_not_rematched():
    # A map whose output contains another key must not chain.
    out = text_replace.apply_mention_rewrites(
        "@alpha", {"@alpha": "@beta", "@beta": "@gamma"})
    assert out == "@beta"


def test_replacement_without_at_still_reads_as_handle():
    out = text_replace.apply_mention_rewrites("see @oldshop", {"@oldshop": "newshop"})
    assert out == "see @newshop"


def test_empty_map_is_a_no_op():
    assert text_replace.apply_mention_rewrites("@alfa @beta", {}) == "@alfa @beta"


# --------------------------------------------------------------------------- #
# Link rewrite map: the catch-all and the URL-prefix key
# --------------------------------------------------------------------------- #
def test_link_star_rewrites_every_link():
    out, _, altered = services.LinkManagementEngine.process_text_and_entities(
        "see https://example.com/x and https://other.net/y",
        None,
        services.LinkPolicy.REWRITE_LINKS,
        rewrite_map={"*": "https://t.me/me"},
    )
    assert "https://t.me/me" in out
    assert "example.com" not in out
    assert altered is True


def test_link_domain_key_rewrites_one_domain():
    out, _, _ = services.LinkManagementEngine.process_text_and_entities(
        "see https://app.old.com/path now",
        None,
        services.LinkPolicy.REWRITE_LINKS,
        rewrite_map={"app.old.com": "app.new.com"},
    )
    assert out == "see https://app.new.com/path now"


def test_link_url_prefix_key_rewrites_the_urls_it_starts_with():
    out, _, _ = services.LinkManagementEngine.process_text_and_entities(
        "see https://old.t.me/x/deep now",
        None,
        services.LinkPolicy.REWRITE_LINKS,
        rewrite_map={"https://old.t.me/x": "https://new.t.me/y"},
    )
    assert out == "see https://new.t.me/y/deep now"


def test_link_exact_key_wins_over_star():
    out, _, _ = services.LinkManagementEngine.process_text_and_entities(
        "see https://special.com/a and https://random.net/b",
        None,
        services.LinkPolicy.REWRITE_LINKS,
        rewrite_map={"*": "https://t.me/me", "special.com": "https://t.me/vip"},
    )
    assert "https://t.me/vip" in out
    assert "https://t.me/me" in out


# --------------------------------------------------------------------------- #
# The channel-only branding gate
# --------------------------------------------------------------------------- #
def _rule(**over) -> ForwardRule:
    base = dict(
        id="r1", session_id="s", source_chat_id="-100A", target_chat_id="-100B",
        intermediate_channel_id="-100C", use_intermediate=True,
    )
    base.update(over)
    return ForwardRule(**base)


def _cfg_rule(**brand_over) -> ForwardRule:
    rule = _rule()
    branding.write_config(rule, {"triggers_enabled": True, **brand_over})
    return rule


def test_user_message_in_group_fails_the_channel_gate():
    rule = _cfg_rule(require_channel=True)
    payload = MessagePayload(message_id=1, chat_id="-100A", chat_type="supergroup", text="hi")
    take, reason = branding.should_hop(payload, rule)
    assert take is False
    assert reason == "branding_requires_channel_origin"


def test_native_channel_post_passes_the_channel_gate():
    rule = _cfg_rule(require_channel=True)
    payload = MessagePayload(message_id=2, chat_id="-100A", chat_type="channel", text="hi")
    take, reason = branding.should_hop(payload, rule)
    assert take is True


def test_forward_from_channel_passes_the_channel_gate():
    rule = _cfg_rule(require_channel=True)
    payload = MessagePayload(
        message_id=3, chat_id="-100A", chat_type="supergroup", text="hi",
        forward_origin={"type": "channel", "from_chat_id": "-100X"},
    )
    take, _ = branding.should_hop(payload, rule)
    assert take is True


def test_forward_from_user_fails_the_channel_gate():
    rule = _cfg_rule(require_channel=True)
    payload = MessagePayload(
        message_id=4, chat_id="-100A", chat_type="supergroup", text="hi",
        forward_origin={"type": "user", "from_chat_id": "123"},
    )
    take, reason = branding.should_hop(payload, rule)
    assert take is False
    assert reason == "branding_requires_channel_origin"


def test_pyrogram_origin_type_spelling_passes_the_gate():
    rule = _cfg_rule(require_channel=True)
    payload = MessagePayload(
        message_id=5, chat_id="-100A", chat_type="supergroup", text="hi",
        forward_origin={"type": "MessageForwardOriginChannel", "from_chat_id": "-100X"},
    )
    take, _ = branding.should_hop(payload, rule)
    assert take is True


def test_the_gate_is_off_by_default():
    rule = _cfg_rule()
    payload = MessagePayload(message_id=6, chat_id="-100A", chat_type="supergroup", text="hi")
    take, reason = branding.should_hop(payload, rule)
    assert take is True
    assert reason == "branding_no_triggers_configured"


def test_entity_bridge_reads_and_writes_the_gate():
    rule = _rule()
    rule.branding_require_channel = True
    assert branding.read_config(rule)["require_channel"] is True


# --------------------------------------------------------------------------- #
# Registry exposure
# --------------------------------------------------------------------------- #
def test_new_settings_are_registered_with_their_groups():
    assert rs.SETTING_SPECS["link_replace_enabled"].group == "cleanup"
    assert rs.SETTING_SPECS["link_rewrite_map"].group == "cleanup"
    assert rs.SETTING_SPECS["mention_rewrite_map"].group == "cleanup"
    assert rs.SETTING_SPECS["branding_require_channel"].group == "branding"


def test_new_settings_render_in_the_schema():
    schema = rs.schema()
    keys = {entry["key"] for entry in schema["settings"]}
    assert {"link_replace_enabled", "link_rewrite_map",
            "mention_rewrite_map", "branding_require_channel"} <= keys


def test_mention_map_setting_round_trips_through_the_entity():
    rule = _rule()
    rs.apply_settings(rule, {"mention_rewrite_map": {"@old": "@new"}})
    assert rule.mention_rewrite_map == {"@old": "@new"}
    assert rs.read_settings(rule)["mention_rewrite_map"] == {"@old": "@new"}


def test_link_replace_setting_round_trips_through_the_entity():
    rule = _rule()
    rs.apply_settings(rule, {"link_replace_enabled": True,
                             "link_rewrite_map": {"*": "https://t.me/me"}})
    assert rule.link_replace_enabled is True
    assert rule.link_rewrite_map == {"*": "https://t.me/me"}


def test_the_settings_surface_refuses_an_unknown_key():
    rule = _rule()
    with pytest.raises(rs.SettingError):
        rs.apply_settings(rule, {"mention_rewrite_map_typo": {}})
