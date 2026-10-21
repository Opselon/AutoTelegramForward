"""The bot's text-command surface, in one place.

Every slash command the bot answers is declared here: its group (the section it
appears under in the guide), the catalog key that describes it, and whether an
older dedicated handler already owns it. Two things read this table:

* the command guide (``/commands``), so the guide can never list a command the
  bot does not answer, or miss one it does;
* the router (``core.infrastructure.telegram.command_router``), which registers
  exactly the commands no earlier handler owns.

Adding a command means adding a row here plus its ``cmd_<name>_desc`` key in all
ten catalogs — not editing a help string. That is the "same format for all
future commands" rule: the guide is generated, never hand-maintained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

#: Group id -> catalog key for the section heading (heading carries its glyph).
GROUPS: Tuple[Tuple[str, str], ...] = (
    ("core", "cmd_group_core"),
    ("forwarding", "cmd_group_forwarding"),
    ("filters", "cmd_group_filters"),
    ("discovery", "cmd_group_discovery"),
    ("account", "cmd_group_account"),
    ("clone", "cmd_group_clone"),
)

#: The divider between sections in the guide. Glyph-only: no translation.
SEPARATOR = "━━━━━━━━━━"


@dataclass(frozen=True)
class CommandSpec:
    """One slash command.

    ``builtin`` marks a command an existing dedicated handler already answers
    (``/start`` has a handler that resets the FSM, ``/settings`` renders the
    client settings screen, ...). The router registers only the rest, so no
    command is handled twice.
    """

    name: str
    group: str
    description_key: str
    aliases: Tuple[str, ...] = ()
    builtin: bool = False

    @property
    def usage(self) -> str:
        return "/" + self.name


COMMANDS: Tuple[CommandSpec, ...] = (
    # ---- core ---------------------------------------------------------- #
    CommandSpec("start", "core", "cmd_start_desc", builtin=True),
    CommandSpec("commands", "core", "cmd_commands_desc", aliases=("guide",)),
    # ---- forwarding ---------------------------------------------------- #
    CommandSpec("forward", "forwarding", "cmd_forward_desc", builtin=True),
    CommandSpec("spam", "forwarding", "cmd_spam_desc"),
    CommandSpec("publish", "forwarding", "cmd_publish_desc"),
    # ---- filters ------------------------------------------------------- #
    CommandSpec("replace", "filters", "cmd_replace_desc"),
    CommandSpec("whitelist", "filters", "cmd_whitelist_desc"),
    CommandSpec("blacklist", "filters", "cmd_blacklist_desc"),
    CommandSpec("filter_users", "filters", "cmd_filter_users_desc"),
    # ---- chat discovery ------------------------------------------------ #
    CommandSpec("getchannel", "discovery", "cmd_getchannel_desc"),
    CommandSpec("getgroup", "discovery", "cmd_getgroup_desc"),
    CommandSpec("getuser", "discovery", "cmd_getuser_desc"),
    CommandSpec("getbot", "discovery", "cmd_getbot_desc"),
    # ---- account ------------------------------------------------------- #
    CommandSpec("profile", "account", "cmd_profile_desc", builtin=True),
    CommandSpec("settings", "account", "cmd_settings_desc", builtin=True),
    CommandSpec("language", "account", "cmd_language_desc", aliases=("lang",)),
    CommandSpec("upgrade", "account", "cmd_upgrade_desc"),
    CommandSpec("make_money", "account", "cmd_make_money_desc"),
    CommandSpec("get_app", "account", "cmd_get_app_desc"),
    CommandSpec("connect", "account", "cmd_connect_desc", builtin=True),
    CommandSpec("disconnect", "account", "cmd_disconnect_desc"),
    # ---- clone --------------------------------------------------------- #
    CommandSpec("clone", "clone", "cmd_clone_desc"),
)

_BY_NAME: Dict[str, CommandSpec] = {}
for _spec in COMMANDS:
    _BY_NAME[_spec.name] = _spec
    for _alias in _spec.aliases:
        _BY_NAME.setdefault(_alias, _spec)

#: Canonical names the router must answer itself (no earlier handler owns them).
#: Aliases are *not* here: they resolve to one of these names, so a handler list
#: that contained them would demand `_cmd_lang` and `_cmd_guide` methods that
#: must not exist (the alias is not a second command).
ROUTED_COMMANDS: Tuple[str, ...] = tuple(
    spec.name for spec in COMMANDS if not spec.builtin
)

#: Every token the router's message filter must claim, aliases included — an
#: alias left out of the filter would fall through to the free-text router and
#: be stored as input for whatever step the user was in.
ROUTED_TOKENS: Tuple[str, ...] = tuple(
    name for name, spec in _BY_NAME.items() if not spec.builtin
)

#: Names that must never reach the free-text FSM routers.
ALL_COMMAND_TOKENS: Tuple[str, ...] = tuple(_BY_NAME.keys())

#: Commands owned by other modules (BotManager, the panel's own builtin
#: handlers). They are routed by their own handlers, but they are still
#: commands a user types while a prompt is pending: each of them goes through
#: the FSM arbiter (`fsm_arbiter`), so a command always leaves the caller on
#: the screen the command is about, never answering a prompt they walked away
#: from. `test_fsm_command_arbitration` scans the package for
#: `filters.command(...)` literals and asserts every one is covered here.
OTHER_COMMAND_TOKENS: Tuple[str, ...] = (
    "admin", "ai", "backup", "cancel", "help", "login", "menu", "panel",
    "reauth", "restore", "route", "rules", "sessions", "stats",
)

#: Every token a user can type as a command, in one list — exactly what the
#: arbiter's message filter registers.
ARBITRATED_COMMANDS: Tuple[str, ...] = tuple(
    sorted(set(ALL_COMMAND_TOKENS) | set(OTHER_COMMAND_TOKENS))
)


def resolve(name: str) -> Optional[CommandSpec]:
    """Look a command (or alias) up, case-insensitively."""
    return _BY_NAME.get(str(name or "").strip().lower())


def commands_in(group: str) -> List[CommandSpec]:
    return [spec for spec in COMMANDS if spec.group == group]


def guide_text(t: Callable[..., str], lang: str = "en") -> str:
    """Render the command guide exactly as specified.

    ``t`` is the catalog lookup (``i18n.t`` or ``ProBotUI._t``); every word in
    the output comes from the user's locale, so the guide is not English-only.
    """
    lines: List[str] = [t("cmd_guide_title", lang=lang), "", SEPARATOR, ""]
    first = True
    for group_id, group_key in GROUPS:
        entries = commands_in(group_id)
        if not entries:
            continue
        if not first:
            lines.extend([SEPARATOR, ""])
        first = False
        lines.append(t(group_key, lang=lang))
        for spec in entries:
            lines.append(f"{spec.usage} -- {t(spec.description_key, lang=lang)}")
    return "\n".join(lines)


def guide_keys() -> Sequence[str]:
    """Every catalog key the guide needs (used by the parity test)."""
    keys: List[str] = ["cmd_guide_title"]
    keys.extend(key for _group, key in GROUPS)
    keys.extend(spec.description_key for spec in COMMANDS)
    return tuple(keys)
