"""Cleaner parity: one setting, every delivery path.

The cleaner was configured per task and applied by the durable pipeline only.
The reply / private-view path (``MessageForwardingUseCase``) forwarded the raw
text, so a task that stripped links or custom words still leaked them whenever
the message arrived as a reply. These tests pin both paths to the same rule
object and prove the strip actually reaches the text the sender receives.
"""

from __future__ import annotations

import asyncio

import pytest

from core.application.use_cases import MessageForwardingUseCase
from core.domain.entities import ForwardRule, MessagePayload
from core.domain.profile_recovery import PRESERVED_ATTRS, reset_rule_settings
from core.domain.value_objects import MediaType

OWNER = 5094837833
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


class RuleRepo:
    def __init__(self, rule: ForwardRule) -> None:
        self.rule = rule

    async def list_active_by_source(self, chat_id):
        return [self.rule]

    async def get_by_id(self, rule_id):
        return self.rule if rule_id == self.rule.id else None


class FilterRepo:
    async def get_by_id(self, filter_id):
        return None


class AIRepo:
    async def get_owned(self, config_id, owner):
        return None


class NoProcessed:
    async def mark_processed(self, *a, **kw):
        return None

    async def was_processed(self, *a, **kw):
        return False


def make_rule(**kwargs) -> ForwardRule:
    rule = ForwardRule(
        id="rule-clean", session_id="sess-c", source_chat_id=SOURCE,
        target_chat_id=TARGET, is_active=True, owner_user_id=OWNER, **kwargs,
    )
    rule.duplicate_filters = False
    return rule


def run_through_use_case(rule: ForwardRule, text: str):
    sent: list[str] = []

    async def scenario():
        uc = MessageForwardingUseCase(
            rule_repo=RuleRepo(rule), filter_repo=FilterRepo(), ai_repo=AIRepo(),
            provider_factory=None, processed_repo=NoProcessed(),
        )

        async def sender(payload, r, out_text, evaluation):
            sent.append(out_text)
            return True

        uc.sender = sender
        payload = MessagePayload(
            message_id=11, chat_id=SOURCE, text=text,
            media_type=MediaType.TEXT,
        )
        outcome = await uc._process_rule(payload, rule)
        return outcome

    return asyncio.run(scenario()), sent


def test_reply_path_applies_the_task_cleaner():
    rule = make_rule()
    rule.cleaner_enabled = True
    rule.clean_custom_words = ["SPONSOR"]
    rule.clean_usernames = True
    rule.remove_links = True

    outcome, sent = run_through_use_case(
        rule, "Hello SPONSOR visit https://spam.example now @ad_channel",
    )

    assert outcome.forwarded is True
    assert sent, "the sender must be called"
    body = sent[0]
    assert "SPONSOR" not in body
    assert "spam.example" not in body
    assert "@ad_channel" not in body
    assert "Hello" in body


def test_cleaner_can_strip_the_inline_keyboard_on_the_reply_path():
    rule = make_rule()
    rule.cleaner_enabled = True
    rule.clean_buttons = True

    payload = MessagePayload(
        message_id=12, chat_id=SOURCE, text="x", media_type=MediaType.TEXT,
        reply_markup={"inline_keyboard": [[{"text": "buy", "url": "https://x"}]]},
    )
    asyncio.run(_run(rule, payload))
    assert payload.reply_markup is None


async def _run(rule: ForwardRule, payload: MessagePayload):
    uc = MessageForwardingUseCase(
        rule_repo=RuleRepo(rule), filter_repo=FilterRepo(), ai_repo=AIRepo(),
        provider_factory=None,
    )

    async def sender(p, r, text, ev):
        return True

    uc.sender = sender
    return await uc._process_rule(payload, rule)


def test_a_disabled_cleaner_leaves_the_text_alone():
    rule = make_rule()
    rule.cleaner_enabled = False
    rule.clean_custom_words = ["SPONSOR"]

    _, sent = run_through_use_case(rule, "Hello SPONSOR")
    assert "SPONSOR" in sent[0]


# ---------------------------------------------------------------------------
# /profile → Reset config
# ---------------------------------------------------------------------------

def test_reset_config_clears_settings_but_preserves_identity_and_routing():
    rule = make_rule()
    rule.custom_header = "HEADER"
    rule.custom_footer = "FOOTER"
    rule.cleaner_enabled = True
    rule.clean_usernames = True
    rule.clean_custom_words = ["ads"]
    rule.remove_links = True
    rule.remove_emojis = True
    rule.block_video = True
    rule.block_text = True
    rule.block_links = True
    rule.max_time_edit = 10
    rule.metadata = {
        "delivery_transport": "BOT",
        "bot_token_id": "tok-1",
        "cleaner_config": {"patterns": ["x"]},
        "replacements": {"a": "b"},
        "feature_group": "pro",
    }
    rule.cleaner_config = {"patterns": ["x"]}
    rule.replacements = {"a": "b"}
    before = {attr: getattr(rule, attr) for attr in PRESERVED_ATTRS}

    changed = reset_rule_settings(rule)

    for attr, value in before.items():
        assert getattr(rule, attr) == value, f"reset must never change {attr}"
    assert rule.is_active is True

    assert changed
    assert rule.custom_header == ""
    assert rule.cleaner_enabled is False
    assert rule.clean_usernames is False
    assert rule.clean_custom_words == []
    assert rule.block_video is False
    assert rule.block_text is False
    assert rule.max_time_edit == 86400
    assert rule.build_cleaner() is None

    # Routing/entitlement metadata survives; only settings blobs are dropped.
    assert rule.metadata["delivery_transport"] == "BOT"
    assert rule.metadata["bot_token_id"] == "tok-1"
    assert rule.metadata["feature_group"] == "pro"
    # The settings properties persist their defaults into the same blob; the
    # routing keys above prove the blob itself was not wiped.
    assert rule.cleaner_config == {}
    assert rule.replacements == {}


def test_cleaner_round_trips_through_the_draft():
    rule = make_rule()
    rule.cleaner_enabled = True
    rule.cleaner_config = {"patterns": ["x"], "apply_to_caption": False}
    rule.clean_usernames = True
    rule.clean_hashtags = True
    rule.clean_phones = True
    rule.clean_empty_lines = True
    rule.clean_buttons = True
    rule.remove_emojis = True
    rule.clean_custom_words = ["ad"]

    draft = rule.to_draft()
    for key in ("cleaner_enabled", "cleaner_config", "clean_usernames", "clean_hashtags",
                "clean_phones", "clean_empty_lines", "clean_buttons", "remove_emojis",
                "clean_custom_words"):
        assert key in draft, f"{key} must travel with the draft"

    clone = make_rule()
    clone.apply_draft(draft)
    assert clone.cleaner_enabled is True
    assert clone.cleaner_config["patterns"] == ["x"]
    assert clone.clean_usernames is True
    assert clone.clean_phones is True
    assert clone.clean_custom_words == ["ad"]
    assert clone.build_cleaner() is not None


def test_a_legacy_draft_with_option_flags_keeps_the_cleaner_enabled():
    clone = make_rule()
    clone.apply_draft({"clean_usernames": True, "clean_hashtags": True})
    assert clone.cleaner_enabled is True
    assert clone.build_cleaner() is not None


def test_reset_config_is_idempotent():
    rule = make_rule()
    assert reset_rule_settings(rule) == []
    assert reset_rule_settings(rule) == []
