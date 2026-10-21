"""/commands and the text-command surface: one registry, real routing.

The guide must list exactly the commands the bot answers, and every handler the
registry claims must exist on the UI. The reported defect this suite pins: the
replacement free-text step `rule_replace_input` was not in the durable step
namespace, so the bot asked for input and then answered every following message
with the main-menu hint — asking for input it could never receive.
"""

from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace

from core.application.command_registry import (
    COMMANDS,
    ROUTED_COMMANDS,
    guide_keys,
    guide_text,
    resolve,
)

UI_PATH = pathlib.Path(__file__).resolve().parents[1] / "infrastructure" / "telegram"
CATALOG_PATH = UI_PATH / "locales" / "translations.json"
LANGS = ("en", "fa", "ru", "zh", "es", "fr", "pt", "it", "id", "uz")

EN = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["en"]

#: Steps whose durable name carries a per-entity scope; the handler is the
#: prefix, so probing `<prefix>x1` is the same path the UI takes.
DYNAMIC = (
    "rge_edit_", "rge_search_", "bot_route_token_", "set_delay_", "set_every_n_",
    "add_bl_", "add_wl_", "set_wm_", "set_topic_", "rename_rule_",
    "ai_prompt_name_", "ai_prompt_sys_",
)


class _Msg:
    def __init__(self, uid: int = 5094837833, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid, type="private")
        self.text = text
        self.sent: list[tuple[str, object, dict]] = []

    async def reply_text(self, text=None, **kwargs):
        self.sent.append(("reply_text", text, kwargs))


# --------------------------------------------------------------------------- #
# The guide is generated
# --------------------------------------------------------------------------- #


def test_guide_matches_the_exact_requested_format():
    t = lambda key, lang="en", **kw: EN.get(key, key)  # noqa: E731
    expected = (
        "❓ Command Guide\n\n━━━━━━━━━━\n\n📋 Core\n/start -- Open the main menu\n"
        "/commands -- Show this command guide\n"
        "━━━━━━━━━━\n\n📨 Forwarding\n/forward -- Create and manage forwarding tasks\n"
        "/spam -- Review smart mass-send tasks\n/publish -- Review publishing tasks\n"
        "━━━━━━━━━━\n\n🔍 Filters\n/replace -- Manage replace rules\n"
        "/whitelist -- Manage allow-list rules\n/blacklist -- Manage block-list rules\n"
        "/filter_users -- Manage sender allow/block lists\n"
        "━━━━━━━━━━\n\n💬 Chat Discovery\n/getchannel -- List your channels\n"
        "/getgroup -- List your groups\n/getuser -- List your private chats\n"
        "/getbot -- List your bot chats\n"
        "━━━━━━━━━━\n\n👤 Account\n/profile -- Open your profile\n"
        "/settings -- Open bot settings\n/language -- Change bot language\n"
        "/upgrade -- View plans\n/make_money -- Open referrals\n/get_app -- Get app links\n"
        "/connect -- Connect your Telegram account\n/disconnect -- Disconnect your account\n"
        "━━━━━━━━━━\n\n📦 Clone\n/clone -- Clone past messages between chats"
    )
    assert guide_text(t) == expected


def test_guide_is_translated_not_english():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    fa = guide_text(lambda key, lang="en", **kw: catalog["fa"].get(key, key), lang="fa")
    assert "راهنمای دستورات" in fa
    assert "--" in fa  # the arrow between command and description is language-free


def test_resolve_is_case_insensitive_and_maps_aliases():
    assert resolve("REPLACE").name == "replace"  # type: ignore[union-attr]
    assert resolve("lang").name == "language"  # type: ignore[union-attr]
    assert resolve("guide").name == "commands"  # type: ignore[union-attr]
    assert resolve("nope-nope") is None


# --------------------------------------------------------------------------- #
# The registry and the catalogs agree
# --------------------------------------------------------------------------- #


def test_every_guide_key_exists_in_every_locale():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    missing = [
        (lang, key)
        for lang in LANGS
        for key in guide_keys()
        if key not in catalog[lang] or not str(catalog[lang][key]).strip()
    ]
    assert not missing, missing


def test_every_english_description_is_unique():
    values = [EN[spec.description_key] for spec in COMMANDS]
    assert len(set(values)) == len(values)


# --------------------------------------------------------------------------- #
# The router answers what the guide claims
# --------------------------------------------------------------------------- #


def test_every_routed_command_has_a_handler():
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI

    missing = [name for name in ROUTED_COMMANDS
               if not callable(getattr(ProBotUI, f"_cmd_{name}", None))]
    assert not missing, missing


def test_commands_are_mounted_at_group_minus_two():
    """Commands run before the FSM router (-1), so text input never eats one."""
    from core.infrastructure.telegram.command_router import TextCommandMixin

    seen: list[tuple[int, object]] = []

    class _Bot:
        def on_message(self, filter=None, group=0):
            def deco(fn):
                seen.append((group, filter))
                return fn

            return deco

    TextCommandMixin._mount_text_commands(type("H", (), {
        "_localized_handler": staticmethod(lambda fn: fn),
    })(), _Bot())
    assert seen and seen[0][0] == -2


# --------------------------------------------------------------------------- #
# The durable step namespace can never again swallow free text
# --------------------------------------------------------------------------- #


def _is_routable(name: str) -> bool:
    from core.infrastructure.telegram.ui_state_manager import _safe_step

    if _safe_step(name) == name:
        return True
    return any(name == prefix.rstrip("_") and _safe_step(f"{prefix}x1") == f"{prefix}x1"
               for prefix in DYNAMIC)


def test_every_static_step_handler_is_routable():
    """A step the UI sets must survive `_safe_step`.

    The pro state manager coerces an unknown step to "" at every persist, so a
    flow whose step is outside the namespace asks for input and then answers
    every follow-up with the main menu. "Add text replacement" shipped exactly
    that way.
    """
    src = (UI_PATH / "pro_bot_ui.py").read_text(encoding="utf-8")
    handlers = sorted(set(re.findall(r"async def _step_([a-z0-9_]+)\(", src)))
    broken = [h for h in handlers if not _is_routable(h)]
    assert not broken, f"steps that silently fall back to the main menu: {broken}"


def test_replace_input_step_is_in_the_namespace():
    from core.infrastructure.telegram.ui_state_manager import _safe_step

    assert _safe_step("rule_replace_input") == "rule_replace_input"


# --------------------------------------------------------------------------- #
# `/replace add …` through the real handler, against a real store
# --------------------------------------------------------------------------- #


async def test_replace_add_stores_the_entry_with_its_options(tmp_path):
    from core.domain.text_replace import entry_from_value
    from core.tests.bot_dispatch import BotHarness

    harness = BotHarness(str(tmp_path / "atf.db"))
    try:
        await harness.seed_session("sess-a", harness.owner)
        await harness.seed_rule("rule-cli-1", harness.owner)
        msg = _Msg(harness.owner)
        await harness.ui._dispatch_text_command(
            msg, "replace",
            'add -"discount 10%" -"discount 50%" --regex -i -w -n yearly',
        )
        rule = await harness.rules.get("rule-cli-1")
        assert rule is not None
        entry = entry_from_value("discount 10%", rule.replacements.get("discount 10%"))
        assert entry.mode == "regex"
        assert entry.ignore_case and entry.whole_word
        assert entry.name == "yearly"
        # No raw catalog key ever reaches the user.
        assert all("ui_" not in str(text) and "cmd_" not in str(text)
                   for _kind, text, _kw in msg.sent)
        assert msg.sent
    finally:
        harness.close()


async def test_replace_del_removes_the_named_entry(tmp_path):
    from core.tests.bot_dispatch import BotHarness

    harness = BotHarness(str(tmp_path / "atf.db"))
    try:
        await harness.seed_session("sess-a", harness.owner)
        await harness.seed_rule("rule-cli-2", harness.owner)
        await harness.ui._dispatch_text_command(
            _Msg(harness.owner), "replace",
            'add -"gold" -"silver" -n metal',
        )
        await harness.ui._dispatch_text_command(
            _Msg(harness.owner), "replace", "del metal",
        )
        rule = await harness.rules.get("rule-cli-2")
        assert rule is not None
        assert "gold" not in rule.replacements
    finally:
        harness.close()


async def test_replace_add_refuses_an_unusable_pattern(tmp_path):
    from core.tests.bot_dispatch import BotHarness

    harness = BotHarness(str(tmp_path / "atf.db"))
    try:
        await harness.seed_session("sess-a", harness.owner)
        await harness.seed_rule("rule-cli-3", harness.owner)
        msg = _Msg(harness.owner)
        await harness.ui._dispatch_text_command(
            msg, "replace", 'add -"(a+)+$" -"BOOM" --regex',
        )
        rule = await harness.rules.get("rule-cli-3")
        assert rule is not None and not rule.replacements
        # The refusal is explained, not silent.
        assert any(str(text).strip() for _k, text, _kw in msg.sent)
    finally:
        harness.close()


async def test_a_command_never_leaks_another_tenants_task(tmp_path):
    from core.tests.bot_dispatch import BotHarness

    harness = BotHarness(str(tmp_path / "atf.db"))
    try:
        await harness.seed_session("sess-a", 6278059256)
        await harness.seed_rule("rule-other", 6278059256)
        msg = _Msg(5094837833)
        await harness.ui._dispatch_text_command(
            msg, "replace", 'add rule-other -"a" -"b"',
        )
        rule = await harness.rules.get("rule-other")
        assert rule is not None and not rule.replacements
    finally:
        harness.close()
