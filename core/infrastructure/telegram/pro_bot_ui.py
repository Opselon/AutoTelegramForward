"""Pro Bot UI — button-driven control panel.

Every operation (login, rules, filters, AI, logs, stats, backup) is driven by
inline keyboards so the user never has to type a command. A per-user session
state machine (`UiState`) tracks multi-step flows and is persisted to the
`ui_states` table, so a restart mid-login resumes the user where they stopped.

Error policy: no handler returns a raw traceback or a bare internal key. Every
pyrogram `RPCError` is translated by `errors.tg_error` into a localized message
that carries the exact reason Telegram reported, and logged to `error_log` so
the debug panel can show it.
"""

import asyncio
import base64
from pathlib import Path
import copy
from contextvars import ContextVar
import json
import logging
import os
import re
import tempfile
import time
import uuid
from functools import wraps
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Tuple
from urllib.parse import quote

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from ...domain.rule_settings import SETTING_SPECS
from ...domain.profile_recovery import reset_rule_settings
from . import diagnostics
from .reason_text import reason_label
from .registry_editor import RegistryEditor
from .bot_transport import (
    BotApiError,
    BotApiTransport,
    BotTokenInvalid,
    BotTransportUnsupported,
    token_looks_valid,
)
from .button_style import (
    DANGER as BTN_DANGER,
    PRIMARY as BTN_PRIMARY,
    SUCCESS as BTN_SUCCESS,
    button,
    disable_styles,
    is_style_rejection,
    plain_markup,
    styles_enabled,
)
from .ui_styles import (
    color_for_key,
    color_for_label,
    decorate as decorate_button_label,
    style_label as style_button_label,
)

from ...application.backup_service import (
    decrypt_bundle,
    deserialize_bundle,
    encrypt_bundle,
    is_encrypted_bundle,
    serialize_bundle,
)
from ...application.repositories import ConcurrencyError
from ...application.effective_settings import describe_rule
from .ui_state import UiState  # per-user UI flow state (dataclass)
from .ui_state_manager import UiStateManager  # noqa: E402
from ...application.use_cases import (
    AIConfigUseCases,
    FilterRuleUseCases,
    ForwardRuleUseCases,
    MessageForwardingUseCase,
    SessionUseCases,
)
from ...domain import branding as branding_policy
from ...domain.entities import (
    AIConfig,
    FilterRule,
    ForwardRule,
    TASK_STATUSES,
    clamp_delay_seconds,
)
from ...domain.value_objects import AIProviderType, ForwardMode, RoutingType, RoutingDecision
from .client_pool import ClientPool
from .errors import SEV_ERROR, SEV_FATAL, tg_detail, tg_error
from .i18n import I18n, SUPPORTED_LANGUAGES
from .login_flow import LoginFlowManager
from .route_lists import (
    ERR_DUPLICATE_SOURCE,
    ERR_DUPLICATE_TARGET,
    ERR_EMPTY_SOURCES,
    ERR_EMPTY_TARGETS,
    ERR_OVERLAP,
    ROUTE_EMPTY_SOURCES,
    ROUTE_EMPTY_TARGETS,
    ROUTE_MISSING_ARROW,
    SHORT_ID_LENGTH,
    STATUS_FAILED,
    STATUS_RESOLVED,
    STATUS_UNCHECKED,
    RouteParseError,
    cb_payload,
    join_ids,
    parse_route_command,
    parse_route_list,
    short_id,
    strip_link_prefix,
    unique_peer_key,
    validate_resolved_uniqueness,
    validate_route_lists,
)

logger = logging.getLogger("atf.botui")

# Request-scoped locale; safe for concurrent Telegram updates.
_BOT_RENDER_LANGUAGE: ContextVar[Optional[str]] = ContextVar("atf_bot_render_language", default=None)

#: One row per language: the name its own speakers write, then its flag.
#:
#: The picker names are endonyms on purpose — a user who cannot read the current
#: interface language still has to recognise their own — so this is the one table
#: of *UI copy* that is deliberately not translated. Which is why it has to be
#: the only copy of the list: the settings language menu, the per-task
#: translation picker and the `/lang` list are all rendered from it, because
#: hand-written copies are how one screen keeps offering nine languages after an
#: eleventh ships.
LANG_ENDONYM = {
    "en": "English 🇬🇧",
    "fa": "فارسی 🇮🇷",
    "ru": "Русский 🇷🇺",
    "zh": "中文 🇨🇳",
    "pt": "Português 🇵🇹",
    "uz": "O'zbek 🇺🇿",
    "id": "Bahasa Indonesia 🇮🇩",
    "es": "Español 🇪🇸",
    "fr": "Français 🇫🇷",
    "it": "Italiano 🇮🇹",
}

#: "<endonym> <flag>": the form the settings language menu and `/lang` show.
LANG_FLAG = dict(LANG_ENDONYM)

#: The flags alone, for the picker's leading-flag form.
LANG_FLAG_EMOJI = {code: row.rpartition(" ")[2] for code, row in LANG_ENDONYM.items()}

#: The condition logic a task stores (`detection_criteria["match_mode"]`).
#:
#: `ANY` and `ALL` are our own value words, and the smart panel printed them
#: raw — next to a translated description, on the toggle button, and on the
#: simulator's "combined logic" line — so a Persian user read `ANY` in three
#: places. They are the same two words the toggle's own alert already names
#: (`ui_logic_all` / `ui_logic_any`), which is why this is a mapping and not a
#: second vocabulary.
LOGIC_WORDS = {"ALL": "ui_logic_all", "ANY": "ui_logic_any"}


def language_row(code: str) -> Tuple[str, str]:
    """(endonym, flag) for one language — `(code.upper(), "")` when unknown."""
    row = LANG_ENDONYM.get(str(code or "").strip().lower())
    if not row:
        return str(code or "").strip().upper(), ""
    name, _, flag = row.rpartition(" ")
    return (name, flag) if name else (row, "")


def language_label(code: str, *, with_code: bool = False) -> str:
    """One language the way its own speakers write it: `🇮🇷 فارسی` / `🇮🇷 فارسی (FA)`.

    A code this build does not know (a task pinned to a language that was
    dropped) is shown as its own upper-cased text rather than dropped from the
    screen, and an empty code renders nothing at all.
    """
    key = str(code or "").strip().lower()
    name, flag = language_row(code)
    if not name:
        return ""
    if key not in LANG_ENDONYM:
        return name
    text = f"{flag} {name}".strip()
    return f"{text} ({key.upper()})" if with_code else text

# Callback data must stay <= 64 bytes for Telegram. Keep keys short.
#: How long an armed FSM step stays armed while the user is idle. A prompt is
#: a question about *now*; after this window the next free-text message is
#: routed normally (menu hint / BotManager) instead of being stored as the
#: answer to a question from a previous session. Login steps are exempt
#: (`fsm_arbiter.PROTECTED_STEP_PREFIXES`) because they are themselves
#: command-driven and settling one orphans a half-finished login.
UI_STEP_TTL_SECONDS = 6 * 60 * 60
CB = {
    "main": "m",
    "sessions": "s",
    "rules": "r",
    "filters": "f",
    "ai": "a",
    "logs": "l",
    "stats": "st",
    "diag": "dg",
    "lang": "lg",
    "login": "sl",
    "backup": "sb",
    "help": "h",
    "back": "b",
    "rule_add": "ra",
    "rule_del": "rdel",
    "rule_toggle": "rt",
    "filter_add": "fa",
    "filter_del": "fd",
    "ai_add": "aa",
    "ai_test": "at",
    "ai_del": "ad",
    "ai_provider_pick": "ap",
    "ai_model_pick": "am",
    "logs_recent": "lr",
    "logs_stats": "ls",
    "logs_errors": "le",
    "lang_set": "lgs",
    "upgrade": "upg",
    "creds": "cv",
    "cred_add": "ca",
    "cred_del": "cd",
    "cred_default": "cdef",
    "login_retry": "slr",
    "cancel": "cx",
    "debug": "dbg",
    "refresh": "rf",
    "perf": "pf",
    "dashboard": "dsh",
    "webpass": "wp",
    "webpass_reset": "wpr",
    # Web dashboard URL configuration (operator sets public URL / domain)
    "weburl": "wurl",
    "weburl_set": "wurls",
    # AI Rewrite (per-rule): pick AI config / prompt / fallback policy / test
    "r_ai": "rai",
    "r_ai_cfg": "raic",
    "r_ai_prompt": "raip",
    "r_ai_policy": "raif",
    "r_ai_test": "rait",
    "r_ai_ttxt": "raix",
    # Global prompt management (bot side)
    "p_list": "pl",
    "p_new": "pn",
    "p_edit": "pe",
    "p_del": "pd",
    "p_test": "pt",
    # Full-installation backup & restore (server migration)
    "migrate": "mg",
    "migrate_export": "mgx",
    "migrate_export_enc": "mgxe",
    "migrate_import": "mgi",
    "migrate_overwrite": "mgo",
    "migrate_help": "mgh",
    # Client settings & user credentials
    "settings": "stg",
    "client_creds": "clc",
    "client_cred_reset": "clcr",
    # Dead-letter queue: list it from the diagnostics screen, then replay one
    # entry or purge the lot. Entry ids are long, so the payload is an index
    # into a per-user page held in memory — never the id itself.
    "diag_dlq": "dgd",
    "diag_dlq_refresh": "dgdr",
    "diag_dlq_replay": "dgdp",
    "diag_dlq_purge": "dgdl",
    "diag_dlq_purge_confirm": "dgdlc",
    # Administrator Control Plane
    "admin": "adm",
    "adm_stats": "admst",
    "adm_users": "admus",
    "adm_user_view": "admuv",
    "adm_user_suspend": "admssp",
    "adm_user_activate": "admact",
    "adm_user_plan": "admpln",
    "adm_bcast": "admbc",
    "adm_bcast_confirm": "admbcc",
    "adm_bcast_cancel": "admbcx",
    "adm_forcesub": "admfs",
    "adm_forcesub_toggle": "admfst",
    "adm_forcesub_set": "admfss",
    "adm_admins": "admadm",
    "adm_admin_add": "admaadd",
    "adm_admin_del": "admadel",
    "adm_config": "admcfg",
    "check_sub": "chksub",
    # Multi-source / multi-target route lists (comma-separated entry screens).
    # Payload shapes are built with cb_payload() so the 64-byte Telegram limit
    # is checked in one place:
    #   rsl:<role>:<scope>  -> open the list screen
    #   rfx:<role>:<scope>  -> fix a single entry
    #   rsv:<scope>         -> save the route built from the lists
    # <role> is "s" or "t"; <scope> is "n" (new rule) or a rule id.
    "route_src_list": "rsl",
    "route_tgt_list": "rtl",
    "route_fix_entry": "rfx",
    "route_save_lists": "rsv",
    "route_back": "rbk",
}


# --------------------------------------------------------------------------- #
# Multi-source -> multi-target routes
# --------------------------------------------------------------------------- #
#
# One task routes M source chats to N target chats, with M and N unbounded.
# The 40 `ui_route_*` catalog keys are the entire vocabulary of this flow, and
# the list contract (splitting, dedupe, reported duplicates, self-loop refusal)
# lives in route_lists.py so the wizard and `/route` cannot disagree.
#
# Callback scopes: "n" starts a brand-new route, anything else is the task id
# being edited. Both fit in the payload budget enforced by cb_payload().
ROUTE_SCOPE_NEW = "n"

#: Per-entry resolution states; the contract lives in route_lists.py and the
#: handler modules read the same constants, so surfaces cannot drift apart.
ROUTE_RESOLVED = STATUS_RESOLVED
ROUTE_FAILED = STATUS_FAILED
ROUTE_UNCHECKED = STATUS_UNCHECKED

#: Per-key caches of catalog-resolved input aliases.
_ALIAS_CACHE: Dict[str, Optional[frozenset]] = {}

#: Cancel aliases resolved from the catalog (see ``cancel_aliases``).
_CANCEL_ALIASES: Optional[frozenset] = None


def _input_aliases(key: str) -> frozenset:
    """Every word a user may type to mean the value of ``key``.

    ``ui_input_*_aliases`` is one comma-separated list per locale, so a user
    typing Russian or Chinese is understood exactly as well as one typing
    Persian — the hardcoded tuples this replaces only ever covered English
    and Persian, and carried literal non-ASCII no audit could see.
    """
    cached = _ALIAS_CACHE.get(key)
    if cached is None:
        i18n = I18n()
        words = set()
        for code in SUPPORTED_LANGUAGES:
            raw = i18n.t(key, lang=code) or ""
            words.update(w.strip().lower() for w in raw.split(",") if w.strip())
        cached = _ALIAS_CACHE[key] = frozenset(words)
    return cached


def cancel_aliases() -> frozenset:
    """Every word a user may type to stop the current step.

    Built from ``ui_input_cancel_aliases`` — one comma-separated list per
    locale — instead of a hardcoded tuple. The panel offers the cancel word in
    the user's own language, so that language's word has to be understood as
    input too: the tuple this replaces accepted Persian in all 10 locales, so a
    Russian or Chinese speaker could only escape with ``/cancel``. Escaped
    Persian literals were also invisible to the cross-language audit, which is
    how 21 call sites drifted apart (two accepted different words).

    Cached at module level: the catalog is a ~1 MB JSON and this runs on every
    text message a multi-step flow receives.
    """
    global _CANCEL_ALIASES
    if _CANCEL_ALIASES is None:
        _CANCEL_ALIASES = _input_aliases("ui_input_cancel_aliases") | {"/cancel"}
    return _CANCEL_ALIASES

#: Clear words accepted inside the branding-filter screens: the neutral ASCII
#: tokens plus the Persian synonyms the editor already accepts, spelled as
#: escapes so this file carries no literal non-ASCII.
BRAND_CLEAR_WORDS = (
    "-",
    "clear",
    "none",
    "0",
    "\u067e\u0627\u06a9",
    "\u062d\u0630\u0641",
)

#: route_lists validation code -> catalog key. Every message names the
#: offending entry where the catalog string has an {id} placeholder.
ROUTE_PROBLEM_KEYS = {
    ERR_DUPLICATE_SOURCE: "ui_route_err_dup_source",
    ERR_DUPLICATE_TARGET: "ui_route_err_dup_target",
    ERR_OVERLAP: "ui_route_err_overlap",
    ERR_EMPTY_SOURCES: "ui_route_err_empty_sources",
    ERR_EMPTY_TARGETS: "ui_route_err_empty_targets",
}

#: `/route` parse code -> catalog key.
ROUTE_PARSE_KEYS = {
    ROUTE_MISSING_ARROW: "ui_route_need_arrow",
    ROUTE_EMPTY_SOURCES: "ui_route_err_empty_sources",
    ROUTE_EMPTY_TARGETS: "ui_route_err_empty_targets",
}

#: Severity → glyph. One definition, owned by the diagnostics renderer, so the
#: logs screens and the report cannot decorate the same severity differently.
SEV_ICON = diagnostics.SEVERITY_ICON





from core.infrastructure.telegram.autoreply_bot_ui import AutoReplyBotMixin
from core.infrastructure.telegram.handlers import build_handler_registry
from core.infrastructure.telegram.handlers.discovery import DiscoveryHandlers
from core.infrastructure.telegram.handlers.forwarding import ForwardingHandlers
from core.infrastructure.telegram.replace_bot_ui import ReplaceBotMixin
from core.infrastructure.telegram.command_router import TextCommandMixin
from core.application.command_registry import ARBITRATED_COMMANDS
from core.infrastructure.telegram import fsm_arbiter
from core.domain.text_replace import (
    MAX_ENTRIES,
    MODE_LITERAL,
    MODE_REGEX,
    ReplaceError,
    clean_flags,
    describe_entry,
    entry_from_value,
    entry_with,
    error_key,
    merge_entry,
    parse_freeform,
)


class ProBotUI(TextCommandMixin, AutoReplyBotMixin, ReplaceBotMixin, DiscoveryHandlers,
               ForwardingHandlers):
    """Renders and routes the entire button-based control panel.

    Every handler surface is a mixin that owns one file; ``_handler_registry``
    is the validated inventory of those modules (see
    :mod:`core.infrastructure.telegram.handlers`).
    """

    def __init__(
        self,
        bot: Client,
        i18n: I18n,
        sessions: SessionUseCases,
        rules: ForwardRuleUseCases,
        filter_rules: FilterRuleUseCases,
        ai_configs: AIConfigUseCases,
        pipeline: MessageForwardingUseCase,
        pool: ClientPool,
        login: LoginFlowManager,
        log_client,
        admin_ids,
        credentials=None,
        bot_tokens=None,
        bot_route=None,
        dispatcher=None,
        boot_manager=None,
        ui_state_repo=None,
        error_log=None,
        metrics=None,
        rule_stats=None,
        delivery_queue=None,
        users=None,
        prompt_service=None,
        ai_transformer=None,
        pv_responder=None,
        autoreply=None,
        replace_rules=None,
        web_url: str = "",
        account_servicer=None,
        backup_service=None,
        settings=None,
        admin_service=None,
        db=None,
    ) -> None:
        self.bot = bot
        self.i18n = i18n
        # NOTE: intentionally NO shared default language. _t() with no
        # explicit lang renders English — the only safe cross-user default.
        # Per-user renderers resolve via _lang_for(rule|st|uid) and pass
        # lang= explicitly, so one user's choice can never leak into
        # another user's render.
        self._sessions = sessions
        self._rules = rules
        self._filters = filter_rules
        self._ai = ai_configs
        self._pipeline = pipeline
        self._pool = pool
        self._login = login
        self._log = log_client
        self._admin_ids = set(admin_ids or [])
        self._admin_service = admin_service
        self._db = db
        self._credentials = credentials
        self._bot_tokens = bot_tokens
        # The same cache the delivery worker uses, so a token entered (or
        # removed) here takes effect on the very next forward.
        self._bot_route = bot_route
        self._dispatcher = dispatcher
        self._boot_manager = boot_manager
        self._ui_state_repo = ui_state_repo
        self._error_log = error_log
        self._metrics = metrics
        self._rule_stats = rule_stats
        #: The delivery queue, so the diagnostics screen can report what is
        #: waiting, retrying or stuck rather than only what has been sent.
        self._delivery_queue = delivery_queue
        # Dead-letter pages, keyed by user: the index in a callback payload
        # resolves against this. Bounded because a page is at most a screen.
        self._dlq_page: Dict[int, List[Dict[str, Any]]] = {}
        self._users = users
        self._prompts = prompt_service
        self._ai_transformer = ai_transformer
        self._pv_responder = pv_responder
        #: AutoReplyService (v28): the same instance the gRPC surface uses, so
        #: a switch flipped here is what the worker applies to the next message.
        self._autoreply = autoreply
        #: ReplaceRuleService: the same instance the gRPC surface serves, so a
        #: rule created from the dashboard is the one /replace lists here.
        self._replace_rules = replace_rules
        self._web_url = (web_url or "").rstrip("/")
        self._accounts = account_servicer
        self._backup = backup_service
        self._settings = settings  # ISettingsRepository, optional
        self._states: Dict[int, UiState] = {}
        # Last time a user's armed step was actually used (answered, re-armed or
        # read): the idle clock for `_expire_stale_step`. Seeded from the
        # durable row's updated_at on resume, so a prompt armed before a
        # restart ages exactly like one armed in this process.
        self._step_touch: Dict[int, float] = {}
        # Pro state manager (durable, validated, single-writer): the plain
        # dict above stays as the sync render cache the ~250 handlers read,
        # while every DURABLE write now flows through the manager so the
        # step namespace, buffer bounds and screen stack cannot regress.
        self._ui_manager = UiStateManager(
            repo=ui_state_repo, logger=log_client,
        )
        #: Single inventory of handler modules, validated (not just listed) at
        #: mount time — see core/infrastructure/telegram/handlers/registry.py.
        self._handler_registry = build_handler_registry()
        self._dialog_use_case = None
        self._register()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    #: pyrogram's propagation signals are StopAsyncIteration subclasses; they
    #: must not be caught by the `except Exception` blocks that guard user
    #: replies, or a handler's "give this to the next group" decision is
    #: swallowed and the user is answered twice.
    @staticmethod
    def _is_propagation_signal(exc: BaseException) -> bool:
        return type(exc).__name__ in {"ContinuePropagation", "StopPropagation"} or isinstance(
            exc, (StopAsyncIteration, StopIteration))

    def _localized_handler(self, handler):
        """Bind the resolved user language for the lifetime of one async update."""
        @wraps(handler)
        async def wrapped(client, event, *args, **kwargs):
            user = getattr(event, "from_user", None)
            raw_user_id = getattr(user, "id", None) if user is not None else None
            try:
                user_id = int(raw_user_id) if raw_user_id is not None else 0
            except (TypeError, ValueError):
                user_id = 0
            language = self._resolve_lang(user_id, user) if user_id else "en"
            token = _BOT_RENDER_LANGUAGE.set(language)
            try:
                return await handler(client, event, *args, **kwargs)
            finally:
                _BOT_RENDER_LANGUAGE.reset(token)

        return wrapped
    def _is_admin(self, user_id: int) -> bool:
        if getattr(self, "_admin_service", None) is not None:
            return self._admin_service.is_admin(user_id)
        admin_ids = getattr(self, "_admin_ids", None)
        try:
            return bool(admin_ids and int(user_id or 0) in admin_ids)
        except Exception:
            return False

    def _scope_uid(self, user_id: int) -> Optional[int]:
        """The owner scope for a bot-side read or write.

        ``None`` means "the whole install" — every tenant's tasks — so it is an
        ADMIN privilege and is returned only when the caller is *positively*
        known to be an admin. Anything else returns the caller's own id.

        The old version inverted that: with no admin list configured it answered
        ``None`` for everyone, because "single-tenant install" was assumed. That
        made an unconfigured deployment the least safe one — `_owned_rule` would
        hand any Telegram user any rule id, which is the cross-account leak users
        reported. Missing configuration, an unreadable admin service and an
        absent identity all now fail closed to "you own only what is yours", and
        a user we cannot name owns nothing at all.
        """
        uid = int(user_id or 0)
        if uid <= 0:
            # No identity => no ownership. An unscoped read is never the fallback.
            return 0
        try:
            if self._is_admin(uid):
                return None
        except Exception:
            # An admin service that cannot answer is not an admin.
            return uid
        return uid

    async def _owned_rule(self, rule_id: str, user_id: int):
        # v12.2: every rule read in the bot funnels through the use-case
        # ownership guard. rule_id comes from conversation state or callback
        # data, which is user-controlled — never trust it unsccoped.
        if not rule_id:
            return None
        return await self._rules.get_owned(rule_id, self._scope_uid(user_id))

    async def _save_owned_rule(self, rule, user_id: int, expected_version=None):
        # v12.2: writes go through update_owned so a rule whose id was
        # transplanted into another user's conversation state is refused.
        return await self._rules.update_owned(rule, self._scope_uid(user_id), expected_version)

    def _lang_memo(self) -> Dict[int, str]:
        """Per-user resolved-language memo.

        ``_resolve_lang`` runs on nearly every render (the main menu resolves
        it per callback) and its second step is a SQLite ``SELECT``. The
        in-memory ``UiState`` covers a user only once something has created
        that state, so a menu built from a bare uid paid the query every
        single time. Memoized here, keyed by user id and invalidated the
        moment the user picks a language.

        Created lazily: several tests and helper paths build this object with
        ``object.__new__`` and never run ``__init__``.
        """
        memo = self.__dict__.get("_lang_cache")
        if memo is None:
            memo = {}
            self.__dict__["_lang_cache"] = memo
        return memo

    def remember_language(self, uid: int, code: str) -> None:
        """Record a user's language choice for subsequent renders."""
        if code in SUPPORTED_LANGUAGES:
            self._lang_memo()[int(uid)] = code

    def _resolve_lang(self, uid: Optional[int] = None, user_obj: Any = None) -> str:
        """Resolve user's chosen language with fail-closed English fallback.

        Resolution order: in-memory UiState → memoized resolution → persisted
        DB row → the user's own Telegram client language_code → 'en' (never the
        process-wide i18n.current, which is shared across all users and would
        leak one user's language choice into every other tenant's renders)."""
        if uid:
            states = getattr(self, "_states", None)
            if states and isinstance(states, dict):
                st = states.get(int(uid))
                if st and getattr(st, "lang", None) and st.lang in SUPPORTED_LANGUAGES:
                    return st.lang

            memo = self._lang_memo().get(int(uid))
            if memo and memo in SUPPORTED_LANGUAGES:
                return memo

            users = getattr(self, "_users", None)
            if users is not None and getattr(users, "_db", None) is not None:
                try:
                    row = users._db.query_one("SELECT language FROM users WHERE user_id=?", (int(uid),))
                    if row and row.get("language") and row["language"].lower() in SUPPORTED_LANGUAGES:
                        resolved = row["language"].lower()
                        self.remember_language(int(uid), resolved)
                        if states and isinstance(states, dict) and int(uid) in states:
                            states[int(uid)].lang = resolved
                        return resolved
                except Exception:
                    pass

        if user_obj and getattr(user_obj, "language_code", None):
            code = str(user_obj.language_code).lower().split("-")[0].split("_")[0]
            if code in SUPPORTED_LANGUAGES:
                return code

        # No identity at all, and no stored preference. That is not the same as
        # "anonymous": several rendered builders (_main_menu,
        # _client_settings_menu, _upgrade_text, the per-rule renderers) are
        # called with no uid from inside an already-wrapped update. The update's
        # own language, isolated on the render ContextVar by
        # _localized_handler, is exactly the user being rendered for, so a bare
        # "en" here painted the panel Persian and the keyboard English. The
        # contextvar lives for ONE update and is reset after it, so this cannot
        # leak between tenants; background callers see None and keep "en".
        return _BOT_RENDER_LANGUAGE.get() or "en"

    def _lang_for(self, *sources: Any, uid: Optional[int] = None) -> str:
        """Resolve the render language from whatever identity is at hand.

        Renderers that cannot take an explicit ``lang`` argument (rule
        menus, keypads, step handlers) call this with whatever identity
        they hold — ``rule.owner_user_id``, ``st`` (UiState) or a
        message's ``from_user`` — so they never fall back to a shared
        process-wide language.

        Resolution: UiState → persisted preference → the user's own
        Telegram client language_code → ``en``. Never reads
        ``i18n.current`` (process-global; would leak between users).
        """
        user_obj = None
        resolved_uid = uid
        for src in sources:
            if src is None:
                continue
            if isinstance(src, int):
                resolved_uid = resolved_uid or src
            elif isinstance(src, str) and src.isdigit():
                resolved_uid = resolved_uid or int(src)
            elif hasattr(src, "language_code"):
                user_obj = user_obj or src
            else:
                # Rule-like object exposing owner_user_id (ForwardRule,
                # drafts) or UiState exposing user_id.
                owner = getattr(src, "owner_user_id", None)
                if owner is None:
                    owner = getattr(src, "user_id", None)
                if isinstance(owner, int) and owner > 0:
                    resolved_uid = resolved_uid or owner
                elif isinstance(owner, str) and owner.isdigit():
                    resolved_uid = resolved_uid or int(owner)
                if hasattr(src, "lang") and not user_obj:
                    # UiState.lang is the in-memory per-user preference;
                    # _resolve_lang reads it from self._states directly.
                    pass
        return self._resolve_lang(resolved_uid, user_obj)

    def _state(self, uid: int, user: Any = None) -> UiState:
        st = self._states.get(uid)
        if st is None:
            st = UiState()
            resumed, updated_at = self._load_state_sync(uid)
            if resumed:
                st = UiState.from_dict(resumed)
                if updated_at:
                    self._touch_map()[uid] = updated_at
                logger.debug("resumed ui state for %s → %s", uid, st.step)
            self._states[uid] = st
        st.user_id = int(uid)
        if not getattr(st, "lang", ""):
            st.lang = self._resolve_lang(uid, user)
        self._expire_stale_step(uid, st)
        return st

    def _user_lock(self, uid: int) -> "fsm_arbiter.UserGate":
        """The per-user mutex for state-mutating entry points.

        pyrogram dispatches updates to a worker pool, so two messages from one
        user can be in flight at once: a command settling the flow while the
        message it referred to is still being routed, or a double-tap on two
        wizard fields. Both would read the same `UiState`, and the later write
        wins with a step the other update already acted on. One lock per user
        serialises the decision points (the arbiter, the free-text router, the
        panel dispatcher) without touching the ~250 handlers that only read.

        The gate is re-entrant (`fsm_arbiter.UserGate`): a handler that already
        holds it — the free-text router, then a step handler that settles the
        same flow — continues instead of deadlocking on itself. Gates are keyed
        by event loop too, because `asyncio.Lock` binds the loop it first waits
        in and a process that runs more than one loop (a test, a script) would
        otherwise fail with "bound to a different event loop".
        """
        try:
            loop: Any = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        locks = getattr(self, "_user_locks", None)
        if locks is None:
            locks = self._user_locks = {}
        entry = locks.get(uid)
        if entry is None or entry[0] is not loop:
            entry = locks[uid] = (loop, fsm_arbiter.UserGate())
        return entry[1]

    def _touch_map(self) -> Dict[int, float]:
        """The per-user step clock, created on demand.

        `ProBotUI` is built without `__init__` in the test harnesses (and by
        `__new__` in the CLI paths), so a lazily created map keeps the idle
        clock from turning a missing attribute into a persist failure.
        """
        touches = getattr(self, "_step_touch", None)
        if touches is None:
            touches = self._step_touch = {}
        return touches

    def _expire_stale_step(self, uid: int, st: UiState) -> None:
        """Drop a prompt nobody answered for too long.

        A step is an armed question. Left armed across a restart it outlives its
        own context: the user comes back the next day, types anything, and the
        text is consumed by a prompt from a flow they have forgotten. An idle
        window is therefore part of the state contract, and expiry is logged so
        "the bot answered the wrong thing" is diagnosable after the fact.
        """
        step = str(getattr(st, "step", "") or "")
        if not step or fsm_arbiter.is_protected(step):
            return
        now = time.time()
        touches = self._touch_map()
        touch = touches.get(uid)
        if not touch:
            touches[uid] = now
            return
        idle = now - touch
        if idle <= UI_STEP_TTL_SECONDS:
            return
        touches[uid] = now
        result = fsm_arbiter.settle(st, reason=f"stale:{int(idle)}s")
        logger.info(
            "[PIPELINE:FSM] expired stale step=%r uid=%s idle=%ds cleared_arms=%s",
            step, uid, int(idle), ",".join(result.arms_cleared) or "-",
        )
        try:
            asyncio.get_running_loop().create_task(self._persist(uid, st))
        except RuntimeError:
            # No loop (a sync caller, a unit test): the row keeps the old step
            # until the next write, which is harmless — every reader now sees
            # the settled in-memory state.
            pass

    def _load_state_sync(self, uid: int) -> Tuple[Optional[dict], float]:
        """Best-effort synchronous read of persisted state (sqlite is sync).

        Returns the resume payload and the row's last-write time, so a resumed
        flow can be aged exactly like a live one.
        """
        repo = self._ui_state_repo
        if repo is None:
            return None, 0.0
        db = getattr(repo, "_db", None)
        if db is None:
            return None, 0.0
        try:
            row = db.query_one(
                "SELECT step, buffer, updated_at FROM ui_states WHERE user_id=?",
                (int(uid),),
            )
        except Exception:
            return None, 0.0
        if not row:
            return None, 0.0
        try:
            updated_at = float(row["updated_at"] or 0)
        except (TypeError, ValueError, KeyError, IndexError):
            updated_at = 0.0
        return {"step": row["step"] or "", "buffer": db.loads(row["buffer"], {})}, updated_at

    async def _settle_flow(self, uid: int, reason: str = "") -> bool:
        """Settle a pending prompt because a command took over (see fsm_arbiter)."""
        st = self._state(uid)
        result = fsm_arbiter.settle(st, reason=reason)
        if result.preserved:
            logger.info(
                "[PIPELINE:FSM] preserved step=%r for uid=%s (%s)",
                result.preserved, uid, reason,
            )
        if not result.touched:
            return False
        await self._persist(uid, st)
        logger.info(
            "[PIPELINE:FSM] settled pending flow for uid=%s (%s): step=%r arms=%s",
            uid, reason or "-", result.step_cleared or "-",
            ",".join(result.arms_cleared) or "-",
        )
        return True

    async def _handle_free_text(self, message: Message) -> None:
        """Route one free-text message through the pending flow (the body of the
        group -1 handler, kept on the class so it can be driven by tests and
        called under `_user_lock`).
        """
        # Every step of every flow is traced: if the bot ever goes quiet
        # again, the log shows exactly which step died and why.
        uid = message.from_user.id if message.from_user else 0
        text = (message.text or "").strip()
        preview = (text[:40] + "...") if len(text) > 40 else text
        step = ""
        try:
            step = self._state(uid).step or ""
        except Exception as exc:
            logger.warning("Could not read state for %s: %s", uid, exc)

        logger.info("ProBotUI._text: uid=%s step=%r text=%r", uid, step, preview)

        if not step:
            # Not in an active UI flow: hand the message to BotManager in
            # group 1. `continue_propagation()` works by raising, so it is
            # never wrapped in a try/except — `except Exception` catches
            # the signal and the message goes nowhere.
            message.continue_propagation()

        try:
            self._log.info(
                "bot", "fsm",
                f"text in from {uid} step={step}: {preview!r}")
        except Exception:
            pass
        try:
            await self._route_text(message)
        except Exception as exc:
            if self._is_propagation_signal(exc):
                # A step handler decided the message is not its own: the
                # signal must reach the dispatcher, so never absorb it.
                raise
            logger.exception("ProBotUI._route_text failed for %s step=%s: %s", uid, step, exc)
            try:
                self._log.error(
                    "bot", "fsm",
                    f"route_text failed for {uid} step={step}: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            try:
                await message.reply_text(
                    self._t("ui_internal_error"),
                    reply_markup=self._cancel_kbd())
            except Exception:
                pass
        # The flow owns this message, so stop here. This call used to sit
        # inside `except Exception: pass`, which swallowed the
        # StopPropagation exception itself: BotManager (group 1) then
        # answered the same message as well, and the user got the flow's
        # answer followed by "use the buttons below" for the same text.
        message.stop_propagation()

    async def _persist(self, uid: int, st: UiState) -> None:
        """Durable save through the pro state manager.

        The manager validates the step namespace and bounds the buffer of the
        payload it stores, so a runaway flow cannot grow the `ui_states` row
        forever, while the object the screen renders from keeps every value the
        screen needs. The stored payload is versioned, so the next process
        resumes against the same screen stack.
        """
        # The manager bounds its own durable copy (absorb() and the v2 payload
        # both run _safe_buffer); it keeps the caller's object as the live
        # render state. Bounding the live object here emptied the very buffer
        # the screen is rendered from: a fetched dialog list (~16 KiB for 100
        # chats) is larger than the per-value cap, so the picker rendered
        # "dialog list not found" on the same press that had just fetched 100
        # chats. Validate the step namespace only; leave the buffer alone.
        from .ui_state_manager import _safe_step
        try:
            stn = st if isinstance(st, UiState) else UiState.from_dict(
                st if isinstance(st, dict) else {})
            stn.step = _safe_step(stn.step or "")
            st.user_id = int(uid)
            self._touch_map()[uid] = time.time()
        except Exception:
            logger.debug("ui state mirror failed", exc_info=True)
            return
        try:
            await self._ui_manager.absorb(uid, stn)
        except Exception:
            logger.debug("ui state persist failed", exc_info=True)

    def _toggle_toast(self, key: str, on: bool) -> str:
        """Callback-toast line for a settings toggle: '<Label>: <ON/OFF>'."""
        return self._t(key, state=self._t("ui_status_on" if on else "ui_status_off"))

    def _toggle_count_toast(self, key: str, **kw: Any) -> str:
        """Callback-toast line for a settings count (placeholders filled)."""
        return self._t(key, **kw)

    def _t(self, key: str, lang: Optional[str] = None, **kw) -> str:
        # Explicit locale wins; otherwise use the current update's isolated locale.
        cur_lang = lang or _BOT_RENDER_LANGUAGE.get() or "en"
        return self.i18n.t(key, lang=cur_lang, **kw)

    async def notify_task_event(self, owner_user_id: int, rule: Any, payload: Dict[str, Any]) -> bool:
        """Tell a task's owner that the engine changed the task's state.

        Called from the delivery worker (auto-pause on a dead chat, an account
        that cannot send). Best effort by contract: it returns False instead of
        raising, because a failed notification must never fail a delivery.
        """
        try:
            lang = self._lang_for(rule)
            kind = str(payload.get("kind") or "")
            if kind == "task_paused":
                text = self._t(
                    "ui_task_pause_reason",
                    lang=lang,
                    reason=payload.get("code") or payload.get("reason") or "?",
                    chat=payload.get("chat_id") or "\u2014",
                    scope=payload.get("scope") or "chat",
                )
                kbd = self._kbd([[
                    (self._t("ui_task_paused_btn", lang=lang), f"rap_check:{rule.id}"),
                ]])
            elif kind == "account_restricted":
                text = self._t(
                    "ui_account_restricted",
                    lang=lang,
                    minutes=int(payload.get("minutes") or 1),
                )
                kbd = None
            else:
                text = self._t("ui_account_blocked", lang=lang, code=payload.get("code") or "?")
                kbd = None
            await self.bot.send_message(owner_user_id, text, reply_markup=kbd)
            return True
        except Exception as exc:
            logger.warning(
                "[PIPELINE:NOTIFY] could not deliver %s notice for rule %s: %s",
                payload.get("kind"), str(getattr(rule, "id", "?"))[:8], exc,
            )
            return False

    def _logic_word(self, mode: str, lang: Optional[str] = None) -> str:
        """The stored condition logic as a word: `ALL` -> "all conditions".

        A value we do not know is shown as stored — the toggle only ever writes
        the two, but stored criteria come from older builds, and a raw `ANY` is
        a smaller failure than a crash or an empty line.
        """
        code = str(mode or "").strip().upper()
        catalog_key = LOGIC_WORDS.get(code)
        if catalog_key is None:
            return str(mode or "")
        return self._t(catalog_key, lang=lang)

    def _btn(self, key: str, lang: Optional[str] = None, style: Optional[str] = None, **kw) -> str:
        """A localized button label with its canonical style glyph.

        The glyph is applied *after* the catalog lookup, so every locale
        renders the same decoration, exactly once — no call site needs to
        hand-roll a prefix, and a catalog value that already carries one
        is never doubled.
        """
        return decorate_button_label(key, self._t(key, lang=lang, **kw), style=style)

    def _fallback_display_name(self, uid: Optional[int], lang: str) -> str:
        """A greeting name for a user whose Telegram profile carries none.

        Both greeting screens inlined the English literals ``User {uid}`` /
        ``Valued User`` straight into a catalog template, so a user with no
        first name was greeted in English in every one of the other nine
        locales — a value leak the key-parity audit cannot see.
        """
        if uid:
            return self._t("ui_user_fallback_id", lang=lang, uid=uid)
        return self._t("ui_user_fallback_name", lang=lang)

    def _addon_label(self, key: str, lang: Optional[str] = None, enabled: Optional[bool] = None) -> str:
        """Catalog label for an add-on row, optionally carrying its state.

        The add-on grid used hardcoded English labels ("🔒 Outbound
        Webhooks", "🔒 Keyword Confirma...") for every locale; the state
        suffix is composed from the shared `ui_toggle_addon` template so
        all 10 catalogs render "<label>: <state>".
        """
        if enabled is None:
            return self._t(key, lang=lang)
        return self._t(
            "ui_toggle_addon",
            lang=lang,
            addon=self._t(key, lang=lang),
            state=self._t("ui_status_on" if enabled else "ui_status_off", lang=lang),
        )

    def _every_n_label(self, rule: ForwardRule, lang: Optional[str] = None) -> str:
        """The add-on row's label, carrying the configured interval.

        The catalogue entry says "Forward Every N"; a task at N = 5 should read
        "Every 5 messages: On" rather than a second trip into the screen to find
        out which N it is on.
        """
        every_n = int(getattr(rule, "forward_every_n", 1) or 1)
        base = (
            self._t("ui_addon_every_n_value", lang=lang, n=every_n)
            if every_n > 1 else self._t("ui_addon_every_n", lang=lang)
        )
        return self._t(
            "ui_toggle_addon",
            lang=lang,
            addon=base,
            state=self._t("ui_status_on" if every_n > 1 else "ui_status_off", lang=lang),
        )

    def _login_fail_text(self, code: str, detail: str = "", lang: Optional[str] = None) -> str:
        """The user-facing line for a LoginFlowManager failure code.

        A `LoginFlowManager` result is `"<code>"` or `"<code>:<detail>"`, and the
        code is an internal identifier. Two rules, both of which the old
        `self._t(f"ui_login_fail_{key}")` broke:

        * a failure the user cannot read is a failure they cannot act on — so a
          code with no catalog entry falls back to the generic line, never to the
          raw key; and
        * no internal detail belongs on screen — `detail` carries exception text
          (`send_failed:TimeoutError: ...`), so it goes to the log, not the chat.
        """
        key = f"ui_login_fail_{code}"
        if self.i18n.contains(key):
            # `error` is the one value a login message may show: the seconds of
            # a cooldown or a flood wait. A key that does not use it ignores the
            # argument, so nothing else can reach the screen through this.
            return self._t(key, lang=lang, error=detail)
        logger.error("no catalog entry for login failure code %r; using the generic line", code)
        if detail:
            logger.debug("login failure detail for code %r: %s", code, detail)
        return self._t("ui_login_fail", lang=lang)

    def _ai_policy_text(self, policy: str, lang: Optional[str] = None) -> str:
        """The human label for a stored AI fallback policy, or "".

        The policy comes from a draft in the UI state, which is a JSON blob that
        outlives the build that wrote it, so it can hold a value this catalog does
        not know. Returning "" is deliberate: the caller already prints the policy
        token itself, so an unknown value reads as the token alone rather than as
        the raw key `ui_ai_policy_short_<value>`.
        """
        key = f"ui_ai_policy_short_{str(policy).lower()}"
        if self.i18n.contains(key):
            return self._t(key, lang=lang)
        logger.error("no catalog entry for AI fallback policy label %r", policy)
        return ""

    @staticmethod
    async def _safe_edit(target, text: str, reply_markup=None, **kw) -> None:
        """edit_message_text without the MESSAGE_NOT_MODIFIED traceback spam.

        Telegram rejects a no-op edit (same text + same keyboard) with 400
        MESSAGE_NOT_MODIFIED — normal on double-clicks / re-renders. Swallow
        only that; everything else propagates to the dispatcher's logger.
        """
        try:
            await target.edit_message_text(text, reply_markup=reply_markup, **kw)
        except MessageNotModified:
            pass
        except Exception as exc:
            # Styled buttons use a newer TL constructor than this client's
            # layer. If anything rejects them, strip the styles once and
            # re-render rather than leaving the panel on a dead screen.
            if reply_markup is not None and styles_enabled() and is_style_rejection(exc):
                disable_styles(str(exc)[:120])
                try:
                    await target.edit_message_text(text, reply_markup=plain_markup(reply_markup), **kw)
                    return
                except MessageNotModified:
                    return
            raise

    def _kbd(self, rows) -> InlineKeyboardMarkup:
        built_rows = []
        for row in rows:
            built_row = []
            for item in row:
                if isinstance(item, InlineKeyboardButton):
                    built_row.append(item)
                    continue
                style = None
                if len(item) == 3:
                    text, data, style = item
                else:
                    text, data = item
                # One style pass for every button in the bot: the label is
                # trimmed of a doubled or drifted glyph, and the background
                # colour follows the action class (an explicit third tuple
                # element always wins).
                text = style_button_label(text)
                if style is None:
                    style = color_for_label(text)
                sdata = str(data)
                if sdata.startswith("webapp:"):
                    built_row.append(InlineKeyboardButton(text, web_app=WebAppInfo(url=sdata[7:])))
                elif sdata.startswith("http://") or sdata.startswith("https://"):
                    built_row.append(InlineKeyboardButton(text, url=data))
                elif style:
                    # (text, data, "primary"|"success"|"danger") — keyboardButtonStyle
                    built_row.append(button(text, data, style))
                else:
                    built_row.append(InlineKeyboardButton(text, callback_data=data))
            built_rows.append(built_row)
        return InlineKeyboardMarkup(built_rows)

    def _cancel_kbd(self) -> InlineKeyboardMarkup:
        return self._kbd([[(self._btn("ui_cancel"), CB["cancel"])]])

    def _code_keypad(self, st: UiState) -> InlineKeyboardMarkup:
        lang = self._lang_for(st)
        return self._kbd([
            [("1", "k:1"), ("2", "k:2"), ("3", "k:3")],
            [("4", "k:4"), ("5", "k:5"), ("6", "k:6")],
            [("7", "k:7"), ("8", "k:8"), ("9", "k:9")],
            [("⌫", "k:del"), ("0", "k:0"), (self._btn("ui_cancel", lang=lang), CB["cancel"])],
        ])

    def _login_code_text(self, st: UiState) -> str:
        lang = self._lang_for(st)
        digits = ""
        if isinstance(st.buffer, dict):
            digits = str(st.buffer.get("code_digits", "") or "")
        slots = []
        for i in range(5):
            if i < len(digits):
                slots.append(digits[i])
            else:
                slots.append("·")
        code_box = " ".join(slots)

        return (
            f"{self._t('ui_login_code')}\n\n"
            f"{self._t('ui_login_code_entered')} `[ {code_box} ]`"
        )

    def _render_welcome_onboarding(self, uid: int, user: Optional[Any] = None) -> Tuple[str, InlineKeyboardMarkup]:
        """First-run onboarding welcome screen for users without a connected Telegram session."""
        lang = self._resolve_lang(uid, user)
        display_name = ""
        if user:
            parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
            display_name = " ".join(p for p in parts if p).strip()
            if not display_name and getattr(user, "username", None):
                display_name = f"@{user.username}"
        if not display_name:
            display_name = self._fallback_display_name(uid, lang)

        text = self._t("ui_onboard_greeting", lang=lang, display_name=display_name) + self._t("ui_onboard_body", lang=lang)
        connect_btn = self._t("ui_onboard_connect_btn", lang=lang)
        support_btn = self._t("ui_onboard_support_btn", lang=lang)

        rows = [
            [(connect_btn, CB["login"])],
            [(support_btn, "https://t.me/Capxi")],
        ]
        return text, self._kbd(rows)

    async def _connect_prompt_text(self, uid: int) -> str:
        """Detailed connect guide text."""
        lang = await self._get_user_lang(uid)
        return (
            self._t("ui_connect_title", lang=lang) + "\n\n"
            + self._t("ui_connect_send_as_one", lang=lang) + "\n"
            + self._t("ui_connect_example_cmd", lang=lang) + "\n\n"
            + self._t("ui_connect_replace_note", lang=lang) + "\n"
            + self._t("ui_connect_bullet_space", lang=lang) + "\n"
            + self._t("ui_connect_bullet_no_token", lang=lang) + "\n"
            + self._t("ui_connect_bullet_two_step", lang=lang) + "\n\n"
            + self._t("ui_connect_country_example", lang=lang)
        )

    # ------------------------------------------------------------------ #
    # Menus
    # ------------------------------------------------------------------ #
    async def _get_user_lang(self, uid: int, user: Any = None) -> str:
        return self._resolve_lang(uid, user)

    async def _main_text(self, uid: int, user: Optional[Any] = None) -> str:
        """Main-menu dashboard: matches the Pro Telegram control panel design.

        Displays user details, connected sessions, applied rules, filters,
        server status, web token, service plan, forwarding metrics, and support links.
        Strictly scoped to the requesting tenant (uid).
        """
        # User details. Resolve per-user WITH the user object so the user's own
        # Telegram client language wins (never the process-wide i18n.current):
        # the greeting name below is a catalog value that needs the locale.
        lang = await self._get_user_lang(uid, user)
        display_name = ""
        if user:
            parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
            display_name = " ".join(p for p in parts if p).strip()
            if not display_name and getattr(user, "username", None):
                display_name = f"@{user.username}"
        if not display_name:
            display_name = self._fallback_display_name(uid, lang)

        uid_str = str(uid) if uid else "N/A"

        # Rules & Active tasks
        rules: list[Any] = []
        if self._rules is not None and uid:
            try:
                rules = await self._rules.list_by_owner(uid)
            except Exception:
                logger.debug("rules lookup failed in _main_text", exc_info=True)

        active_rules = sum(1 for r in rules if getattr(r, "is_active", False))
        total_rules = len(rules)

        # Filters & Replacements
        filters_: list[Any] = []
        if self._filters is not None and uid:
            try:
                filters_ = await self._filters.list_by_owner(uid)
            except Exception:
                logger.debug("filters lookup failed in _main_text", exc_info=True)

        blocked_kw = sum(len(getattr(f, "blacklist_keywords", []) or []) for f in filters_)
        allowed_kw = sum(len(getattr(f, "whitelist_keywords", []) or []) for f in filters_)

        replace_count = 0
        for r in rules:
            try:
                if hasattr(r, "replacements") and callable(r.replacements):
                    reps = r.replacements()
                    if isinstance(reps, dict):
                        replace_count += len(reps)
                elif isinstance(getattr(r, "metadata", None), dict):
                    reps = r.metadata.get("replacements")
                    if isinstance(reps, dict):
                        replace_count += len(reps)
            except Exception:
                pass

        # AI Configurations
        ai_configs: list[Any] = []
        if self._ai is not None and uid:
            try:
                ai_configs = await self._ai.list_by_owner(uid)
            except Exception:
                logger.debug("ai configs lookup failed in _main_text", exc_info=True)

        active_ai = sum(1 for a in ai_configs if getattr(a, "is_enabled", False))
        total_ai = len(ai_configs)
        active_ai_model = self._t("ui_none_label", lang=lang)
        for a in ai_configs:
            if getattr(a, "is_enabled", False):
                active_ai_model = f"{getattr(a, 'provider', '')}/{getattr(a, 'model', '')}".strip("/")
                break

        # PV Responder Config
        pv_cfg = self._pv_responder.get_config_for_user(uid) if (self._pv_responder and uid) else None
        pv_enabled = bool(pv_cfg and pv_cfg.enabled)
        pv_delays = f"{pv_cfg.typing_delay_min:.1f}-{pv_cfg.typing_delay_max:.1f}s" if pv_cfg else "N/A"

        # Sessions & Connected Phone
        sessions: list[Any] = []
        if self._sessions is not None and uid:
            try:
                sessions = await self._sessions.list_by_owner(uid)
            except Exception:
                logger.debug("sessions lookup failed in _main_text", exc_info=True)

        live_sessions = sum(1 for s in sessions if getattr(s, "is_active", False))
        total_sessions = len(sessions)

        connected_phone = self._t("ui_none_label", lang=lang)
        if sessions:
            p = getattr(sessions[0], "phone_number", None) or getattr(sessions[0], "phone", None)
            if p:
                clean_phone = str(p).lstrip("+")
                connected_phone = f"+{clean_phone}"

        # Forwarded metrics strictly scoped to user's rules
        fwd_24h = await self._forwarded_24h(uid, rules=rules)

        # Server info
        server_name = "VIP 995"

        # Web access is provisioned here so the Dashboard button always has an
        # account to hand a login code to. Nothing is minted or shown: the old
        # menu printed the first 24 characters of the session token, which put a
        # live credential in a message body.
        if self._accounts is not None:
            try:
                await self._accounts.get_or_create_credentials(
                    uid, display_name=display_name, force_reset=False
                )
            except Exception:
                pass

        # Service plan. Both words are interpolated into the catalog templates
        # below, so they have to come from the catalog themselves: a hardcoded
        # English word renders English inside every other locale's plan line.
        is_pro = bool(active_rules > 0 or live_sessions > 0)
        plan = self._t("ui_plan_pro" if is_pro else "ui_plan_free", lang=lang)
        remaining = self._t("ui_plan_status_active" if is_pro else "ui_plan_status_none", lang=lang)

        session_status = (
            self._t("ui_main_sess_active", lang=lang) if live_sessions > 0
            else (self._t("ui_main_sess_registered", lang=lang) if total_sessions > 0
                  else self._t("ui_main_sess_disconnected", lang=lang))
        )
        pv_status = self._t("ui_main_pv_on", lang=lang) if pv_enabled else self._t("ui_main_pv_off", lang=lang)
        sep = self._t("ui_sep", lang=lang)
        heavy = self._t("ui_main_sep_heavy", lang=lang)

        def _t(key, **kw):
            return self._t(key, lang=lang, **kw)

        lines = [
            _t("ui_main_welcome", name=display_name),
            "",
            _t("ui_main_service_status"),
            _t("ui_main_active_tasks", n=active_rules),
            "",
            _t("ui_main_rules_header"),
            _t("ui_main_blocked_kw", n=blocked_kw),
            _t("ui_main_allowed_kw", n=allowed_kw),
            _t("ui_main_replace_rules", n=replace_count),
            _t("ui_main_ai_configs", a=active_ai, t=total_ai, model=active_ai_model),
            _t("ui_main_pv_line", s=pv_status, d=pv_delays),
            "",
            _t("ui_main_support_1"),
            _t("ui_main_support_2"),
            "",
            heavy,
            _t("ui_main_account_header"),
            sep,
            _t("ui_main_display_name", n=display_name),
            _t("ui_main_user_id", id=uid_str),
            _t("ui_main_phone", p=connected_phone),
            _t("ui_main_server", s=server_name),
            "",
            _t("ui_main_plan_header"),
            sep,
            _t("ui_main_plan", p=plan),
            _t("ui_main_remaining", r=remaining),
            _t("ui_main_service_running"),
            "",
            _t("ui_main_tasks_header"),
            sep,
            _t("ui_main_active_tasks2", n=active_rules),
            _t("ui_main_fwd_24h", n=fwd_24h),
            _t("ui_main_total_tasks", n=total_rules),
            "",
            _t("ui_main_sessions_header"),
            sep,
            _t("ui_main_connected_sessions", a=live_sessions, b=total_sessions),
            _t("ui_main_sess_status", s=session_status),
            "",
            _t("ui_main_credits_header"),
            sep,
            _t("ui_main_balance"),
            heavy,
        ]

        return "\n".join(lines)

    async def _forwarded_24h(self, uid: int, rules: Optional[list] = None) -> int:
        """Forwarded message count in the trailing 24h for this user's rules."""
        if not uid:
            return 0
        total = 0
        if rules and self._rule_stats is not None:
            for r in rules:
                try:
                    st = await self._rule_stats.get(r.id)
                    if st:
                        total += int(st.get("forwarded") or 0)
                except Exception:
                    pass
            return total
        return 0

    async def _dlq_count(self) -> int:
        """Failed messages sitting in the dead-letter queue."""
        if self._error_log is None:
            return 0
        try:
            counts = await self._error_log.counts_by_severity()
            return int(counts.get("error", 0) or 0)
        except Exception:
            return 0

    def _main_menu(self, uid: int = 0) -> InlineKeyboardMarkup:
        cur_lang = self._resolve_lang(uid)
        is_adm = self._is_admin(uid) if uid else False
        rows = []
        if is_adm:
            rows.append([
                (self._btn("ui_main_menu_admin", lang=cur_lang), CB["admin"]),
            ])

        # Every dashboard row goes through the same funnel as the rest of the
        # bot: `_kbd` applies the canonical glyph of the catalog key and the
        # background colour of its action class. Built directly as
        # InlineKeyboardButton this panel was the one surface in the bot that
        # never reached the funnel — 450 of 450 dashboard buttons rendered
        # without a colour, and the four glyph-less entries (Forward / Spam /
        # Publish / Clone) sat as bare grey text beside their decorated
        # neighbours in all 10 locales.
        rows.extend([
            [
                # The dashboard's class colour comes from the key, never from
                # the authored copy: "🌐 Language" is navigation text that used
                # to be read as an open action and painted blue.
                (self._btn("ui_main_menu_forward", lang=cur_lang), CB["rules"],
                 color_for_key("ui_main_menu_forward")),
                (self._btn("ui_main_menu_filters", lang=cur_lang), CB["filters"],
                 color_for_key("ui_main_menu_filters")),
            ],
            [
                (self._btn("ui_main_menu_publish", lang=cur_lang), CB["rule_add"],
                 color_for_key("ui_main_menu_publish")),
                (self._btn("ui_main_menu_clone", lang=cur_lang), CB["ai"],
                 color_for_key("ui_main_menu_clone")),
            ],
            [
                (self._btn("ui_main_menu_settings", lang=cur_lang), CB["settings"],
                 color_for_key("ui_main_menu_settings")),
                (self._btn("ui_main_menu_web", lang=cur_lang), CB["dashboard"],
                 color_for_key("ui_main_menu_web")),
            ],
            [
                # The account's one purchase path carries green.
                (self._btn("ui_main_menu_upgrade", lang=cur_lang), CB["upgrade"],
                 color_for_key("ui_main_menu_upgrade")),
                (self._btn("ui_main_menu_stats", lang=cur_lang), CB["stats"],
                 color_for_key("ui_main_menu_stats")),
            ],
            [
                (self._btn("ui_main_menu_diag", lang=cur_lang), CB["diag"],
                 color_for_key("ui_main_menu_diag")),
            ],
            [
                (self._btn("ui_main_menu_help", lang=cur_lang), CB["help"],
                 color_for_key("ui_main_menu_help")),
                (self._btn("ui_main_menu_lang", lang=cur_lang), CB["lang"],
                 color_for_key("ui_main_menu_lang")),
            ],
            [
                (self._btn("ui_main_menu_support", lang=cur_lang), "https://t.me/Capxi"),
            ],
            [
                (self._btn("ui_main_menu_sessions", lang=cur_lang), CB["sessions"],
                 color_for_key("ui_main_menu_sessions")),
            ],
        ])
        return self._kbd(rows)

    def _cancelled_connect_texts(self, lang: str) -> Tuple[str, str, str]:
        """The cancelled-connect screen: (message, retry label, menu label).

        These three lines were hardcoded per language, which meant Russian and
        Chinese lived in the source and the other six supported languages were
        served English. They are catalog keys like everything else, so every
        locale renders its own text.
        """
        return (
            self._t("ui_cancelled", lang=lang),
            self._t("ui_retry_connect", lang=lang),
            self._t("ui_back_main_menu", lang=lang),
        )

    def _upgrade_text(self, uid: int = 0) -> str:
        """Render upgrade information in the account's selected language."""
        return self._t("ui_upgrade_body", lang=self._resolve_lang(uid))

    def _client_settings_menu(self, uid: int = 0) -> InlineKeyboardMarkup:
        lang = self._resolve_lang(uid)
        is_admin = self._is_admin(uid) if uid else False
        rows = [
            [(self._btn("ui_settings_creds", lang=lang), CB["client_creds"],
              color_for_key("ui_settings_creds"))],
            [
                (self._btn("ui_settings_lang", lang=lang), CB["lang"],
                 color_for_key("ui_settings_lang")),
                (self._btn("ui_settings_plan", lang=lang), CB["upgrade"],
                 color_for_key("ui_settings_plan")),
            ],
            [(self._btn("ui_settings_webtoken", lang=lang), CB["webpass"],
              color_for_key("ui_settings_webtoken"))],
        ]
        if is_admin:
            rows.append([(self._btn("ui_settings_admin", lang=lang), CB["admin"],
                          color_for_key("ui_settings_admin"))])
        rows.append([(self._btn("ui_back_main_menu", lang=lang), CB["main"],
                      color_for_key("ui_back_main_menu"))])
        return self._kbd(rows)

    async def _render_admin_panel(self, uid: int) -> tuple[str, InlineKeyboardMarkup]:
        # Fail closed at the renderer, not only at each caller. The panel body
        # carries install-wide numbers (every tenant's user count, every active
        # task, the dead-letter count) and the forced-subscription channel, so a
        # single ungated caller — one was, the broadcast *cancel* button — used
        # to hand all of it to whoever pressed. Asking here means a future
        # caller cannot reopen that by forgetting a check.
        if not self._is_admin(uid):
            logger.warning("[ADMIN] refused to render the admin panel for non-admin uid=%s", uid)
            lang = self._resolve_lang(uid)
            return (
                self._t("ui_rule_not_found_or_not_owner", lang=lang),
                self._kbd([[(self._btn("ui_back_main_menu", lang=lang), CB["back"])]]),
            )
        lang = self._resolve_lang(uid)
        total_users = 0
        active_rules = 0
        dlq_errors = await self._dlq_count()
        fs_channel = ""
        fs_enabled = False

        if self._users:
            try:
                total_users = await self._users.count_all()
            except Exception:
                logger.exception("Could not count registered users for admin panel")
        if self._rules:
            try:
                all_rules = await self._rules.list_all()
                active_rules = sum(1 for rule in all_rules if getattr(rule, "enabled", True))
            except Exception:
                logger.exception("Could not count active rules for admin panel")
        if self._db:
            try:
                enabled_row = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_enabled'")
                fs_enabled = bool(enabled_row and enabled_row["value"] == "1")
                channel_row = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_channel'")
                if channel_row and channel_row["value"]:
                    fs_channel = str(channel_row["value"])
            except Exception:
                logger.exception("Could not read forced-subscription settings")

        fs_status = self._t(
            "ui_admin_force_active" if fs_enabled and fs_channel else "ui_admin_force_inactive",
            lang=lang,
        )
        if fs_enabled and fs_channel:
            fs_status = f"{fs_status} ({fs_channel})"
        body = self._t(
            "ui_admin_panel_body",
            lang=lang,
            total_users=total_users,
            active_rules=active_rules,
            dlq_errors=dlq_errors,
            fs_status=fs_status,
        )
        rows = [
            [
                (self._btn("ui_admin_btn_metrics", lang=lang), CB["adm_stats"],
                 color_for_key("ui_admin_btn_metrics")),
                (self._btn("ui_admin_btn_users", lang=lang), CB["adm_users"],
                 color_for_key("ui_admin_btn_users")),
            ],
            [
                (self._btn("ui_admin_btn_broadcast", lang=lang), CB["adm_bcast"],
                 color_for_key("ui_admin_btn_broadcast")),
                (self._btn("ui_admin_btn_force_sub", lang=lang), CB["adm_forcesub"],
                 color_for_key("ui_admin_btn_force_sub")),
            ],
            [
                (self._btn("ui_admin_btn_admins", lang=lang), CB["adm_admins"],
                 color_for_key("ui_admin_btn_admins")),
                (self._btn("ui_admin_btn_credentials", lang=lang), CB["creds"],
                 color_for_key("ui_admin_btn_credentials")),
            ],
            [
                (self._btn("ui_admin_btn_config", lang=lang), CB["adm_config"],
                 color_for_key("ui_admin_btn_config")),
                (self._btn("ui_admin_btn_migrate", lang=lang), CB["migrate"],
                 color_for_key("ui_admin_btn_migrate")),
            ],
            [(self._btn("ui_back_main_menu", lang=lang), CB["main"],
              color_for_key("ui_back_main_menu"))],
        ]
        return body, self._kbd(rows)

    def _migrate_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [(self._btn("ui_migrate_export"), CB["migrate_export"])],
            [(self._btn("ui_migrate_export_enc"), CB["migrate_export_enc"])],
            [(self._btn("ui_migrate_import"), CB["migrate_import"])],
            [(self._btn("ui_migrate_help"), CB["migrate_help"])],
            [(self._btn("ui_back"), CB["back"])],
        ])

    def _sessions_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [(self._btn("ui_login"), CB["login"])],
            [(self._btn("ui_backup"), CB["backup"])],
            [(self._btn("ui_back"), CB["main"])],
        ])

    def _rules_menu(
        self,
        rules: Optional[list] = None,
        page: int = 0,
        page_size: int = 5,
        lang: Optional[str] = None,
    ) -> InlineKeyboardMarkup:
        cur_lang = lang or _BOT_RENDER_LANGUAGE.get() or "en"
        rows: List[List[Tuple[str, str]]] = [
            [(self._btn("ui_rule_add", lang=cur_lang), CB["rule_add"])],
            # Start a route by listing its sources and targets; the task is
            # created on save and carries as many of each as the user sends.
            [
                (self._t("ui_route_add_sources", lang=cur_lang, count=0),
                 self._route_cb(CB["route_src_list"], "s", ROUTE_SCOPE_NEW)),
                (self._t("ui_route_add_targets", lang=cur_lang, count=0),
                 self._route_cb(CB["route_tgt_list"], "t", ROUTE_SCOPE_NEW)),
            ],
        ]
        if rules:
            total_items = len(rules)
            total_pages = max(1, (total_items + page_size - 1) // page_size)
            page = max(0, min(page, total_pages - 1))
            start = page * page_size
            end = min(start + page_size, total_items)
            page_rules = rules[start:end]

            for r in page_rules:
                status_icon = "🟢" if r.is_active else "🔴"
                src = (r.source_chat_name or r.source_chat_id)[:12]
                dst = (r.target_chat_name or r.target_chat_id)[:12]
                rows.append([
                    (f"{status_icon} {src} ➔ {dst}", f"rd:{r.id}"),
                    ("📊", f"fwd_st:{r.id}"),
                    (self._t("ui_rule_edit_btn", lang=cur_lang), f"re:{r.id}"),
                    ("🗑", f'{CB["rule_del"]}:{r.id}'),
                ])

            if total_pages > 1:
                nav_row: List[Tuple[str, str]] = []
                if page > 0:
                    nav_row.append((self._btn('ui_prev', lang=cur_lang), f"r_pg:{page - 1}"))
                nav_row.append((f"📄 {page + 1} / {total_pages}", "noop"))
                if page < total_pages - 1:
                    nav_row.append((f"{self._t('ui_next', lang=cur_lang)} ▶️", f"r_pg:{page + 1}"))
                rows.append(nav_row)

        rows.append([(self._btn("ui_back", lang=cur_lang), CB["main"])])
        return self._kbd(rows)

    def _render_rules_list(
        self, rules: Optional[list] = None, page: int = 0, page_size: int = 5
    ) -> Tuple[str, InlineKeyboardMarkup]:
        rules_list = rules if rules is not None else []
        # Resolve the language from the first rule's owner — all rules in
        # this list belong to the same tenant (owner-scoped queries).
        lang = self._lang_for(rules_list[0]) if rules_list else None
        lines = [self._t("ui_rules_title", lang=lang), ""]
        if not rules_list:
            lines.append(self._t("ui_none", lang=lang))
            return "\n".join(lines), self._rules_menu([], page=0, page_size=page_size)

        total_items = len(rules_list)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        end = min(start + page_size, total_items)
        page_items = rules_list[start:end]

        for idx, r in enumerate(page_items, start=start + 1):
            status = "🟢" if r.is_active else "🔴"
            target = getattr(r, "target_label", None) or (r.target_chat_name or r.target_chat_id)
            lines.append(
                f"{status} **{idx}.** `{r.id[:8]}` **{r.source_chat_name or r.source_chat_id}** ➔ **{target}**"
            )
            mode = getattr(r.forward_mode, "value", r.forward_mode)
            delay = float(getattr(r, "delay_seconds", 0) or 0)
            lines.append(
                "     " + self._t("ui_rules_row_meta", lang=lang, mode=mode, delay=f"{delay:.0f}")
            )

        if total_pages > 1:
            lines.append("")
            lines.append(self._t("ui_rules_page_info", lang=lang, page=page + 1, total=total_pages, count=total_items))

        return "\n".join(lines), self._rules_menu(rules_list, page=page, page_size=page_size, lang=lang)

    def _status_label(self, rule, lang: Optional[str] = None) -> str:
        """The task's status as the user must read it: the engine's own state.

        Derived from `ForwardRule.status_state()`, which folds both mechanisms
        that stop a task (the on/off switch and the dashboard pause) into one
        value. Reading `is_active` alone is what displayed a paused task — one
        whose messages the pipeline was dropping and whose jobs the worker was
        postponing — as "RUNNING".
        """
        from ...domain.entities import (
            STATUS_PAUSED,
            STATUS_PAUSED_UNTIL,
            STATUS_RUNNING,
        )

        state = rule.status_state() if hasattr(rule, "status_state") else (
            STATUS_RUNNING if getattr(rule, "is_active", False) else STATUS_PAUSED
        )
        if state == STATUS_RUNNING:
            return self._t("ui_rule_status_running", lang=lang)
        if state == STATUS_PAUSED_UNTIL:
            until = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(int(getattr(rule, "paused_until", 0) or 0))
            )
            return self._t("ui_rule_status_paused_until", lang=lang, until=until)
        if state == STATUS_PAUSED:
            return self._t("ui_rule_status_paused_manual", lang=lang)
        return self._t("ui_rule_status_paused", lang=lang)

    def _render_rule_detail(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render the complete Task Management & Routing Overview (Screens 1, 4, 5)."""
        task_name = rule.name or f"task_{rule.id[:8]}"
        src_label = rule.source_chat_name or rule.source_chat_id or self._t("ui_rule_source_fallback", lang=lang)
        dst_label = getattr(rule, "target_label", None) or (
            rule.target_chat_name or rule.target_chat_id or self._t("ui_rule_target_fallback", lang=lang)
        )

        status_text = self._status_label(rule, lang=lang)
        debug_text = self._t("ui_rule_on", lang=lang) if rule.debug_mode else self._t("ui_rule_off", lang=lang)
        sender_type = rule.sender_type or self._t("ui_rule_sender_personal", lang=lang)
        topic_str = f"#{rule.topic_id}" if rule.topic_id else self._t("ui_none_label", lang=lang)
        fwd_hdr_status = self._t("ui_rule_yes", lang=lang) if rule.show_forward_header else self._t("ui_rule_no", lang=lang)
        dup_status = self._t("ui_rule_on", lang=lang) if rule.duplicate_filters else self._t("ui_rule_off", lang=lang)
        rep_sync_status = self._t("ui_rule_on", lang=lang) if rule.sync_replies else self._t("ui_rule_off", lang=lang)
        edit_sync_status = self._t("ui_rule_off", lang=lang) if rule.ignore_edits else self._t("ui_rule_on", lang=lang)
        del_sync_status = self._t("ui_rule_on", lang=lang) if rule.sync_deletes else self._t("ui_rule_off", lang=lang)
        cleaner_status = self._t("ui_rule_on", lang=lang) if rule.cleaner_enabled else self._t("ui_rule_off", lang=lang)

        warning_line = (
            self._t("ui_rule_detail_header_warning", lang=lang)
            if rule.show_forward_header
            else ""
        )

        bl_count = len(rule.blacklist)
        wl_count = len(rule.whitelist)
        cl_count = rule.cleaner_active_option_count
        fl_count = rule.filter_active_option_count
        rep_count = len(rule.replacements)
        delay_val = f"{rule.delay_seconds:.0f}s" if rule.delay_seconds > 0 else "0s"
        # An engine pause (dead source/target) must be visible *here*: the
        # generic "PAUSED" alone tells the owner nothing about what to fix.
        ap = rule.auto_pause if getattr(rule, "auto_pause", None) else None
        if ap:
            status_text = self._t("ui_task_paused_status", lang=lang)
            warning_line = "\n" + self._t(
                "ui_task_pause_reason",
                lang=lang,
                reason=ap.get("reason", "unavailable"),
                chat=ap.get("chat_id", "") or "\u2014",
                scope=ap.get("scope", "") or "chat",
            )
        # Who the copies are sent as. The bot route only shows as "Bot" when a
        # token is actually attached and active, so the button never promises a
        # sender the task cannot use.
        transport_state = self._t(
            "ui_route_transport_bot" if rule.delivery_transport == "BOT" else "ui_route_transport_account",
            lang=lang,
        )
        token_label = rule.bot_token_label or self._t("ui_route_token_none", lang=lang)

        grp = rule.feature_group or "all"
        chips = {
            "all": self._t("ui_rule_chip_all", lang=lang),
            "crypto": self._t("ui_rule_chip_crypto", lang=lang),
            "mmo": self._t("ui_rule_chip_mmo", lang=lang),
            "content": self._t("ui_rule_chip_content", lang=lang),
        }

        # Header and Task Overview text matching the Telegram Mini App & Mobile screen
        text = (
            self._t("ui_rule_detail_title", lang=lang) + "\n"
            + self._t("ui_rule_detail_sep", lang=lang) + "\n"
            + self._t("ui_rule_detail_task_name", lang=lang, task_name=task_name) + "\n\n"
            + self._t("ui_rule_detail_overview", lang=lang) + "\n"
            + self._t("ui_rule_detail_fwd_status", lang=lang, status_text=status_text) + "\n"
            + self._t("ui_rule_detail_source", lang=lang, src_label=src_label, source_chat_id=rule.source_chat_id) + "\n"
            + self._t("ui_rule_detail_target", lang=lang, dst_label=dst_label, target_chat_id=rule.target_chat_id) + "\n"
            + self._t("ui_rule_detail_show_header", lang=lang, fwd_hdr_status=fwd_hdr_status) + warning_line + "\n"
            + self._t("ui_rule_detail_debug_topic", lang=lang, debug_text=debug_text, topic_str=topic_str) + "\n\n"
            + self._t("ui_rule_detail_features", lang=lang) + "\n"
            + self._t("ui_rule_detail_sync_row1", lang=lang, edit_sync_status=edit_sync_status, del_sync_status=del_sync_status, rep_sync_status=rep_sync_status) + "\n"
            + self._t("ui_rule_detail_sync_row2", lang=lang, fwd_hdr_status=fwd_hdr_status, dup_status=dup_status) + "\n"
            + self._t("ui_rule_detail_sync_row3", lang=lang, cleaner_status=cleaner_status, delay_val=delay_val) + "\n\n"
            + self._t("ui_rule_detail_sender", lang=lang, sender_type=sender_type) + "\n"
            # Where the copies come from — visible on the task itself, not
            # only one screen deeper, because it changes the sender identity.
            + self._t("ui_rule_btn_route", lang=lang, state=transport_state) + "\n"
            + self._t("ui_rule_btn_bot_token", lang=lang, label=token_label) + "\n"
            + self._t("ui_rule_detail_paid_addon", lang=lang) + "\n"
        )

        rows: List[List[Tuple[str, str]]] = [
            # Top routing controls
            [
                (self._t("ui_rule_edit_source", lang=lang), f"res:{rule.id}"),
                (self._t("ui_rule_edit_target", lang=lang), f"ret:{rule.id}"),
            ],
            # M sources -> N targets: one screen per side, with the current
            # count on each button (the wizard behind them has no cap).
            [
                (self._t("ui_route_add_sources", lang=lang, count=len(rule.source_chat_ids)),
                 self._route_cb(CB["route_src_list"], "s", rule.id)),
                (self._t("ui_route_add_targets", lang=lang, count=len(rule.all_targets)),
                 self._route_cb(CB["route_tgt_list"], "t", rule.id)),
            ],
            [
                (self._t("ui_rule_btn_header", lang=lang, fwd_hdr_status=fwd_hdr_status), f"rtg_fwdhdr:{rule.id}"),
                (self._t("ui_rule_btn_topic", lang=lang, topic_str=topic_str), f"rtpc:{rule.id}"),
            ],
            # Features: 5 Toggle Pills (matching mobile screen)
            [
                (self._t("ui_rule_btn_edit", lang=lang, edit_sync_status=edit_sync_status), f"red:{rule.id}"),
                (self._t("ui_rule_btn_delete", lang=lang, del_sync_status=del_sync_status), f"rdl:{rule.id}"),
                (self._t("ui_rule_btn_reply", lang=lang, rep_sync_status=rep_sync_status), f"rtg_repsync:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_header", lang=lang, fwd_hdr_status=fwd_hdr_status), f"rtg_fwdhdr:{rule.id}"),
                (self._t("ui_rule_btn_dup", lang=lang, dup_status=dup_status), f"rtg_dup:{rule.id}"),
            ],
            # 6 Modular Option Cards (matching mobile screen)
            [
                (self._t("ui_rule_btn_replace", lang=lang, rep_count=rep_count), f"rtx:{rule.id}"),
                (self._t("ui_rule_btn_blacklist", lang=lang, bl_count=bl_count), f"rfl_bl:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_whitelist", lang=lang, wl_count=wl_count), f"rfl_wl:{rule.id}"),
                (self._t("ui_rule_btn_cleaner", lang=lang, cl_count=cl_count), f"rcl:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_filters", lang=lang, fl_count=fl_count), f"rfl:{rule.id}"),
                (self._t("ui_rule_btn_delay", lang=lang, delay_val=delay_val), f"rdly:{rule.id}"),
            ],
            # Task operations
            [
                (self._t("ui_rule_btn_status", lang=lang, status_text=status_text), f"rtg:{rule.id}:{rule.status_state()}"),
            ]
            + ([
                (self._t("ui_task_paused_btn", lang=lang), f"rap_check:{rule.id}"),
            ] if ap else [])
            + [
                (self._t("ui_rule_btn_duplicate", lang=lang), f"rdup:{rule.id}"),
                (self._t("ui_rule_btn_rename", lang=lang), f"rrn:{rule.id}"),
                (self._t("ui_rule_btn_draft", lang=lang), f"re:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_advanced", lang=lang), f"radv:{rule.id}"),
                # The curated panels cover the common path; this one is the whole
                # registry, so a setting that has no hand-written panel is still
                # reachable without leaving Telegram.
                (self._t("ui_reg_btn_all", lang=lang), f"rge:g:{rule.id}"),
                (self._t("ui_rule_btn_delete_task", lang=lang), f"rrm:{rule.id}"),
            ],
            # Route: the owner's account, or a bot they attached. The token row
            # is where that bot is chosen; the token itself is never shown back.
            [
                (self._t("ui_rule_btn_route", lang=lang, state=transport_state), f"rdt:{rule.id}"),
                (self._t("ui_rule_btn_bot_token", lang=lang, label=token_label), f"rbt:{rule.id}"),
            ],
            # Feature groups tabs / chips
            [
                (f"● {chips['all']}" if grp == "all" else chips["all"], f"rgrp:{rule.id}:all"),
                (f"● {chips['crypto']}" if grp == "crypto" else chips["crypto"], f"rgrp:{rule.id}:crypto"),
                (f"● {chips['mmo']}" if grp == "mmo" else chips["mmo"], f"rgrp:{rule.id}:mmo"),
                (f"● {chips['content']}" if grp == "content" else chips["content"], f"rgrp:{rule.id}:content"),
            ],
            [
                (self._btn('ui_rule_back_rules'), CB["rules"]),
            ],
        ]

        # Filtered Add-ons grid based on selected feature group
        addons = rule.paid_addons
        if grp == "crypto":
            rows.extend([
                [
                    (self._t("ui_rule_grp_crypto_mode", lang=lang, state=self._t("ui_rule_on", lang=lang) if rule.crypto_mode else self._t("ui_rule_off", lang=lang)), f"rtg_crypto:{rule.id}"),
                    (self._t("ui_rule_addon_sig_format", lang=lang), f"radon:{rule.id}:sig_format"),
                ],
                [
                    (self._t("ui_rule_addon_wallet_clean", lang=lang), f"radon:{rule.id}:wallet_clean"),
                    (self._t("ui_rule_addon_webhook", lang=lang), f"radon:{rule.id}:webhook"),
                ],
            ])
        elif grp == "mmo":
            ak_on = rule.affiliate_kit.get("enabled", False)
            rows.extend([
                [
                    (self._t("ui_toggle_affiliate_kit", lang=lang, state=self._t("ui_status_on" if ak_on else "ui_status_off", lang=lang)), f"rafl:{rule.id}"),
                    (self._addon_label("ui_rule_addon_webhook", lang), f"radon:{rule.id}:webhook"),
                ],
                [
                    (self._t("ui_adv_btn_link_buttons", lang=lang, count=len(rule.link_buttons)), f"rbtn:{rule.id}"),
                    (self._addon_label("ui_addon_auto_del", lang), f"radon:{rule.id}:auto_del"),
                ],
            ])
        elif grp == "content":
            c_comm = addons.get("channel_comments", False)
            rows.extend([
                [
                    (self._addon_label("ui_addon_comments", lang, c_comm), f"radon:{rule.id}:comments"),
                    (self._every_n_label(rule, lang), f"radon:{rule.id}:every_n"),
                ],
                [
                    (self._addon_label("ui_addon_kw_confirm", lang), f"radon:{rule.id}:kw_confirm"),
                    (self._addon_label("ui_addon_kw_reply", lang), f"radon:{rule.id}:kw_reply"),
                ],
                [
                    (self._addon_label("ui_addon_auto_del", lang), f"radon:{rule.id}:auto_del"),
                    (self._addon_label("ui_rule_addon_watermark", lang), f"rwm:{rule.id}"),
                ],
            ])
        else:  # "all" (matches Image 4 exactly)
            c_comm = addons.get("channel_comments", False)
            ev_n = int(getattr(rule, "forward_every_n", 1) or 1) > 1
            kw_c = addons.get("kw_confirm", False)
            kw_r = addons.get("kw_reply", False)
            a_del = addons.get("auto_del", False)
            wh = addons.get("webhook", False)

            rows.extend([
                [
                    (self._addon_label("ui_addon_comments", lang, c_comm), f"radon:{rule.id}:comments"),
                    (self._every_n_label(rule, lang), f"radon:{rule.id}:every_n"),
                ],
                [
                    (self._addon_label("ui_addon_kw_confirm", lang, kw_c), f"radon:{rule.id}:kw_confirm"),
                    (self._addon_label("ui_addon_kw_reply", lang, kw_r), f"radon:{rule.id}:kw_reply"),
                ],
                [
                    (self._addon_label("ui_addon_auto_del", lang, a_del), f"radon:{rule.id}:auto_del"),
                    (self._addon_label("ui_rule_addon_webhook", lang, wh), f"radon:{rule.id}:webhook"),
                ],
            ])

        rows.append([
            (self._t("ui_rule_addon_all", lang=lang), f"radon_all:{rule.id}"),
        ])
        rows.append([
            (self._btn('ui_rule_back_rules'), CB["rules"]),
        ])

        return text, self._kbd(rows)

    # ------------------------------------------------------------------ #
    # bot route: the sender identity of a task, and the token behind it   #
    # ------------------------------------------------------------------ #
    #
    # A task normally forwards as the owner's own account. It can instead
    # forward through a bot the owner attached: that bot is the middle hop, so
    # copies arrive as the bot, the owner's account is never the visible sender,
    # and Bot API extras such as inline buttons become reachable. The token is
    # the credential for that hop — written encrypted, never shown back, never
    # logged, and the message that carried it is deleted.

    async def _active_bot_token(self, token_id: str, uid: int = 0):
        """The stored token row behind an id, or None when it is unusable.

        Scoped by owner: an id transplanted from another tenant's screen must
        not resolve, let alone return a decrypted token.
        """
        token_id = str(token_id or "").strip()
        if not token_id or self._bot_tokens is None:
            return None
        try:
            if uid:
                record = await self._bot_tokens.get_owned(token_id, int(uid))
            else:
                record = await self._bot_tokens.get(token_id)
        except Exception as exc:  # a broken vault must not take the panel down
            logger.warning("[BOT ROUTE] token %s lookup failed: %s", token_id[:8], exc)
            return None
        if record is None or not getattr(record, "is_active", False) or not getattr(record, "token", ""):
            return None
        return record

    async def _rules_using_bot_token(self, uid: int, token_id: str) -> List[str]:
        """Assistant task ids that a route is currently pointing at this token."""
        token_id = str(token_id or "").strip()
        if not token_id or self._rules is None:
            return []
        owner_id = int(self._scope_uid(uid) or 0)
        try:
            owned = await self._rules.list_by_owner(owner_id)
        except Exception as exc:
            logger.warning("[BOT ROUTE] rule scan for token %s failed: %s", token_id[:8], exc)
            return []
        return [r.id for r in (owned or []) if getattr(r, "bot_token_id", "") == token_id]

    async def _validate_bot_token(self, token: str) -> Tuple[bool, Dict[str, Any]]:
        """Ask Telegram whether a pasted token works before storing it.

        A token that fails here is never written: attaching a dead token would
        turn into a task that stops forwarding, which is the worst outcome.
        """
        try:
            if self._bot_route is not None:
                me = await self._bot_route.validate(token, token_id="pasted")
            else:
                async with BotApiTransport(token) as transport:
                    me = await transport.get_me()
        except BotApiError as exc:
            logger.warning("[BOT ROUTE] pasted token refused: %s (code %s)", exc.description, exc.code)
            return False, {}
        except BotTransportUnsupported as exc:
            logger.warning("[BOT ROUTE] pasted token check unsupported: %s", exc)
            return False, {}
        except (BotTokenInvalid, ValueError):
            return False, {}
        except Exception as exc:  # network trouble: report, do not store
            logger.warning("[BOT ROUTE] pasted token check failed: %s", type(exc).__name__)
            return False, {}
        return True, dict(me or {})

    @staticmethod
    def _cb_rule_id(cq: CallbackQuery) -> str:
        """The rule id from "<verb>:<rule_id>[:...]" callback data."""
        raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
        parts = raw.split(":")
        return parts[2] if len(parts) > 2 else ""

    async def _step_registry_edit(self, message: Message, st: "UiState", text: str,
                                  rule_id: str) -> None:
        """A typed value for one registry setting: validated, then written."""
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_rule_not_found"), reply_markup=self._main_menu()
            )
        editor = self._registry_editor(rule, uid)
        idx = int(st.buffer.get("reg_key_idx", -1))
        raw = str(text or "").strip()
        spec = editor.spec_for(str(st.buffer.get("reg_key") or "")) if st.buffer.get("reg_key") in SETTING_SPECS else None
        if spec is not None and spec.kind == "list" and raw == "-":
            raw = ""  # the documented way to empty a list
        notice = await editor.set_value(rule, st, idx, raw)
        st.step = ""
        await self._persist(uid, st)
        detail, rows = editor.detail(rule, st, idx)
        await message.reply_text(f"{notice}\n\n{detail}", reply_markup=self._kbd(rows))

    async def _step_registry_search(self, message: Message, st: "UiState", text: str,
                                    rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_rule_not_found"), reply_markup=self._main_menu()
            )
        st.step = ""
        st.buffer["reg_query"] = str(text or "").strip()
        await self._persist(uid, st)
        editor = self._registry_editor(rule, uid)
        rendered, rows = editor.search(rule, st, st.buffer["reg_query"], 0, await editor.sources_for(rule))
        await message.reply_text(rendered, reply_markup=self._kbd(rows))

    def _registry_editor(self, rule: ForwardRule, uid: int) -> RegistryEditor:
        """The registry editor, bound to this rule's language and owner.

        `save` goes through `_save_owned_rule`, so a rule id transplanted into
        another user's callback data is refused at the write, exactly like every
        other bot write.
        """
        lang = self._lang_for(rule)
        return RegistryEditor(
            translate=lambda key, **params: self._t(key, lang=lang, **params),
            # A group is defined by the registry, so its title is normally in the
            # catalog; `contains` lets `label` prove that instead of rendering the
            # key when a registry grows a group nobody has translated yet.
            contains=self.i18n.contains,
            save=lambda r: self._save_owned_rule(
                r, uid, getattr(r, "version", None)
            ),
            # Where each value comes from. Owner-scoped: this is a human path, so
            # another tenant's bot is not found and never supplies a default.
            describe=lambda r: describe_rule(r, self._bot_tokens, uid),
        )

    async def _registry_show(self, cq: CallbackQuery, rule: ForwardRule, uid: int,
                             text: str, rows) -> None:
        """Render one registry screen and persist the FSM it was built from.

        The rows carry indexes into `st.buffer["reg_keys"]`, so the state has to
        be persisted with them: a button tapped after a restart would otherwise
        point at a list this process no longer has.
        """
        st = self._state(uid)
        await self._persist(uid, st)
        await self._safe_edit(cq, text, reply_markup=self._kbd(rows))
        await cq.answer()

    async def _render_bot_route_screen(self, rule: ForwardRule, st: "UiState") -> Tuple[str, InlineKeyboardMarkup]:
        """Where a task's copies are sent from, and which bots are available."""
        lang = self._lang_for(rule)
        owner_id = int(getattr(rule, "owner_user_id", 0) or getattr(st, "user_id", 0) or 0)
        records: List[Any] = []
        if self._bot_tokens is not None and owner_id:
            try:
                listed = await self._bot_tokens.list_by_owner(owner_id)
                records = [r for r in (listed or []) if getattr(r, "is_active", False)]
            except Exception as exc:
                logger.warning("[BOT ROUTE] token list failed: %s", exc)
        records.sort(key=lambda r: (int(getattr(r, "created_at", 0) or 0), str(r.id)))

        # The keyboard carries indexes, not ids: two uuids plus a prefix would not
        # fit Telegram's 64-byte callback_data. The handler resolves an index
        # against this exact list, and refuses when the list has moved on.
        st.buffer["bot_route_tokens"] = [str(r.id) for r in records]
        st.buffer["bot_route_rule"] = str(rule.id)

        transport_state = self._t(
            "ui_route_transport_bot" if rule.delivery_transport == "BOT" else "ui_route_transport_account",
            lang=lang,
        )
        token_label = rule.bot_token_label or self._t("ui_route_token_none", lang=lang)
        lines = [
            self._t("ui_route_token_title", lang=lang),
            "",
            self._t("ui_rule_btn_route", lang=lang, state=transport_state),
            self._t("ui_rule_btn_bot_token", lang=lang, label=token_label),
            "",
        ]
        for idx, rec in enumerate(records, start=1):
            mark = " ✅" if str(rec.id) == rule.bot_token_id else ""
            lines.append(f"{idx}. {rec.label}{mark}")

        rows: List[List[Tuple[str, str]]] = []
        for idx, rec in enumerate(records):
            rows.append([(self._t("ui_route_token_use", lang=lang, label=str(rec.label)[:24]), f"rbts:{rule.id}:{idx}")])
        rows.append([(self._t("ui_route_token_add", lang=lang), f"rbta:{rule.id}")])
        for idx, rec in enumerate(records):
            rows.append([(f"{self._t('ui_route_token_remove', lang=lang)} {str(rec.label)[:20]}", f"rbtd:{rule.id}:{idx}")])
        rows.append([(self._btn('ui_rule_back_rules'), f"rbtb:{rule.id}")])
        return "\n".join(lines), self._kbd(rows)

    def _route_token_choice(self, st: "UiState", rule_id: str, idx: int) -> str:
        """Resolve a button index to a token id, or "" when the screen is stale."""
        if st.buffer.get("bot_route_rule") != str(rule_id):
            return ""
        tokens = st.buffer.get("bot_route_tokens") or []
        if not isinstance(tokens, list) or idx < 0 or idx >= len(tokens):
            return ""
        return str(tokens[idx] or "")

    async def _step_bot_route_token(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        """The owner pasted a bot token. Take it, store it encrypted, forget it."""
        uid = message.from_user.id if message.from_user else 0
        st.step = ""
        await self._persist(uid, st)
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            return await message.reply_text(self._t("ui_rule_not_found"))
        lang = self._lang_for(rule)
        token = (text or "").strip()

        # This message is a credential: remove it before doing anything else so
        # the token does not sit in the chat history.
        try:
            await message.delete()
        except Exception as exc:
            logger.warning("[BOT ROUTE] could not delete the token message (uid=%s): %s", uid, exc)

        if not token_looks_valid(token):
            return await message.reply_text(self._t("ui_route_token_invalid", lang=lang))

        ok, me = await self._validate_bot_token(token)
        if not ok:
            return await message.reply_text(self._t("ui_route_token_rejected", lang=lang))

        bot_label = f"@{me.get('username')}" if me.get("username") else f"bot {me.get('id')}"
        if self._bot_tokens is None:
            return await message.reply_text(self._t("ui_route_token_rejected", lang=lang))
        try:
            record = await self._bot_tokens.create(bot_label, token, owner_user_id=uid)
        except Exception as exc:
            logger.warning("[BOT ROUTE] storing the pasted token failed: %s", type(exc).__name__)
            return await message.reply_text(self._t("ui_route_token_rejected", lang=lang))

        rule.bot_token_id = record.id
        rule.bot_token_label = bot_label
        rule.delivery_transport = "BOT"
        await self._save_owned_rule(rule, uid)
        # Never the token itself: id, resolved bot, and the rule it serves.
        logger.info(
            "[BOT ROUTE] token %s attached by uid=%s to rule=%s (bot %s resolved)",
            str(record.id)[:8], uid, str(rule.id)[:8], me.get("id"),
        )
        await message.reply_text(self._t("ui_route_token_attached", lang=lang, bot=bot_label))
        text_ob, kbd_ob = self._render_rule_detail(rule)
        await message.reply_text(text_ob, reply_markup=kbd_ob)

    # ------------------------------------------------------------------ #
    # multi-source -> multi-target route wizard (rendering + state)       #
    # ------------------------------------------------------------------ #
    #
    # M sources -> N targets per task, no caps. Two entry points share every
    # method below: the inline-keyboard wizard in the rules dashboard and the
    # `/route` command. Lists, duplicates and self-loops always go through
    # route_lists; callback payloads always through cb_payload(); every string
    # through _t() with the per-user language.

    def _route_cb(self, *parts: Any) -> str:
        """Build a callback payload that can never exceed Telegram's limit.

        A rule id longer than a uuid would otherwise make a keyboard
        unbuildable; the shortened form still resolves by prefix.
        """
        try:
            return cb_payload(*parts)
        except ValueError:
            return cb_payload(*[
                short_id(str(part)) if len(str(part)) > SHORT_ID_LENGTH else part
                for part in parts
            ])

    def _route_buffer(self, st: UiState) -> Dict[str, Any]:
        """The FSM buffer, guaranteed to be a dict (it is persisted as JSON)."""
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        return st.buffer

    def _route_scope(self, st: UiState) -> str:
        """The scope the wizard works on: "n" for a new route, else a task id."""
        return str(self._route_buffer(st).get("route_scope") or "").strip()

    def _route_id(self, st: UiState) -> str:
        """The task id shown as `{rid}` — minted once for a new route.

        A new route gets its id up front so every screen can name the task it
        is building; the rule is created with exactly that id on save.
        """
        buf = self._route_buffer(st)
        rid = str(buf.get("route_rule_id") or "").strip()
        if not rid:
            rid = str(uuid.uuid4())
            buf["route_rule_id"] = rid
        return rid

    def _route_reset(self, uid: int, st: UiState) -> None:
        """Drop only the route keys, leaving any other flow's state intact."""
        buf = self._route_buffer(st)
        for key in [k for k in list(buf) if str(k).startswith("route_")]:
            buf.pop(key, None)
        st.step = ""

    def _route_role_data(self, st: UiState, role: str):
        """(entries, ids, names, statuses) for one role, always index-aligned."""
        buf = self._route_buffer(st)
        if role == "s":
            entries = list(buf.get("route_sources") or [])
            ids = list(buf.get("route_src_ids") or [])
            names = list(buf.get("route_src_names") or [])
            statuses = list(buf.get("route_src_status") or [])
        else:
            entries = list(buf.get("route_targets") or [])
            ids = list(buf.get("route_tgt_ids") or [])
            names = list(buf.get("route_tgt_names") or [])
            statuses = list(buf.get("route_tgt_status") or [])
        size = len(entries)
        ids = (ids + [""] * size)[:size]
        names = (names + [""] * size)[:size]
        statuses = (statuses + [ROUTE_UNCHECKED] * size)[:size]
        return entries, ids, names, statuses

    def _route_store_role(self, st: UiState, role: str, entries, ids, names, statuses) -> None:
        """Persist one role's list with its resolution result per entry."""
        buf = self._route_buffer(st)
        prefix = "route_src" if role == "s" else "route_tgt"
        buf["route_sources" if role == "s" else "route_targets"] = list(entries)
        buf[prefix + "_ids"] = list(ids)
        buf[prefix + "_names"] = list(names)
        buf[prefix + "_status"] = list(statuses)

    def _route_entries(self, st: UiState, role: str):
        """The stored *raw* entries for one role (used for validation)."""
        return self._route_role_data(st, role)[0]

    def _route_mark_name(self, lang: str, cid: str, name: str, status: str) -> Tuple[str, str]:
        """(mark, label) for one entry: resolved, failed, or never checked."""
        if status == ROUTE_RESOLVED:
            return self._t("ui_route_status_ok", lang=lang), (name or cid)
        if status == ROUTE_UNCHECKED:
            return (
                self._t("ui_route_status_fail", lang=lang),
                self._t("ui_route_list_unverified", lang=lang),
            )
        return (
            self._t("ui_route_status_fail", lang=lang),
            self._t("ui_route_list_failed", lang=lang),
        )

    def _route_entry_line(self, key: str, lang: str, index: int, cid: str, name: str, status: str) -> str:
        mark, label = self._route_mark_name(lang, cid, name, status)
        return self._t(key, lang=lang, index=index, mark=mark, cid=cid or name, name=label)

    def _route_problem_text(self, problems, lang: str) -> str:
        """Render the first list problem, naming the offending entry."""
        if not problems:
            return ""
        code, offending = problems[0]
        key = ROUTE_PROBLEM_KEYS.get(code)
        if not key:
            return ""
        if offending:
            return self._t(key, lang=lang, id=offending)
        return self._t(key, lang=lang)

    def _route_unresolved(self, st: UiState):
        """Every entry of either list that is not resolved yet."""
        pending = []
        for role in ("s", "t"):
            entries, _ids, _names, statuses = self._route_role_data(st, role)
            pending.extend(
                token for token, status in zip(entries, statuses)
                if status != ROUTE_RESOLVED
            )
        return pending

    def _route_summary_line(self, st: UiState, lang: str) -> str:
        """The verdict line under the lists (ok / skipped / failed)."""
        entries_s, ids_s, _ns, status_s = self._route_role_data(st, "s")
        entries_t, ids_t, _nt, status_t = self._route_role_data(st, "t")
        pairs = list(zip(entries_s, status_s)) + list(zip(entries_t, status_t))
        total = len(pairs)
        if not total:
            return ""
        failed = [token for token, status in pairs if status != ROUTE_RESOLVED]
        if not failed:
            distinct = {cid for cid in (list(ids_s) + list(ids_t)) if cid}
            return self._t("ui_route_summary_ok", lang=lang, count=len(distinct))
        ok = total - len(failed)
        if len(failed) == 1:
            return self._t("ui_route_resolve_failed", lang=lang, id=failed[0])
        return self._t(
            "ui_route_summary_skip", lang=lang, ok=ok, total=total, id=failed[0]
        )

    def _route_screen_text(self, st: UiState, role: str, lang: str, note: str = "") -> str:
        """The numbered list screen for one role."""
        rid = short_id(self._route_id(st))
        entries, ids, names, statuses = self._route_role_data(st, role)
        title_key = "ui_route_sources_title" if role == "s" else "ui_route_targets_title"
        line_key = "ui_route_source_line" if role == "s" else "ui_route_target_line"
        lines = [self._t(title_key, lang=lang, rid=rid), ""]
        if entries:
            lines.append(self._t(line_key, lang=lang, count=len(entries)))
            for index, (cid, name, status) in enumerate(zip(ids, names, statuses), 1):
                lines.append(
                    self._route_entry_line("ui_route_list_item", lang, index, cid, name, status)
                )
        else:
            lines.append(self._t("ui_route_list_empty", lang=lang))
        if self._route_scope(st) == ROUTE_SCOPE_NEW and not entries:
            # A brand-new route has no lists yet: show the one-shot syntax too.
            lines.extend(["", self._t("ui_route_usage", lang=lang)])
        summary = self._route_summary_line(st, lang)
        if summary:
            lines.extend(["", summary])
        if note:
            lines.extend(["", note])
        return "\n".join(lines)

    def _route_summary_text(self, st: UiState, lang: str, note: str = "") -> str:
        """The whole route with a confirmation mark per entry."""
        rid = short_id(self._route_id(st))
        name = str(self._route_buffer(st).get("route_name") or "")
        lines = [self._t("ui_route_task_line", lang=lang, task=name or rid, rid=rid)]
        for role, line_key in (("s", "ui_route_source_line"), ("t", "ui_route_target_line")):
            entries, ids, names, statuses = self._route_role_data(st, role)
            lines.extend(["", self._t(line_key, lang=lang, count=len(entries))])
            if not entries:
                lines.append(self._t("ui_route_list_empty", lang=lang))
                continue
            for cid, label, status in zip(ids, names, statuses):
                mark, shown = self._route_mark_name(lang, cid, label, status)
                lines.append(self._t(
                    "ui_route_confirmed_line", lang=lang, mark=mark, cid=cid or label, name=shown
                ))
        summary = self._route_summary_line(st, lang)
        if summary:
            lines.extend(["", summary])
        if note:
            lines.extend(["", note])
        return "\n".join(lines)

    def _route_keyboard(self, st: UiState, role: str, lang: str, rule=None) -> InlineKeyboardMarkup:
        """Save / both lists / fix one entry / per-task content rules / back."""
        scope = self._route_scope(st) or ROUTE_SCOPE_NEW
        sources = self._route_entries(st, "s")
        targets = self._route_entries(st, "t")
        rows: List[List[Tuple[str, str]]] = [
            [(self._t("ui_route_btn_save", lang=lang),
              self._route_cb(CB["route_save_lists"], scope))],
            [
                (self._t("ui_route_add_sources", lang=lang, count=len(sources)),
                 self._route_cb(CB["route_src_list"], "s", scope)),
                (self._t("ui_route_add_targets", lang=lang, count=len(targets)),
                 self._route_cb(CB["route_tgt_list"], "t", scope)),
            ],
            [(self._t("ui_route_edit_one", lang=lang),
              self._route_cb(CB["route_fix_entry"], "s" if role == "s" else "t", scope))],
        ]
        if rule is not None:
            # The per-task content rules the task card opens, reachable from the
            # route screen so a task's route and its content rules are set up in
            # the same pass (same menus, same handlers, no second mechanism).
            rows.append([
                (self._t("ui_rule_btn_blacklist", lang=lang, bl_count=len(rule.blacklist)),
                 f"rfl_bl:{rule.id}"),
                (self._t("ui_rule_btn_whitelist", lang=lang, wl_count=len(rule.whitelist)),
                 f"rfl_wl:{rule.id}"),
            ])
            rows.append([
                (self._t("ui_rule_btn_replace", lang=lang, rep_count=len(rule.replacements)),
                 f"rtx:{rule.id}"),
            ])
        rows.append([(self._t("ui_route_btn_back", lang=lang),
                      self._route_cb(CB["route_back"], scope))])
        return self._kbd(rows)

    def _route_render(self, st: UiState, role: str, lang: str, rule=None,
                      view: str = "list", note: str = ""):
        """(text, markup) for one wizard screen."""
        if view == "summary":
            text = self._route_summary_text(st, lang, note)
        else:
            text = self._route_screen_text(st, role, lang, note)
        return text, self._route_keyboard(st, role, lang, rule)

    # -- resolution (the rule editor's path, shared) ---------------------

    async def _resolve_chat_token(self, uid: int, st: UiState, raw: str):
        """Resolve one entry to (id, title, status) exactly like the rule editor.

        The buffered dialog list answers first (no network); otherwise the
        connected client's `get_chat` is asked. `status` is "resolved",
        "failed", or "unchecked" when there is no connected client to ask —
        that last case is why the wizard can say "not checked" instead of
        pretending an id failed.
        """
        token = str(raw or "").strip()
        if not token:
            return "", "", ROUTE_FAILED
        buffer = st.buffer if isinstance(st.buffer, dict) else {}
        if "chats" in buffer:
            token_key = unique_peer_key(token)
            for chat in buffer.get("chats") or []:
                chat_id = str(chat.get("id") or "")
                username = str(chat.get("username") or "")
                # Exact spelling first; then the raw unique id matches the
                # `-100…` Bot-API spelling and vice versa, so an id copied
                # from any list resolves to the buffered chat without a
                # network probe.
                if chat_id == token or (
                    chat_id and token_key.isdigit()
                    and unique_peer_key(chat_id) == token_key
                ) or (username and username.lower() == token.lstrip("@").lower()):
                    title = "{} {}".format(chat.get("emoji") or "", chat.get("title") or "").strip()
                    return chat_id or token, title or chat_id or token, ROUTE_RESOLVED
        try:
            _fsm_uid = self._scope_uid(uid)
            sessions = (await self._sessions.list_all() if _fsm_uid is None
                        else await self._sessions.list_by_owner(uid))
            if not sessions:
                return token, token, ROUTE_UNCHECKED
            client = self._pool.get(sessions[0].id)
            if not client or not getattr(client, "is_connected", False):
                return token, token, ROUTE_UNCHECKED
            chat_obj = await client.get_chat(token)
            chat_id = getattr(chat_obj, "id", None)
            if chat_obj is None or chat_id is None:
                return token, token, ROUTE_FAILED
            title = (getattr(chat_obj, "title", None) or getattr(chat_obj, "first_name", None)
                     or str(chat_id))
            return str(chat_id), "\U0001F4E2 " + str(title), ROUTE_RESOLVED
        except Exception:
            return token, token, ROUTE_FAILED

    async def _route_resolve_all(self, uid: int, st: UiState, entries):
        """(ids, titles, statuses) aligned with `entries`, resolved like above."""
        ids: List[str] = []
        names: List[str] = []
        statuses: List[str] = []
        for token in entries:
            cid, title, status = await self._resolve_chat_token(uid, st, token)
            if status == ROUTE_RESOLVED:
                ids.append(cid)
                names.append(title)
            else:
                ids.append(str(token))
                names.append("")
            statuses.append(status)
        return ids, names, statuses

    async def _route_find_rule(self, uid: int, scope: str):
        """The owned task a scope points at, or None.

        The scope is a rule id — or its 8-character form when a payload had to
        be shortened — and the command also accepts a task name.
        """
        if not scope or scope == ROUTE_SCOPE_NEW:
            return None
        rule = await self._owned_rule(scope, uid)
        if rule is not None:
            return rule
        if hasattr(self._rules, "list_by_owner"):
            needle = str(scope).strip().lower()
            for candidate in await self._rules.list_by_owner(uid):
                if short_id(candidate.id) == needle[:SHORT_ID_LENGTH]:
                    return candidate
                if (candidate.name or "").strip().lower() == needle:
                    return candidate
        return None

    async def _route_session(self, uid: int):
        """The session a newly created task runs on (first owned, like above)."""
        if not self._sessions:
            return None
        try:
            if self._scope_uid(uid) is None and hasattr(self._sessions, "list_all"):
                sessions = await self._sessions.list_all()
            else:
                sessions = await self._sessions.list_by_owner(uid)
        except Exception:
            return None
        return sessions[0] if sessions else None

    @staticmethod
    def _route_apply_lists(rule, src_ids, src_names, tgt_ids, tgt_names) -> None:
        """Write the whole lists *and* the mirrored primary on both sides."""
        rule.source_chat_ids = [str(x) for x in src_ids if str(x).strip()]  # setter mirrors the first
        rule.source_chat_names = [str(x) for x in src_names]
        rule.source_chat_name = (rule.source_chat_names[0] if rule.source_chat_names
                                 else str(rule.source_chat_id))
        rule.target_chat_ids = [str(x) for x in tgt_ids if str(x).strip()]
        if rule.target_chat_ids:
            rule.target_chat_id = rule.target_chat_ids[0]  # mirror of the first target
            first = str(tgt_names[0]).strip() if tgt_names else ""
            rule.target_chat_name = first or rule.target_chat_id

    async def _route_seed(self, uid: int, st: UiState, scope: str) -> None:
        """Point the wizard at `scope` and seed the lists from the saved task.

        A saved task can only carry resolved ids, so seeded entries start
        resolved with the names the task already stores.
        """
        buffer = self._route_buffer(st)
        if self._route_scope(st) == scope and buffer.get("route_seeded"):
            return
        self._route_reset(uid, st)
        buffer = self._route_buffer(st)
        buffer["route_scope"] = scope or ROUTE_SCOPE_NEW
        buffer["route_seeded"] = True
        if not scope or scope == ROUTE_SCOPE_NEW:
            buffer["route_rule_id"] = str(uuid.uuid4())
            return
        rule = await self._route_find_rule(uid, scope)
        if rule is None:
            buffer["route_rule_id"] = scope
            return
        buffer["route_rule_id"] = rule.id
        buffer["route_name"] = rule.name or ""
        src_ids = [str(x) for x in rule.source_chat_ids]
        src_names = list(rule.source_chat_names) + [""] * len(src_ids)
        self._route_store_role(
            st, "s", src_ids, src_ids, src_names[:len(src_ids)],
            [ROUTE_RESOLVED] * len(src_ids),
        )
        tgt_ids = [str(x) for x in rule.all_targets]
        tgt_names = [(rule.target_chat_name or "")] + [""] * max(0, len(tgt_ids) - 1)
        self._route_store_role(
            st, "t", tgt_ids, tgt_ids, tgt_names[:len(tgt_ids)],
            [ROUTE_RESOLVED] * len(tgt_ids),
        )

    async def _route_commit(self, uid: int, st: UiState, lang: str, name: str,
                            sources, src_ids, src_names, targets, tgt_ids, tgt_names):
        """Create the task that carries these lists, or update the one named.

        Returns (ok, message, rule, kind) with kind "created" or "saved".
        """
        rule = None
        scope = self._route_scope(st)
        if scope and scope != ROUTE_SCOPE_NEW:
            rule = await self._route_find_rule(uid, scope)
        if rule is None and name:
            rule = await self._route_find_rule(uid, name)
        if rule is not None:
            self._route_apply_lists(rule, src_ids, src_names, tgt_ids, tgt_names)
            try:
                await self._save_owned_rule(rule, uid, getattr(rule, "version", None))
            except ConcurrencyError:
                return False, self._t("ui_concurrency_error", lang=lang), None, ""
            except Exception as exc:
                await self._record_error("bot", exc, user_id=uid, rule_id=rule.id)
                return False, self._t("ui_route_failed", lang=lang, error=tg_detail(exc)), None, ""
            return True, self._t("ui_route_saved_alert", lang=lang), rule, "saved"

        session = await self._route_session(uid)
        if session is None:
            return False, self._t("ui_no_sessions", lang=lang), None, ""
        rid = self._route_id(st) if scope == ROUTE_SCOPE_NEW else str(uuid.uuid4())
        task_name = (name or "").strip() or f"task_{short_id(rid)}"
        rule = ForwardRule(
            id=rid,
            session_id=session.id,
            name=task_name,
            routing_type=RoutingType.CHANNEL_TO_CHANNEL,
            forward_mode=ForwardMode.COPY_MESSAGE,
            owner_user_id=uid,
        )
        self._route_apply_lists(rule, src_ids, src_names, tgt_ids, tgt_names)
        try:
            await self._rules.create(rule)
        except Exception as exc:
            await self._record_error("bot", exc, user_id=uid)
            return False, self._t("ui_route_failed", lang=lang, error=tg_detail(exc)), None, ""
        try:
            self._log.info("bot", "rule", f"route created {len(src_ids)} -> {len(tgt_ids)}")
        except Exception:
            pass
        return (
            True,
            self._t("ui_route_created", lang=lang, task=rule.name, rid=short_id(rule.id)),
            rule,
            "created",
        )

    # -- wizard flows ----------------------------------------------------

    async def _route_back(self, uid: int, st: UiState, lang: str, cq=None, message=None, scope=None):
        """Leave the wizard: back to the task card, or to the rules list."""
        scope = scope if scope is not None else self._route_scope(st)
        self._route_reset(uid, st)
        await self._persist(uid, st)
        rule = await self._route_find_rule(uid, scope) if scope and scope != ROUTE_SCOPE_NEW else None
        if rule is not None:
            text, markup = self._render_rule_detail(rule)
        else:
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, markup = self._render_rules_list(rules, page=0)
        if cq is not None:
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer(self._t("ui_route_btn_back", lang=lang))
        elif message is not None:
            await message.reply_text(text, reply_markup=markup)
        return text, markup

    async def _route_problem_reply(self, st: UiState, role: str, lang: str, note: str, uid: int):
        """Re-render the wizard with a problem note; nothing is stored."""
        rule = await self._route_find_rule(uid, self._route_scope(st))
        return self._route_render(st, role, lang, rule=rule, view="summary" if note else "list", note=note)

    async def _route_replace_role(self, message: Message, uid: int, st: UiState, role: str,
                                  raw: str, lang: str) -> None:
        """Take a submitted list for one role: validate, resolve, then show it."""
        parsed = parse_route_list(raw)
        if not parsed.items:
            reason_key = "ui_route_err_empty_sources" if role == "s" else "ui_route_err_empty_targets"
            note = self._t("ui_route_bad_input", lang=lang, reason=self._t(reason_key, lang=lang))
            text_out, markup = await self._route_problem_reply(st, role, lang, note, uid)
            await message.reply_text(text_out, reply_markup=markup)
            return
        other = "t" if role == "s" else "s"
        other_entries = self._route_entries(st, other)
        other_text = ", ".join(other_entries)
        problems = validate_route_lists(
            raw if role == "s" else other_text,
            raw if role == "t" else other_text,
            require_sources=bool(raw if role == "s" else other_text),
            require_targets=bool(raw if role == "t" else other_text),
        )
        if problems:
            note = self._route_problem_text(problems, lang)
            text_out, markup = await self._route_problem_reply(st, role, lang, note, uid)
            await message.reply_text(text_out, reply_markup=markup)
            return
        ids, names, statuses = await self._route_resolve_all(uid, st, parsed.items)
        self._route_store_role(st, role, parsed.items, ids, names, statuses)
        await self._persist(uid, st)
        rule = await self._route_find_rule(uid, self._route_scope(st))
        text_out, markup = self._route_render(st, role, lang, rule=rule, view="summary")
        await message.reply_text(text_out, reply_markup=markup)

    async def _route_apply_fix(self, message: Message, uid: int, st: UiState, role: str,
                               index_text: str, value_text: str, lang: str) -> None:
        """`index = value` (or `index = -` to remove) on one entry."""
        entries, ids, names, statuses = self._route_role_data(st, role)
        try:
            index = int(str(index_text).strip())
        except (TypeError, ValueError):
            await message.reply_text(self._t("ui_route_edit_one_bad", lang=lang))
            return
        if index < 1 or index > len(entries):
            await message.reply_text(self._t(
                "ui_route_edit_one_out_of_range", lang=lang, index=index, total=len(entries)))
            return
        value = str(value_text or "").strip()
        if value in ("-", ""):
            removed = entries.pop(index - 1)
            ids.pop(index - 1)
            names.pop(index - 1)
            statuses.pop(index - 1)
            note = self._t("ui_route_edit_one_removed", lang=lang, index=index, value=removed)
        else:
            cid, title, status = await self._resolve_chat_token(uid, st, value)
            entries[index - 1] = value
            ids[index - 1] = cid if status == ROUTE_RESOLVED else value
            names[index - 1] = title if status == ROUTE_RESOLVED else ""
            statuses[index - 1] = status
            note = self._t("ui_route_edit_one_updated", lang=lang, index=index, value=value)
        self._route_store_role(st, role, entries, ids, names, statuses)
        st.step = "route_sources" if role == "s" else "route_targets"
        await self._persist(uid, st)
        rule = await self._route_find_rule(uid, self._route_scope(st))
        text_out, markup = self._route_render(st, role, lang, rule=rule, view="summary", note=note)
        await message.reply_text(text_out, reply_markup=markup)

    async def _route_one_shot(self, message: Message, uid: int, st: UiState, lang: str, cmd) -> None:
        """`<name> <sources> -> <targets>`: resolve everything, then write.

        Shared by `/route` and by a full route line typed inside the wizard, so
        both produce exactly the same task and the same messages.
        """
        problems = validate_route_lists(cmd.source_text, cmd.target_text)
        if problems:
            await message.reply_text(self._route_problem_text(problems, lang))
            return
        src_ids, src_names, src_status = await self._route_resolve_all(uid, st, cmd.sources)
        tgt_ids, tgt_names, tgt_status = await self._route_resolve_all(uid, st, cmd.targets)
        pairs = list(zip(cmd.sources, src_status)) + list(zip(cmd.targets, tgt_status))
        failed = [token for token, status in pairs if status != ROUTE_RESOLVED]
        if failed:
            if len(failed) == 1:
                note = self._t("ui_route_resolve_failed", lang=lang, id=failed[0])
            else:
                note = self._t("ui_route_summary_fail", lang=lang, ok=len(pairs) - len(failed),
                               total=len(pairs), id=failed[0])
            await message.reply_text(note)
            return
        problems = validate_resolved_uniqueness(src_ids, tgt_ids)
        if problems:
            await message.reply_text(self._route_problem_text(problems, lang))
            return
        ok, message_text, rule, kind = await self._route_commit(
            uid, st, lang, cmd.name, cmd.sources, src_ids, src_names,
            cmd.targets, tgt_ids, tgt_names,
        )
        if not ok or rule is None:
            await message.reply_text(message_text)
            return
        self._route_reset(uid, st)
        await self._persist(uid, st)
        if kind == "created":
            body = message_text + "\n\n" + self._t("ui_route_done_footer", lang=lang)
            markup = self._kbd([
                [(self._t("ui_rule_adv_settings", lang=lang), f"rd:{rule.id}")],
                [(self._t("ui_rule_view_all", lang=lang), CB["rules"])],
            ])
            await message.reply_text(body, reply_markup=markup)
        else:
            await message.reply_text(self._t("ui_route_saved_alert", lang=lang))
            text_out, markup = self._render_rule_detail(rule)
            await message.reply_text(text_out, reply_markup=markup)

    async def _handle_route_command(self, message: Message, uid: int) -> None:
        """`/route <task name or id> <sources> -> <targets>`."""
        lang = self._resolve_lang(uid)
        body = (message.text or "").strip()
        st = self._state(uid) if uid else UiState()
        args = body.split(None, 1)
        if len(args) < 2 or not args[1].strip():
            await message.reply_text(
                self._t("ui_route_usage", lang=lang),
                reply_markup=self._kbd([[
                    (self._t("ui_route_btn_back", lang=lang), CB["rules"]),
                ]]),
            )
            return
        try:
            cmd = parse_route_command(body)
        except RouteParseError as exc:
            key = ROUTE_PARSE_KEYS.get(exc.code)
            if key:
                await message.reply_text(self._t(key, lang=lang))
                return
            raise
        await self._route_one_shot(message, uid, st, lang, cmd)

    async def _route_save(self, cq: CallbackQuery, uid: int, st: UiState, lang: str) -> None:
        """The save button: only a fully resolved, distinct route is written."""
        sources, src_ids, src_names, _ss = self._route_role_data(st, "s")
        targets, tgt_ids, tgt_names, _ts = self._route_role_data(st, "t")
        rule = await self._route_find_rule(uid, self._route_scope(st))
        if not sources or not targets or self._route_unresolved(st):
            await cq.answer(self._t("ui_route_blocked_alert", lang=lang), show_alert=True)
            text_out, markup = self._route_render(st, "s", lang, rule=rule, view="summary")
            await self._safe_edit(cq, text_out, reply_markup=markup)
            return
        problems = list(validate_route_lists(", ".join(sources), ", ".join(targets)))
        problems += list(validate_resolved_uniqueness(src_ids, tgt_ids))
        if problems:
            note = self._route_problem_text(problems, lang)
            text_out, markup = self._route_render(st, "s", lang, rule=rule, view="summary", note=note)
            await self._safe_edit(cq, text_out, reply_markup=markup)
            await cq.answer(note, show_alert=True)
            return
        name = str(self._route_buffer(st).get("route_name") or "")
        ok, message_text, rule, kind = await self._route_commit(
            uid, st, lang, name, sources, src_ids, src_names, targets, tgt_ids, tgt_names)
        if not ok or rule is None:
            await cq.answer(message_text, show_alert=True)
            return
        self._route_reset(uid, st)
        await self._persist(uid, st)
        if kind == "created":
            body = message_text + "\n\n" + self._t("ui_route_done_footer", lang=lang)
            await self._safe_edit(cq, body, reply_markup=self._kbd([
                [(self._t("ui_rule_adv_settings", lang=lang), f"rd:{rule.id}")],
                [(self._t("ui_rule_view_all", lang=lang), CB["rules"])],
            ]))
        else:
            await cq.answer(self._t("ui_route_saved_alert", lang=lang), show_alert=True)
            text_out, markup = self._render_rule_detail(rule)
            await self._safe_edit(cq, text_out, reply_markup=markup)

    def _route_open(self, uid: int, st: UiState, role: str) -> str:
        """Set the step for the screen being opened and return its language."""
        lang = self._lang_for(st, uid)
        st.step = "route_sources" if role == "s" else "route_targets"
        return lang

    # -- text steps ------------------------------------------------------

    async def _route_step_input(self, message: Message, st: UiState, role: str, text: str) -> None:
        """One text step serves the list, a fix (`index = value`) and a route line."""
        uid = int(message.from_user.id if message.from_user else 0)
        lang = self._lang_for(st, message.from_user, uid=uid)
        raw = (text or "").strip()
        if raw.lower() in cancel_aliases():
            await self._route_back(uid, st, lang, message=message)
            return
        fix = re.match(r"^\s*(\d+)\s*=\s*(.*)$", raw, re.S)
        if fix:
            await self._route_apply_fix(message, uid, st, role, fix.group(1), fix.group(2), lang)
            return
        route_line = None
        try:
            route_line = parse_route_command(raw)
        except RouteParseError as exc:
            # An arrow with an empty side is worth saying out loud; no arrow at
            # all just means "this is a plain list".
            key = ROUTE_PARSE_KEYS.get(exc.code) if exc.code != ROUTE_MISSING_ARROW else None
            if key:
                note = self._t("ui_route_bad_input", lang=lang, reason=self._t(key, lang=lang))
                text_out, markup = await self._route_problem_reply(st, role, lang, note, uid)
                await message.reply_text(text_out, reply_markup=markup)
                return
        if route_line is not None:
            await self._route_one_shot(message, uid, st, lang, route_line)
            return
        await self._route_replace_role(message, uid, st, role, raw, lang)

    async def _route_step_fix(self, message: Message, st: UiState, role: str, text: str) -> None:
        """The `Fix one entry` step accepts only `index = value`."""
        uid = int(message.from_user.id if message.from_user else 0)
        lang = self._lang_for(st, message.from_user, uid=uid)
        raw = (text or "").strip()
        if raw.lower() in cancel_aliases():
            await self._route_back(uid, st, lang, message=message)
            return
        fix = re.match(r"^\s*(\d+)\s*=\s*(.*)$", raw, re.S)
        if not fix:
            await message.reply_text(self._t("ui_route_bad_edit", lang=lang))
            return
        await self._route_apply_fix(message, uid, st, role, fix.group(1), fix.group(2), lang)

    async def _step_route_sources(self, message: Message, st: UiState, text: str) -> None:
        await self._route_step_input(message, st, "s", text)

    async def _step_route_targets(self, message: Message, st: UiState, text: str) -> None:
        await self._route_step_input(message, st, "t", text)

    async def _step_route_edit_s(self, message: Message, st: UiState, text: str) -> None:
        await self._route_step_fix(message, st, "s", text)

    async def _step_route_edit_t(self, message: Message, st: UiState, text: str) -> None:
        await self._route_step_fix(message, st, "t", text)

    def _render_advanced_settings(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        """Render advanced rule controls using the rule owner's selected locale."""
        lang = self._lang_for(rule)
        task_name = rule.name or f"task_{rule.id[:8]}"
        src_label = rule.source_chat_name or rule.source_chat_id or self._t("ui_rule_source_fallback", lang=lang)
        dst_label = getattr(rule, "target_label", None) or (
            rule.target_chat_name or rule.target_chat_id or self._t("ui_rule_target_fallback", lang=lang)
        )
        status_on = lambda enabled: self._t(
            "ui_status_on" if enabled else "ui_status_off", lang=lang
        )
        dup_status = status_on(bool(rule.duplicate_filters))
        crypto_status = status_on(bool(rule.crypto_mode))
        bl_count, wl_count = len(rule.blacklist), len(rule.whitelist)
        bu_count, wu_count = len(rule.blacklist_users), len(rule.whitelist_users)
        cl_count = rule.cleaner_active_option_count
        fl_count = rule.filter_active_option_count
        sched_status = status_on(bool(rule.auto_post_scheduler.get("enabled", False)))
        pwr_status = status_on(bool(rule.power_schedule.get("enabled", False)))
        delay_val = self._t("ui_secs_value", lang=lang, sec=f"{rule.delay_seconds:.0f}" if rule.delay_seconds > 0 else "0")
        mte_val = self._t("ui_secs_value", lang=lang, sec=f"{rule.max_time_edit}" if rule.max_time_edit > 0 else "0")
        raw_status = status_on(bool(rule.process_raw_text))
        btn2txt_status = status_on(bool(rule.button_to_text))

        links_removed = rule.remove_links or (
            isinstance(rule.metadata, dict) and bool(rule.metadata.get("remove_links"))
        )
        lk_badge = self._t(
            "ui_status_remove" if links_removed else "ui_status_allowed", lang=lang
        )
        vc_badge = self._t(
            "ui_status_blocked" if rule.block_voice else "ui_status_allowed", lang=lang
        )
        st_badge = self._t(
            "ui_status_blocked" if rule.block_stickers else "ui_status_allowed", lang=lang
        )
        em_badge = self._t(
            "ui_status_clean" if rule.remove_emojis else "ui_status_keep", lang=lang
        )
        album_key = {"first": "ui_album_first", "split": "ui_album_split"}.get(
            rule.album_mode, "ui_album_full"
        )
        alb_badge = self._t(album_key, lang=lang)
        rep_count = len(rule.replacements)
        reps_badge = (
            self._t("ui_adv_replacements_count", lang=lang, count=rep_count)
            if rep_count else self._t("ui_status_off", lang=lang)
        )
        configured = self._t("ui_adv_status_configured", lang=lang)
        empty = self._t("ui_adv_status_empty", lang=lang)
        hdr_badge = configured if rule.header else empty
        ftr_badge = configured if rule.footer else empty
        ed_badge = status_on(not rule.ignore_edits)
        dl_badge = status_on(bool(rule.sync_deletes))
        tr_status = language_label(rule.translate_lang) if rule.translate_lang else self._t("ui_status_off", lang=lang)
        wm_status = configured if rule.watermark else empty
        btn_count = len(rule.link_buttons)
        ak_status = self._t(
            "ui_ai_enabled" if rule.affiliate_kit.get("enabled", False) else "ui_ai_disabled",
            lang=lang,
        )
        ocr_status = self._t("ui_ai_enabled" if rule.ocr_image_ai else "ui_ai_disabled", lang=lang)
        ai_rw_status = self._t("ui_ai_enabled" if rule.smart_ai_rewrite else "ui_ai_disabled", lang=lang)
        digest_status = self._t("ui_ai_enabled" if rule.news_digest else "ui_ai_disabled", lang=lang)

        text = self._t(
            "ui_adv_summary",
            lang=lang,
            task_name=task_name,
            src_label=src_label,
            source_chat_id=rule.source_chat_id,
            dst_label=dst_label,
            target_chat_id=rule.target_chat_id,
            dup_status=dup_status,
            crypto_status=crypto_status,
            bl_count=bl_count,
            wl_count=wl_count,
            bu_count=bu_count,
            wu_count=wu_count,
            cl_count=cl_count,
            fl_count=fl_count,
            lk_badge=lk_badge,
            vc_badge=vc_badge,
            st_badge=st_badge,
            em_badge=em_badge,
            sched_status=sched_status,
            pwr_status=pwr_status,
            delay_val=delay_val,
            mte_val=mte_val,
            raw_status=raw_status,
            btn2txt_status=btn2txt_status,
            hdr_badge=hdr_badge,
            ftr_badge=ftr_badge,
            reps_badge=reps_badge,
            tr_status=tr_status,
            wm_status=wm_status,
            btn_count=btn_count,
            ak_status=ak_status,
            alb_badge=alb_badge,
            ai_creativity=rule.ai_creativity,
            ai_max_tokens=rule.ai_max_tokens,
            ocr_status=ocr_status,
            ai_rw_status=ai_rw_status,
            digest_status=digest_status,
        )
        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_rule_btn_dup", lang=lang, dup_status=dup_status), f"rtg_dup:{rule.id}"),
                (self._t("ui_adv_btn_crypto", lang=lang, status=crypto_status), f"rtg_crypto:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_blacklist", lang=lang, bl_count=bl_count), f"rfl_bl:{rule.id}"),
                (self._t("ui_rule_btn_whitelist", lang=lang, wl_count=wl_count), f"rfl_wl:{rule.id}"),
            ],
            [
                (self._t("ui_adv_btn_block_users", lang=lang, count=bu_count), f"rfl_bu:{rule.id}"),
                (self._t("ui_adv_btn_allow_users", lang=lang, count=wu_count), f"rfl_wu:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_cleaner", lang=lang, cl_count=cl_count), f"rcl:{rule.id}"),
                (self._t("ui_rule_btn_filters", lang=lang, fl_count=fl_count), f"rfl:{rule.id}"),
            ],
            [
                (self._t("ui_fl_btn_link", lang=lang, status=lk_badge), f"rlk:{rule.id}"),
                (self._t("ui_fl_btn_voice", lang=lang, status=vc_badge), f"rvc:{rule.id}"),
            ],
            [
                (self._t("ui_fl_btn_sticker", lang=lang, status=st_badge), f"rst:{rule.id}"),
                (self._t("ui_clean_btn_emojis", lang=lang, status=em_badge), f"rem:{rule.id}"),
            ],
            [
                (self._t("ui_adv_btn_auto_schedule", lang=lang, status=sched_status), f"rsch_auto:{rule.id}"),
                (self._t("ui_adv_btn_power_schedule", lang=lang, status=pwr_status), f"rsch_pwr:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_delay", lang=lang, delay_val=delay_val), f"rdly:{rule.id}"),
                (self._t("ui_adv_btn_max_edit", lang=lang, value=mte_val), f"rmte:{rule.id}"),
            ],
            [
                (self._t("ui_adv_btn_raw_text", lang=lang, status=raw_status), f"rtg_raw:{rule.id}"),
                (self._t("ui_adv_btn_button_to_text", lang=lang, status=btn2txt_status), f"rtg_btn2txt:{rule.id}"),
            ],
            [
                (self._t("ui_adv_btn_header_footer", lang=lang), f"rhf:{rule.id}"),
                (self._t("ui_rule_btn_replace", lang=lang, rep_count=rep_count), f"rtx:{rule.id}"),
            ],
            [
                (self._t("ui_adv_btn_translate", lang=lang, language=tr_status), f"rtr:{rule.id}"),
                (self._t("ui_wm_btn_set", lang=lang), f"rwm:{rule.id}"),
            ],
            [
                (self._t("ui_adv_btn_link_buttons", lang=lang, count=btn_count), f"rbtn:{rule.id}"),
                (self._t("ui_adv_btn_affiliate", lang=lang, status=ak_status), f"rafl:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_edit", lang=lang, edit_sync_status=ed_badge), f"red:{rule.id}"),
                (self._t("ui_rule_btn_delete", lang=lang, del_sync_status=dl_badge), f"rdl:{rule.id}"),
            ],
            [
                (self._t("ui_adv_btn_album", lang=lang, mode=alb_badge), f"rmg:{rule.id}"),
                (self._t("ui_rule_btn_delete_task", lang=lang), f"rrm:{rule.id}"),
            ],
            [
                (self._t("ui_adv_btn_ocr", lang=lang, status=ocr_status), f"rtg_ocr:{rule.id}"),
                (self._t("ui_adv_btn_ai_rewrite", lang=lang, status=ai_rw_status), f"rtg_ai:{rule.id}"),
            ],
            [
                (self._t("ui_adv_btn_news_digest", lang=lang, status=digest_status), f"rtg_digest:{rule.id}"),
                (self._t("ui_adv_btn_ai_config", lang=lang, creativity=rule.ai_creativity, max_tokens=rule.ai_max_tokens), f"rai_cfg:{rule.id}"),
            ],
            [(self._t("ui_reg_btn_all", lang=lang), f"rge:g:{rule.id}")],
            [(self._t("ui_rule_back_task", lang=lang), f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    #: The add-on catalogue's single vocabulary: add-on key -> catalog key.
    #:
    #: The screen hardcoded all eighteen labels in English ("🔒 Forward Every N
    #: Messages", "🧹 Cleaner Protocol"), which is invisible to the key-parity
    #: audits — the keys were fine, the labels never came from one. Each row
    #: reuses the SAME key its add-on grid renders elsewhere, so a feature never
    #: reads two names and the state suffix is composed per locale.
    _ADDON_ROWS: ClassVar[Tuple[Tuple[str, str], ...]] = (
        ("comments", "ui_addon_comments"),
        ("every_n", "ui_addon_every_n"),
        ("kw_confirm", "ui_addon_kw_confirm"),
        ("kw_reply", "ui_addon_kw_reply"),
        ("auto_del", "ui_addon_auto_del"),
        ("webhook", "ui_rule_addon_webhook"),
        ("crypto", "ui_addon_crypto"),
        ("affiliate", "ui_addon_affiliate"),
        ("link_buttons", "ui_addon_link_buttons"),
        ("watermark", "ui_rule_addon_watermark"),
        ("duplicate", "ui_addon_duplicate"),
        ("cleaner", "ui_addon_cleaner"),
        ("scheduler", "ui_addon_scheduler"),
        ("power_schedule", "ui_addon_power_schedule"),
        ("raw_text", "ui_addon_raw_text"),
        ("button_to_text", "ui_addon_button_to_text"),
        ("ocr_ai", "ui_addon_ocr_ai"),
        ("ai_rewrite", "ui_addon_ai_rewrite"),
    )

    def _render_all_addons(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        """Render the add-ons catalogue with toggle controls, localized."""
        lang = self._lang_for(rule)
        task_name = rule.name or f"task_{rule.id[:8]}"
        addons = rule.paid_addons

        text = (
            self._t("ui_addons_catalogue_title", lang=lang, n=len(self._ADDON_ROWS)) + "\n"
            + self._t("ui_route_task_line", lang=lang, task=task_name, rid=rule.id[:8]) + "\n"
            + "━━━━━━━━━━━━━━━━━━━━\n"
            + self._t("ui_addon_catalogue_desc", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = []
        for i in range(0, len(self._ADDON_ROWS), 2):
            pair: List[Tuple[str, str]] = []
            for addon_key, catalog_key in self._ADDON_ROWS[i:i + 2]:
                # The dot, not ui_toggle_addon's word form: this grid packs two
                # labels per line and "<label>: <state word>" wraps there.
                dot = "🟢" if addons.get(addon_key) else "⚪"
                label = f"{self._addon_label(catalog_key, lang)} {dot}"
                pair.append((label, f"radon_tg:{rule.id}:{addon_key}"))
            rows.append(pair)

        rows.append([(self._btn('ui_rule_back_task'), f"rd:{rule.id}")])
        return text, self._kbd(rows)

    def _draft_ai_badge(self, draft: Dict[str, Any], lang: str) -> str:
        """Badge for the task panel's AI row — computed, never remembered.

        The row used to render ``draft["ai_status_badge"]``, a string only the
        AI menu ever set, so a freshly opened editor read "Off" for every task
        while the pipeline rewrote every message (it gated on ai_config_id
        alone). This mirrors ForwardRule.ai_rewrite_active and is the panel's
        only source for that row.
        """
        if not draft.get("ai_config_id"):
            return self._t("ui_draft_ai_off", lang=lang)
        addons = draft.get("paid_addons")
        if isinstance(addons, dict) and addons.get("ai_rewrite") is False:
            return self._t("ui_draft_ai_off", lang=lang)
        pol = str(draft.get("ai_fallback_policy") or "DROP")
        return f"🟢 {pol}"

    def _render_draft_editor(self, rule: ForwardRule, draft: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render comprehensive draft editor screen where settings are staged safely."""
        src_label = draft.get("source_name") or draft.get("source_chat_id", "")
        dst_label = draft.get("target_name") or draft.get("target_chat_id", "")
        status_str = self._t("ui_draft_status_on", lang=lang) if draft.get("is_active", True) else self._t("ui_draft_status_off", lang=lang)

        f_mode = draft.get("forward_mode", ForwardMode.COPY_MESSAGE.value)
        mode_val = getattr(f_mode, "value", str(f_mode))
        mode_names = {
            "COPY_MESSAGE": self._t("ui_draft_mode_copy", lang=lang),
            "DIRECT_FORWARD": self._t("ui_draft_mode_forward", lang=lang),
            "REWRITE_AI": self._t("ui_draft_mode_ai", lang=lang),
        }
        mode_title = mode_names.get(mode_val, mode_val)

        reps = draft.get("replacements") or {}
        reps_count = len(reps) if isinstance(reps, dict) else 0
        reps_badge = self._t("ui_draft_count_n", lang=lang, n=reps_count) if reps_count else self._t("ui_draft_empty", lang=lang)

        hdr = str(draft.get("header") or "")
        ftr = str(draft.get("footer") or "")
        hdr_badge = f"`{hdr[:14]}…`" if len(hdr) > 14 else (f"`{hdr}`" if hdr else self._t("ui_draft_empty", lang=lang))
        ftr_badge = f"`{ftr[:14]}…`" if len(ftr) > 14 else (f"`{ftr}`" if ftr else self._t("ui_draft_empty", lang=lang))

        lk_badge = self._t("ui_draft_remove", lang=lang) if draft.get("remove_links") else self._t("ui_draft_allowed", lang=lang)
        vc_badge = self._t("ui_draft_blocked", lang=lang) if draft.get("block_voice") else self._t("ui_draft_allowed", lang=lang)
        st_badge = self._t("ui_draft_blocked", lang=lang) if draft.get("block_stickers") else self._t("ui_draft_allowed", lang=lang)
        em_badge = self._t("ui_draft_clean", lang=lang) if draft.get("remove_emojis") else self._t("ui_draft_keep", lang=lang)
        ed_badge = self._t("ui_draft_ignore", lang=lang) if draft.get("ignore_edits") else self._t("ui_draft_sync", lang=lang)
        dl_badge = self._t("ui_draft_delete_sync", lang=lang) if draft.get("sync_deletes") else self._t("ui_draft_ignore", lang=lang)

        album_names = {
            "album": self._t("ui_album_full", lang=lang),
            "first": self._t("ui_album_first", lang=lang),
            "split": self._t("ui_album_split", lang=lang),
        }
        alb_mode = str(draft.get("album_mode") or "album")
        alb_badge = album_names.get(alb_mode, self._t("ui_album_full", lang=lang))

        text = (
            self._t("ui_draft_title", lang=lang) + "\n\n"
            + self._t("ui_draft_rule_id", lang=lang, rid=rule.id[:8], v=rule.version) + "\n"
            + self._t("ui_sep", lang=lang) + "\n"
            + self._t("ui_draft_source", lang=lang, src=src_label, sid=draft.get('source_chat_id')) + "\n"
            + self._t("ui_draft_target", lang=lang, dst=dst_label, tid=draft.get('target_chat_id')) + "\n"
            + self._t("ui_draft_status", lang=lang, s=status_str) + "\n"
            + self._t("ui_draft_fmode", lang=lang, m=mode_title) + "\n"
            + self._t("ui_sep", lang=lang) + "\n"
            + self._t("ui_draft_replace", lang=lang, n=reps_badge) + "\n"
            + self._t("ui_draft_hf", lang=lang, h=hdr_badge, f=ftr_badge) + "\n"
            + self._t("ui_draft_pair_links_voice", lang=lang, lk=lk_badge, vc=vc_badge) + "\n"
            + self._t("ui_draft_pair_st_emoji", lang=lang, st=st_badge, em=em_badge) + "\n"
            + self._t("ui_draft_pair_ed_del", lang=lang, ed=ed_badge, dl=dl_badge) + "\n"
            + self._t("ui_draft_media", lang=lang, a=alb_badge) + "\n\n"
            + self._t("ui_draft_hint", lang=lang)
        )

        rows: List[List[tuple]] = [
            [
                (self._t("ui_draft_btn_src", lang=lang), f"ed_s:{rule.id}", BTN_PRIMARY),
                (self._t("ui_draft_btn_dst", lang=lang), f"ed_t:{rule.id}", BTN_PRIMARY),
            ],
            [
                (self._t("ui_draft_btn_mode", lang=lang, m=mode_val), f"ed_m:{rule.id}", BTN_PRIMARY),
                (self._t("ui_draft_btn_status", lang=lang, s=status_str), f"ed_a:{rule.id}", BTN_PRIMARY),
            ],
            [
                (self._t("ui_draft_btn_links", lang=lang, b=lk_badge), f"ed_lk:{rule.id}"),
                (self._t("ui_draft_btn_voice", lang=lang, b=vc_badge), f"ed_vc:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_stickers", lang=lang, b=st_badge), f"ed_st:{rule.id}"),
                (self._t("ui_draft_btn_emoji", lang=lang, b=em_badge), f"ed_em:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_edit", lang=lang, b=ed_badge), f"ed_ed:{rule.id}"),
                # Deleting in the target is destructive: colour it red while it
                # is armed, neutral while it is off.
                (
                    (self._t("ui_draft_btn_del", lang=lang, b=dl_badge), f"ed_dl:{rule.id}", BTN_DANGER)
                    if draft.get("sync_deletes")
                    else (self._t("ui_draft_btn_del", lang=lang, b=dl_badge), f"ed_dl:{rule.id}")
                ),
            ],
            [
                (self._t("ui_draft_btn_album", lang=lang, b=alb_badge), f"ed_mg:{rule.id}"),
                (self._t("ui_draft_btn_replace", lang=lang, n=reps_count), f"rtx:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_ai", lang=lang, b=self._draft_ai_badge(draft, lang)), f"r_ai:{rule.id}", BTN_PRIMARY),
            ],
            [
                (self._t("ui_draft_btn_hf", lang=lang), f"rhf:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_save", lang=lang), f"ed_save:{rule.id}", BTN_SUCCESS),
                (self._t("ui_draft_btn_cancel", lang=lang), f"ed_can:{rule.id}", BTN_DANGER),
            ],
        ]
        return text, self._kbd(rows)

    @staticmethod
    def _resolve_replace_click(rule, idx: int, version_token: str) -> Optional[str]:
        """The replacement key a delete click refers to, or ``None``.

        The menu addresses an entry by position because a user-supplied key
        does not fit in Telegram's 64-byte callback data. A position is only
        meaningful for the list generation it was rendered from, so the click
        also carries ``rule.version`` (bumped by every write). A mismatch means
        the list changed while the menu was open: the click is refused and the
        caller re-renders, rather than deleting whichever entry has moved into
        that slot.
        """
        try:
            idx = int(idx)
            wanted = int(str(version_token))
        except (TypeError, ValueError):
            return None
        if wanted != int(getattr(rule, "version", 1) or 1):
            return None
        keys = list((getattr(rule, "replacements", None) or {}).keys())
        if 0 <= idx < len(keys):
            return keys[idx]
        return None

    def _render_replace_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render text replacement configuration menu.

        Each entry shows its mode marker (``lit``/``re``) and the flags that
        change how it matches, so "why did this rule not fire?" is answerable
        from the screen instead of from the engine's logs.
        """
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        reps = rule.replacements
        version = int(getattr(rule, "version", 1) or 1)

        lines = [
            self._t("ui_rep_title", lang=lang) + "\n",
            self._t("ui_rep_rule", lang=lang, s=src_label, d=dst_label) + "\n",
            self._t("ui_sep", lang=lang),
            self._t("ui_rep_active_list", lang=lang),
        ]
        if not reps:
            lines.append(self._t("ui_rep_none", lang=lang) + "\n")
        else:
            for idx, (stored_key, value) in enumerate(reps.items(), start=1):
                entry = entry_from_value(stored_key, value)
                rep_display = f"`{entry.to}`" if entry.to else self._t("ui_rep_word_deleted", lang=lang)
                name = f" · {entry.name}" if entry.name else ""
                # The stored key is usually the source text, but a managed rule
                # is keyed by id; the source is what the user typed either way.
                label = entry.name or entry.source
                lines.append(f"{idx}. `{label[:40]}` ➔ {rep_display}  `{entry.badge()}`{name}")
            lines.append("")

        lines.extend([
            self._t("ui_rep_help_title", lang=lang),
            self._t("ui_rep_help_add", lang=lang),
            self._t("ui_rep_help_fmt", lang=lang),
            self._t("ui_rep_help_regex", lang=lang),
            self._t("ui_rep_help_edit", lang=lang),
            self._t("ui_rep_help_del", lang=lang),
            self._t("ui_rep_help_cmd", lang=lang),
        ])

        rows: List[List[Tuple[str, str]]] = [
            [(self._t("ui_rep_btn_add", lang=lang), f"rtxa:{rule.id}")],
        ]
        if reps:
            for idx, old in enumerate(list(reps.keys())[:6]):
                entry = entry_from_value(old, reps.get(old))
                display_k = (entry.name or entry.source)[:10]
                rows.append([
                    (self._t("ui_rep_btn_entry", lang=lang, source=display_k),
                     f"rtxe:{rule.id}:{idx}:{version}"),
                    (f"❌ {display_k}", f"rtxd:{rule.id}:{idx}:{version}"),
                ])
            rows.append([(self._t("ui_rep_btn_clear", lang=lang), f"rtxc:{rule.id}")])

        rows.append([(self._btn('ui_rule_back_rule'), f"rd:{rule.id}")])
        return "\n".join(lines), self._kbd(rows)

    def _replace_option_entry(self, replacements: Dict[str, Any], key: str, option: str):
        """The entry with one option flipped (mode / case / whole word)."""
        entry = entry_from_value(key, replacements.get(key))
        if option == "mode":
            mode = MODE_LITERAL if entry.is_regex else MODE_REGEX
            return entry_with(replacements, key, mode=mode)
        if option == "i":
            if entry.ignore_case:
                flags = entry.flags.replace("i", "")
            else:
                flags = clean_flags(entry.flags.replace("c", "") + "i", entry.mode)
            return entry_with(replacements, key, flags=flags)
        flags = entry.flags.replace("w", "") if entry.whole_word else entry.flags + "w"
        return entry_with(replacements, key, flags=flags)

    def _render_replace_entry(self, rule: ForwardRule, key: str) -> Tuple[str, InlineKeyboardMarkup]:
        """One entry's option screen: mode, case, whole word, delete."""
        lang = self._lang_for(rule)
        entry = entry_from_value(key, rule.replacements.get(key))
        target = f"`{entry.to}`" if entry.to else self._t("ui_rep_word_deleted", lang=lang)
        mode_name = self._t(
            "ui_rep_mode_regex" if entry.is_regex else "ui_rep_mode_literal", lang=lang
        )
        keys = list(rule.replacements.keys())
        idx = keys.index(key) if key in keys else 0
        version = int(getattr(rule, "version", 1) or 1)
        lines = [
            self._t("ui_rep_entry_title", lang=lang, source=key),
            self._t(
                "ui_rep_entry_body", lang=lang, mode=mode_name,
                flags=entry.flags or "—", target=target, name=entry.name or "—",
            ),
            self._t("ui_rep_entry_hint", lang=lang),
        ]
        rows: List[List[Tuple[str, str]]] = [
            [(self._t("ui_rep_btn_mode", lang=lang, mode=mode_name), f"rtxem:{rule.id}:{idx}:{version}")],
            [(self._t("ui_rep_btn_case", lang=lang,
                      state=self._t("ui_yes" if entry.ignore_case else "ui_no", lang=lang)),
              f"rtxef:{rule.id}:{idx}:{version}:i")],
            [(self._t("ui_rep_btn_word", lang=lang,
                      state=self._t("ui_yes" if entry.whole_word else "ui_no", lang=lang)),
              f"rtxef:{rule.id}:{idx}:{version}:w")],
            [(self._t("ui_delete", lang=lang), f"rtxd:{rule.id}:{idx}:{version}")],
            [(self._btn("ui_rule_back_rule", lang=lang), f"rtx:{rule.id}")],
        ]
        return "\n".join(lines), self._kbd(rows)

    def _render_header_footer_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render header and footer configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        hdr = rule.header
        ftr = rule.footer

        lines = [
            self._t("ui_hf_title", lang=lang),
            self._t("ui_hf_rule_line", lang=lang, src=src_label, dst=dst_label),
            "────────────────────",
            self._t("ui_hf_hdr_title", lang=lang),
            f"`{hdr}`\n" if hdr else self._t("ui_hf_not_set", lang=lang),
            self._t("ui_hf_ftr_title", lang=lang),
            f"`{ftr}`\n" if ftr else self._t("ui_hf_not_set", lang=lang),
            self._t("ui_hf_hint", lang=lang),
        ]

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_hf_btn_set_hdr", lang=lang), f"rh_s:{rule.id}"),
                (self._t("ui_hf_btn_del_hdr", lang=lang), f"rh_d:{rule.id}"),
            ],
            [
                (self._t("ui_hf_btn_set_ftr", lang=lang), f"rf_s:{rule.id}"),
                (self._t("ui_hf_btn_del_ftr", lang=lang), f"rf_d:{rule.id}"),
            ],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return "\n".join(lines), self._kbd(rows)

    def _render_cleaner_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render cleaner settings menu for this task."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)

        rm = self._t("ui_clean_removed", lang=lang)
        alw = self._t("ui_clean_allowed", lang=lang)
        cln = self._t("ui_clean_cleaned", lang=lang)
        kep = self._t("ui_clean_kept", lang=lang)
        url_b = rm if rule.remove_links else alw
        usr_b = rm if rule.clean_usernames else alw
        tag_b = rm if rule.clean_hashtags else alw
        emo_b = cln if rule.remove_emojis else kep
        phn_b = rm if rule.clean_phones else alw
        btn_b = rm if rule.clean_buttons else alw
        spc_b = cln if rule.clean_empty_lines else kep
        wrd_cnt = len(rule.clean_custom_words)
        wrd_b = self._t("ui_clean_words_count", lang=lang, count=wrd_cnt) if wrd_cnt else self._t("ui_clean_not_set", lang=lang)

        text = (
            self._t("ui_clean_title", lang=lang)
            + self._t("ui_clean_rule_line", lang=lang, src=src_label, dst=dst_label)
            + "────────────────────\n"
            + self._t("ui_clean_intro", lang=lang)
            + self._t("ui_clean_urls", lang=lang, status=url_b)
            + self._t("ui_clean_usernames", lang=lang, status=usr_b)
            + self._t("ui_clean_hashtags", lang=lang, status=tag_b)
            + self._t("ui_clean_emojis", lang=lang, status=emo_b)
            + self._t("ui_clean_phones", lang=lang, status=phn_b)
            + self._t("ui_clean_buttons", lang=lang, status=btn_b)
            + self._t("ui_clean_empty_lines", lang=lang, status=spc_b)
            + self._t("ui_clean_custom_words", lang=lang, status=wrd_b)
            + self._t("ui_clean_hint", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_clean_btn_urls", lang=lang, status=url_b), f"rcl_url:{rule.id}"),
                (self._t("ui_clean_btn_usernames", lang=lang, status=usr_b), f"rcl_usr:{rule.id}"),
            ],
            [
                (self._t("ui_clean_btn_hashtags", lang=lang, status=tag_b), f"rcl_tag:{rule.id}"),
                (self._t("ui_clean_btn_emojis", lang=lang, status=emo_b), f"rcl_emo:{rule.id}"),
            ],
            [
                (self._t("ui_clean_btn_phones", lang=lang, status=phn_b), f"rcl_phn:{rule.id}"),
                (self._t("ui_clean_btn_buttons", lang=lang, status=btn_b), f"rcl_btn:{rule.id}"),
            ],
            [
                (self._t("ui_clean_btn_empty_lines", lang=lang, status=spc_b), f"rcl_spc:{rule.id}"),
                (self._t("ui_clean_btn_custom_words", lang=lang, status=wrd_b), f"rcl_wrd:{rule.id}"),
            ],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_filters_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render content filter settings menu for this task."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)

        blk = self._t("ui_fl_blocked", lang=lang)
        alw = self._t("ui_clean_allowed", lang=lang)
        lnk_b = blk if rule.block_links else alw
        vid_b = blk if rule.block_video else alw
        aud_b = blk if rule.block_audio else alw
        stk_b = blk if rule.block_stickers else alw
        txt_b = blk if rule.block_text else alw
        voc_b = blk if rule.block_voice else alw

        text = (
            self._t("ui_fl_title", lang=lang)
            + self._t("ui_hf_rule_line", lang=lang, src=src_label, dst=dst_label)
            + "────────────────────\n"
            + self._t("ui_fl_intro", lang=lang)
            + self._t("ui_fl_link", lang=lang, status=lnk_b)
            + self._t("ui_fl_video", lang=lang, status=vid_b)
            + self._t("ui_fl_audio", lang=lang, status=aud_b)
            + self._t("ui_fl_sticker", lang=lang, status=stk_b)
            + self._t("ui_fl_text", lang=lang, status=txt_b)
            + self._t("ui_fl_voice", lang=lang, status=voc_b)
            + self._t(
                "ui_fl_advanced",
                lang=lang,
                status=self._t("ui_yes", lang=lang) if rule.filter_rule_id else self._t("ui_no", lang=lang),
            )
            + self._t("ui_fl_hint", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_fl_btn_link", lang=lang, status=lnk_b), f"rfl_lnk:{rule.id}"),
                (self._t("ui_fl_btn_video", lang=lang, status=vid_b), f"rfl_vid:{rule.id}"),
            ],
            [
                (self._t("ui_fl_btn_audio", lang=lang, status=aud_b), f"rfl_aud:{rule.id}"),
                (self._t("ui_fl_btn_sticker", lang=lang, status=stk_b), f"rfl_stk:{rule.id}"),
            ],
            [
                (self._t("ui_fl_btn_text", lang=lang, status=txt_b), f"rfl_txt:{rule.id}"),
                (self._t("ui_fl_btn_voice", lang=lang, status=voc_b), f"rfl_voc:{rule.id}"),
            ],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_max_time_edit_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render max edit time configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        current = rule.max_time_edit
        curr_str = self._t("ui_mte_seconds", lang=lang, sec=current) if current > 0 else self._t("ui_mte_unlimited", lang=lang)

        text = (
            self._t("ui_mte_title", lang=lang)
            + self._t("ui_hf_rule_line", lang=lang, src=src_label, dst=dst_label)
            + self._t("ui_mte_current", lang=lang, val=curr_str)
            + "────────────────────\n"
            + self._t("ui_mte_hint", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_mte_btn_10m", lang=lang), f"rmte_s:{rule.id}:600"),
                (self._t("ui_mte_btn_1h", lang=lang), f"rmte_s:{rule.id}:3600"),
            ],
            [
                (self._t("ui_mte_btn_24h", lang=lang), f"rmte_s:{rule.id}:86400"),
                (self._t("ui_mte_btn_48h", lang=lang), f"rmte_s:{rule.id}:172800"),
            ],
            [
                (self._t("ui_mte_btn_unlimited", lang=lang), f"rmte_s:{rule.id}:0"),
                (self._t("ui_mte_btn_custom", lang=lang), f"rmte_c:{rule.id}"),
            ],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_delay_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render forward delay configuration menu with quick presets."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        current = rule.delay_seconds
        curr_str = self._t("ui_dly_seconds", lang=lang, sec=current) if current > 0 else self._t("ui_dly_none", lang=lang)

        text = (
            self._t("ui_dly_title", lang=lang)
            + self._t("ui_hf_rule_line", lang=lang, src=src_label, dst=dst_label)
            + self._t("ui_dly_current", lang=lang, val=curr_str)
            + "────────────────────\n"
            + self._t("ui_dly_hint", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_dly_btn_0", lang=lang), f"rdly_s:{rule.id}:0"),
                (self._t("ui_dly_btn_3", lang=lang), f"rdly_s:{rule.id}:3"),
            ],
            [
                (self._t("ui_dly_btn_5", lang=lang), f"rdly_s:{rule.id}:5"),
                (self._t("ui_dly_btn_10", lang=lang), f"rdly_s:{rule.id}:10"),
            ],
            [
                (self._t("ui_dly_btn_30", lang=lang), f"rdly_s:{rule.id}:30"),
                (self._t("ui_dly_btn_60", lang=lang), f"rdly_s:{rule.id}:60"),
            ],
            [
                (self._t("ui_dly_btn_custom", lang=lang), f"rdly_c:{rule.id}"),
            ],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_every_n_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        """The "forward every Nth message" panel, one tap from the task card.

        The interval is a number, not a toggle, so this screen replaces the old
        boolean add-on switch for the same row: presets cover the useful range
        and a free-text step covers the rest (1..1000). The counter is not shown
        here — it is the engine's state, and a stale copy of it on a button is
        worse than none.
        """
        lang = self._lang_for(rule)
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        every_n = int(getattr(rule, "forward_every_n", 1) or 1)
        curr_str = (
            self._t("ui_en_every", lang=lang, n=every_n)
            if every_n > 1 else self._t("ui_en_none", lang=lang)
        )
        text = (
            self._t("ui_en_title", lang=lang)
            + self._t("ui_hf_rule_line", lang=lang, src=src_label, dst=dst_label)
            + self._t("ui_en_current", lang=lang, val=curr_str)
            + "────────────────────\n"
            + self._t("ui_en_hint", lang=lang)
        )
        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_en_btn_off", lang=lang), f"ren:{rule.id}:1"),
                (self._t("ui_en_btn_n", lang=lang, n=2), f"ren:{rule.id}:2"),
                (self._t("ui_en_btn_n", lang=lang, n=3), f"ren:{rule.id}:3"),
            ],
            [
                (self._t("ui_en_btn_n", lang=lang, n=5), f"ren:{rule.id}:5"),
                (self._t("ui_en_btn_n", lang=lang, n=10), f"ren:{rule.id}:10"),
                (self._t("ui_en_btn_n", lang=lang, n=25), f"ren:{rule.id}:25"),
            ],
            [
                (self._t("ui_en_btn_n", lang=lang, n=50), f"ren:{rule.id}:50"),
                (self._t("ui_en_btn_n", lang=lang, n=100), f"ren:{rule.id}:100"),
                (self._t("ui_en_btn_custom", lang=lang), f"renc:{rule.id}"),
            ],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_blacklist_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render blacklist keyword configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        bl = list(rule.blacklist or [])

        items_str = "\n".join([f"• `{w}`" for w in bl[:20]]) if bl else self._t("ui_bl_empty", lang=lang)
        if len(bl) > 20:
            items_str += self._t("ui_bl_more", lang=lang, more=len(bl) - 20)

        text = (
            self._t("ui_bl_title", lang=lang)
            + self._t("ui_hf_rule_line", lang=lang, src=src_label, dst=dst_label)
            + self._t("ui_bl_count", lang=lang, count=len(bl))
            + "────────────────────\n"
            + self._t("ui_bl_desc", lang=lang)
            + f"{items_str}\n\n"
            + self._t("ui_bl_hint", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_bl_btn_add", lang=lang), f"rfl_bl_add:{rule.id}"),
                (self._t("ui_bl_btn_clear", lang=lang), f"rfl_bl_clr:{rule.id}"),
            ],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_whitelist_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render whitelist keyword configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        wl = list(rule.whitelist or [])

        items_str = "\n".join([f"• `{w}`" for w in wl[:20]]) if wl else self._t("ui_wl_empty", lang=lang)
        if len(wl) > 20:
            items_str += self._t("ui_wl_more", lang=lang, more=len(wl) - 20)

        text = (
            self._t("ui_wl_title", lang=lang)
            + self._t("ui_hf_rule_line", lang=lang, src=src_label, dst=dst_label)
            + self._t("ui_wl_count", lang=lang, count=len(wl))
            + "────────────────────\n"
            + self._t("ui_wl_desc", lang=lang)
            + f"{items_str}\n\n"
            + self._t("ui_wl_hint", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_wl_btn_add", lang=lang), f"rfl_wl_add:{rule.id}"),
                (self._t("ui_wl_btn_clear", lang=lang), f"rfl_wl_clr:{rule.id}"),
            ],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_watermark_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render watermark configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        wm = rule.watermark

        cur = wm if wm else self._t("ui_wm_not_set", lang=lang)
        text = (
            self._t("ui_wm_title", lang=lang)
            + self._t("ui_hf_rule_line", lang=lang, src=src_label, dst=dst_label)
            + self._t("ui_wm_current", lang=lang, val=cur)
            + "────────────────────\n"
            + self._t("ui_wm_hint", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_wm_btn_set", lang=lang), f"rwm_set:{rule.id}"),
                (self._t("ui_wm_btn_del", lang=lang), f"rwm_del:{rule.id}"),
            ],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_translate_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render target translation language picker."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        current = (rule.translate_lang or "").lower()
        curr_label = (language_label(current, with_code=True) if current
                      else self._t("ui_tr_off", lang=lang))

        text = (
            self._t("ui_tr_title", lang=lang)
            + self._t("ui_hf_rule_line", lang=lang, src=src_label, dst=dst_label)
            + self._t("ui_tr_current", lang=lang, val=curr_label)
            + "────────────────────\n"
            + self._t("ui_tr_hint", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_tr_btn_off", lang=lang), f"rtr_set:{rule.id}:none"),
            ],
        ]
        # Every supported language, in the canonical order, two per row — built
        # from the one table rather than spelled out here.
        choices = [
            (language_label(code, with_code=True), f"rtr_set:{rule.id}:{code}")
            for code in SUPPORTED_LANGUAGES
        ]
        rows += [choices[i:i + 2] for i in range(0, len(choices), 2)]
        rows.append([(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")])
        return text, self._kbd(rows)

    async def _render_profile_menu(self, uid: int, user: Any = None) -> Tuple[str, InlineKeyboardMarkup]:
        """Render a fully localized account overview and its actions."""
        lang = self._resolve_lang(uid, user)
        name = getattr(user, "first_name", "") or getattr(user, "username", "") or str(uid)
        sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
        rules = await self._rules.list_by_owner(uid) if (self._rules and uid) else []
        active_rules = [rule for rule in rules if rule.is_active]
        connected_sessions = sum(
            1 for session in sessions if bool(getattr(session, "is_active", False))
        )
        body = "\n".join((
            self._t("ui_main_account_header", lang=lang),
            self._t("ui_main_user_id", lang=lang, id=uid),
            self._t("ui_main_display_name", lang=lang, n=name),
            self._t("ui_main_sessions_header", lang=lang),
            self._t("ui_main_connected_sessions", lang=lang, a=connected_sessions, b=len(sessions)),
            self._t("ui_main_tasks_header", lang=lang),
            self._t("ui_main_active_tasks2", lang=lang, n=len(active_rules)),
            self._t("ui_main_total_tasks", lang=lang, n=len(rules)),
            "────────────────────",
            self._t("ui_profile_actions", lang=lang),
        ))
        keyboard = self._kbd([
            [(self._t("ui_profile_restart_setup", lang=lang), "pr_restart")],
            [(self._t("ui_profile_reset_config", lang=lang), "pr_reset_cfg")],
            [(self._t("ui_back_main_menu", lang=lang), CB["main"])],
        ])
        return body, keyboard

    def _render_smart_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render dedicated smart routing, VIP channel, and deduplication menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)

        use_mid = bool(getattr(rule, "use_intermediate", False))
        mid_id = getattr(rule, "intermediate_channel_id", "") or ""
        mid_name = getattr(rule, "intermediate_channel_name", "") or ""
        mid_display = f"`{mid_name or mid_id}`" if mid_id else self._t("ui_smart_not_set", lang=lang)

        cat = getattr(rule, "message_category", "ALL")
        crit = getattr(rule, "detection_criteria", {}) or {}
        match_mode = (crit.get("match_mode") or "ANY").upper()
        keywords = crit.get("text_contains") or crit.get("keywords") or []
        kw_display = ", ".join(f"`{k}`" for k in keywords) if keywords else self._t("ui_smart_not_set", lang=lang)
        regex_p = crit.get("regex_pattern") or crit.get("text_regex") or ""
        regex_display = f"`{regex_p}`" if regex_p else self._t("ui_smart_not_set", lang=lang)

        f_mode = getattr(rule, "forward_mode", ForwardMode.COPY_MESSAGE)
        mode_val = getattr(f_mode, "value", str(f_mode))

        _ns = self._t("ui_smart_not_set", lang=lang)
        # Determine active routing path
        if use_mid and mid_id and cat in ("VIP", "VIP_ONLY"):
            path_desc = self._t("ui_smart_path2_desc", lang=lang)
            path_tag = self._t("ui_smart_path2_tag", lang=lang)
        elif mode_val == "DIRECT_FORWARD":
            path_desc = self._t("ui_smart_path3_desc", lang=lang)
            path_tag = self._t("ui_smart_path3_tag", lang=lang)
        else:
            path_desc = self._t("ui_smart_path1_desc", lang=lang)
            path_tag = self._t("ui_smart_path1_tag", lang=lang)

        chdr = getattr(rule, "custom_header", "") or ""
        hdr_display = f"`{chdr[:25]}…`" if len(chdr) > 25 else (f"`{chdr}`" if chdr else _ns)
        prio = int(getattr(rule, "priority", 10) or 10)
        mode_desc = (self._t("ui_smart_match_all", lang=lang) if match_mode == "ALL"
                     else self._t("ui_smart_match_any", lang=lang))

        # Branding hop (A -> C -> B): the per-task filter that decides whether a
        # message actually travels through C before reaching B. Rendered from the
        # same config the engine reads, so the panel cannot drift from the route.
        brand_cfg = branding_policy.read_config(rule)
        brand_origins = list(brand_cfg.get("origin_titles") or []) + list(brand_cfg.get("origin_ids") or [])
        brand_words = list(brand_cfg.get("keywords") or [])
        brand_filtered = bool(brand_cfg.get("triggers_enabled")) and bool(brand_origins or brand_words)
        brand_state = self._t("ui_smart_brand_state_filtered" if brand_filtered else "ui_smart_brand_state_all",
                              lang=lang)

        def _brand_display(values: List[str]) -> str:
            if not values:
                return _ns
            return ", ".join(f"`{str(v).replace('`', '')[:30]}`" for v in values)

        origins_display = _brand_display(brand_origins)
        words_display = _brand_display(brand_words)
        gate_label = self._t("ui_smart_btn_brand_gate_on" if brand_cfg.get("require_forward")
                             else "ui_smart_btn_brand_gate_off", lang=lang)
        force_label = self._t("ui_smart_btn_brand_force_on" if brand_cfg.get("force_forward")
                              else "ui_smart_btn_brand_force_off", lang=lang)

        lines = [
            self._t("ui_smart_title", lang=lang),
            self._t("ui_smart_rule_line", lang=lang, src=src_label, dst=dst_label),
            "────────────────────",
            self._t("ui_smart_active_path", lang=lang, path_desc=path_desc),
            self._t("ui_smart_mid_channel", lang=lang, mid=mid_display),
            self._t("ui_smart_brand_block", lang=lang),
            self._t("ui_smart_brand_state", lang=lang, state=brand_state),
            self._t("ui_smart_brand_origins", lang=lang, origins=origins_display),
            self._t("ui_smart_brand_words", lang=lang, words=words_display),
            # The description says which logic is in force; the raw `ANY`/`ALL`
            # token beside it said the same thing in English.
            self._t("ui_smart_match_logic", lang=lang, mode_desc=mode_desc),
            self._t("ui_smart_keywords", lang=lang, keywords=kw_display),
            self._t("ui_smart_regex", lang=lang, regex=regex_display),
            self._t("ui_smart_custom_header", lang=lang, header=hdr_display),
            self._t("ui_smart_priority", lang=lang, prio=prio),
            self._t("ui_smart_lock_policy", lang=lang),
            self._t("ui_smart_antiloop", lang=lang),
            self._t("ui_smart_hint", lang=lang),
        ]

        # Buttons
        rows: List[List[Tuple[str, str]]] = [
            [(self._t("ui_smart_btn_change_path", lang=lang, path_tag=path_tag[:20]), f"rsmpth:{rule.id}")],
            [
                (self._t("ui_smart_btn_logic", lang=lang,
                         mode=self._logic_word(match_mode, lang=lang)), f"rsmmod:{rule.id}"),
                (self._t("ui_smart_btn_mid", lang=lang), f"rsmw:{rule.id}"),
            ],
            [
                (self._t("ui_smart_btn_keywords", lang=lang), f"rsmtx:{rule.id}"),
                (self._t("ui_smart_btn_regex", lang=lang), f"rsmrx:{rule.id}"),
            ],
            [
                (self._t("ui_smart_btn_brand_origins", lang=lang), f"rsmbo:{rule.id}"),
                (self._t("ui_smart_btn_brand_words", lang=lang), f"rsmbk:{rule.id}"),
            ],
            [
                (gate_label, f"rsmbr:{rule.id}"),
                (force_label, f"rsmbf:{rule.id}"),
            ],
            [
                (self._t("ui_smart_btn_header", lang=lang), f"rsmh:{rule.id}"),
                (self._t("ui_smart_btn_del_header", lang=lang), f"rsmhd:{rule.id}"),
            ],
            [
                (self._t("ui_smart_btn_prio_up", lang=lang), f"rsmpu:{rule.id}"),
                (self._t("ui_smart_btn_prio_down", lang=lang), f"rsmpd:{rule.id}"),
            ],
            [(self._t("ui_smart_btn_dryrun", lang=lang), f"rsmtst:{rule.id}")],
            [(self._btn('ui_rule_back_rule', lang=lang), f"rd:{rule.id}")],
        ]
        return "\n".join(lines), self._kbd(rows)

    def _render_chat_picker(self, st: UiState, role: str, page: int = 0, uid: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(st)
        """Render a rich, paginated dialog selection screen with inline buttons and fail-closed i18n."""
        rc = "s" if role == "source" else "t"
        page_size = 5

        effective_uid = uid or getattr(st, "user_id", None)
        cur_lang = self._resolve_lang(effective_uid) if hasattr(self, "_resolve_lang") else "en"

        def _clean_md(text: Any) -> str:
            if not text:
                return ""
            return str(text).replace("*", "").replace("_", " ").replace("`", "'").replace("[", "(").replace("]", ")")

        is_edit = str(st.step).startswith("rule_edit_")
        if is_edit and isinstance(st.buffer, dict) and st.buffer.get("rule_id"):
            rid = st.buffer.get("rule_id")
            cancel_cb = f"re:{rid}" if st.buffer.get("in_draft") else f"rd:{rid}"
        elif st.step in ("rule_source", "rule_target"):
            cancel_cb = "r_cancel"
        else:
            cancel_cb = CB["cancel"]

        chats = st.buffer.get("chats", []) if isinstance(st.buffer, dict) else []

        if chats:
            total_items = len(chats)
            total_pages = max(1, (total_items + page_size - 1) // page_size)
            page = max(0, min(page, total_pages - 1))
            start = page * page_size
            end = min(start + page_size, total_items)
            page_items = chats[start:end]

            if role == "source":
                header = (
                    f"{self._t('ui_src_sel', lang=cur_lang)}\n"
                    f"*{self._t('ui_src_desc', lang=cur_lang)}*\n"
                    "────────────────────"
                )
            else:
                src_name = st.buffer.get("source_name") or st.rule_source
                header = (
                    f"✅ **{self._t('ui_source', lang=cur_lang)}:** {src_name} (`{st.rule_source}`)\n\n"
                    f"{self._t('ui_tgt_sel', lang=cur_lang)}\n"
                    f"*{self._t('ui_tgt_desc', lang=cur_lang)}*\n"
                    "────────────────────"
                )

            lines = [header]
            for idx, item in enumerate(page_items, start=start + 1):
                clean_title = _clean_md(item["title"])
                uname = f" • @{item['username']}" if item.get("username") else ""
                lines.append(f"{item['emoji']} **{idx}. {clean_title}**\n   🆔 `{item['id']}`{uname}")

            lines.append("────────────────────")
            lines.append(self._t("ui_picker_page_info", lang=cur_lang, page=page + 1, total=total_pages, count=total_items))
            lines.append(self._t("ui_picker_prompt", lang=cur_lang))
            text = "\n".join(lines)

            rows: List[List[Tuple[str, str]]] = []
            for local_idx, item in enumerate(page_items):
                global_idx = start + local_idx
                title_disp = item["title"][:22] + "…" if len(item["title"]) > 22 else item["title"]
                btn_label = f"{item['emoji']} {start + local_idx + 1}. {title_disp}"
                rows.append([(btn_label, f"ch:{rc}:{global_idx}")])

            nav_row: List[Tuple[str, str]] = []
            if page > 0:
                nav_row.append((self._btn('ui_prev', lang=cur_lang), f"pg:{rc}:{page - 1}"))
            nav_row.append((f"📄 {page + 1} / {total_pages}", "noop"))
            if page < total_pages - 1:
                nav_row.append((f"{self._t('ui_next', lang=cur_lang)} ▶️", f"pg:{rc}:{page + 1}"))
            rows.append(nav_row)

            bottom_row: List[Tuple[str, str]] = [
                (self._t("ui_refresh_list", lang=cur_lang), f"rfc:{rc}"),
            ]
            if st.step == "rule_target":
                bottom_row.append((self._t("ui_change_src", lang=cur_lang), "r_back_source"))
            bottom_row.append((self._t("ui_cancel_back", lang=cur_lang), cancel_cb))
            rows.append(bottom_row)
            return text, self._kbd(rows)

        # Fallback when no chats could be fetched automatically
        if role == "source":
            text = (
                f"{self._t('ui_src_sel', lang=cur_lang)}\n\n"
                f"{self._t('ui_no_dialog', lang=cur_lang)}\n\n"
                f"{self._t('ui_no_dialog_bytes', lang=cur_lang)}"
            )
        else:
            src_name = st.buffer.get("source_name") or st.rule_source
            text = (
                f"✅ **{self._t('ui_source', lang=cur_lang)}:** {src_name} (`{st.rule_source}`)\n\n"
                f"{self._t('ui_tgt_sel', lang=cur_lang)}\n\n"
                f"{self._t('ui_target_no_dialog', lang=cur_lang)}\n\n"
                f"{self._t('ui_no_dialog_bytes', lang=cur_lang)}"
            )

        fallback_rows: List[List[Tuple[str, str]]] = [
            [(self._t("ui_refresh_list", lang=cur_lang), f"rfc:{rc}")],
        ]
        if st.step == "rule_target":
            fallback_rows.append([(self._t("ui_change_src", lang=cur_lang), "r_back_source")])
        fallback_rows.append([(self._t("ui_cancel_back", lang=cur_lang), cancel_cb)])
        return text, self._kbd(fallback_rows)

    def _dialog_catalog(self):
        """The application use case over the session-backed gateway.

        Built once per panel instance. The gateway is infrastructure: it owns
        reconnection and the scan; classification, ordering and paging are
        application policy in :mod:`core.application.discovery`.
        """
        if getattr(self, "_dialog_use_case", None) is None:
            from core.application.discovery import ListDialogsUseCase
            from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

            self._dialog_use_case = ListDialogsUseCase(
                SessionDialogGateway(self._sessions, self._pool, is_admin=self._is_admin)
            )
        return self._dialog_use_case

    async def _finish_rule_creation(self, event: Any, st: UiState, source_title: str, target_title: str) -> None:
        lang = self._lang_for(st)
        """Persist ForwardRule and present the completion card."""
        uid: int = int(event.from_user.id if getattr(event, "from_user", None) else (event.chat.id if getattr(event, "chat", None) else 0))
        cur_lang = self._resolve_lang(uid)

        def _norm_ep(s: str) -> str:
            s = (s or "").strip().lower()
            for p in ("https://t.me/", "http://t.me/", "t.me/", "@"):
                if s.startswith(p):
                    s = s[len(p):]
            return s.strip()

        if _norm_ep(st.rule_source) and _norm_ep(st.rule_source) == _norm_ep(st.rule_target):
            st.step = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            err_text = self._t("ui_loop_detected_title", lang=cur_lang)
            markup = self._kbd([
                [(self._t("ui_retry", lang=cur_lang), CB["rule_add"])],
                [(self._btn('ui_main_title', lang=cur_lang), CB["main"])],
            ])
            if hasattr(event, "edit_message_text"):
                await self._safe_edit(event, err_text, reply_markup=markup)
            else:
                await event.reply_text(err_text, reply_markup=markup)
            return

        _fuid = self._scope_uid(uid)
        if _fuid is None and hasattr(self._sessions, "list_all"):
            _res = self._sessions.list_all()
            sessions = await _res if asyncio.iscoroutine(_res) else _res
        elif hasattr(self._sessions, "list_by_owner"):
            _res = self._sessions.list_by_owner(uid)
            sessions = await _res if asyncio.iscoroutine(_res) else _res
        elif hasattr(self._sessions, "list_all"):
            _res = self._sessions.list_all()
            sessions = await _res if asyncio.iscoroutine(_res) else _res
        else:
            sessions = []
        if not sessions:
            st.step = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            msg_text = self._t("ui_no_sessions", lang=cur_lang)
            if hasattr(event, "edit_message_text"):
                await self._safe_edit(event, msg_text, reply_markup=self._sessions_menu())
            else:
                await event.reply_text(msg_text, reply_markup=self._sessions_menu())
            return

        session = sessions[0]
        try:
            from core.domain.entities import ForwardMode, ForwardRule, RoutingType

            rule = ForwardRule(
                session_id=session.id,
                source_chat_id=st.rule_source,
                source_chat_name=source_title,
                target_chat_id=st.rule_target,
                target_chat_name=target_title,
                routing_type=RoutingType.CHANNEL_TO_CHANNEL,
                forward_mode=ForwardMode.COPY_MESSAGE,
                owner_user_id=uid,
            )
            await self._rules.create(rule)
            try:
                self._log.info("bot", "rule", f"rule created {st.rule_source} -> {st.rule_target}")
            except Exception:
                pass

            st.step = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)

            success_text = (
                f"{self._t('ui_rule_created_header', lang=cur_lang)}\n\n"
                f"📥 **{self._t('ui_source', lang=cur_lang)}:** {source_title}\n"
                f"   🆔 `{st.rule_source}`\n\n"
                f"📤 **{self._t('ui_target', lang=cur_lang)}:** {target_title}\n"
                f"   🆔 `{st.rule_target}`\n\n"
                f"{self._t('ui_rule_fwd_mode_desc', lang=cur_lang)}\n"
                f"{self._t('ui_rule_status_monitoring', lang=cur_lang)}"
            )
            markup = self._kbd([
                [(self._t("ui_rule_adv_settings", lang=cur_lang), f"rd:{rule.id}")],
                [(self._t("ui_rule_add_new", lang=cur_lang), CB["rule_add"]), (self._t("ui_rule_view_all", lang=cur_lang), CB["rules"])],
                [(self._btn('ui_main_title', lang=cur_lang), CB["main"])],
            ])

            if hasattr(event, "edit_message_text"):
                await self._safe_edit(event, success_text, reply_markup=markup)
            else:
                await event.reply_text(success_text, reply_markup=markup)

        except Exception as exc:
            await self._record_error("bot", exc, user_id=uid)
            err_text = self._t("ui_rule_fail", lang=lang, error=tg_detail(exc))
            if hasattr(event, "edit_message_text"):
                await self._safe_edit(event, err_text, reply_markup=self._rules_menu())
            else:
                await event.reply_text(err_text, reply_markup=self._rules_menu())

    def _filters_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [(self._btn("ui_filter_add"), CB["filter_add"])],
            [(self._btn("ui_back"), CB["main"])],
        ])

    def _ai_menu(self, cfgs: Optional[list] = None) -> InlineKeyboardMarkup:
        rows: list = []
        if cfgs:
            for c in cfgs:
                status = "🟢" if c.is_enabled else "🔴"
                prov_val = c.provider.value if hasattr(c.provider, "value") else str(c.provider)
                prov = self._provider_label(prov_val)
                label = f"{status} {c.name or 'AI'} [{prov} • {c.model}]"
                rows.append([(label, f"aid:{c.id}")])
        rows.append([(self._t("ui_btn_ai_pv_assistant"), "pv:menu")])
        # The v28 subsystem: same state as the web dashboard, driven from the
        # bot so a phone-only user has the full surface, not a status line.
        rows.append([(self._t("ui_btn_ar_assistant"), "ar:menu")])
        rows.append([(self._btn("ui_ai_add"), CB["ai_add"])])
        rows.append([(self._btn("ui_back"), CB["main"])])
        return self._kbd(rows)

    @staticmethod
    def _endpoint_problem(url: str) -> str:
        """Why the core must not be asked to call `url`, or "" when it may.

        The bot's wizard is the tenant's own way to point the service at a
        gateway, so the same guard the API applies has to run here too: without
        it a chat message could aim the core's HTTP client at the machine it
        runs on.
        """
        if not url:
            return ""
        try:
            from core.infrastructure.ai.providers import endpoint_guard

            return endpoint_guard(url) or ""
        except Exception:
            # A guard that cannot run must not accept the URL.
            return "endpoint_blocked"

    def _render_ai_detail(self, cfg: AIConfig, health_info: Optional[dict] = None) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(cfg)
        status_emoji = self._t("ui_ai_enabled", lang=lang) if cfg.is_enabled else self._t("ui_ai_disabled", lang=lang)
        prov_key = cfg.provider.value if hasattr(cfg.provider, "value") else str(cfg.provider)
        prov_name = self._provider_label(prov_key)

        key = cfg.api_key or ""
        if len(key) > 12:
            masked_key = f"{key[:7]}...{key[-4:]}"
        elif key:
            masked_key = "********"
        else:
            masked_key = self._t("ui_ai_not_set_paren", lang=lang)

        from core.infrastructure.ai.providers import OpenAICompatibleProvider
        base_url = cfg.base_url or OpenAICompatibleProvider.default_base_urls.get(prov_key, "") or self._t("ui_ai_default_paren", lang=lang)

        _t = lambda k, **kw: self._t(k, lang=lang, **kw)
        lines = [
            _t("ui_ai_detail_title"),
            "────────────────────",
            _t("ui_ai_detail_name", name=cfg.name or '—'),
            _t("ui_ai_detail_id", id=cfg.id),
            _t("ui_ai_detail_provider", prov_name=prov_name, prov_key=prov_key),
            _t("ui_ai_detail_model", model=cfg.model),
            _t("ui_ai_detail_baseurl", base_url=base_url),
            _t("ui_ai_detail_key", masked_key=masked_key),
            _t("ui_ai_detail_temp", temp=cfg.temperature),
            _t("ui_ai_detail_status", status_emoji=status_emoji),
        ]

        if health_info:
            lines.append("────────────────────")
            if health_info.get("ok"):
                lines.append(_t("ui_ai_health_ok"))
                lines.append(_t("ui_ai_health_latency", latency=f"{health_info.get('latency_ms', 0):.0f}ms"))
                if health_info.get("reply"):
                    reply_preview = str(health_info["reply"])[:120].replace("\n", " ")
                    lines.append(_t("ui_ai_health_reply", reply=reply_preview))
            else:
                lines.append(_t("ui_ai_health_fail"))
                err_msg = str(health_info.get("error", "unknown"))[:200]
                lines.append(_t("ui_ai_health_error", error=err_msg))

        lines.append("────────────────────")
        lines.append(_t("ui_ai_detail_hint"))
        text = "\n".join(lines)

        tog_text = _t("ui_ai_btn_disable") if cfg.is_enabled else _t("ui_ai_btn_enable")
        rows = [
            [
                (_t("ui_ai_btn_health"), f"ai_hc:{cfg.id}"),
                (_t("ui_ai_btn_sample"), f"ai_sample:{cfg.id}"),
            ],
            [
                (_t("ui_ai_btn_baseurl"), f"ai_eh:{cfg.id}"),
                (_t("ui_ai_btn_model"), f"ai_em:{cfg.id}"),
            ],
            [
                (_t("ui_ai_btn_key"), f"ai_ek:{cfg.id}"),
                (tog_text, f"ai_tog:{cfg.id}"),
            ],
            [
                (_t("ui_ai_btn_delete"), f"ai_del_ask:{cfg.id}"),
                (_t("ui_ai_btn_back"), CB["ai"]),
            ],
        ]
        return text, self._kbd(rows)

    def _render_pv_menu(self, uid: int = 0, lang: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
        # An explicit lang wins for EVERY locale — re-resolving a caller's
        # "fa" against the database silently substituted another language.
        lang = lang or self._lang_for(uid=uid)
        resp = getattr(self, "_pv_responder", None)
        if not resp:
            return self._t("ui_pv_not_loaded", lang=lang), self._kbd([[(self._btn("ui_back", lang=lang), CB["ai"])]])

        cfg = resp.get_config_for_user(uid) if hasattr(resp, "get_config_for_user") else resp.config
        persona_preview = (cfg.persona_prompt[:140] + "...") if len(cfg.persona_prompt) > 140 else cfg.persona_prompt

        status_str = self._t("ui_ai_enabled", lang=lang) if cfg.enabled else self._t("ui_ai_disabled", lang=lang)
        tog_btn = self._t("ui_ai_btn_disable", lang=lang) if cfg.enabled else self._t("ui_ai_btn_enable", lang=lang)
        ignore_str = self._t("ui_yes", lang=lang) if cfg.ignore_bots else self._t("ui_no", lang=lang)

        text = (
            self._t("ui_pv_title", lang=lang)
            + self._t("ui_pv_status_line", lang=lang, status=status_str)
            + self._t("ui_pv_typing_line", lang=lang, min=cfg.typing_delay_min, max=cfg.typing_delay_max)
            + self._t("ui_pv_cooldown_line", lang=lang, cooldown=cfg.cooldown_seconds)
            + self._t("ui_pv_bots_line", lang=lang, ignore=ignore_str)
            + self._t("ui_pv_persona_line", lang=lang, persona=persona_preview)
            + self._t("ui_pv_features_line", lang=lang)
        )
        rows = [
            [(self._t("ui_pv_btn_status", lang=lang, btn=tog_btn), "pv:toggle")],
            [(self._t("ui_pv_btn_persona", lang=lang), "pv:persona")],
            [(self._t("ui_pv_btn_delays", lang=lang), "pv:delays")],
            [(self._t("ui_pv_btn_test", lang=lang), "pv:test")],
            [(self._btn("ui_back", lang=lang), CB["ai"])],
        ]
        return text, self._kbd(rows)

    def _render_pv_persona_menu(self, lang: Optional[str] = None, uid: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        # An explicit lang wins for EVERY locale — re-resolving a caller's
        # "fa" against the database silently substituted another language.
        lang = lang or self._lang_for(uid=uid)
        text = self._t("ui_pv_p_title", lang=lang)
        rows = [
            [(self._t("ui_pv_p_casual", lang=lang), "pv:set_p:casual")],
            [(self._t("ui_pv_p_business", lang=lang), "pv:set_p:business")],
            [(self._t("ui_pv_p_short", lang=lang), "pv:set_p:short")],
            [(self._t("ui_pv_p_custom", lang=lang), "pv:set_p:custom")],
            [(self._t("ui_pv_p_back", lang=lang), "pv:menu")],
        ]
        return text, self._kbd(rows)

    def _render_pv_delays_menu(self, lang: Optional[str] = None, uid: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        # An explicit lang wins for EVERY locale — re-resolving a caller's
        # "fa" against the database silently substituted another language.
        lang = lang or self._lang_for(uid=uid)
        text = self._t("ui_pv_d_title", lang=lang)
        rows = [
            [(self._t("ui_pv_d_fast", lang=lang), "pv:set_d:fast")],
            [(self._t("ui_pv_d_normal", lang=lang), "pv:set_d:normal")],
            [(self._t("ui_pv_d_slow", lang=lang), "pv:set_d:slow")],
            [(self._t("ui_pv_d_back", lang=lang), "pv:menu")],
        ]
        return text, self._kbd(rows)

    def _logs_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [(self._btn("ui_logs_recent"), CB["logs_recent"]),
             (self._btn("ui_logs_errors"), CB["logs_errors"])],
            [(self._btn("ui_logs_stats"), CB["logs_stats"])],
            [(self._btn("ui_back"), CB["main"])],
        ])

    def _debug_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [(self._btn("ui_logs_recent"), CB["logs_recent"]),
             (self._btn("ui_logs_errors"), CB["logs_errors"])],
            [(self._btn("ui_perf"), CB["perf"])],
            [(self._btn("ui_refresh"), CB["refresh"])],
            [(self._btn("ui_back"), CB["main"])],
        ])

    def _lang_menu(self) -> InlineKeyboardMarkup:
        # The same label the picker shows, from the same row: flag, then the
        # language's own name.
        items = [(language_label(code), f'{CB["lang_set"]}:{code}') for code in LANG_FLAG]
        rows = [items[i:i + 2] for i in range(0, len(items), 2)]
        rows.append([(self._t("ui_back"), CB["main"])])
        return self._kbd(rows)

    # ------------------------------------------------------------------ #
    # API credentials vault
    # ------------------------------------------------------------------ #
    def _creds_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [(self._btn("ui_cred_add"), CB["cred_add"])],
            [(self._btn("ui_migrate"), CB["migrate"]), (self._btn("ui_debug"), CB["debug"])],
            [(self._btn("ui_weburl"), CB["weburl"])],
            [(self._btn("ui_back"), CB["main"])],
        ])

    def _provider_label(self, key: str) -> str:
        return {
            "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google Gemini",
            "groq": "Groq", "deepseek": "DeepSeek", "openrouter": "OpenRouter",
            "9router": "9Router", "ninerouter": "9Router", "xai": "xAI",
            "mistral": "Mistral", "together": "Together AI", "fireworks": "Fireworks",
            "custom": "Custom",
        }.get(key, key or "—")

    def _provider_picker(self) -> InlineKeyboardMarkup:
        names = self._ai.provider_names() if hasattr(self._ai, "provider_names") else []
        if not names:
            names = [p.value for p in AIProviderType]
        rows: list = []
        row: list = []
        for name in names:
            row.append((self._provider_label(name), f'{CB["ai_provider_pick"]}:{name}'))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([(self._btn("ui_cancel"), CB["cancel"])])
        return self._kbd(rows)

    def _model_picker(self, st: UiState, provider: str) -> InlineKeyboardMarkup | None:
        lang = self._lang_for(st)
        models = self._ai.models_for(provider) if hasattr(self._ai, "models_for") else []
        models = list(models or [])[:10]
        if not models:
            return None
        # Index-based callback data keeps payloads far under the 64-byte limit.
        st.buffer["models"] = models
        rows: list = []
        for i, model in enumerate(models):
            rows.append([(model, f'{CB["ai_model_pick"]}:{i}')])
        rows.append([(self._btn("ui_cancel", lang=lang), CB["cancel"])])
        return self._kbd(rows)

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #
    def _register(self) -> None:
        b = self.bot

        # v28 auto-reply panel: a mixin so its ~20 callbacks stay in one file.
        self.register_autoreply_handlers()
        # The unified /replace manager: list, detail, create/edit, tasks, test.
        self.register_replace_handlers()

        # v29 text-command surface: the commands the registry says no earlier
        # handler owns. Mounted at group -2 so a command always reaches the
        # router before the free-text FSM router can see it.
        try:
            self._mount_text_commands(b)
        except Exception as exc:  # a broken command table must not kill the UI
            logger.exception("text command mount failed: %s", exc)

        # Handler modules: each surface (discovery, forwarding, ...) is a mixin
        # whose handlers mount through one validated registry, so a command can
        # never be owned twice and a declared handler cannot be missing.
        self._handler_registry.mount(b, self)

        # Trace all callback queries across the UI
        @b.on_callback_query(group=-1)
        @self._localized_handler
        async def _trace_all_callbacks(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            data = cq.data or ""
            logger.info("Button clicked by uid=%s: callback_data=%r", uid, data)
            try:
                self._log.info("bot", "cb", f"click from {uid}: {data}")
            except Exception:
                pass
            # Do NOT swallow dispatcher flow: `continue_propagation()` raises
            # ContinuePropagation (a StopAsyncIteration subclass) and the
            # dispatcher relies on that exception propagating to reach handlers
            # in the next group. Wrapping it in `except Exception` silently ate
            # it, so no real handler ever ran and buttons appeared dead.
            # Best-effort local log only; never touch the propagation signal.
            if not data:
                await cq.continue_propagation()

        @b.on_message(filters.command("start") & filters.private)
        @self._localized_handler
        async def _start(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /start from uid=%s", uid)
            # An invite link (`/start ref_<id>`) is attributed before anything
            # else can fail: the referral is the visitor's first impression and
            # must not depend on the menu render below.
            await self._record_referral(uid, message.text or "")
            try:
                # Reset any in-progress FSM state on fresh /start
                st = self._state(uid)
                st.step = ""
                st.rule_source = ""
                st.rule_target = ""
                if isinstance(st.buffer, dict):
                    st.buffer.clear()
                await self._persist(uid, st)
                self._login.cancel(uid)

                # If user has no sessions connected yet, show onboarding screen
                sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
                if not sessions:
                    text, kbd = self._render_welcome_onboarding(uid, user=message.from_user)
                    await message.reply_text(text, reply_markup=kbd)
                else:
                    body = await self._main_text(uid, user=message.from_user)
                    await message.reply_text(body, reply_markup=self._main_menu())
                try:
                    self._log.info("bot", "system", f"/start by {uid}")
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("/start failed for uid=%s: %s", uid, exc)

        @b.on_message(filters.command("connect") & filters.private)
        @self._localized_handler
        async def _connect_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /connect from uid=%s", uid)
            if not uid:
                return
            st = self._state(uid)
            st.step = "login_phone"
            await self._persist(uid, st)
            args = (message.text or "").split(None, 1)
            phone_arg = args[1].strip() if len(args) > 1 else ""
            if phone_arg:
                await self._step_login_phone(message, st, phone_arg)
            else:
                prompt = await self._connect_prompt_text(uid)
                await message.reply_text(prompt, reply_markup=self._cancel_kbd())

        @b.on_message(filters.command("cancel") & filters.private)
        @self._localized_handler
        async def _cancel_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /cancel from uid=%s", uid)
            if not uid:
                return
            st = self._state(uid)
            # Preserve active login flow to avoid "no_pending_login" errors
            if not getattr(st, "step", "").startswith("login_"):
                st.step = ""
                st.rule_source = ""
                st.rule_target = ""
                if isinstance(st.buffer, dict):
                    st.buffer.clear()
                await self._persist(uid, st)
                if self._login.has_pending(uid):
                    await self._login.cancel(uid)
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                lang = await self._get_user_lang(uid)
                c_text, btn_connect, btn_menu = self._cancelled_connect_texts(lang)
                kbd = self._kbd([
                    [(btn_connect, CB["login"])],
                    [(btn_menu, CB["main"])],
                ])
                await message.reply_text(c_text, reply_markup=kbd)
            else:
                await message.reply_text(self._t("ui_cancelled"), reply_markup=self._main_menu())

        @b.on_message(filters.command("reauth") & filters.private)
        @self._localized_handler
        async def _reauth_cmd(_, message: Message):
            """Explicit reauthentication: a fresh single-use dashboard link.

            Root cause of the old 'logged out with no way back in' bug: the
            7-day session token expired and no bot-side path minted another
            one. /reauth answers exactly that.
            """
            uid = message.from_user.id if message.from_user else 0
            if not uid:
                return
            base = self._web_base_url()
            magic_link = ""
            try:
                # try_issue refuses a second mint inside the cooldown, so a
                # rapid /reauth /reauth /reauth burst cannot generate three
                # links or three messages — the first still-valid one wins.
                issued = self._accounts._login_codes.try_issue(uid, cooldown_seconds=60)
                if issued is not None:
                    code, _exp = issued
                    magic_link = f"{base}/?code={quote(code, safe='')}"
            except Exception as e:
                logger.error("reauth code mint failed for %s: %s", uid, e)
            if not magic_link:
                # Not an error on purpose: a still-valid link exists, so the
                # short cooldown answer is the honest reply — no new link,
                # no repeated Telegram prompt.
                remaining = 60
                try:
                    remaining = self._accounts._login_codes.cooldown_remaining(uid, cooldown_seconds=60)
                except Exception:
                    pass
                wait = max(1, remaining)
                try:
                    await message.reply_text(self._t("ui_reauth_cooldown").replace("{wait}", str(wait)))
                except Exception as e:
                    logger.error("reauth cooldown memo failed for %s: %s", uid, e)
                return
            text = self._t("ui_reauth_required")
            btn_open = self._btn("ui_dashboard_open")
            btn_menu = self._btn("ui_back")
            kbd = InlineKeyboardMarkup([
                [InlineKeyboardButton(btn_open, url=magic_link)],
                [InlineKeyboardButton(btn_menu, callback_data=CB["main"])],
            ])
            await message.reply_text(text, reply_markup=kbd)

        @b.on_message(filters.command(["menu", "panel"]) & filters.private)
        @self._localized_handler
        async def _menu(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /menu from uid=%s", uid)
            try:
                sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
                if not sessions:
                    text, kbd = self._render_welcome_onboarding(uid, user=message.from_user)
                    await message.reply_text(text, reply_markup=kbd)
                else:
                    body = await self._main_text(uid, user=message.from_user)
                    await message.reply_text(body, reply_markup=self._main_menu())
            except Exception as exc:
                logger.exception("/menu failed for uid=%s: %s", uid, exc)

        @b.on_message(filters.command("settings") & filters.private)
        @self._localized_handler
        async def _settings_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /settings from uid=%s", uid)
            lang = self._resolve_lang(uid)
            role_key = "ui_settings_role_admin" if self._is_admin(uid) else "ui_settings_role_user"
            text = self._t("ui_settings_body", lang=lang, uid=uid, role=self._t(role_key, lang=lang))
            kbd = self._client_settings_menu(uid)
            await message.reply_text(text, reply_markup=kbd)

        @b.on_message(filters.command("admin") & filters.private)
        @self._localized_handler
        async def _admin_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /admin from uid=%s", uid)
            if not self._is_admin(uid):
                return await message.reply_text(self._t("ui_need_admin", lang=self._resolve_lang(uid)))
            text, kbd = await self._render_admin_panel(uid)
            await message.reply_text(text, reply_markup=kbd)

        @b.on_message(filters.command("profile") & filters.private)
        @self._localized_handler
        async def _profile_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /profile from uid=%s", uid)
            try:
                body, kbd = await self._render_profile_menu(uid, message.from_user)
                await message.reply_text(body, reply_markup=kbd)
            except Exception as exc:
                logger.exception("/profile failed for uid=%s: %s", uid, exc)

        @b.on_callback_query(filters.regex("^" + CB["main"] + "$"))
        @self._localized_handler
        async def _cb_main(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text, kbd = self._render_welcome_onboarding(uid, user=cq.from_user)
                await self._safe_edit(cq, text, reply_markup=kbd)
            else:
                await self._safe_edit(
                    cq, 
                    await self._main_text(uid, user=cq.from_user),
                    reply_markup=self._main_menu(),
                )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["sessions"] + "$"))
        @self._localized_handler
        async def _cb_sessions(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await self._ui_manager.enter_screen(uid, CB["sessions"])
            logger.info("Executing _cb_sessions for uid=%s", uid)
            try:
                sessions = await self._sessions.list_by_owner(uid) if uid else []
                lines = [self._t("ui_sessions_title"), ""]
                if not sessions:
                    lines.append(self._t("ui_none"))
                for s in sessions:
                    try:
                        live = self._pool.get(s.id) is not None
                    except Exception:
                        live = False
                    mark = "🟢" if live else ("🟡" if s.is_active else "🔴")
                    lines.append(f"{mark} `{s.id[:8]}` +{s.phone_number}")
                await self._safe_edit(cq, "\n".join(lines), reply_markup=self._sessions_menu())
                await cq.answer()
            except Exception as exc:
                logger.exception("_cb_sessions failed for uid=%s: %s", uid, exc)
                try:
                    await cq.answer(self._t("ui_internal_error"), show_alert=True)
                except Exception:
                    pass

        @b.on_callback_query(filters.regex("^" + CB["rules"] + "$"))
        @self._localized_handler
        async def _cb_rules(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await self._ui_manager.enter_screen(uid, CB["rules"])
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, kbd = self._render_rules_list(rules, page=0)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_pg:(\d+)$"))
        @self._localized_handler
        async def _cb_rules_page(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            page = int(raw.split(":", 1)[1])
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, kbd = self._render_rules_list(rules, page=page)
            try:
                await self._safe_edit(cq, text, reply_markup=kbd)
            except Exception:
                pass
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["filters"] + "$"))
        @self._localized_handler
        async def _cb_filters(_, cq: CallbackQuery):
            uid_f = cq.from_user.id if cq.from_user else 0
            filters_ = await self._filters.list_by_owner(uid_f) if uid_f else []
            lines = [self._t("ui_filters_title"), ""]
            if not filters_:
                lines.append(self._t("ui_none"))
            for f in filters_:
                lines.append(f"🧹 `{f.id[:8]}` {f.name}")
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._filters_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["ai"] + "$"))
        @self._localized_handler
        async def _cb_ai(_, cq: CallbackQuery):
            uid_a = cq.from_user.id if cq.from_user else 0
            cfgs = await self._ai.list_by_owner(uid_a) if uid_a else []
            lines = [self._t("ui_ai_title"), ""]
            if not cfgs:
                lines.append(self._t("ui_none"))
            else:
                lines.append(self._t("ui_ai_menu_click_hint"))
            for c in cfgs:
                status = "🟢" if c.is_enabled else "🔴"
                lines.append(
                    f"{status} `{c.id[:8]}` {c.name} "
                    f"[{self._provider_label(c.provider.value)}/{c.model}]")
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._ai_menu(cfgs))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["stats"] + "$"))
        @self._localized_handler
        async def _cb_stats(_, cq: CallbackQuery):
            kbd = self._kbd([
                [(self._btn("ui_refresh"), CB["refresh"])],
                [(self._btn("ui_back"), CB["main"])],
            ])
            await self._safe_edit(cq, 
                await self._dashboard_body(), reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["diag"] + "$"))
        @self._localized_handler
        async def _cb_diag(_, cq: CallbackQuery):
            """What is stuck, what failed and under which id — for this user only.

            The error log and the per-task counters are global tables, so the
            rule ids are gathered owner-scoped here and everything else is
            filtered against them: another tenant's task can never appear, and a
            row that names an unowned rule is dropped rather than summarised.
            """
            uid = cq.from_user.id if cq.from_user else 0
            await self._ui_manager.enter_screen(uid, CB["diag"])
            await self._ui_manager.enter_screen(uid, CB["stats"])
            await self._ui_manager.enter_screen(uid, CB["ai"])
            await self._ui_manager.enter_screen(uid, CB["filters"])
            owned: list[str] = []
            try:
                owned = diagnostics.owned_ids_of(
                    await self._rules.list_by_owner(uid)
                ) if uid else []
            except Exception:
                owned = []
            lang = self._resolve_lang(uid)
            body = await diagnostics.diagnostics_message(
                lambda key, **params: self._t(key, lang=lang, **params),
                queue=self._delivery_queue,
                errors=self._error_log,
                metrics=self._metrics,
                rule_stats=self._rule_stats,
                owned_rule_ids=owned,
            )
            # A dead letter is the one queue state a user can act on: offer the
            # list only when there is one, so a healthy account is not offered a
            # button that answers "nothing".
            stuck = 0
            try:
                stuck = await diagnostics.count_dead_letters(
                    queue=self._delivery_queue, owned_rule_ids=owned
                ) if self._delivery_queue is not None else 0
            except Exception:
                stuck = 0
            rows = [
                [(self._btn("ui_refresh"), CB["diag"])]
            ]
            if stuck > 0:
                rows.insert(0, [
                    (self._t("ui_dlq_open", count=stuck), CB["diag_dlq"]),
                ])
            kbd = self._kbd(rows + [
                [(self._btn("ui_back"), CB["main"])]
            ])
            await self._safe_edit(cq, body, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["diag_dlq"] + "$"))
        @b.on_callback_query(filters.regex("^" + CB["diag_dlq_refresh"] + "$"))
        @self._localized_handler
        async def _cb_diag_dlq(_, cq: CallbackQuery):
            """The entries behind the badge, for this user only.

            The list lives in the diagnostics module so the read goes through
            the repository's owner-scoped contract, not a query of our own;
            `owned` is what keeps another tenant's failed delivery off this
            screen.
            """
            uid = cq.from_user.id if cq.from_user else 0
            owned: list[str] = []
            try:
                owned = diagnostics.owned_ids_of(
                    await self._rules.list_by_owner(uid)
                ) if uid else []
            except Exception:
                owned = []
            lang = self._resolve_lang(uid)
            collected = await diagnostics.collect_dead_letters(
                queue=self._delivery_queue, owned_rule_ids=owned
            )
            self._dlq_page[uid] = collected["entries"]
            text = diagnostics.render_dead_letters(
                lambda key, **params: self._t(key, lang=lang, **params), collected
            )
            rows = []
            for idx, entry in enumerate(collected["entries"]):
                rows.append([InlineKeyboardButton(
                    self._t("ui_dlq_retry_one", n=idx + 1),
                    callback_data=f"{CB['diag_dlq_replay']}:{uid}:{idx}",
                )])
            rows.append([InlineKeyboardButton(
                self._btn("ui_refresh"), callback_data=CB["diag_dlq_refresh"]
            )])
            if collected["entries"]:
                rows.append([InlineKeyboardButton(
                    self._t("ui_dlq_purge_all"), callback_data=CB["diag_dlq_purge"]
                )])
            rows.append([InlineKeyboardButton(
                self._btn("ui_back"), callback_data=CB["diag"]
            )])
            await self._safe_edit(cq, text, reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["diag_dlq_replay"] + r":\d+:\d+$"))
        @self._localized_handler
        async def _cb_diag_dlq_replay(_, cq: CallbackQuery):
            """Re-queue one entry by its index on this user's page.

            The index, not the id, travels in the payload. The id is looked up
            in this user's own page, so a payload cannot name another tenant's
            entry — and a stale index (page refreshed, message old) is a
            no-op rather than a guess.
            """
            uid = cq.from_user.id if cq.from_user else 0
            parts = cq.data.split(":", 2)
            page_owner = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 0
            idx = int(parts[2]) if len(parts) > 2 and str(parts[2]).isdigit() else -1
            lang = self._resolve_lang(uid)
            entries = self._dlq_page.get(page_owner) or []
            if page_owner != uid or not (0 <= idx < len(entries)):
                # Not this user's page, or an index that is no longer on it.
                await cq.answer(self._t("ui_dlq_not_found", lang=lang), show_alert=True)
                return
            entry = entries[idx]
            result = await diagnostics.replay_dead_letter(
                queue=self._delivery_queue,
                entry_id=str(entry.get("id") or ""),
                owned_rule_ids=diagnostics.owned_ids_of(
                    await self._rules.list_by_owner(uid)
                ) if uid else [],
            )
            key = diagnostics.result_code_key(result) or "ui_dlq_replay_failed"
            await cq.answer(self._t(key, lang=lang), show_alert=not (
                result == "requeued"
            ))
            # Refresh the page in place: a successful replay drops its entry and
            # a refused one tells the user why it is still there.
            collected = await diagnostics.collect_dead_letters(
                queue=self._delivery_queue,
                owned_rule_ids=diagnostics.owned_ids_of(
                    await self._rules.list_by_owner(uid)
                ) if uid else [],
            )
            self._dlq_page[uid] = collected["entries"]
            text = diagnostics.render_dead_letters(
                lambda key, **params: self._t(key, lang=lang, **params), collected
            )
            rows = []
            for i, e in enumerate(collected["entries"]):
                rows.append([InlineKeyboardButton(
                    self._t("ui_dlq_retry_one", n=i + 1),
                    callback_data=f"{CB['diag_dlq_replay']}:{uid}:{i}",
                )])
            rows.append([InlineKeyboardButton(
                self._btn("ui_refresh"), callback_data=CB["diag_dlq_refresh"]
            )])
            if collected["entries"]:
                rows.append([InlineKeyboardButton(
                    self._t("ui_dlq_purge_all"), callback_data=CB["diag_dlq_purge"]
                )])
            rows.append([InlineKeyboardButton(
                self._btn("ui_back"), callback_data=CB["diag"]
            )])
            await self._safe_edit(cq, text, reply_markup=InlineKeyboardMarkup(rows))

        @b.on_callback_query(filters.regex("^" + CB["diag_dlq_purge_confirm"] + "$"))
        @self._localized_handler
        async def _cb_diag_dlq_purge_confirm(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            lang = self._resolve_lang(uid)
            owned = diagnostics.owned_ids_of(
                await self._rules.list_by_owner(uid)
            ) if uid else []
            removed = await diagnostics.purge_dead_letters(
                queue=self._delivery_queue, owned_rule_ids=owned
            )
            self._dlq_page[uid] = []
            await self._safe_edit(
                cq,
                self._t("ui_dlq_purged", count=removed, lang=lang),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        self._btn("ui_back", lang=lang),
                        callback_data=CB["diag"],
                    )
                ]]),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["diag_dlq_purge"] + "$"))
        @self._localized_handler
        async def _cb_diag_dlq_purge(_, cq: CallbackQuery):
            """Purge is irreversible, so it gets a confirm step."""
            uid = cq.from_user.id if cq.from_user else 0
            lang = self._resolve_lang(uid)
            await self._safe_edit(
                cq,
                self._t("ui_dlq_purge_confirm", lang=lang),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        self._btn("ui_dlq_purge_yes", lang=lang),
                        callback_data=CB["diag_dlq_purge_confirm"],
                    )],
                    [InlineKeyboardButton(
                        self._btn("ui_dlq_back_to_list", lang=lang),
                        callback_data=CB["diag_dlq"],
                    )],
                ]),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["perf"] + "$"))
        @self._localized_handler
        async def _cb_perf(_, cq: CallbackQuery):
            await self._safe_edit(cq, await self._perf_body(), reply_markup=self._debug_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["refresh"] + "$"))
        @self._localized_handler
        async def _cb_refresh(_, cq: CallbackQuery):
            kbd = self._kbd([
                [(self._btn("ui_refresh"), CB["refresh"])],
                [(self._btn("ui_back"), CB["main"])],
            ])
            await self._safe_edit(cq, 
                await self._dashboard_body(), reply_markup=kbd)
            await cq.answer(self._t("ui_done"), show_alert=False)

        @b.on_callback_query(filters.regex("^" + CB["lang"] + "$"))
        @self._localized_handler
        async def _cb_lang(_, cq: CallbackQuery):
            await self._safe_edit(cq, self._t("ui_lang_choose"), reply_markup=self._lang_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["lang_set"] + ":"))
        @self._localized_handler
        async def _cb_lang_set(_, cq: CallbackQuery):
            code = cq.data.split(":", 1)[1]
            uid = cq.from_user.id if cq.from_user else 0
            # PERSISTENCE ONLY — never mutate the process-wide i18n.current:
            # it is shared by every concurrent user, so one user's choice
            # would leak into everyone else's renders. The language is read
            # back per-user via _resolve_lang(uid).
            if code not in SUPPORTED_LANGUAGES:
                await cq.answer()
                return
            st = self._state(uid)
            st.lang = code
            # The memo is what every subsequent render reads first; a stale
            # entry here would render the *previous* language until restart.
            self.remember_language(uid, code)
            if self._users is not None:
                try:
                    await self._users.set_language(uid, code)
                except Exception:
                    pass
            try:
                self._log.info("bot", "system", f"language set to {code} for user {uid}")
            except Exception:
                pass
            await self._safe_edit(
                cq,
                await self._main_text(uid, user=cq.from_user),
                reply_markup=self._main_menu(),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["help"] + "$"))
        @self._localized_handler
        async def _cb_help(_, cq: CallbackQuery):
            btn_back = self._btn("ui_back")
            btn_sup = self._t("ui_support_btn", lang=self._resolve_lang(cq.from_user.id))
            kbd = InlineKeyboardMarkup([
                [InlineKeyboardButton(btn_sup, url="https://t.me/Capxi")],
                [InlineKeyboardButton(btn_back, callback_data=CB["main"])],
            ])
            await self._safe_edit(cq, self._t("ui_help_body"), reply_markup=kbd)
            await cq.answer()

        # ---------------------------------------------------------- web dashboard
        @b.on_callback_query(filters.regex("^" + CB["dashboard"] + "$"))
        @self._localized_handler
        async def _cb_dashboard(_, cq: CallbackQuery):
            await self._show_dashboard(cq, force_reset=False)

        @b.on_callback_query(filters.regex("^" + CB["webpass"] + "$"))
        @self._localized_handler
        async def _cb_webpass(_, cq: CallbackQuery):
            await self._show_dashboard(cq, force_reset=False)

        @b.on_callback_query(filters.regex("^" + CB["webpass_reset"] + "$"))
        @self._localized_handler
        async def _cb_webpass_reset(_, cq: CallbackQuery):
            await self._show_dashboard(cq, force_reset=True)

        # ---------------------------------------------------------- upgrade / VIP
        @b.on_callback_query(filters.regex("^" + CB["upgrade"] + "$"))
        @self._localized_handler
        async def _cb_upgrade(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            text = self._upgrade_text(uid)
            btn_buy = self._t("ui_buy_btn", lang=self._resolve_lang(uid))
            btn_back = self._btn("ui_back")
            kbd = InlineKeyboardMarkup([
                [InlineKeyboardButton(btn_buy, url="https://t.me/Capxi")],
                [InlineKeyboardButton(btn_back, callback_data=CB["main"])],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["back"] + "$"))
        @self._localized_handler
        async def _cb_back(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            # Pro state manager: Back means "the screen before this one",
            # not "the main menu" — the screen stack survives restarts.
            _dropped, _st = await self._ui_manager.pop_screen(uid)
            stack = await self._ui_manager.current_stack(uid)
            target = stack[-1] if stack else ""
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            # Re-render whatever the stack says the user was on; anything the
            # stack does not know (fresh restart mid-menu, unknown screen)
            # falls back to the main menu — the same behaviour as before, no
            # worse, and now logged instead of guessy.
            try:
                if not sessions:
                    text, kbd = self._render_welcome_onboarding(uid, user=cq.from_user)
                    await self._safe_edit(cq, text, reply_markup=kbd)
                elif target == CB["rules"] or target == CB["sessions"] or target == CB["filters"] or target == CB["stats"] or target == CB["settings"] or target == CB["ai"]:
                    if target == CB["rules"]:
                        rules = await self._rules.list_by_owner(uid) if uid else []
                        text, kbd = self._render_rules_list(rules, page=0)
                    elif target == CB["sessions"]:
                        rules_or_sessions = await self._sessions.list_by_owner(uid) if uid else []
                        lines = [self._t("ui_sessions_title"), ""]
                        for s0 in rules_or_sessions:
                            lines.append(f"`{s0.id[:8]}` +{s0.phone_number}")
                        text, kbd = "\n".join(lines), self._sessions_menu()
                    elif target == CB["filters"]:
                        fs = await self._filters.list_by_owner(uid) if uid else []
                        lines = [self._t("ui_filters_title"), ""]
                        for f0 in fs:
                            lines.append(f"🧹 `{f0.id[:8]}` {f0.name}")
                        text, kbd = "\n".join(lines), self._filters_menu()
                    elif target == CB["stats"]:
                        stats_kbd = self._kbd([
                            [(self._btn("ui_refresh"), CB["refresh"])],
                            [(self._btn("ui_back"), CB["back"])],
                        ])
                        text, kbd = await self._dashboard_body(), stats_kbd
                    elif target == CB["settings"]:
                        lang = self._resolve_lang(uid)
                        role_key = "ui_settings_role_admin" if self._is_admin(uid) else "ui_settings_role_user"
                        text = self._t("ui_settings_body", lang=lang, uid=uid, role=self._t(role_key, lang=lang))
                        kbd = self._client_settings_menu(uid)
                    else:
                        cfgs = await self._ai.list_by_owner(uid) if uid else []
                        lines = [self._t("ui_ai_title"), ""]
                        for c0 in cfgs:
                            lines.append(f"`{c0.id[:8]}` {c0.name}")
                        text, kbd = "\n".join(lines), self._ai_menu(cfgs)
                    await self._safe_edit(cq, text, reply_markup=kbd)
                else:
                    text, kbd = self._render_welcome_onboarding(uid, user=cq.from_user)
                    body = await self._main_text(uid, user=cq.from_user)
                    await self._safe_edit(cq, body, reply_markup=self._main_menu())
            except Exception as exc:
                logger.exception("back navigation failed for uid=%s: %s", uid, exc)
                body = await self._main_text(uid, user=cq.from_user)
                await self._safe_edit(cq, body, reply_markup=self._main_menu())
            await cq.answer()

        # ---------------- client settings & personal credentials ----------------
        @b.on_callback_query(filters.regex("^" + CB["settings"] + "$"))
        @self._localized_handler
        async def _cb_settings(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await self._ui_manager.enter_screen(uid, CB["settings"])
            lang = self._resolve_lang(uid)
            role_key = "ui_settings_role_admin" if self._is_admin(uid) else "ui_settings_role_user"
            text = self._t("ui_settings_body", lang=lang, uid=uid, role=self._t(role_key, lang=lang))
            kbd = self._client_settings_menu(uid)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["client_creds"] + "$"))
        @self._localized_handler
        async def _cb_client_creds(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            has_custom = False
            masked_hash = ""
            api_id = 0
            if self._credentials:
                try:
                    owned = await self._credentials.list_by_owner(uid)
                    if owned:
                        has_custom = True
                        api_id = owned[0].api_id
                        h = owned[0].api_hash or ""
                        masked_hash = f"{h[:2]}***{h[-4:]}" if len(h) >= 8 else "***"
                except Exception:
                    pass

            lang = self._resolve_lang(uid)
            lines = [self._t("ui_client_credentials_intro", lang=lang)]
            if has_custom:
                lines.extend((
                    self._t("ui_client_credentials_active", lang=lang),
                    self._t("ui_client_credentials_api_id", lang=lang, api_id=api_id),
                    self._t("ui_client_credentials_api_hash", lang=lang, masked_hash=masked_hash),
                ))
            else:
                lines.append(self._t("ui_client_credentials_default", lang=lang))

            rows = [[InlineKeyboardButton(
                self._t("ui_client_credentials_edit", lang=lang),
                callback_data="clc_edit",
            )]]
            if has_custom:
                rows.append([InlineKeyboardButton(
                    self._t("ui_client_credentials_reset", lang=lang),
                    callback_data=CB["client_cred_reset"],
                )])
            rows.append([InlineKeyboardButton(
                self._t("ui_back_main_menu", lang=lang),
                callback_data=CB["settings"],
            )])
            await self._safe_edit(cq, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex("^clc_edit$"))
        @self._localized_handler
        async def _cb_client_creds_edit(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            st.step = "client_api_id"
            await self._persist(uid, st)
            msg = self._t("ui_client_credentials_api_id_prompt", lang=self._resolve_lang(uid))
            await self._safe_edit(
                cq, msg,
                reply_markup=self._kbd([[(self._btn("ui_cancel"), CB["cancel"])]])
            )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["client_cred_reset"] + "$"))
        @self._localized_handler
        async def _cb_client_cred_reset(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if self._credentials:
                try:
                    owned = await self._credentials.list_by_owner(uid)
                    for c in owned:
                        await self._credentials.delete(c.id)
                except Exception:
                    pass
            await cq.answer(self._t("ui_client_credentials_reset_done", lang=self._resolve_lang(uid)), show_alert=True)
            cq.data = CB["client_creds"]
            await _cb_client_creds(_, cq)

        # ---------------- administrator control plane ----------------
        @b.on_callback_query(filters.regex("^" + CB["admin"] + "$"))
        @self._localized_handler
        async def _cb_admin_panel(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_owner_only_alert"), show_alert=True)
            text, kbd = await self._render_admin_panel(uid)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_stats"] + "$"))
        @self._localized_handler
        async def _cb_adm_stats(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_need_admin", lang=self._resolve_lang(uid)), show_alert=True)
            total_users = await self._users.count_all() if self._users else 0
            active_rules = 0
            if self._rules:
                all_r = await self._rules.list_all()
                active_rules = sum(1 for r in all_r if getattr(r, "enabled", True))
            dlq_count = await self._dlq_count()
            lang = self._resolve_lang(uid)
            body = self._t(
                "ui_admin_stats_body",
                lang=lang,
                total_users=total_users,
                active_rules=active_rules,
                dlq_count=dlq_count,
                web_url=self._web_url or self._t("ui_not_configured", lang=lang),
                service_status=self._t("ui_status_on", lang=lang),
            )
            kbd = InlineKeyboardMarkup([
                [InlineKeyboardButton(self._t("ui_back_main_menu", lang=lang), callback_data=CB["admin"])],
            ])
            await self._safe_edit(cq, body, reply_markup=kbd)
            await cq.answer()
        @b.on_callback_query(filters.regex(r"^" + CB["adm_users"] + r"(?:_pg_(\d+))?$"))
        @self._localized_handler
        async def _cb_adm_users(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_need_admin", lang=self._resolve_lang(uid)), show_alert=True)

            page = 0
            m = re.match(r"^" + CB["adm_users"] + r"_pg_(\d+)$", cq.data or "")
            if m:
                page = int(m.group(1))

            limit = 6
            offset = page * limit
            users = await self._users.list_all(limit=limit, offset=offset) if self._users else []
            total = await self._users.count_all() if self._users else 0

            lines = [
                self._t("ui_admin_users_heading", lang=self._resolve_lang(uid), page=page + 1, pages=max(1, (total + limit - 1) // limit)),
            ]
            rows = []
            for u in users:
                user_uid = u.get("user_id")
                status = u.get("status", "active")
                is_adm = self._is_admin(user_uid)
                status_icon = "👑" if is_adm else ("🟢" if status == "active" else "🔴")
                plan = u.get("plan", "free")
                lines.append(self._t("ui_admin_user_row", lang=self._resolve_lang(uid), status_icon=status_icon, uid=user_uid, plan=plan, status=status))

                btn_toggle = (
                    InlineKeyboardButton(self._t("ui_admin_suspend_user", lang=self._resolve_lang(uid), uid=user_uid), callback_data=f"{CB['adm_user_suspend']}_{user_uid}")
                    if status == "active" and not is_adm
                    else (
                        InlineKeyboardButton(self._t("ui_admin_activate_user", lang=self._resolve_lang(uid), uid=user_uid), callback_data=f"{CB['adm_user_activate']}_{user_uid}")
                        if status == "suspended" and not is_adm
                        else None
                    )
                )
                if btn_toggle:
                    rows.append([btn_toggle])

            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(self._t("ui_admin_prev", lang=self._resolve_lang(uid)), callback_data=f"{CB['adm_users']}_pg_{page - 1}"))
            if offset + limit < total:
                nav.append(InlineKeyboardButton(self._t("ui_admin_next", lang=self._resolve_lang(uid)), callback_data=f"{CB['adm_users']}_pg_{page + 1}"))
            if nav:
                rows.append(nav)

            rows.append([InlineKeyboardButton(self._t("ui_back_main_menu"), callback_data=CB["admin"])])
            await self._safe_edit(cq, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^" + CB["adm_user_suspend"] + r"_(\d+)$"))
        @self._localized_handler
        async def _cb_adm_user_suspend(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            target = int(cq.matches[0].group(1))
            if self._is_admin(target):
                return await cq.answer(self._t("ui_admin_suspend_forbidden", lang=self._resolve_lang(uid)), show_alert=True)
            if self._users:
                await self._users.set_status(target, "suspended")
            await cq.answer(self._t("ui_admin_user_suspended", lang=self._resolve_lang(uid), uid=target), show_alert=True)
            cq.data = CB["adm_users"]
            await _cb_adm_users(_, cq)

        @b.on_callback_query(filters.regex(r"^" + CB["adm_user_activate"] + r"_(\d+)$"))
        @self._localized_handler
        async def _cb_adm_user_activate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            target = int(cq.matches[0].group(1))
            if self._users:
                await self._users.set_status(target, "active")
            await cq.answer(self._t("ui_admin_user_activated", lang=self._resolve_lang(uid), uid=target), show_alert=True)
            cq.data = CB["adm_users"]
            await _cb_adm_users(_, cq)

        @b.on_callback_query(filters.regex("^" + CB["adm_bcast"] + "$"))
        @self._localized_handler
        async def _cb_adm_bcast(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            st = self._state(uid)
            st.step = "admin_broadcast_text"
            await self._persist(uid, st)
            msg = self._t("ui_admin_broadcast_prompt", lang=self._resolve_lang(uid))
            await self._safe_edit(cq, msg, reply_markup=self._kbd([[(self._btn("ui_cancel"), CB["cancel"])]]))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_bcast_confirm"] + "$"))
        @self._localized_handler
        async def _cb_adm_bcast_confirm(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            st = self._state(uid)
            text = st.buffer.get("bcast_text", "")
            st.buffer.clear()
            await self._persist(uid, st)

            if not text:
                return await cq.answer(self._t("ui_admin_broadcast_empty", lang=self._resolve_lang(uid)), show_alert=True)

            bcast_id = str(uuid.uuid4())
            now = int(time.time())
            if self._db:
                try:
                    self._db.execute(
                        "INSERT INTO broadcast_queue (id, initiator_user_id, message_text, target, status, "
                        "total_recipients, sent_count, failed_count, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'all', 'running', 0, 0, 0, ?, ?)",
                        (bcast_id, uid, text, now, now),
                    )
                except Exception:
                    pass

            asyncio.create_task(self._run_broadcast_job(bcast_id, uid, text))
            await cq.answer(self._t("ui_admin_broadcast_started", lang=self._resolve_lang(uid)), show_alert=True)
            text_panel, kbd_panel = await self._render_admin_panel(uid)
            await self._safe_edit(cq, text_panel, reply_markup=kbd_panel)

        @b.on_callback_query(filters.regex("^" + CB["adm_bcast_cancel"] + "$"))
        @self._localized_handler
        async def _cb_adm_bcast_cancel(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            st = self._state(uid)
            st.buffer.clear()
            await self._persist(uid, st)
            await cq.answer(self._t("ui_admin_broadcast_cancelled", lang=self._resolve_lang(uid)))
            text_panel, kbd_panel = await self._render_admin_panel(uid)
            await self._safe_edit(cq, text_panel, reply_markup=kbd_panel)

        @b.on_callback_query(filters.regex("^" + CB["adm_forcesub"] + "$"))
        @self._localized_handler
        async def _cb_adm_forcesub(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)

            fs_enabled = False
            fs_channel = ""
            if self._db:
                r1 = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_enabled'")
                if r1 and r1["value"] == "1":
                    fs_enabled = True
                r2 = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_channel'")
                if r2 and r2["value"]:
                    fs_channel = str(r2["value"])

            status_str = self._t("ui_admin_force_active" if fs_enabled else "ui_admin_force_inactive", lang=self._resolve_lang(uid))
            lang = self._resolve_lang(uid)
            status_str = self._t("ui_admin_force_active" if fs_enabled else "ui_admin_force_inactive", lang=lang)
            body = self._t(
                "ui_admin_force_sub_body",
                lang=lang,
                status=status_str,
                channel=fs_channel or self._t("ui_not_configured", lang=lang),
            )
            rows = [
                [
                    InlineKeyboardButton(
                        self._t("ui_admin_force_disable" if fs_enabled else "ui_admin_force_enable", lang=lang),
                        callback_data=CB["adm_forcesub_toggle"],
                    ),
                    InlineKeyboardButton(
                        self._t("ui_admin_force_set_channel", lang=lang),
                        callback_data=CB["adm_forcesub_set"],
                    ),
                ],
                [InlineKeyboardButton(self._t("ui_back_main_menu", lang=lang), callback_data=CB["admin"])],
            ]
            await self._safe_edit(cq, body, reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_forcesub_toggle"] + "$"))
        @self._localized_handler
        async def _cb_adm_forcesub_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            if self._db:
                r = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_enabled'")
                new_val = "0" if (r and r["value"] == "1") else "1"
                now = int(time.time())
                self._db.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('force_sub_enabled', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (new_val, now),
                )
            await cq.answer(self._t("ui_admin_force_updated", lang=self._resolve_lang(uid)))
            cq.data = CB["adm_forcesub"]
            await _cb_adm_forcesub(_, cq)

        @b.on_callback_query(filters.regex("^" + CB["adm_forcesub_set"] + "$"))
        @self._localized_handler
        async def _cb_adm_forcesub_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            st = self._state(uid)
            st.step = "admin_forcesub_channel"
            await self._persist(uid, st)
            msg = self._t("ui_admin_force_prompt", lang=self._resolve_lang(uid))
            await self._safe_edit(cq, msg, reply_markup=self._kbd([[(self._btn("ui_cancel"), CB["cancel"])]]))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_admins"] + "$"))
        @self._localized_handler
        async def _cb_adm_admins(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)

            admins = self._admin_service.list_admins() if self._admin_service else []
            lang = self._resolve_lang(uid)
            lines = [self._t("ui_admin_admins_heading", lang=lang)]
            rows = []
            for admin in admins:
                admin_uid = admin["user_id"]
                role = admin.get("role", "admin")
                role_key = "ui_admin_role_owner" if role == "owner" else "ui_admin_role_admin"
                lines.append(f"• {admin_uid} — {self._t(role_key, lang=lang)}")
                if role != "owner":
                    rows.append([InlineKeyboardButton(
                        self._t("ui_admin_remove_admin", lang=lang, uid=admin_uid),
                        callback_data=f"{CB['adm_admin_del']}_{admin_uid}",
                    )])
            rows.append([InlineKeyboardButton(self._t("ui_admin_add_admin", lang=lang), callback_data=CB["adm_admin_add"])])
            rows.append([InlineKeyboardButton(self._t("ui_back_main_menu", lang=lang), callback_data=CB["admin"])])
            await self._safe_edit(cq, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_admin_add"] + "$"))
        @self._localized_handler
        async def _cb_adm_admin_add(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            st = self._state(uid)
            st.step = "admin_add_id"
            await self._persist(uid, st)
            msg = self._t("ui_admin_add_prompt", lang=self._resolve_lang(uid))
            await self._safe_edit(cq, msg, reply_markup=self._kbd([[(self._btn("ui_cancel"), CB["cancel"])]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^" + CB["adm_admin_del"] + r"_(\d+)$"))
        @self._localized_handler
        async def _cb_adm_admin_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            target = int(cq.matches[0].group(1))
            try:
                if self._admin_service:
                    self._admin_service.remove_admin(target, removed_by=uid)
                await cq.answer(self._t("ui_admin_removed", lang=self._resolve_lang(uid), uid=target), show_alert=True)
            except Exception as exc:
                await cq.answer(self._t("ui_internal_error"), show_alert=True)
            cq.data = CB["adm_admins"]
            await _cb_adm_admins(_, cq)

        @b.on_callback_query(filters.regex("^" + CB["adm_config"] + "$"))
        @self._localized_handler
        async def _cb_adm_config(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)

            lang = self._resolve_lang(uid)
            lines = [self._t(
                "ui_admin_config_body",
                lang=lang,
                web_url=self._web_url or self._t("ui_not_configured", lang=lang),
                master_key_status=self._t("ui_adv_status_configured", lang=lang),
            )]
            rows = [
                [InlineKeyboardButton(self._t("ui_admin_config_web", lang=lang), callback_data=CB["weburl_set"])],
                [InlineKeyboardButton(self._t("ui_admin_config_vault", lang=lang), callback_data=CB["creds"])],
                [InlineKeyboardButton(self._t("ui_back_main_menu", lang=lang), callback_data=CB["admin"])],
            ]
            await self._safe_edit(cq, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["check_sub"] + "$"))
        @self._localized_handler
        async def _cb_check_sub(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            fs_channel = ""
            if self._db:
                r = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_channel'")
                if r and r["value"]:
                    fs_channel = str(r["value"])

            is_member = True
            if fs_channel:
                try:
                    chat_member = await self.bot.get_chat_member(fs_channel, uid)
                    st_str = str(getattr(chat_member, "status", "")).lower()
                    if "left" in st_str or "banned" in st_str:
                        is_member = False
                except Exception as exc:
                    logger.warning("Force-sub verification check error: %s", exc)

            if not is_member:
                return await cq.answer(self._t("ui_not_joined_alert"), show_alert=True)

            await cq.answer(self._t("ui_join_verified_alert"), show_alert=True)
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text, kbd = self._render_welcome_onboarding(uid, user=cq.from_user)
            else:
                text = await self._main_text(uid, user=cq.from_user)
                kbd = self._main_menu(uid)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------- cancel any active flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["cancel"] + "$"))
        @self._localized_handler
        async def _cb_cancel(_, cq: CallbackQuery):
            uid = cq.from_user.id
            st = self._state(uid)
            old_step = str(st.step or "")
            st.step = ""
            st.rule_source = ""
            st.rule_target = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            self._login.cancel(uid)
            if old_step.startswith("rule_"):
                # v12.1: scope to the caller's tenant; admins see everything.
                rules = await self._rules.list_by_owner(uid)
                text, kbd = self._render_rules_list(rules, page=0)
                await self._safe_edit(cq, text, reply_markup=kbd)
            else:
                sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
                if not sessions:
                    lang = await self._get_user_lang(uid)
                    c_text, btn_connect, btn_menu = self._cancelled_connect_texts(lang)
                    kbd = self._kbd([
                        [(btn_connect, CB["login"])],
                        [(btn_menu, CB["main"])],
                    ])
                    await self._safe_edit(cq, c_text, reply_markup=kbd)
                else:
                    await self._safe_edit(cq, self._t("ui_cancelled"), reply_markup=self._main_menu())
            await cq.answer()

        # ---------------- login flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["login_retry"] + "$"))
        @self._localized_handler
        async def _cb_login_retry(_, cq: CallbackQuery):
            # One-tap restart of a login that expired in a restart.
            # Reuses the persisted phone, so the user never retypes it.
            uid = cq.from_user.id
            st = self._state(uid)
            phone = (getattr(st, "login_phone", "") or "").strip()
            if not LoginFlowManager.valid_phone(phone):
                st.step = "login_phone"
                await self._persist(uid, st)
                await cq.answer()
                prompt = await self._connect_prompt_text(uid)
                return await cq.message.edit_text(prompt, reply_markup=self._cancel_kbd())
            result = await self._login.start(uid, LoginFlowManager.normalize_phone(phone))
            await cq.answer()
            if result != "send_code":
                code, _, detail = result.partition(":")
                return await cq.message.edit_text(
                    self._login_fail_text(code, detail, lang=self._lang_for(st)),
                    reply_markup=self._cancel_kbd())
            st.step = "login_code"
            if not isinstance(st.buffer, dict):
                st.buffer = {}
            st.buffer["code_digits"] = ""
            await self._persist(uid, st)
            await cq.message.edit_text(
                self._login_code_text(st), reply_markup=self._code_keypad(st))

        @b.on_callback_query(filters.regex(r"^k:"))
        @self._localized_handler
        async def _cb_keypad_wrap(_, cq: CallbackQuery):
            await self._cb_keypad(cq)

        @b.on_callback_query(filters.regex("^" + CB["login"] + "$"))
        @self._localized_handler
        async def _cb_login(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            logger.info("Executing _cb_login for uid=%s", uid)
            try:
                st = self._state(uid)
                st.step = "login_phone"
                await self._persist(uid, st)
                prompt = await self._connect_prompt_text(uid)
                await self._safe_edit(cq, prompt, reply_markup=self._cancel_kbd())
                await cq.answer()
                logger.info("_cb_login prompt sent successfully for uid=%s; step=login_phone", uid)
            except Exception as exc:
                logger.exception("_cb_login failed for uid=%s: %s", uid, exc)
                try:
                    await cq.answer(self._t("ui_internal_error"), show_alert=True)
                except Exception:
                    pass
                    pass

        # ---------------- api credentials vault ----------------
        @b.on_callback_query(filters.regex("^" + CB["creds"] + "$"))
        @self._localized_handler
        async def _cb_creds(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            lines = [self._t("ui_creds_title"), ""]
            if self._credentials is not None:
                try:
                    creds = await self._credentials.list_all()
                    if not creds:
                        lines.append(self._t("ui_none"))
                    for c in creds:
                        mark = "★" if c.is_default else "·"
                        lines.append(f"{mark} `{c.id[:8]}` {c.label or ''} ({c.api_id})")
                except Exception as exc:
                    await self._record_error("bot", exc, user_id=cq.from_user.id)
                    lines.append("⚠️ " + tg_detail(exc))
            else:
                lines.append(self._t("ui_none"))
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._creds_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["cred_add"] + "$"))
        @self._localized_handler
        async def _cb_cred_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "cred_label"
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, 
                self._t("ui_cred_label"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["cred_default"] + ":"))
        @self._localized_handler
        async def _cb_cred_default(_, cq: CallbackQuery):
            cid = cq.data.split(":", 1)[1]
            if self._credentials is not None:
                try:
                    # v12.1: a forged credential id must not flip another
                    # tenant's default credential.
                    await self._credentials.set_default_owned(
                        cid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
                    try:
                        self._log.info("bot", "cred", f"credential {cid[:8]} set default")
                    except Exception:
                        pass
                except PermissionError:
                    return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
                except Exception as exc:
                    await self._record_error("bot", exc, user_id=cq.from_user.id)
            await cq.answer(self._t("ui_cred_set_default", id=cid[:8]), show_alert=True)

        # ---------------- rule add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["rule_add"] + "$"))
        @self._localized_handler
        async def _cb_rule_add(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions and self._is_admin(uid):
                sessions = await self._sessions.list_all()

            if not sessions:
                await self._safe_edit(cq, 
                    self._t("ui_no_sessions", lang=cur_lang), reply_markup=self._sessions_menu())
                return await cq.answer()

            st = self._state(uid)
            st.step = "rule_source"
            st.rule_source = ""
            st.rule_target = ""
            st.buffer = {}

            try:
                await cq.answer(self._t("ui_loading_dialogs", lang=cur_lang))
            except Exception:
                pass

            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            st.buffer["page"] = 0
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="source", page=0, uid=uid)
            await self._safe_edit(cq, text, reply_markup=markup)

        @b.on_callback_query(filters.regex(r"^ch:(s|t):(\d+)$"))
        @self._localized_handler
        async def _cb_pick_chat(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            st = self._state(uid)
            raw_data = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw_data.split(":")
            role = "source" if parts[1] == "s" else "target"
            idx = int(parts[2])

            chats = st.buffer.get("chats", []) if isinstance(st.buffer, dict) else []
            if not (0 <= idx < len(chats)):
                return await cq.answer(self._t("ui_chat_not_found", lang=cur_lang), show_alert=True)

            selected = chats[idx]
            chat_id = selected["id"]
            chat_title = f"{selected['emoji']} {selected['title']}"

            # Check if this selection is part of an edit flow
            rule_id = st.buffer.get("rule_id") if isinstance(st.buffer, dict) else ""
            in_draft = bool(st.buffer.get("in_draft")) if isinstance(st.buffer, dict) else False
            edit_role = st.buffer.get("edit_role") if isinstance(st.buffer, dict) else (
                "source" if st.step == "rule_edit_source" else ("target" if st.step == "rule_edit_target" else None)
            )

            if rule_id and edit_role:
                rule = await self._owned_rule(rule_id, uid)
                if not rule:
                    return await cq.answer(self._t("ui_rule_not_found", lang=cur_lang), show_alert=True)

                draft_obj = st.buffer.get("draft") if (in_draft and isinstance(st.buffer, dict)) else None
                draft: Dict[str, Any] = draft_obj if isinstance(draft_obj, dict) else rule.to_draft()
                if in_draft and not isinstance(draft_obj, dict) and isinstance(st.buffer, dict):
                    st.buffer["draft"] = draft

                if edit_role == "source":
                    target_id = draft.get("target_chat_id") if in_draft else rule.target_chat_id
                    if str(chat_id).strip() == str(target_id).strip():
                        return await cq.answer(self._t("ui_loop_prevention_src", lang=cur_lang), show_alert=True)

                    if in_draft:
                        draft["source_chat_id"] = str(chat_id)
                        draft["source_name"] = selected["title"]
                        st.step = "rule_editing_draft"
                        await self._persist(uid, st)
                        await cq.answer(self._t("ui_src_selected", lang=cur_lang, title=selected['title']))
                        text, markup = self._render_draft_editor(rule, draft)
                        return await self._safe_edit(cq, text, reply_markup=markup)
                    else:
                        rule.source_chat_id = str(chat_id)
                        rule.source_chat_name = selected["title"]
                        expected_ver = st.buffer.get("version")
                        try:
                            await self._save_owned_rule(rule, uid, expected_ver)
                        except ConcurrencyError:
                            return await cq.answer(self._t("ui_concurrency_error", lang=cur_lang), show_alert=True)
                        st.step = ""
                        st.buffer = {}
                        await self._persist(uid, st)
                        await cq.answer(self._t("ui_src_selected", lang=cur_lang, title=selected['title']))
                        text, markup = self._render_rule_detail(rule)
                        return await self._safe_edit(cq, text, reply_markup=markup)

                elif edit_role == "target":
                    source_id = draft.get("source_chat_id") if in_draft else rule.source_chat_id
                    if str(chat_id).strip() == str(source_id).strip():
                        return await cq.answer(self._t("ui_loop_prevention_tgt", lang=cur_lang), show_alert=True)

                    if in_draft:
                        draft["target_chat_id"] = str(chat_id)
                        draft["target_name"] = selected["title"]
                        st.step = "rule_editing_draft"
                        await self._persist(uid, st)
                        await cq.answer(self._t("ui_tgt_selected", lang=cur_lang, title=selected['title']))
                        text, markup = self._render_draft_editor(rule, draft)
                        return await self._safe_edit(cq, text, reply_markup=markup)
                    else:
                        rule.target_chat_id = str(chat_id)
                        rule.target_chat_name = selected["title"]
                        expected_ver = st.buffer.get("version")
                        try:
                            await self._save_owned_rule(rule, uid, expected_ver)
                        except ConcurrencyError:
                            return await cq.answer(self._t("ui_concurrency_error", lang=cur_lang), show_alert=True)
                        st.step = ""
                        st.buffer = {}
                        await self._persist(uid, st)
                        await cq.answer(self._t("ui_tgt_selected", lang=cur_lang, title=selected['title']))
                        text, markup = self._render_rule_detail(rule)
                        return await self._safe_edit(cq, text, reply_markup=markup)

            if role == "source":
                st.rule_source = chat_id
                st.buffer["source_name"] = chat_title
                if isinstance(st.buffer, dict):
                    # Target-first flow: the catalog already chose the target,
                    # so this source selection completes the staged build.
                    pending = st.buffer.get("pending_target") if isinstance(st.buffer, dict) else None
                    if isinstance(pending, dict) and pending.get("id"):
                        st.rule_target = str(pending.get("id"))
                        target_title = str(pending.get("title") or st.rule_target)
                        st.buffer.pop("pending_target", None)
                        await cq.answer(self._t("ui_creating_rule", lang=cur_lang), show_alert=False)
                        return await self._finish_rule_creation(cq, st, chat_title, target_title)
                st.step = "rule_target"
                st.buffer["page"] = 0
                await self._persist(uid, st)
                await cq.answer(self._t("ui_src_selected", lang=cur_lang, title=selected['title']), show_alert=False)
                text, markup = self._render_chat_picker(st, role="target", page=0, uid=uid)
                await self._safe_edit(cq, text, reply_markup=markup)
            else:
                st.rule_target = chat_id
                target_title = chat_title
                source_title = st.buffer.get("source_name") or st.rule_source
                await cq.answer(self._t("ui_creating_rule", lang=cur_lang), show_alert=False)
                await self._finish_rule_creation(cq, st, source_title, target_title)

        @b.on_callback_query(filters.regex(r"^pg:(s|t):(\d+)$"))
        @self._localized_handler
        async def _cb_page_chat(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            raw_data = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw_data.split(":")
            role = "source" if parts[1] == "s" else "target"
            page = int(parts[2])

            if isinstance(st.buffer, dict):
                st.buffer["page"] = page
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role=role, page=page, uid=uid)
            try:
                await self._safe_edit(cq, text, reply_markup=markup)
            except Exception:
                pass
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfc:(s|t)$"))
        @self._localized_handler
        async def _cb_refresh_chats(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            st = self._state(uid)
            raw_data = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw_data.split(":")
            role = "source" if parts[1] == "s" else "target"

            try:
                await cq.answer(self._t("ui_refreshing_dialogs", lang=cur_lang))
            except Exception:
                pass

            chats = await self._fetch_dialogs(uid)
            if isinstance(st.buffer, dict):
                st.buffer["chats"] = chats
                st.buffer["page"] = 0
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role=role, page=0, uid=uid)
            await self._safe_edit(cq, text, reply_markup=markup)

        @b.on_callback_query(filters.regex(r"^r_cancel$"))
        @self._localized_handler
        async def _cb_rule_cancel(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            st = self._state(uid)
            st.step = ""
            st.rule_source = ""
            st.rule_target = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, kbd = self._render_rules_list(rules, page=0)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_op_cancelled", lang=cur_lang))

        @b.on_callback_query(filters.regex(r"^r_back_source$"))
        @self._localized_handler
        async def _cb_rule_back_source(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            st = self._state(uid)
            st.step = "rule_source"
            st.rule_target = ""
            if isinstance(st.buffer, dict):
                st.buffer["page"] = 0
            await self._persist(uid, st)
            text, markup = self._render_chat_picker(st, role="source", page=0, uid=uid)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer(self._t("ui_back_to_source", lang=cur_lang))

        @b.on_callback_query(filters.regex(r"^noop$"))
        @self._localized_handler
        async def _cb_noop(_, cq: CallbackQuery):
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rdel:(.+)$"))
        @self._localized_handler
        async def _cb_rule_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            lang = self._resolve_lang(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rid = raw.split(":", 1)[1]
            # An owner deletes their own task; an admin acts on the whole
            # install. Anything else is a forged id and must be refused here,
            # before the confirmation screen renders another tenant's name.
            rule = await self._owned_rule(rid, uid)
            if rule is None:
                return await cq.answer(
                    self._t("ui_rule_not_found_or_not_owner", lang=lang), show_alert=True)
            name = rule.source_chat_name or rule.source_chat_id
            confirm_text = (
                self._t("ui_confirm_delete_rule") + "\n\n" + self._t("ui_confirm_delete_rule_body", name=name)
            )
            confirm_kbd = self._kbd([
                [(self._t("ui_confirm_yes_delete"), f"rdelyes:{rid}")],
                [(self._t("ui_cancel_back"), f"rd:{rid}")],
            ])
            await self._safe_edit(cq, confirm_text, reply_markup=confirm_kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rdelyes:(.+)$"))
        @self._localized_handler
        async def _cb_rule_del_confirm(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rid = raw.split(":", 1)[1]
            try:
                ok = await self._rules.delete_owned(rid, self._scope_uid(uid))
            except PermissionError:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            if not ok:
                return await cq.answer(self._t("ui_none"), show_alert=True)
            try:
                self._log.info("bot", "rule", f"rule {rid} deleted")
            except Exception:
                pass
            rules = await (self._rules.list_all() if self._is_admin(uid) else self._rules.list_by_owner(uid))
            text, kbd = self._render_rules_list(rules, page=0)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_rule_deleted"), show_alert=True)

        @b.on_callback_query(filters.regex("^" + CB["rule_toggle"] + ":"))
        @self._localized_handler
        async def _cb_rule_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rid = raw.split(":", 1)[1]
            rule = await self._rules.toggle_owned(rid, self._scope_uid(uid))
            if not rule:
                return await cq.answer(self._t("ui_none"), show_alert=True)
            try:
                self._log.info("bot", "rule", f"rule {rid} toggled")
            except Exception:
                pass
            rules = await (self._rules.list_all() if self._is_admin(uid) else self._rules.list_by_owner(uid))
            lines = [self._t("ui_rules_title"), ""]
            if not rules:
                lines.append(self._t("ui_none"))
            for r in rules:
                status = "🟢" if r.is_active else "🔴"
                target = getattr(r, "target_label", None) or (
                    r.target_chat_name or r.target_chat_id)
                lines.append(
                    f"{status} `{r.id[:8]}` **{r.source_chat_name or r.source_chat_id}** ➔ **{target}**")
                mode = getattr(r.forward_mode, "value", r.forward_mode)
                delay = float(getattr(r, "delay_seconds", 0) or 0)
                lines.append(
                    "     " + self._t("ui_rules_row_meta", lang=self._lang_for(r), mode=mode, delay=f"{delay:.0f}")
                )
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._rules_menu(rules))
            await cq.answer(self._t("ui_done"))

        # ---------------- rule detailed settings & controls ----------------
        @b.on_callback_query(filters.regex(r"^rd:(.+)$"))
        @self._localized_handler
        async def _cb_rule_detail(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            # v12.1: resolve through the ownership guard — a forged callback id
            # for another tenant's rule must not render.
            rule = await self._rules.get_owned(rule_id, None if self._is_admin(uid) else uid)
            if not rule:
                return await cq.answer(self._t("ui_none"), show_alert=True)
            if not (self._is_admin(uid) or rule.owner_user_id == uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtx:(.+)$"))
        @self._localized_handler
        async def _cb_rule_replace_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            text, kbd = self._render_replace_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtxa:(.+)$"))
        @self._localized_handler
        async def _cb_rule_replace_add(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "rule_replace_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = (
                self._t("ui_rep_rule_instructions")
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[(self._t("ui_cancel_back"), f"rtx:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtxc:(.+)$"))
        @self._localized_handler
        async def _cb_rule_replace_clear(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["replacements"] = {}
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_rep_all_cleared_alert"))
            text, kbd = self._render_replace_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtxd:(.+):(\d+):(\d+)$"))
        @self._localized_handler
        async def _cb_rule_replace_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, idx_str, ver_str = parts[1], parts[2], parts[3]
            rule = await self._owned_rule(rule_id, uid)
            if not rule or not rule.replacements:
                return await cq.answer(self._t("ui_rep_none_found_alert"), show_alert=True)
            # The click carries the generation of the list it was rendered
            # from; a menu left open across another edit must never delete
            # whichever entry has meanwhile moved into that position.
            target_key = self._resolve_replace_click(rule, int(idx_str), ver_str)
            if target_key is None:
                await cq.answer(
                    self._t("ui_rep_list_changed_alert", lang=self._lang_for(rule)),
                    show_alert=True,
                )
                text, kbd = self._render_replace_menu(rule)
                return await self._safe_edit(cq, text, reply_markup=kbd)
            reps = dict(rule.replacements)
            reps.pop(target_key, None)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["replacements"] = reps
            await self._save_owned_rule(rule, uid)
            text, kbd = self._render_replace_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtxe:(.+):(\d+):(\d+)$"))
        @self._localized_handler
        async def _cb_rule_replace_entry(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, idx_str, ver_str = parts[1], parts[2], parts[3]
            rule = await self._owned_rule(rule_id, uid)
            if not rule or not rule.replacements:
                return await cq.answer(self._t("ui_rep_none_found_alert"), show_alert=True)
            key = self._resolve_replace_click(rule, int(idx_str), ver_str)
            if key is None:
                await cq.answer(
                    self._t("ui_rep_list_changed_alert", lang=self._lang_for(rule)),
                    show_alert=True,
                )
                text, kbd = self._render_replace_menu(rule)
                return await self._safe_edit(cq, text, reply_markup=kbd)
            text, kbd = self._render_replace_entry(rule, key)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        async def _replace_toggle(cq: CallbackQuery, option: str) -> None:
            uid = cq.from_user.id if cq.from_user else 0
            lang = self._resolve_lang(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, idx_str, ver_str = parts[1], parts[2], parts[3]
            rule = await self._owned_rule(rule_id, uid)
            if not rule or not rule.replacements:
                return await cq.answer(self._t("ui_rep_none_found_alert", lang=lang), show_alert=True)
            key = self._resolve_replace_click(rule, int(idx_str), ver_str)
            if key is None:
                await cq.answer(self._t("ui_rep_list_changed_alert", lang=lang), show_alert=True)
                text, kbd = self._render_replace_menu(rule)
                return await self._safe_edit(cq, text, reply_markup=kbd)
            replacements = dict(rule.replacements)
            try:
                updated = self._replace_option_entry(replacements, key, option)
            except ReplaceError:
                # Switching a literal to regex can turn a valid source into an
                # invalid pattern ("(" is a word literally, not a regex): the
                # refusal is explained instead of storing something that would
                # be skipped at delivery time.
                return await cq.answer(self._t("ui_rep_err_invalid_regex", lang=lang), show_alert=True)
            replacements[key] = updated.to_value()
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["replacements"] = replacements
            await self._save_owned_rule(rule, uid)
            text, kbd = self._render_replace_entry(rule, key)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtxem:(.+):(\d+):(\d+)$"))
        @self._localized_handler
        async def _cb_rule_replace_mode(_, cq: CallbackQuery):
            await _replace_toggle(cq, "mode")

        @b.on_callback_query(filters.regex(r"^rtxef:(.+):(\d+):(\d+):([icw])$"))
        @self._localized_handler
        async def _cb_rule_replace_flag(_, cq: CallbackQuery):
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            await _replace_toggle(cq, raw.rsplit(":", 1)[-1])

        @b.on_callback_query(filters.regex(r"^rhf:(.+)$"))
        @self._localized_handler
        async def _cb_rule_header_footer_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            text, kbd = self._render_header_footer_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rh_s:(.+)$"))
        @self._localized_handler
        async def _cb_rule_header_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            st = self._state(uid)
            st.step = "rule_header_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            lang = self._resolve_lang(uid)
            prompt = self._t("ui_header_prompt", lang=lang)
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[(self._t("ui_cancel", lang=lang), f"rhf:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rh_d:(.+)$"))
        @self._localized_handler
        async def _cb_rule_header_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.header = ""
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_header_removed", lang=self._resolve_lang(uid)))
            text, kbd = self._render_header_footer_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rf_s:(.+)$"))
        @self._localized_handler
        async def _cb_rule_footer_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(uid)), show_alert=True)
            st = self._state(uid)
            st.step = "rule_footer_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            lang = self._resolve_lang(uid)
            prompt = self._t("ui_footer_prompt", lang=lang)
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[(self._t("ui_cancel", lang=lang), f"rhf:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rf_d:(.+)$"))
        @self._localized_handler
        async def _cb_rule_footer_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.footer = ""
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_footer_removed", lang=self._resolve_lang(uid)))
            text, kbd = self._render_header_footer_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------- Cleaner Callbacks ----------------
        @b.on_callback_query(filters.regex(r"^rcl:(.+)$"))
        @self._localized_handler
        async def _cb_rule_cleaner_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rcl_url:(.+)$"))
        @self._localized_handler
        async def _cb_clean_url_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.remove_links = not bool(rule.remove_links)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_urls_on") if rule.remove_links else self._t("ui_clean_urls_off")
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_usr:(.+)$"))
        @self._localized_handler
        async def _cb_clean_usr_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.clean_usernames = not bool(rule.clean_usernames)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_mentions_on") if rule.clean_usernames else self._t("ui_clean_mentions_off")
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_tag:(.+)$"))
        @self._localized_handler
        async def _cb_clean_tag_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.clean_hashtags = not bool(rule.clean_hashtags)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_hashtags_on") if rule.clean_hashtags else self._t("ui_clean_hashtags_off")
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_emo:(.+)$"))
        @self._localized_handler
        async def _cb_clean_emo_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.remove_emojis = not bool(rule.remove_emojis)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_emojis_on") if rule.remove_emojis else self._t("ui_clean_emojis_off")
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_phn:(.+)$"))
        @self._localized_handler
        async def _cb_clean_phn_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.clean_phones = not bool(rule.clean_phones)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_phones_on") if rule.clean_phones else self._t("ui_clean_phones_off")
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_btn:(.+)$"))
        @self._localized_handler
        async def _cb_clean_btn_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.clean_buttons = not bool(rule.clean_buttons)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_buttons_on") if rule.clean_buttons else self._t("ui_clean_buttons_off")
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_spc:(.+)$"))
        @self._localized_handler
        async def _cb_clean_spc_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.clean_empty_lines = not bool(rule.clean_empty_lines)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_empty_lines_on") if rule.clean_empty_lines else self._t("ui_clean_empty_lines_off")
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_wrd:(.+)$"))
        @self._localized_handler
        async def _cb_clean_wrd_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "rule_clean_words_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = self._t("ui_custom_words_prompt")
            await self._safe_edit(
                cq,
                prompt,
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rcl:{rule_id}")]]),
            )
            await cq.answer()

        # ---------------- Filters Callbacks ----------------
        @b.on_callback_query(filters.regex(r"^rfl:(.+)$"))
        @self._localized_handler
        async def _cb_rule_filters_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_lnk:(.+)$"))
        @self._localized_handler
        async def _cb_filter_link_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.block_links = not bool(rule.block_links)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_filter_link_on") if rule.block_links else self._t("ui_filter_link_off")
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_vid:(.+)$"))
        @self._localized_handler
        async def _cb_filter_video_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.block_video = not bool(rule.block_video)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_block_video_on") if rule.block_video else self._t("ui_block_video_off")
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_aud:(.+)$"))
        @self._localized_handler
        async def _cb_filter_audio_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.block_audio = not bool(rule.block_audio)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_block_audio_on") if rule.block_audio else self._t("ui_block_audio_off")
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_stk:(.+)$"))
        @self._localized_handler
        async def _cb_filter_sticker_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.block_stickers = not bool(rule.block_stickers)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_block_stickers_on") if rule.block_stickers else self._t("ui_block_stickers_off")
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_txt:(.+)$"))
        @self._localized_handler
        async def _cb_filter_text_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.block_text = not bool(rule.block_text)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_block_text_on") if rule.block_text else self._t("ui_block_text_off")
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_voc:(.+)$"))
        @self._localized_handler
        async def _cb_filter_voice_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.block_voice = not bool(rule.block_voice)
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_block_voice_on") if rule.block_voice else self._t("ui_block_voice_off")
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------- Max Time Edit Callbacks ----------------
        @b.on_callback_query(filters.regex(r"^rmte:(.+)$"))
        @self._localized_handler
        async def _cb_rule_max_time_edit_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            text, kbd = self._render_max_time_edit_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rmte_s:(.+?):(\d+)$"))
        @self._localized_handler
        async def _cb_rule_max_time_edit_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, secs = parts[1], int(parts[2])
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.max_time_edit = secs
            await self._save_owned_rule(rule, uid)
            msg = self._t("ui_mte_secs_changed", secs=secs) if secs > 0 else self._t("ui_mte_unlimited")
            await cq.answer(msg)
            text, kbd = self._render_max_time_edit_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rmte_c:(.+)$"))
        @self._localized_handler
        async def _cb_rule_max_time_edit_custom(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "rule_max_edit_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = self._t("ui_rule_max_edit_prompt")
            await self._safe_edit(
                cq,
                prompt,
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rmte:{rule_id}")]]),
            )
            await cq.answer()

        # ---------------- Profile Management Callbacks ----------------
        @b.on_callback_query(filters.regex(r"^pr_restart$"))
        @self._localized_handler
        async def _cb_profile_restart_setup(_, cq: CallbackQuery):
            # Restart *setup* — not the account. The UI state is the only thing
            # reset: rules, sessions and every per-task setting survive, so the
            # owner walks the onboarding again with their config intact.
            uid = cq.from_user.id if cq.from_user else 0
            st = UiState()
            await self._persist(uid, st)
            rules = await self._rules.list_by_owner(uid) if uid else []
            logger.info("[PROFILE:RESTART_SETUP] uid=%s re-entered setup (%d rule(s) kept)", uid, len(rules))
            await cq.answer(self._t("ui_setup_reset_done"))
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text, kbd = self._render_welcome_onboarding(uid, user=cq.from_user)
            else:
                text = await self._main_text(uid, user=cq.from_user)
                kbd = self._main_menu()
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pr_reset_cfg$"))
        @self._localized_handler
        async def _cb_profile_reset_cfg_prompt(_, cq: CallbackQuery):
            text = (
                self._t("ui_reset_config_confirmation")
            )
            kbd = self._kbd([
                [(self._t("ui_reset_config_confirm_button"), "pr_reset_cfg_yes")],
                [(self._t("ui_cancel_back"), "pr_view")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^pr_reset_cfg_yes$"))
        @self._localized_handler
        async def _cb_profile_reset_cfg_confirm(_, cq: CallbackQuery):
            # Reset exactly what the confirmation screen names — headers,
            # footers, cleaner, filters and blocked words — through one helper
            # (`domain.profile_recovery`) so the promise on screen, the code and
            # the tests cannot drift apart. Sessions, rule ids, routing and
            # `is_active` are never touched, so a misclick cannot delete a
            # working task or log a session out.
            uid = cq.from_user.id if cq.from_user else 0
            rules = await self._rules.list_by_owner(uid) if uid else []
            touched = 0
            for r in rules:
                changed = reset_rule_settings(r)
                if changed:
                    await self._save_owned_rule(r, uid)
                    touched += 1
            logger.info("[PROFILE:RESET_CONFIG] uid=%s reset settings on %d/%d rule(s)", uid, touched, len(rules))
            await cq.answer(self._t("ui_all_configs_reset_alert"), show_alert=True)
            body, kbd = await self._render_profile_menu(uid, cq.from_user)
            await self._safe_edit(cq, body, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pr_view$"))
        @self._localized_handler
        async def _cb_profile_view(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            body, kbd = await self._render_profile_menu(uid, cq.from_user)
            await self._safe_edit(cq, body, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rlk:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_link(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.remove_links = not rule.remove_links
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["remove_links"] = rule.remove_links
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_urls_on") if rule.remove_links else self._t("ui_clean_urls_off")
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rvc:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_voice(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            new_val = not rule.block_voice
            rule.metadata["block_voice"] = new_val
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_block_voice_on") if new_val else self._t("ui_block_voice_off")
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rst:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_sticker(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            new_val = not rule.block_stickers
            rule.metadata["block_stickers"] = new_val
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_block_stickers_on") if new_val else self._t("ui_block_stickers_off")
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rem:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_emoji(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            new_val = not rule.remove_emojis
            rule.metadata["remove_emojis"] = new_val
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_emoji_on") if new_val else self._t("ui_clean_emoji_off")
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^red:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_edit(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.ignore_edits = not rule.ignore_edits
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_ignore_edits_on") if rule.ignore_edits else self._t("ui_clean_ignore_edits_off")
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rdl:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_delete(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            new_val = not rule.sync_deletes
            rule.sync_deletes = new_val
            await self._save_owned_rule(rule, uid)
            status = self._t("ui_clean_sync_deletes_on") if new_val else self._t("ui_clean_sync_deletes_off")
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rmg:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_media_group(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            cur = rule.album_mode
            cycle = {"album": "first", "first": "split", "split": "album"}
            next_mode = cycle.get(cur, "album")
            rule.metadata["album_mode"] = next_mode
            await self._save_owned_rule(rule, uid)
            names = {"album": self._t("ui_media_album_label"), "first": self._t("ui_media_first_label"), "split": self._t("ui_media_split_label")}
            await cq.answer(self._t("ui_media_group_alert", mode=names.get(next_mode, next_mode)))
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rmd:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_mode(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cur = rule.forward_mode
            rule.forward_mode = (
                ForwardMode.DIRECT_FORWARD
                if cur == ForwardMode.COPY_MESSAGE
                else ForwardMode.COPY_MESSAGE
            )
            await self._save_owned_rule(rule, uid)
            mode_names = {
                ForwardMode.COPY_MESSAGE: self._t("ui_fmode_copy_msg"),
                ForwardMode.DIRECT_FORWARD: self._t("ui_fmode_direct_fwd"),
            }
            await cq.answer(mode_names.get(rule.forward_mode, rule.forward_mode.value))
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rap_check:(.+)$"))
        @self._localized_handler
        async def _cb_rule_auto_pause_check(_, cq: CallbackQuery):
            """Recovery path for an engine pause: verify the chat, then resume.

            Only a successful Telegram lookup clears the pause — pressing the
            button is not proof the chat exists, and resuming on a guess would
            just fail (and notify) again on the next message.
            """
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner_alert"), show_alert=True)
            ap = rule.auto_pause
            if not ap:
                return await cq.answer(self._t("ui_rule_status_alert", status=self._status_label(rule)))
            chat_id = ap.get("chat_id") or rule.source_chat_id
            client = self._pool.get(rule.session_id) if self._pool is not None else None
            if client is None:
                return await cq.answer(
                    self._t("ui_rule_paused_check_failed", code="NO_CLIENT"), show_alert=True
                )
            try:
                await client.get_chat(chat_id)
            except Exception as exc:
                code = tg_detail(exc)[:120] or type(exc).__name__
                logger.info(
                    "[PIPELINE:AUTO_PAUSE] owner %s re-checked %s on rule %s: still failing (%s)",
                    uid, chat_id, rule.id[:8], code,
                )
                return await cq.answer(
                    self._t("ui_rule_paused_check_failed", code=code,
                            lang=self._lang_for(rule)),
                    show_alert=True,
                )
            rule.clear_auto_pause()
            await self._save_owned_rule(rule, uid)
            logger.info(
                "[PIPELINE:AUTO_PAUSE] rule %s resumed by owner %s after %s resolved",
                rule.id[:8], uid, chat_id,
            )
            await cq.answer(self._t("ui_task_paused_resume_ok", lang=self._lang_for(rule)))
            fresh = await self._owned_rule(rule_id, uid) or rule
            text, kbd = self._render_rule_detail(fresh)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_active(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            parts = raw.split(":")
            rule_id = parts[1]
            # The button carries the state it was rendered from, so the write can
            # be conditional on it: a stale screen (the task was paused from the
            # dashboard, or changed on another device) is refused and re-rendered
            # instead of being silently inverted. Buttons from before this change
            # carry no state — those fall back to the stored state, which keeps
            # the old "flip it" meaning while still being a single atomic UPDATE.
            expected = parts[2] if len(parts) > 2 and parts[2] in TASK_STATUSES else None
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner_alert"), show_alert=True)
            observed = expected or rule.status_state()
            outcome, fresh = await self._rules.set_status(
                rule_id, observed, self._scope_uid(uid)
            )
            if outcome != self._rules.STATUS_OK or fresh is None:
                if outcome == self._rules.STATUS_STALE and fresh is not None:
                    await cq.answer(
                        self._t(
                            "ui_rule_status_stale_alert",
                            status=self._status_label(fresh, lang=self._lang_for(fresh)),
                        ),
                        show_alert=True,
                    )
                else:
                    await cq.answer(self._t("ui_rule_not_found_or_not_owner_alert"), show_alert=True)
                if fresh is not None:
                    text, kbd = self._render_rule_detail(fresh)
                    await self._safe_edit(cq, text, reply_markup=kbd)
                return
            # Resuming is the owner's override: drop the stored engine pause
            # (its reason is stale the moment they say the chat is fixed), or
            # the task would stay "auto-paused" forever.
            if fresh.is_active and not fresh.is_currently_paused() and fresh.auto_pause:
                fresh.clear_auto_pause()
                await self._save_owned_rule(fresh, uid)
            status = self._status_label(fresh, lang=self._lang_for(fresh))
            await cq.answer(self._t("ui_rule_status_alert", status=status))
            text, kbd = self._render_rule_detail(fresh)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rrm:(.+)$"))
        @self._localized_handler
        async def _cb_rule_remove_ask(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            text = (
                self._t("ui_rule_delete_confirm_full") + "\n\n"
                + f"📡 {self._t('ui_rule_source')}: `{rule.source_chat_name or rule.source_chat_id}`\n"
                + f"🎯 {self._t('ui_rule_target')}: `{rule.target_chat_name or rule.target_chat_id}`\n\n"
                + self._t("ui_rule_delete_confirm_note")
            )
            kbd = self._kbd([
                [(self._t("ui_confirm_yes_delete"), f"rrm_confirm:{rule.id}")],
                [(self._t("ui_cancel_back"), f"rd:{rule.id}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rrm_confirm:(.+)$"))
        @self._localized_handler
        async def _cb_rule_remove_confirm(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            await self._rules.delete(rule_id)
            await cq.answer(self._t("ui_rule_deleted_alert"), show_alert=True)
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, kbd = self._render_rules_list(rules, page=0)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rdup:(.+)$"))
        @self._localized_handler
        async def _cb_rule_duplicate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            draft = rule.to_draft()
            new_rule = ForwardRule(
                session_id=rule.session_id,
                name=(
                    self._t("ui_rule_copy_of", lang=self._lang_for(rule), name=rule.name)
                    if rule.name else self._t("ui_rule_new_copy", lang=self._lang_for(rule))
                ),
                source_chat_id=rule.source_chat_id,
                source_chat_name=rule.source_chat_name,
                target_chat_id=rule.target_chat_id,
                target_chat_name=rule.target_chat_name,
                owner_user_id=uid,
                metadata=copy.deepcopy(rule.metadata) if isinstance(rule.metadata, dict) else {},
            )
            draft["name"] = new_rule.name
            new_rule.apply_draft(draft)
            new_rule.owner_user_id = uid
            # A duplicate is a NEW row: it must go through `create`, not the
            # owner-scoped update path. `_save_owned_rule` resolves ownership of
            # an existing row, so it refuses a copy whose id does not exist yet —
            # which is exactly what this handler builds, so every duplicate
            # answered "not_owner" and quietly did nothing.
            saved = await self._rules.create(new_rule)
            await cq.answer(self._t("ui_rule_duplicated_alert"), show_alert=True)
            text, kbd = self._render_rule_detail(saved)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^radv:(.+)$"))
        @self._localized_handler
        async def _cb_rule_advanced_settings(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^radon_all:(.+)$"))
        @self._localized_handler
        async def _cb_rule_all_addons(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            text, kbd = self._render_all_addons(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rgrp:(.+?):(.+)$"))
        @self._localized_handler
        async def _cb_rule_group_switch(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, grp = parts[1], parts[2]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.feature_group = grp
            await self._save_owned_rule(rule, uid)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_rule_category_alert", grp=grp))

        @b.on_callback_query(filters.regex(r"^(?:radon|radon_tg):(.+?):(.+)$"))
        @self._localized_handler
        async def _cb_rule_addon_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            is_modal = raw.startswith("radon_tg:")
            parts = raw.split(":")
            rule_id, addon_key = parts[1], parts[2]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            if addon_key == "every_n":
                # Not a boolean: this row opens the interval screen, which owns
                # the number. Toggling it would have set a flag the engine no
                # longer reads, leaving the panel saying "On" at N = 1.
                text, kbd = self._render_every_n_menu(rule)
                await self._safe_edit(cq, text, reply_markup=kbd)
                return await cq.answer()
            addons = rule.paid_addons
            curr = bool(addons.get(addon_key, False))
            addons[addon_key] = not curr
            rule.paid_addons = addons
            await self._save_owned_rule(rule, uid)
            st_text = self._t("ui_rule_status_on") if not curr else self._t("ui_rule_status_off_white")
            await cq.answer(self._t("ui_toggle_addon", addon=addon_key, state=st_text))
            if is_modal:
                text, kbd = self._render_all_addons(rule)
            else:
                text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtgbg:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_debug(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.debug_mode = not rule.debug_mode
            await self._save_owned_rule(rule, uid)
            st = self._t("ui_rule_status_on") if rule.debug_mode else self._t("ui_rule_status_off_white")
            await cq.answer(self._t("ui_rule_debug_alert", st=st))
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_fwdhdr:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_fwd_header(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.show_forward_header = not rule.show_forward_header
            await self._save_owned_rule(rule, uid)
            st = self._t("ui_rule_status_on") if rule.show_forward_header else self._t("ui_rule_status_off_white")
            await cq.answer(self._t("ui_rule_fwd_header_alert", st=st))
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_repsync:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_reply_sync(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.sync_replies = not rule.sync_replies
            await self._save_owned_rule(rule, uid)
            st = self._t("ui_rule_status_on") if rule.sync_replies else self._t("ui_rule_status_off_white")
            await cq.answer(self._t("ui_rule_sync_replies_alert", st=st))
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_dup:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_duplicate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.duplicate_filters = not rule.duplicate_filters
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_duplicate_filters", rule.duplicate_filters))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # -- bot route: the sender identity of a task ------------------------ #
        @b.on_callback_query(filters.regex(r"^rdt:(.+)$"))
        @self._localized_handler
        async def _cb_rule_route_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            lang = self._lang_for(rule)
            if rule.delivery_transport == "BOT":
                rule.delivery_transport = "USER"
            else:
                # Fail closed: a task must not be pointed at a bot route whose
                # token is gone — that would stop it forwarding altogether.
                if not await self._active_bot_token(rule.bot_token_id, uid):
                    return await cq.answer(self._t("ui_route_bot_need_token", lang=lang), show_alert=True)
                rule.delivery_transport = "BOT"
            await self._save_owned_rule(rule, uid)
            logger.info(
                "[BOT ROUTE] rule=%s now delivers via %s (uid=%s)",
                str(rule.id)[:8], rule.delivery_transport, uid,
            )
            state = self._t(
                "ui_route_transport_bot" if rule.delivery_transport == "BOT" else "ui_route_transport_account",
                lang=lang,
            )
            await cq.answer(self._t("ui_rule_btn_route", lang=lang, state=state))
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ------------------------------------------------------------------ #
        # The whole settings registry, from the bot.
        #
        # Callback data stays short (Telegram caps it at 64 bytes): "rge:<verb>"
        # plus the rule id, and an *index* into the list the user is looking at.
        # The index is re-validated against the FSM on every tap, so a stale
        # button from a previous screen is refused instead of applied to
        # whatever now sits in that position.
        # ------------------------------------------------------------------ #
        @b.on_callback_query(filters.regex(r"^rge:g:(.+)$"))
        @self._localized_handler
        async def _cb_reg_groups(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            rule = await self._owned_rule(self._cb_rule_id(cq), uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            editor = self._registry_editor(rule, uid)
            text, rows = editor.groups(rule, st, await editor.sources_for(rule))
            await self._registry_show(cq, rule, uid, text, rows)

        @b.on_callback_query(filters.regex(r"^rge:G:([^:]+):([^:]+):(\d+)$"))
        @self._localized_handler
        async def _cb_reg_group(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = str(cq.data or "")
            _, _, rule_id, group, page = raw.split(":", 4)
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            editor = self._registry_editor(rule, uid)
            text, rows = editor.group(rule, st, group, int(page), await editor.sources_for(rule))
            await self._registry_show(cq, rule, uid, text, rows)

        @b.on_callback_query(filters.regex(r"^rge:k:([^:]+):(\d+)$"))
        @self._localized_handler
        async def _cb_reg_key(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = str(cq.data or "")
            _, _, rule_id, idx = raw.split(":", 3)
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            editor = self._registry_editor(rule, uid)
            text, rows = editor.detail(rule, st, int(idx), await editor.sources_for(rule))
            await self._registry_show(cq, rule, uid, text, rows)

        @b.on_callback_query(filters.regex(r"^rge:t:([^:]+):(\d+)$"))
        @self._localized_handler
        async def _cb_reg_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = str(cq.data or "")
            _, _, rule_id, idx = raw.split(":", 3)
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            editor = self._registry_editor(rule, uid)
            notice = await editor.toggle(rule, st, int(idx))
            text, rows = editor.detail(rule, st, int(idx), await editor.sources_for(rule))
            await self._registry_show(cq, rule, uid, text, rows)
            await cq.answer(notice, show_alert=True)

        @b.on_callback_query(filters.regex(r"^rge:c:([^:]+):(\d+)$"))
        @self._localized_handler
        async def _cb_reg_choices(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = str(cq.data or "")
            _, _, rule_id, idx = raw.split(":", 3)
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            editor = self._registry_editor(rule, uid)
            text, rows = editor.choices(rule, st, int(idx), await editor.sources_for(rule))
            await self._registry_show(cq, rule, uid, text, rows)

        @b.on_callback_query(filters.regex(r"^rge:v:([^:]+):(\d+):(\d+)$"))
        @self._localized_handler
        async def _cb_reg_choose(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = str(cq.data or "")
            _, _, rule_id, idx, pos = raw.split(":", 4)
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            editor = self._registry_editor(rule, uid)
            notice = await editor.set_choice(rule, st, int(idx), int(pos))
            text, rows = editor.detail(rule, st, int(idx), await editor.sources_for(rule))
            await self._registry_show(cq, rule, uid, text, rows)
            await cq.answer(notice, show_alert=True)

        @b.on_callback_query(filters.regex(r"^rge:r:([^:]+):(\d+)$"))
        @self._localized_handler
        async def _cb_reg_reset(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = str(cq.data or "")
            _, _, rule_id, idx = raw.split(":", 3)
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            editor = self._registry_editor(rule, uid)
            notice = await editor.reset(rule, st, int(idx))
            text, rows = editor.detail(rule, st, int(idx), await editor.sources_for(rule))
            await self._registry_show(cq, rule, uid, text, rows)
            await cq.answer(notice, show_alert=True)

        @b.on_callback_query(filters.regex(r"^rge:e:([^:]+):(\d+)$"))
        @self._localized_handler
        async def _cb_reg_edit(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = str(cq.data or "")
            _, _, rule_id, idx = raw.split(":", 3)
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            editor = self._registry_editor(rule, uid)
            key = editor.key_at(st, int(idx))
            if key is None:
                return await cq.answer(self._t("ui_reg_stale"), show_alert=True)
            spec = editor.spec_for(key)
            st.buffer["reg_key_idx"] = int(idx)
            st.buffer["reg_key"] = key
            st.step = f"rge_edit_{rule_id}"
            await self._persist(uid, st)
            hint = self._t("ui_reg_list_hint") if spec.kind == "list" else ""
            await self._safe_edit(
                cq,
                self._t("ui_reg_prompt_value", label=editor.label_for(key), kind=spec.kind,
                        help=(editor.help_for(key) or hint)),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rge:s:(.+)$"))
        @self._localized_handler
        async def _cb_reg_search(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            rule = await self._owned_rule(self._cb_rule_id(cq), uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = f"rge_search_{rule.id}"
            await self._persist(uid, st)
            await self._safe_edit(cq, self._t("ui_reg_prompt_search"))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rge:q:([^:]+):(\d+)$"))
        @self._localized_handler
        async def _cb_reg_search_page(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = str(cq.data or "")
            _, _, rule_id, page = raw.split(":", 3)
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            editor = self._registry_editor(rule, uid)
            text, rows = editor.search(
                rule, st, str(st.buffer.get("reg_query") or ""), int(page)
            )
            await self._registry_show(cq, rule, uid, text, rows)

        @b.on_callback_query(filters.regex(r"^rbt:(.+)$"))
        @self._localized_handler
        async def _cb_rule_bot_token_screen(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            text, kbd = await self._render_bot_route_screen(rule, st)
            await self._persist(uid, st)
            await cq.answer()
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rbtb:(.+)$"))
        @self._localized_handler
        async def _cb_rule_bot_token_back(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            await cq.answer()
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rbta:(.+)$"))
        @self._localized_handler
        async def _cb_rule_bot_token_attach(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            lang = self._lang_for(rule)
            st = self._state(uid)
            st.step = f"bot_route_token_{rule.id}"
            await self._persist(uid, st)
            await cq.answer()
            await self._safe_edit(
                cq,
                self._t("ui_route_token_prompt", lang=lang),
                reply_markup=self._kbd([[(self._btn('ui_rule_back_rules'), f"rbt:{rule.id}")]]),
            )

        @b.on_callback_query(filters.regex(r"^rbts:(.+):(\d+)$"))
        @self._localized_handler
        async def _cb_rule_bot_token_use(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, idx = parts[1], int(parts[2])
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            lang = self._lang_for(rule)
            st = self._state(uid)
            token_id = self._route_token_choice(st, rule.id, idx)
            record = await self._active_bot_token(token_id, uid) if token_id else None
            if record is None:
                # The screen the user tapped is stale (a token was removed, or
                # another device moved on). Re-render instead of attaching a
                # token they did not pick.
                await cq.answer(self._t("ui_route_token_rejected", lang=lang), show_alert=True)
                text, kbd = await self._render_bot_route_screen(rule, st)
                await self._persist(uid, st)
                return await self._safe_edit(cq, text, reply_markup=kbd)
            rule.bot_token_id = record.id
            rule.bot_token_label = record.label
            rule.delivery_transport = "BOT"
            await self._save_owned_rule(rule, uid)
            logger.info(
                "[BOT ROUTE] rule=%s attached token %s (uid=%s)",
                str(rule.id)[:8], str(record.id)[:8], uid,
            )
            await cq.answer(self._t("ui_route_token_attached", lang=lang, bot=record.label))
            text, kbd = await self._render_bot_route_screen(rule, st)
            await self._persist(uid, st)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rbtd:(.+):(\d+)$"))
        @self._localized_handler
        async def _cb_rule_bot_token_remove(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, idx = parts[1], int(parts[2])
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            lang = self._lang_for(rule)
            st = self._state(uid)
            token_id = self._route_token_choice(st, rule.id, idx)
            # Ownership is checked before the in-use scan: a foreign id must
            # not even be probed for use, let alone deleted.
            if not token_id or await self._active_bot_token(token_id, uid) is None:
                await cq.answer(self._t("ui_route_token_rejected", lang=lang), show_alert=True)
                text, kbd = await self._render_bot_route_screen(rule, st)
                await self._persist(uid, st)
                return await self._safe_edit(cq, text, reply_markup=kbd)
            # Deleting a token a task still routes through would silently break
            # that task, so the tasks have to be moved off it first.
            in_use = await self._rules_using_bot_token(uid, token_id)
            if in_use:
                return await cq.answer(
                    self._t("ui_route_token_in_use", lang=lang, count=len(in_use)),
                    show_alert=True,
                )
            removed = False
            if self._bot_tokens is not None:
                try:
                    removed = bool(await self._bot_tokens.delete_owned(token_id, uid))
                except Exception as exc:
                    logger.warning("[BOT ROUTE] deleting token %s failed: %s", token_id[:8], exc)
            if self._bot_route is not None:
                # Drop the pooled client so nothing keeps posting with a token
                # the owner just revoked.
                self._bot_route.forget(token_id)
            logger.info("[BOT ROUTE] token %s deleted by uid=%s (removed=%s)", token_id[:8], uid, removed)
            await cq.answer(self._t("ui_route_token_remove", lang=lang))
            text, kbd = await self._render_bot_route_screen(rule, st)
            await self._persist(uid, st)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_crypto:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_crypto(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.crypto_mode = not rule.crypto_mode
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_crypto_mode", rule.crypto_mode))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_raw:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_raw_text(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.process_raw_text = not rule.process_raw_text
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_process_raw_text", rule.process_raw_text))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_btn2txt:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_btn2txt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.button_to_text = not rule.button_to_text
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_button_to_text", rule.button_to_text))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_ocr:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_ocr(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.ocr_image_ai = not rule.ocr_image_ai
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_ocr_image_ai", rule.ocr_image_ai))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_ai:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_ai_rewrite(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.smart_ai_rewrite = not rule.smart_ai_rewrite
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_smart_ai_rewrite", rule.smart_ai_rewrite))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_digest:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_news_digest(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.news_digest = not rule.news_digest
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_news_digest", rule.news_digest))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # --- Delay Forward Handlers ---
        @b.on_callback_query(filters.regex(r"^rdly:(.+)$"))
        @self._localized_handler
        async def _cb_rule_delay_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            text, kbd = self._render_delay_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rdly_s:(.+?):(.+)$"))
        @self._localized_handler
        async def _cb_rule_delay_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, sec_str = parts[1], parts[2]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            # The callback payload is client-controlled: `rdly_s:<id>:nan` used to
            # reach SQLite as NULL (NOT NULL violation), `inf` reached the job
            # payload as invalid JSON, and a negative value left the screen showing
            # a delay the queue ignored. Clamp to what the engine can honour.
            rule.delay_seconds = clamp_delay_seconds(sec_str)
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_delay_status_alert", delay=f"{rule.delay_seconds:.0f}"))
            text, kbd = self._render_delay_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rdly_c:(.+)$"))
        @self._localized_handler
        async def _cb_rule_delay_custom(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = f"set_delay_{rule.id}"
            await self._persist(uid, st)
            prompt = (
                self._t("ui_delay_menu_title") + "\n\n" + self._t("ui_prompt_enter_delay")
            )
            kbd = self._kbd([[(self._t("ui_cancel_back"), f"rdly:{rule.id}")]])
            await self._safe_edit(cq, prompt, reply_markup=kbd)
            await cq.answer()

        # -- forward every Nth message -------------------------------------- #
        @b.on_callback_query(filters.regex(r"^ren:(.+?):(\d+)$"))
        @self._localized_handler
        async def _cb_rule_every_n_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, n_str = parts[1], parts[2]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            # Client-controlled payload: the interval must be a whole number in
            # the registry's own range, whatever a crafted callback says.
            try:
                every_n = int(n_str)
            except (TypeError, ValueError):
                return await cq.answer(self._t("ui_step_every_n_invalid_error"), show_alert=True)
            if not 1 <= every_n <= 1000:
                return await cq.answer(self._t("ui_step_every_n_invalid_error"), show_alert=True)
            rule.forward_every_n = every_n
            addons = rule.paid_addons
            addons["every_n"] = every_n > 1
            rule.paid_addons = addons
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_step_every_n_saved", n=every_n))
            text, kbd = self._render_every_n_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^renc:(.+)$"))
        @self._localized_handler
        async def _cb_rule_every_n_custom(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = f"set_every_n_{rule.id}"
            await self._persist(uid, st)
            kbd = self._kbd([[(self._t("ui_cancel_back"), f"ren:{rule.id}:{int(getattr(rule, 'forward_every_n', 1) or 1)}")]])
            await self._safe_edit(cq, self._t("ui_prompt_enter_every_n"), reply_markup=kbd)
            await cq.answer()

        # --- Blacklist Handlers ---
        @b.on_callback_query(filters.regex(r"^rfl_bl:(.+)$"))
        @self._localized_handler
        async def _cb_rule_blacklist_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            text, kbd = self._render_blacklist_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_bl_add:(.+)$"))
        @self._localized_handler
        async def _cb_rule_blacklist_add(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = f"add_bl_{rule.id}"
            await self._persist(uid, st)
            prompt = (
                self._t("ui_prompt_add_blacklist")
            )
            kbd = self._kbd([[(self._t("ui_cancel_back"), f"rfl_bl:{rule.id}")]])
            await self._safe_edit(cq, prompt, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_bl_clr:(.+)$"))
        @self._localized_handler
        async def _cb_rule_blacklist_clear(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.blacklist = []
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_blacklist_cleared_alert"))
            text, kbd = self._render_blacklist_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_bu:(.+)$"))
        @self._localized_handler
        async def _cb_rule_blacklist_users(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cnt = len(rule.blacklist_users)
            await cq.answer(self._t("ui_blacklist_users_count", count=cnt), show_alert=True)

        @b.on_callback_query(filters.regex(r"^rfl_wu:(.+)$"))
        @self._localized_handler
        async def _cb_rule_whitelist_users(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cnt = len(rule.whitelist_users)
            await cq.answer(self._t("ui_whitelist_users_count", count=cnt), show_alert=True)

        # --- Whitelist Handlers ---
        @b.on_callback_query(filters.regex(r"^rfl_wl:(.+)$"))
        @self._localized_handler
        async def _cb_rule_whitelist_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            text, kbd = self._render_whitelist_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_wl_add:(.+)$"))
        @self._localized_handler
        async def _cb_rule_whitelist_add(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = f"add_wl_{rule.id}"
            await self._persist(uid, st)
            prompt = (
                self._t("ui_prompt_add_whitelist")
            )
            kbd = self._kbd([[(self._t("ui_cancel_back"), f"rfl_wl:{rule.id}")]])
            await self._safe_edit(cq, prompt, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_wl_clr:(.+)$"))
        @self._localized_handler
        async def _cb_rule_whitelist_clear(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.whitelist = []
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_whitelist_cleared_alert"))
            text, kbd = self._render_whitelist_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # --- Watermark Handlers ---
        @b.on_callback_query(filters.regex(r"^rwm:(.+)$"))
        @self._localized_handler
        async def _cb_rule_watermark_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            text, kbd = self._render_watermark_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rwm_set:(.+)$"))
        @self._localized_handler
        async def _cb_rule_watermark_set_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = f"set_wm_{rule.id}"
            await self._persist(uid, st)
            prompt = (
                self._t("ui_prompt_set_watermark")
            )
            kbd = self._kbd([[(self._t("ui_cancel_back"), f"rwm:{rule.id}")]])
            await self._safe_edit(cq, prompt, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rwm_del:(.+)$"))
        @self._localized_handler
        async def _cb_rule_watermark_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.watermark = ""
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_watermark_deleted_alert"))
            text, kbd = self._render_watermark_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # --- Translation Handlers ---
        @b.on_callback_query(filters.regex(r"^rtr:(.+)$"))
        @self._localized_handler
        async def _cb_rule_translate_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            text, kbd = self._render_translate_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtr_set:(.+?):(.+)$"))
        @self._localized_handler
        async def _cb_rule_translate_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, lang_code = parts[1], parts[2]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.translate_lang = "" if lang_code == "none" else lang_code
            await self._save_owned_rule(rule, uid)
            # The label already names the language (`🌐 Translation: 🇮🇷 فارسی`),
            # so the toast *is* the label. Building it from the template without
            # the placeholder printed `{language}` at the user.
            status_msg = (self._t("ui_adv_btn_translate", language=language_label(rule.translate_lang))
                          if rule.translate_lang else self._t("ui_filter_link_off"))
            await cq.answer(status_msg)
            text, kbd = self._render_translate_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # --- Scheduler, Power, Affiliate, Link Buttons & AI Callbacks ---
        @b.on_callback_query(filters.regex(r"^rsch_auto:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_auto_sched(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            curr = bool(rule.auto_post_scheduler.get("enabled", False))
            rule.auto_post_scheduler["enabled"] = not curr
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_auto_post_scheduler", not curr))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsch_pwr:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_power_sched(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            curr = bool(rule.power_schedule.get("enabled", False))
            rule.power_schedule["enabled"] = not curr
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_power_schedule", not curr))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rafl:(.+)$"))
        @self._localized_handler
        async def _cb_rule_toggle_affiliate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            curr = bool(rule.affiliate_kit.get("enabled", False))
            rule.affiliate_kit["enabled"] = not curr
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._toggle_toast("ui_toggle_affiliate_kit", not curr))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rbtn:(.+)$"))
        @self._localized_handler
        async def _cb_rule_link_buttons_info(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            btn_cnt = len(rule.link_buttons)
            await cq.answer(self._t("ui_link_buttons_count", count=btn_cnt), show_alert=True)

        @b.on_callback_query(filters.regex(r"^rai_cfg:(.+)$"))
        @self._localized_handler
        async def _cb_rule_ai_cfg_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            # Cycle creativity presets: 0.2 -> 0.7 -> 1.0
            curr_c = rule.ai_creativity
            next_c = 0.7 if curr_c == 0.2 else (1.0 if curr_c == 0.7 else 0.2)
            rule.ai_creativity = next_c
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_ai_creativity_cycle", value=next_c))
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtpc:(.+)$"))
        @self._localized_handler
        async def _cb_rule_set_topic(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = f"set_topic_{rule.id}"
            await self._persist(uid, st)
            text = (
                self._t("ui_topic_panel_title", lang=self._lang_for(rule)) + "\n\n"
                + self._t(
                    "ui_prompt_set_topic",
                    lang=self._lang_for(rule),
                    topic=rule.topic_id or self._t("ui_none_label", lang=self._lang_for(rule)),
                )
            )
            kbd = self._kbd([[(self._t("ui_cancel_back"), f"rd:{rule.id}")]])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rrn:(.+)$"))
        @self._localized_handler
        async def _cb_rule_rename(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = f"rename_rule_{rule.id}"
            await self._persist(uid, st)
            text = self._t("ui_prompt_set_task_name", name=rule.name or f'task_{rule.id[:8]}')
            kbd = self._kbd([[(self._t("ui_cancel_back"), f"rd:{rule.id}")]])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        # ---------------- rule edit (draft & direct) handlers ----------------
        @b.on_callback_query(filters.regex(r"^re:(.+)$"))
        @self._localized_handler
        async def _cb_rule_edit_draft(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                st = self._state(uid)
                st.step = ""
                if isinstance(st.buffer, dict):
                    st.buffer.clear()
                await self._persist(uid, st)
                rules = await self._rules.list_by_owner(uid) if uid else []
                text, kbd = self._render_rules_list(rules, page=0)
                try:
                    await self._safe_edit(cq, text, reply_markup=kbd)
                except Exception:
                    pass
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            st.step = "rule_editing_draft"
            draft = rule.to_draft()
            st.buffer = {
                "rule_id": rule.id,
                "version": rule.version,
                "draft": draft,
                "in_draft": True,
            }
            await self._persist(uid, st)
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_rule_draft_mode_on_alert"))

        @b.on_callback_query(filters.regex(r"^ed_save:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_save(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                return await cq.answer(self._t("ui_draft_expired_alert"), show_alert=True)
            staged_rule_id = str(st.buffer.get("rule_id") or "")
            if staged_rule_id and staged_rule_id != rule_id:
                # The staged draft belongs to a different task: another screen in
                # the same chat, a stale Save button, a reused buffer. Applying it
                # here would overwrite THIS task with another task's entire
                # configuration — name, endpoints, switches — and report success.
                # The optimistic-concurrency check cannot catch it, because the
                # version it compares comes from the same wrong buffer.
                logger.warning(
                    "[UI:DRAFT] refusing ed_save for %s: the staged draft belongs to %s",
                    rule_id[:8], staged_rule_id[:8],
                )
                st.buffer = {}
                st.step = ""
                await self._persist(uid, st)
                return await cq.answer(self._t("ui_draft_expired_alert"), show_alert=True)

            src = str(draft.get("source_chat_id", "")).strip()
            dst = str(draft.get("target_chat_id", "")).strip()
            if not src or not dst:
                return await cq.answer(self._t("ui_rule_src_dst_empty_alert"), show_alert=True)
            if src == dst:
                return await cq.answer(self._t("ui_rule_src_dst_same_alert"), show_alert=True)

            rule.apply_draft(draft)
            expected_version = st.buffer.get("version")
            try:
                await self._save_owned_rule(rule, uid, expected_version)
            except ConcurrencyError:
                return await cq.answer(self._t("ui_rule_concurrent_edit_error"), show_alert=True)
            except Exception as exc:
                return await cq.answer(self._t("ui_rule_save_error_alert", exc=str(exc)), show_alert=True)

            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await cq.answer(self._t("ui_rule_saved_alert"), show_alert=True)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------------------------------------------------------
        # AI Rewrite per-rule menu (pipeline stage — off by default)
        # ---------------------------------------------------------------
        def _ai_badge(draft: Dict[str, Any]) -> str:
            if not draft.get("ai_config_id"):
                return self._t("ui_ai_status_off")
            pol = str(draft.get("ai_fallback_policy") or "DROP")
            return f"🟢 {pol}"

        async def _render_ai_menu(rule_id: str, draft: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
            cfg_id = str(draft.get("ai_config_id") or "")
            prompt_id = str(draft.get("prompt_id") or "")
            pol = str(draft.get("ai_fallback_policy") or "DROP")
            tmo = draft.get("ai_timeout_seconds") or 15.0
            lines = [self._t("ui_ai_menu_title"), ""]
            if cfg_id:
                try:
                    cfg = await self._ai.get(cfg_id)
                    lines.append(f"🧠 {self._t('ui_ai_title')}: `{cfg.name}` [{self._provider_label(cfg.provider.value)}/{cfg.model}]" if cfg else f"🧠 {self._t('ui_ai_title')}: `{cfg_id[:8]}`")
                except Exception:
                    lines.append(f"🧠 {self._t('ui_ai_title')}: `{cfg_id[:8]}`")
            else:
                lines.append(self._t("ui_ai_cfg_status_off"))
            if prompt_id and self._prompts:
                try:
                    tpl = await self._prompts.get_template(prompt_id)
                    lines.append(f"📝 {self._t('ui_ai_prompt_label')}: `{tpl.name}` (v{tpl.current_version})" if tpl else f"📝 {self._t('ui_ai_prompt_label')}: `{prompt_id[:8]}`")
                except Exception:
                    lines.append(f"📝 {self._t('ui_ai_prompt_label')}: `{prompt_id[:8]}`")
            else:
                lines.append(f"📝 {self._t('ui_ai_prompt_label')}: {self._t('ui_ai_prompt_default')}")

            pol_desc = self._ai_policy_text(pol)
            lines += [
                f"⚠️ {self._t('ui_ai_policy_label')}: `{pol}`"
                + (f" ({pol_desc})" if pol_desc else ""),
                f"⏱ {self._t('ui_ai_timeout_label')}: {tmo}s",
            ]
            toggle_btn = (
                (self._btn('ui_ai_btn_disable'), f"r_aitoggle:{rule_id}:off")
                if cfg_id
                else (self._btn('ui_ai_btn_enable'), f"r_aitoggle:{rule_id}:on")
            )
            rows: List[List[Tuple[str, str]]] = [
                [toggle_btn],
                [(self._t("ui_ai_btn_pick_config"), f"r_ai_cfg:{rule_id}")],
                [(self._t("ui_ai_btn_pick_prompt"), f"r_ai_prompt:{rule_id}")],
                [(self._t("ui_ai_btn_change_policy"), f"r_ai_policy:{rule_id}")],
                [(self._t("ui_ai_btn_test_sample"), f"r_ai_test:{rule_id}")],
                [(self._t("ui_ai_btn_back_to_editor"), f"rd:{rule_id}")],
            ]
            return "\n".join(lines), self._kbd(rows)

        @b.on_callback_query(filters.regex(r"^r_aitoggle:(.+?):(on|off)$"))
        @self._localized_handler
        async def _cb_rule_ai_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            st = self._state(uid)
            raw = cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)
            parts = raw.split(":")
            rule_id = parts[1]
            action = parts[2]
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict) or st.buffer.get("rule_id") != rule_id:
                rule = await self._owned_rule(rule_id, uid)
                if not rule:
                    return await cq.answer(self._t("ui_rule_not_found_or_not_owner_alert"), show_alert=True)
                draft = rule.to_draft()
                st.buffer["rule_id"] = rule_id
                st.buffer["draft"] = draft

            if action == "on":
                cfgs = await self._ai.list_by_owner(uid) if (self._ai and uid) else []
                if not cfgs:
                    return await cq.answer(self._t("ui_ai_no_configs_alert"), show_alert=True)
                prev_id = st.buffer.get("prev_ai_cfg_id")
                target_cfg = None
                if prev_id:
                    target_cfg = next((c for c in cfgs if c.id == prev_id), None)
                if not target_cfg:
                    target_cfg = next((c for c in cfgs if c.is_enabled), cfgs[0])
                draft["ai_config_id"] = target_cfg.id
                draft["smart_ai_rewrite"] = True
                draft["ai_status_badge"] = _ai_badge(draft)
                rule = await self._owned_rule(rule_id, uid)
                saved_ok = False
                if rule:
                    before = rule.ai_config_id
                    rule.ai_config_id = target_cfg.id
                    rule.smart_ai_rewrite = True
                    # The panel's paid-addon switch and the pipeline's gate read
                    # the same keys: enabling here must open that switch too,
                    # otherwise ai_rewrite_active keeps saying "off".
                    _addons = dict(rule.paid_addons or {})
                    _addons["ai_rewrite"] = True
                    rule.paid_addons = _addons
                    try:
                        await self._save_owned_rule(rule, uid)
                        saved_ok = True
                        logger.info(
                            "[AI_TOGGLE] user=%s rule=%s ENABLE cfg=%s (%s) persisted %s->%s",
                            uid, rule_id[:8], target_cfg.id[:8], target_cfg.name, before, target_cfg.id,
                        )
                    except Exception as exc:
                        logger.error(
                            "[AI_TOGGLE] user=%s rule=%s ENABLE cfg=%s FAILED to persist: %s",
                            uid, rule_id[:8], target_cfg.id[:8], exc, exc_info=True,
                        )
                else:
                    logger.warning(
                        "[AI_TOGGLE] user=%s rule=%s ENABLE: rule not found for owner — nothing saved",
                        uid, rule_id[:8],
                    )
                await self._persist(uid, st)
                if not saved_ok:
                    # The UI must not show ON when the write did not land.
                    draft["ai_config_id"] = ""
                    draft["smart_ai_rewrite"] = False
                    draft["ai_status_badge"] = _ai_badge(draft)
                    text, kbd = await _render_ai_menu(rule_id, draft)
                    await self._safe_edit(cq, text, reply_markup=kbd)
                    return await cq.answer(self._t("ui_internal_error"), show_alert=True)
                text, kbd = await _render_ai_menu(rule_id, draft)
                await self._safe_edit(cq, text, reply_markup=kbd)
                await cq.answer(self._t("ui_ai_enabled_alert"))
            else:
                if draft.get("ai_config_id"):
                    st.buffer["prev_ai_cfg_id"] = draft.get("ai_config_id")
                draft["ai_config_id"] = ""
                draft["smart_ai_rewrite"] = False
                draft["ai_status_badge"] = _ai_badge(draft)
                rule = await self._owned_rule(rule_id, uid)
                if rule:
                    before = rule.ai_config_id
                    rule.ai_config_id = None
                    rule.smart_ai_rewrite = False
                    _addons = dict(rule.paid_addons or {})
                    _addons["ai_rewrite"] = False
                    rule.paid_addons = _addons
                    try:
                        await self._save_owned_rule(rule, uid)
                        logger.info(
                            "[AI_TOGGLE] user=%s rule=%s DISABLE persisted %s->None",
                            uid, rule_id[:8], before,
                        )
                    except Exception as exc:
                        logger.error(
                            "[AI_TOGGLE] user=%s rule=%s DISABLE FAILED to persist: %s",
                            uid, rule_id[:8], exc, exc_info=True,
                        )
                else:
                    logger.warning(
                        "[AI_TOGGLE] user=%s rule=%s DISABLE: rule not found for owner",
                        uid, rule_id[:8],
                    )
                await self._persist(uid, st)
                text, kbd = await _render_ai_menu(rule_id, draft)
                await self._safe_edit(cq, text, reply_markup=kbd)
                await cq.answer(self._t("ui_ai_disabled_alert"))

        @b.on_callback_query(filters.regex(r"^r_ai:(.+)$"))
        @self._localized_handler
        async def _cb_rule_ai_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict) or st.buffer.get("rule_id") != rule_id:
                rule = await self._owned_rule(rule_id, uid)
                if not rule:
                    return await cq.answer(self._t("ui_rule_not_found_or_not_owner_alert"), show_alert=True)
                draft = rule.to_draft()
            draft["ai_status_badge"] = _ai_badge(draft)
            text, kbd = await _render_ai_menu(rule_id, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_ai_cfg:(.+)$"))
        @self._localized_handler
        async def _cb_rule_ai_pick_cfg(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            st.buffer["ai_pick_rule_id"] = rule_id
            await self._persist(uid, st)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else {}
            cfgs = await self._ai.list_by_owner(uid) if (self._ai and uid) else []
            current = str(draft.get("ai_config_id") or "")
            rows: List[List[Tuple[str, str]]] = [
                [(self._t("ui_ai_opt_off"), "r_aicset:")],
            ]
            for c in cfgs:
                mark = "✅" if c.id == current else ("🟢" if c.is_enabled else "🔴")
                rows.append([(f"{mark} {c.name} [{self._provider_label(c.provider.value)}/{c.model}]", f"r_aicset:{c.id}")])
            rows.append([(self._t("ui_back"), f"r_ai:{rule_id}")])
            await self._safe_edit(cq, self._t("ui_ai_pick_cfg_title"), reply_markup=self._kbd(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_aicset:(.*)$"))
        @self._localized_handler
        async def _cb_rule_ai_set_cfg(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            st = self._state(uid)
            raw = cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)
            parts = raw.split(":")
            if len(parts) >= 3:
                _, rule_id, cfg_id = parts[0], parts[1], parts[2]
            else:
                cfg_id = parts[1] if len(parts) > 1 else ""
                rule_id = st.buffer.get("ai_pick_rule_id") or ""
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                return await cq.answer(self._t("ui_draft_expired"), show_alert=True)
            rule_id = rule_id or draft.get("id") or st.buffer.get("rule_id") or ""
            draft["ai_config_id"] = cfg_id or ""
            draft["smart_ai_rewrite"] = bool(cfg_id)
            draft["ai_status_badge"] = _ai_badge(draft)
            rule = await self._owned_rule(rule_id, uid)
            if rule:
                rule.ai_config_id = cfg_id or None
                rule.smart_ai_rewrite = bool(cfg_id)
                await self._save_owned_rule(rule, uid)
            await self._persist(uid, st)
            text, kbd = await _render_ai_menu(rule_id, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_ai_cfg_staged_alert"))

        @b.on_callback_query(filters.regex(r"^r_ai_prompt:(.+)$"))
        @self._localized_handler
        async def _cb_rule_ai_pick_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            st = self._state(uid)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            st.buffer["ai_pick_rule_id"] = rule_id
            await self._persist(uid, st)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else {}
            current = str(draft.get("prompt_id") or "")
            rows: List[List[Tuple[str, str]]] = [
                [(self._t("ui_ai_btn_add_custom_prompt"), f"r_aipnew:{rule_id}")],
                [(self._t("ui_ai_opt_no_prompt"), "r_aipset:")],
            ]
            if self._prompts:
                templates = await self._prompts.list_templates_by_owner(uid) if hasattr(self._prompts, "list_templates_by_owner") else await self._prompts.list_templates()
                for t in templates:
                    mark = "✅" if t.id == current else ("⭐" if t.is_system else "👤")
                    rows.append([(f"{mark} {t.name} (v{t.current_version})", f"r_aipset:{t.id}")])
            rows.append([(self._t("ui_back"), f"r_ai:{rule_id}")])
            await self._safe_edit(cq, self._t("ui_ai_pick_prompt_title"), reply_markup=self._kbd(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_aipnew:(.+)$"))
        @self._localized_handler
        async def _cb_rule_ai_new_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            st = self._state(uid)
            st.step = f"ai_prompt_name_{rule_id}"
            st.buffer["ai_prompt_rule_id"] = rule_id
            await self._persist(uid, st)
            kbd = self._kbd([[(self._t("ui_cancel_back"), f"r_ai_prompt:{rule_id}")]])
            await self._safe_edit(cq, self._t("ui_ai_prompt_enter_name"), reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_aipset:(.*)$"))
        @self._localized_handler
        async def _cb_rule_ai_set_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            st = self._state(uid)
            raw = cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)
            parts = raw.split(":")
            if len(parts) >= 3:
                _, rule_id, prompt_id = parts[0], parts[1], parts[2]
            else:
                prompt_id = parts[1] if len(parts) > 1 else ""
                rule_id = st.buffer.get("ai_pick_rule_id") or ""
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                return await cq.answer(self._t("ui_draft_expired"), show_alert=True)
            rule_id = rule_id or draft.get("id") or st.buffer.get("rule_id") or ""
            draft["prompt_id"] = prompt_id or ""
            rule = await self._owned_rule(rule_id, uid)
            if rule:
                rule.prompt_id = prompt_id or None
                await self._save_owned_rule(rule, uid)
            await self._persist(uid, st)
            text, kbd = await _render_ai_menu(rule_id, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_ai_prompt_staged_alert"))

        @b.on_callback_query(filters.regex(r"^r_ai_policy:(.+)$"))
        @self._localized_handler
        async def _cb_rule_ai_pick_policy(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            rows: List[List[Tuple[str, str]]] = [
                [(self._t("ui_ai_policy_send_orig"), f"r_aifset:{rule_id}:SEND_ORIGINAL")],
                [(self._t("ui_ai_policy_drop"), f"r_aifset:{rule_id}:DROP")],
                [(self._btn("ui_retry"), f"r_aifset:{rule_id}:RETRY")],
                [(self._t("ui_ai_policy_quarantine"), f"r_aifset:{rule_id}:QUARANTINE")],
                [(self._t("ui_back"), f"r_ai:{rule_id}")],
            ]
            await self._safe_edit(cq, self._t("ui_ai_pick_policy_title"), reply_markup=self._kbd(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_aifset:(.+?):(.+)$"))
        @self._localized_handler
        async def _cb_rule_ai_set_policy(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner"), show_alert=True)
            st = self._state(uid)
            raw = cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)
            _, rule_id, policy = raw.split(":", 2)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                return await cq.answer(self._t("ui_draft_expired"), show_alert=True)
            draft["ai_fallback_policy"] = policy
            draft["ai_status_badge"] = _ai_badge(draft)
            rule = await self._owned_rule(rule_id, uid)
            if rule:
                try:
                    from ...domain.entities import AIFallbackPolicy
                    rule.ai_fallback_policy = AIFallbackPolicy(policy)
                    await self._save_owned_rule(rule, uid)
                except Exception:
                    pass
            await self._persist(uid, st)
            text, kbd = await _render_ai_menu(rule_id, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_ai_policy_updated", policy=policy))

        @b.on_callback_query(filters.regex(r"^r_ai_test:(.+)$"))
        @self._localized_handler
        async def _cb_rule_ai_test(_, cq: CallbackQuery):
            """Dry-run: uses the real prompt+config pipeline, sends nothing."""
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else {}
            cfg_id = str(draft.get("ai_config_id") or "")
            if not cfg_id:
                return await cq.answer(self._t("ui_ai_must_pick_cfg_alert"), show_alert=True)
            sample = str(draft.get("ai_test_text") or "💎 XAUUSD SIGNAL: BUY NOW @ 2655 | TP 2680 | SL 2640 https://t.me/signal #gold")
            await self._safe_edit(cq, self._t("ui_ai_testing_rewrite", sample=sample[:200]), reply_markup=self._kbd([[(self._t("ui_back"), f"r_ai:{rule_id}")]]))
            try:
                prompt_id = str(draft.get("prompt_id") or "")
                if prompt_id and self._prompts and self._ai_transformer:
                    result = await self._prompts.test_prompt(prompt_id, sample, cfg_id, self._ai_transformer)
                else:
                    ok, text_out = await self._ai.test_rewrite_owned(cfg_id, sample, self._scope_uid(uid))
                    result = {"success": ok, "transformed_text": text_out if ok else "", "error_message": "" if ok else text_out,
                              "execution_duration_ms": 0.0, "model_used": "", "prompt_version_used": 0}
            except Exception as exc:
                result = {"success": False, "transformed_text": "", "error_message": f"{type(exc).__name__}: {exc}",
                          "execution_duration_ms": 0.0, "model_used": "", "prompt_version_used": 0}
            if result.get("success"):
                body = (
                    self._t("ui_ai_test_result_title") + "\n\n"
                    + f"{result.get('transformed_text') or self._t('ui_empty')}\n\n"
                    + f"⏱ {result.get('execution_duration_ms', 0):.0f}ms"
                    + (f" | 🧠 {result.get('model_used')}" if result.get("model_used") else "")
                    + (f" | 📝 v{result.get('prompt_version_used')}" if result.get("prompt_version_used") else "")
                )
            else:
                body = self._t(
                    "ui_ai_test_error_body",
                    error=result.get("error_message") or self._t("ui_ai_test_unknown_error"),
                )
            await self._safe_edit(cq, body, reply_markup=self._kbd([
                [(self._btn("ui_retry"), f"r_ai_test:{rule_id}")],
                [(self._t("ui_back"), f"r_ai:{rule_id}")],
            ]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ed_can:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_cancel(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)

            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            await cq.answer(self._t("ui_draft_edit_cancelled_alert"))
            if rule:
                text, kbd = self._render_rule_detail(rule)
                await self._safe_edit(cq, text, reply_markup=kbd)
            else:
                rules = await self._rules.list_by_owner(uid) if uid else []
                text, kbd = self._render_rules_list(rules, page=0)
                await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_s:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_source(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner_alert"), show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            st.step = "rule_edit_source"
            if not isinstance(st.buffer, dict):
                st.buffer = {}
            if "draft" not in st.buffer:
                st.buffer["draft"] = rule.to_draft()
            st.buffer["rule_id"] = rule.id
            st.buffer["version"] = rule.version
            st.buffer["in_draft"] = True
            st.buffer["edit_role"] = "source"
            st.buffer["page"] = 0
            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="source", page=0)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ed_t:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_target(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            st.step = "rule_edit_target"
            if not isinstance(st.buffer, dict):
                st.buffer = {}
            if "draft" not in st.buffer:
                st.buffer["draft"] = rule.to_draft()
            st.buffer["rule_id"] = rule.id
            st.buffer["version"] = rule.version
            st.buffer["in_draft"] = True
            st.buffer["edit_role"] = "target"
            st.buffer["page"] = 0
            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="target", page=0)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^res:(.+)$"))
        @self._localized_handler
        async def _cb_rule_edit_source(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            st.step = "rule_edit_source"
            st.buffer = {
                "rule_id": rule.id,
                "version": rule.version,
                "in_draft": False,
                "edit_role": "source",
                "page": 0,
            }
            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="source", page=0)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ret:(.+)$"))
        @self._localized_handler
        async def _cb_rule_edit_target(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            st.step = "rule_edit_target"
            st.buffer = {
                "rule_id": rule.id,
                "version": rule.version,
                "in_draft": False,
                "edit_role": "target",
                "page": 0,
            }
            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="target", page=0)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ed_m:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_mode(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            cur = draft.get("forward_mode", ForwardMode.COPY_MESSAGE.value)
            cur_val = getattr(cur, "value", str(cur))
            new_mode = ForwardMode.DIRECT_FORWARD.value if cur_val == ForwardMode.COPY_MESSAGE.value else ForwardMode.COPY_MESSAGE.value
            draft["forward_mode"] = new_mode
            await self._persist(uid, st)
            await cq.answer(self._t("ui_send_mode_changed_alert"))
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_a:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_active(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["is_active"] = not bool(draft.get("is_active", True))
            await self._persist(uid, st)
            stat = self._t("ui_rule_enabled_short") if draft["is_active"] else self._t("ui_rule_disabled_short")
            # The catalog template declares `{stat}`; passing `status=` left the
            # placeholder in the toast the user reads.
            await cq.answer(self._t("ui_draft_status_alert", stat=stat))
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_lk:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_link(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["remove_links"] = not bool(draft.get("remove_links", False))
            await self._persist(uid, st)
            stat = self._t("ui_stat_remove_links_word") if draft["remove_links"] else self._t("ui_stat_allowed_word")
            await cq.answer(self._t("ui_clean_url_status", stat=stat))
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_vc:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_voice(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["block_voice"] = not bool(draft.get("block_voice", False))
            await self._persist(uid, st)
            stat = self._t("ui_clean_removed") if draft["block_voice"] else self._t("ui_clean_kept")
            await cq.answer(self._t("ui_clean_voice_status", stat=stat))
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_st:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_sticker(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["block_stickers"] = not bool(draft.get("block_stickers", False))
            await self._persist(uid, st)
            stat = self._t("ui_clean_removed") if draft["block_stickers"] else self._t("ui_clean_kept")
            await cq.answer(self._t("ui_clean_sticker_status", stat=stat))
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_em:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_emoji(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["remove_emojis"] = not bool(draft.get("remove_emojis", False))
            await self._persist(uid, st)
            stat = self._t("ui_stat_clean_word") if draft["remove_emojis"] else self._t("ui_stat_keep_word")
            await cq.answer(self._t("ui_clean_emoji_status", stat=stat))
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_ed:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_edit(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["ignore_edits"] = not bool(draft.get("ignore_edits", False))
            await self._persist(uid, st)
            stat = self._t("ui_stat_ignore_word") if draft["ignore_edits"] else self._t("ui_stat_sync_word")
            await cq.answer(self._t("ui_clean_edit_status", stat=stat))
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_dl:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_delete(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["sync_deletes"] = not bool(draft.get("sync_deletes", False))
            await self._persist(uid, st)
            stat = self._t("ui_stat_sync_del_word") if draft["sync_deletes"] else self._t("ui_stat_ignore_word")
            await cq.answer(self._t("ui_clean_delete_status", stat=stat))
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_mg:(.+)$"))
        @self._localized_handler
        async def _cb_rule_draft_media_group(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            cur = str(draft.get("album_mode") or "album")
            cycle = {"album": "first", "first": "split", "split": "album"}
            next_mode = cycle.get(cur, "album")
            draft["album_mode"] = next_mode
            await self._persist(uid, st)
            names = {"album": self._t("ui_media_album_label"), "first": self._t("ui_media_first_label"), "split": self._t("ui_media_split_label")}
            await cq.answer(self._t("ui_media_group_alert", mode=names.get(next_mode, next_mode)))
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------- smart routing & VIP controls ----------------
        @b.on_callback_query(filters.regex(r"^rsm:(.+)$"))
        @self._localized_handler
        async def _cb_smart_rule_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmpth:(.+)$"))
        @self._localized_handler
        async def _cb_smart_toggle_route_path(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            use_mid = bool(getattr(rule, "use_intermediate", False))
            cat = getattr(rule, "message_category", "ALL")
            f_mode = getattr(rule.forward_mode, "value", str(rule.forward_mode))

            if not use_mid and f_mode != "DIRECT_FORWARD":
                # Currently Route 1 -> Switch to Route 2 (VIP Hop A -> C -> B)
                rule.use_intermediate = True
                rule.message_category = "VIP_ONLY"
                rule.forward_mode = ForwardMode.CUSTOM_HEADER_COPY
                ans = self._t("ui_smart_path2_alert")
            elif use_mid:
                # Currently Route 2 -> Switch to Route 3 (Native A -> B)
                rule.use_intermediate = False
                rule.message_category = "ALL"
                rule.forward_mode = ForwardMode.DIRECT_FORWARD
                ans = self._t("ui_smart_path3_alert")
            else:
                # Currently Route 3 -> Switch to Route 1 (Direct Copy A -> B)
                rule.use_intermediate = False
                rule.message_category = "ALL"
                rule.forward_mode = ForwardMode.COPY_MESSAGE
                ans = self._t("ui_smart_path1_alert")

            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["use_intermediate"] = rule.use_intermediate
            rule.metadata["message_category"] = rule.message_category
            await self._save_owned_rule(rule, uid)
            await cq.answer(ans)
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmmod:(.+)$"))
        @self._localized_handler
        async def _cb_smart_toggle_match_mode(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)

            if not isinstance(rule.detection_criteria, dict):
                rule.detection_criteria = {}
            cur = (rule.detection_criteria.get("match_mode") or "ANY").upper()
            nxt = "ALL" if cur == "ANY" else "ANY"
            rule.detection_criteria["match_mode"] = nxt
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["detection_criteria"] = rule.detection_criteria
            await self._save_owned_rule(rule, uid)
            desc = self._t("ui_logic_all") if nxt == "ALL" else self._t("ui_logic_any")
            await cq.answer(self._t("ui_match_logic_alert", desc=desc))
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmtx:(.+)$"))
        @self._localized_handler
        async def _cb_smart_set_text_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "smart_set_keywords"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = self._t("ui_smart_btn_keywords") + "\n\n" + self._t("ui_cancel_hint")
            await self._safe_edit(
                cq,
                prompt,
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rsm:{rule_id}")]]),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmrx:(.+)$"))
        @self._localized_handler
        async def _cb_smart_set_regex_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "smart_set_regex"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = self._t("ui_smart_btn_regex") + "\n\n" + self._t("ui_cancel_hint")
            await self._safe_edit(
                cq,
                prompt,
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rsm:{rule_id}")]]),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmc:(.+)$"))
        @self._localized_handler
        async def _cb_smart_toggle_category(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cur = getattr(rule, "message_category", "ALL")
            cycle = {"ALL": "VIP_ONLY", "VIP_ONLY": "NORMAL_ONLY", "NORMAL_ONLY": "ALL"}
            nxt = cycle.get(cur, "ALL")
            rule.message_category = nxt
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["message_category"] = nxt
            await self._save_owned_rule(rule, uid)
            labels = {"ALL": self._t("ui_cat_all"), "VIP_ONLY": self._t("ui_cat_vip"), "NORMAL_ONLY": self._t("ui_cat_normal")}
            await cq.answer(self._t("ui_msg_cat_alert", cat=labels.get(nxt, nxt)))
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmt:(.+)$"))
        @self._localized_handler
        async def _cb_smart_toggle_intermediate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cur = bool(getattr(rule, "use_intermediate", False))
            rule.use_intermediate = not cur
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["use_intermediate"] = rule.use_intermediate
            await self._save_owned_rule(rule, uid)
            ans = self._t("ui_interm_route_on") if rule.use_intermediate else self._t("ui_interm_route_off")
            await cq.answer(ans)
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmm:(.+)$"))
        @self._localized_handler
        async def _cb_smart_toggle_mode(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cur = getattr(rule.forward_mode, "value", str(rule.forward_mode))
            cycle = {
                "COPY_MESSAGE": ForwardMode.DIRECT_FORWARD,
                "DIRECT_FORWARD": ForwardMode.CUSTOM_HEADER_COPY,
                "CUSTOM_HEADER_COPY": ForwardMode.COPY_MESSAGE,
                "REWRITE_AI": ForwardMode.COPY_MESSAGE,
            }
            nxt_mode = cycle.get(cur, ForwardMode.COPY_MESSAGE)
            rule.forward_mode = nxt_mode
            await self._save_owned_rule(rule, uid)
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmpu:(.+)$"))
        @self._localized_handler
        async def _cb_smart_priority_up(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.priority = int(getattr(rule, "priority", 10) or 10) + 10
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["priority"] = rule.priority
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_priority_alert", priority=rule.priority))
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmpd:(.+)$"))
        @self._localized_handler
        async def _cb_smart_priority_down(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cur = int(getattr(rule, "priority", 10) or 10)
            rule.priority = max(0, cur - 10)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["priority"] = rule.priority
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_priority_alert", priority=rule.priority))
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmhd:(.+)$"))
        @self._localized_handler
        async def _cb_smart_clear_header(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            rule.clear_header()
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_custom_header_deleted_alert"))
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmw:(.+)$"))
        @self._localized_handler
        async def _cb_smart_set_intermediate_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "smart_set_intermediate"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = self._t("ui_smart_btn_mid") + "\n\n" + self._t("ui_cancel_hint")
            await self._safe_edit(
                cq,
                prompt,
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rsm:{rule_id}")]]),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmbo:(.+)$"))
        @self._localized_handler
        async def _cb_smart_brand_origins_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "smart_set_brand_origins"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = self._t("ui_brand_origins_prompt") + "\n\n" + self._t("ui_cancel_hint")
            await self._safe_edit(
                cq,
                prompt,
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rsm:{rule_id}")]]),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmbk:(.+)$"))
        @self._localized_handler
        async def _cb_smart_brand_words_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "smart_set_brand_words"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = self._t("ui_brand_words_prompt") + "\n\n" + self._t("ui_cancel_hint")
            await self._safe_edit(
                cq,
                prompt,
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rsm:{rule_id}")]]),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmbr:(.+)$"))
        @self._localized_handler
        async def _cb_smart_brand_gate_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cfg = branding_policy.read_config(rule)
            cfg["require_forward"] = not bool(cfg.get("require_forward"))
            branding_policy.write_config(rule, {"require_forward": cfg["require_forward"]})
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_brand_gate_saved", lang=self._resolve_lang(uid),
                                    state=self._t("ui_brand_on" if cfg["require_forward"] else "ui_brand_off")))
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmbf:(.+)$"))
        @self._localized_handler
        async def _cb_smart_brand_force_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cfg = branding_policy.read_config(rule)
            cfg["force_forward"] = not bool(cfg.get("force_forward"))
            branding_policy.write_config(rule, {"force_forward": cfg["force_forward"]})
            await self._save_owned_rule(rule, uid)
            await cq.answer(self._t("ui_brand_force_saved", lang=self._resolve_lang(uid),
                                    state=self._t("ui_brand_on" if cfg["force_forward"] else "ui_brand_off")))
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmbt:(.+)$"))
        @self._localized_handler
        async def _cb_smart_brand_triggers_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            cfg = branding_policy.read_config(rule)
            enable = not bool(cfg.get("triggers_enabled"))
            # Enabling the filter with nothing configured would make the hop
            # unreachable (a filter with no criteria matches nothing); refuse it
            # instead of saving a rule that can never deliver.
            if enable and not (cfg.get("origin_titles") or cfg.get("origin_ids") or cfg.get("keywords")):
                return await cq.answer(
                    self._t("ui_brand_needs_criteria", lang=self._resolve_lang(uid)),
                    show_alert=True,
                )
            branding_policy.write_config(rule, {"triggers_enabled": enable})
            await self._save_owned_rule(rule, uid)
            state_key = "ui_smart_brand_state_filtered" if enable else "ui_smart_brand_state_all"
            await cq.answer(self._t("ui_brand_triggers_saved", lang=self._resolve_lang(uid),
                                    state=self._t(state_key)))
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmh:(.+)$"))
        @self._localized_handler
        async def _cb_smart_set_header_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "smart_set_header"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = self._t("ui_smart_btn_header") + "\n\n" + self._t("ui_cancel_hint")
            await self._safe_edit(
                cq,
                prompt,
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rsm:{rule_id}")]]),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmtst:(.+)$"))
        @self._localized_handler
        async def _cb_smart_test_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer(self._t("ui_rule_not_found"), show_alert=True)
            st = self._state(uid)
            st.step = "smart_test_rule"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = self._t("ui_smart_btn_dryrun") + "\n\n" + self._t("ui_cancel_hint")
            await self._safe_edit(
                cq,
                prompt,
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rsm:{rule_id}")]]),
            )
            await cq.answer()

        # ---------------- filter add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["filter_add"] + "$"))
        @self._localized_handler
        async def _cb_filter_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "filter_name"
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, 
                self._t("ui_filter_name"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["filter_del"] + ":"))
        @self._localized_handler
        async def _cb_filter_del(_, cq: CallbackQuery):
            fid = cq.data.split(":", 1)[1]
            # v12.1: IDOR guard — a non-admin can only delete their own filter.
            fuid = cq.from_user.id if cq.from_user else 0
            try:
                await self._filters.delete_owned(fid, None if self._is_admin(fuid) else fuid)
            except PermissionError:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            await self._safe_edit(cq, self._t("ui_done"), reply_markup=self._filters_menu())
            await cq.answer()

        # ---------------- ai add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["ai_add"] + "$"))
        @self._localized_handler
        async def _cb_ai_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "ai_name"
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, self._t("ui_ai_name"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["ai_provider_pick"] + ":"))
        @self._localized_handler
        async def _cb_ai_provider_pick(_, cq: CallbackQuery):
            st = self._state(cq.from_user.id)
            if st.step != "ai_provider":
                return await cq.answer()
            provider = cq.data.split(":", 1)[1]
            if provider not in {p.value for p in AIProviderType}:
                return await cq.answer(self._t("ui_ai_provider_bad"), show_alert=True)
            st.ai_provider = provider
            st.step = "ai_base_url"
            from core.infrastructure.ai.providers import OpenAICompatibleProvider
            default_url = OpenAICompatibleProvider.default_base_urls.get(provider, "")
            st.buffer["default_base_url"] = default_url
            prompt_text = self._t("ui_prompt_enter_base_url", provider=self._provider_label(provider), default_url=default_url or self._t("ui_none_manual"))
            kbd = self._kbd([
                [(self._t("ui_btn_use_default_host"), "ai_url_default")],
                [(self._btn("ui_cancel"), CB["cancel"])],
            ])
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, prompt_text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^ai_url_default$"))
        @self._localized_handler
        async def _cb_ai_url_default(_, cq: CallbackQuery):
            st = self._state(cq.from_user.id)
            if st.step != "ai_base_url":
                return await cq.answer()
            st.ai_base_url = st.buffer.get("default_base_url", "")
            st.step = "ai_model"
            kb = self._model_picker(st, st.ai_provider)
            await self._persist(cq.from_user.id, st)
            msg_text = (
                self._t("ui_ai_model")
                + self._t("ui_prompt_select_model_or_type")
            )
            if kb is not None:
                await self._safe_edit(cq, msg_text, reply_markup=kb)
            else:
                await self._safe_edit(cq, msg_text, reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["ai_model_pick"] + ":"))
        @self._localized_handler
        async def _cb_ai_model_pick(_, cq: CallbackQuery):
            st = self._state(cq.from_user.id)
            if st.step != "ai_model":
                return await cq.answer()
            try:
                idx = int(cq.data.split(":", 1)[1])
                model = st.buffer.get("models", [])[idx]
            except (ValueError, IndexError):
                return await cq.answer(self._t("ui_ai_provider_bad"), show_alert=True)
            st.ai_model = model
            st.step = "ai_api_key"
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, self._t("ui_ai_key"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^aid:(.+)"))
        @self._localized_handler
        async def _cb_ai_detail(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                await cq.answer(self._t("ui_ai_config_not_found_alert"), show_alert=True)
                _d_uid = self._scope_uid(cq.from_user.id if cq.from_user else 0)
                cfgs = await (self._ai.list_all() if _d_uid is None else self._ai.list_by_owner(cq.from_user.id))
                return await self._safe_edit(cq, self._t("ui_ai_title"), reply_markup=self._ai_menu(cfgs))
            text, kbd = self._render_ai_detail(cfg)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ai_hc:(.+)"))
        @self._localized_handler
        async def _cb_ai_hc(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer(self._t("ui_ai_config_not_found"), show_alert=True)
            await cq.answer(self._t("ui_ai_checking_health_alert"), show_alert=False)
            ok, reply_or_err, latency = await self._ai.health_check_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            health_info = {
                "ok": ok,
                "latency_ms": latency,
                "reply": reply_or_err if ok else "",
                "error": reply_or_err if not ok else "",
            }
            text, kbd = self._render_ai_detail(cfg, health_info=health_info)
            try:
                await self._safe_edit(cq, text, reply_markup=kbd)
            except Exception:
                pass

        @b.on_callback_query(filters.regex(r"^(?:ai_sample|" + CB["ai_test"] + r"):(.+)"))
        @self._localized_handler
        async def _cb_ai_sample(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer(self._t("ui_ai_config_not_found"), show_alert=True)
            await cq.answer(self._t("ui_ai_sending_sample_alert"), show_alert=False)
            sample_text = self._t("ui_ai_sample_probe_text")
            ok, reply = await self._ai.test_rewrite_owned(aid, sample_text, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if ok:
                body = self._t("ui_ai_test_success_body", model=cfg.model, reply=reply[:1000])
            else:
                body = self._t("ui_ai_test_failed_body", reply=reply[:1000])
            kbd = self._kbd([[(self._t("ui_back_to_config"), f"aid:{aid}")]])
            await self._safe_edit(cq, body, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ai_tog:(.+)"))
        @self._localized_handler
        async def _cb_ai_toggle(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer(self._t("ui_ai_config_not_found"), show_alert=True)
            cfg.is_enabled = not cfg.is_enabled
            try:
                await self._ai.update_owned(cfg, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            except PermissionError:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            status_text = self._t("ui_rule_status_on") if cfg.is_enabled else self._t("ui_rule_status_off")
            await cq.answer(self._t("ui_ai_status_changed_alert", status=status_text), show_alert=False)
            text, kbd = self._render_ai_detail(cfg)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ai_del_ask:(.+)"))
        @self._localized_handler
        async def _cb_ai_del_ask(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer(self._t("ui_ai_config_not_found"), show_alert=True)
            text = self._t("ui_ai_delete_confirm_title", name=cfg.name, id=cfg.id[:8])
            kbd = self._kbd([
                [(self._t("ui_confirm_yes_delete"), f"ai_del_confirm:{aid}")],
                [(self._t("ui_cancel_back"), f"aid:{aid}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^(?:ai_del_confirm|" + CB["ai_del"] + r"):(.+)"))
        @self._localized_handler
        async def _cb_ai_del_confirm(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            # v12.1: IDOR guard — a non-admin can only delete their own config.
            auid = cq.from_user.id if cq.from_user else 0
            try:
                await self._ai.delete_owned(aid, None if self._is_admin(auid) else auid)
            except PermissionError:
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            await cq.answer(self._t("ui_ai_config_deleted_alert"), show_alert=True)
            cfgs = await (self._ai.list_all() if self._is_admin(auid) else self._ai.list_by_owner(auid))
            lines = [self._t("ui_ai_title"), "", self._t("ui_ai_config_deleted_body_full")]
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._ai_menu(cfgs))

        @b.on_callback_query(filters.regex(r"^ai_eh:(.+)"))
        @self._localized_handler
        async def _cb_ai_edit_host(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer(self._t("ui_ai_config_not_found"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "ai_edit_host"
            st.buffer["ai_edit_id"] = aid
            await self._persist(cq.from_user.id, st)
            current = cfg.base_url or "(default)"
            text = self._t("ui_prompt_change_base_url", current=current)
            kbd = self._kbd([
                [(self._btn("ui_cancel"), f"aid:{aid}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ai_em:(.+)"))
        @self._localized_handler
        async def _cb_ai_edit_model(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer(self._t("ui_ai_config_not_found"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "ai_edit_model"
            st.buffer["ai_edit_id"] = aid
            await self._persist(cq.from_user.id, st)
            text = self._t("ui_prompt_change_model", model=cfg.model)
            kbd = self._kbd([
                [(self._btn("ui_cancel"), f"aid:{aid}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ai_ek:(.+)"))
        @self._localized_handler
        async def _cb_ai_edit_key(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer(self._t("ui_ai_config_not_found"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "ai_edit_key"
            st.buffer["ai_edit_id"] = aid
            await self._persist(cq.from_user.id, st)
            text = self._t("ui_prompt_change_api_key")
            kbd = self._kbd([
                [(self._btn("ui_cancel"), f"aid:{aid}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        # ---------------- PV Responder (AI PV Assistant) ----------------
        @b.on_callback_query(filters.regex(r"^pv:menu$"))
        @self._localized_handler
        async def _cb_pv_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_menu(uid, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:toggle$"))
        @self._localized_handler
        async def _cb_pv_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer(self._t("ui_invalid_user_id"), show_alert=True)
            resp = getattr(self, "_pv_responder", None)
            if not resp:
                return await cq.answer(self._t("ui_ai_module_unavailable"), show_alert=True)
            cfg = resp.get_config_for_user(uid)
            cfg.enabled = not cfg.enabled
            resp.save_config_for_user(uid, cfg)
            status_msg = self._t("ui_pv_enabled_alert") if cfg.enabled else self._t("ui_pv_disabled_alert")
            await cq.answer(status_msg)
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_menu(uid, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:persona$"))
        @self._localized_handler
        async def _cb_pv_persona(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_persona_menu(lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:set_p:(.+)$"))
        @self._localized_handler
        async def _cb_pv_set_persona(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer(self._t("ui_invalid_user_id"), show_alert=True)
            resp = getattr(self, "_pv_responder", None)
            if not resp:
                return await cq.answer(self._t("ui_ai_module_unavailable"), show_alert=True)
            cfg = resp.get_config_for_user(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            p_type = raw.split(":", 2)[2]
            if p_type == "casual":
                from core.infrastructure.telegram.pv_responder import DEFAULT_PERSONA
                cfg.persona_prompt = DEFAULT_PERSONA
                resp.save_config_for_user(uid, cfg)
                await cq.answer(self._t("ui_pv_persona_friendly_alert"))
                lang = await self._get_user_lang(uid)
                text, kbd = self._render_pv_menu(uid, lang=lang)
                await self._safe_edit(cq, text, reply_markup=kbd)
            elif p_type == "business":
                cfg.persona_prompt = self._t("ui_pv_formal_prompt_text")
                resp.save_config_for_user(uid, cfg)
                await cq.answer(self._t("ui_pv_persona_formal_alert"))
                lang = await self._get_user_lang(uid)
                text, kbd = self._render_pv_menu(uid, lang=lang)
                await self._safe_edit(cq, text, reply_markup=kbd)
            elif p_type == "short":
                cfg.persona_prompt = self._t("ui_pv_concise_prompt_text")
                resp.save_config_for_user(uid, cfg)
                await cq.answer(self._t("ui_pv_persona_concise_alert"))
                lang = await self._get_user_lang(uid)
                text, kbd = self._render_pv_menu(uid, lang=lang)
                await self._safe_edit(cq, text, reply_markup=kbd)
            elif p_type == "custom":
                st = self._state(uid)
                st.step = "pv_persona_input"
                await self._persist(uid, st)
                prompt_text = self._t("ui_pv_custom_prompt_intro")
                await self._safe_edit(cq, prompt_text, reply_markup=self._kbd([[(self._btn("ui_cancel"), "pv:persona")]]))
                await cq.answer()

        @b.on_callback_query(filters.regex(r"^pv:delays$"))
        @self._localized_handler
        async def _cb_pv_delays(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_delays_menu(lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:set_d:(.+)$"))
        @self._localized_handler
        async def _cb_pv_set_delay(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer(self._t("ui_invalid_user_id"), show_alert=True)
            resp = getattr(self, "_pv_responder", None)
            if not resp:
                return await cq.answer(self._t("ui_ai_module_unavailable"), show_alert=True)
            cfg = resp.get_config_for_user(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            d_type = raw.split(":", 2)[2]
            if d_type == "fast":
                cfg.typing_delay_min = 1.5
                cfg.typing_delay_max = 3.0
            elif d_type == "normal":
                cfg.typing_delay_min = 2.0
                cfg.typing_delay_max = 4.5
            elif d_type == "slow":
                cfg.typing_delay_min = 3.5
                cfg.typing_delay_max = 6.5
            resp.save_config_for_user(uid, cfg)
            await cq.answer(self._t("ui_pv_delay_applied_alert"))
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_menu(uid, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:test$"))
        @self._localized_handler
        async def _cb_pv_test(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer(self._t("ui_invalid_user_id"), show_alert=True)
            st = self._state(uid)
            st.step = "pv_test_input"
            await self._persist(uid, st)
            prompt = self._t("ui_pv_simulation_guide")
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[(self._t("ui_cancel_back"), "pv:menu")]]))
            await cq.answer()

        # ---------------- backup ----------------
        @b.on_callback_query(filters.regex("^" + CB["backup"] + "$"))
        @self._localized_handler
        async def _cb_backup(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                await self._safe_edit(
                    cq, self._t("ui_no_sessions"), reply_markup=self._sessions_menu()
                )
                return await cq.answer()
            st = self._state(uid)
            st.step = "session_backup_passphrase"
            st.buffer["backup_session_id"] = sessions[0].id
            await self._persist(uid, st)
            await self._safe_edit(
                cq,
                self._t("ui_migrate_ask_passphrase"),
                reply_markup=self._kbd(
                    [[(self._btn("ui_cancel"), CB["cancel"])]]
                ),
            )
            await cq.answer()

        # ---------------- migration backup/restore -----------------------------
        # Full-installation snapshot: everything needed to move this box to
        # another server. The bundle carries live credentials, so the encrypted
        # variant is the safe one to send over Telegram.
        async def _do_migration_export(cq: CallbackQuery, passphrase: str):
            if self._backup is None:
                await self._safe_edit(cq, self._t("ui_migrate_unavailable"),
                                      reply_markup=self._migrate_menu())
                return await cq.answer()
            try:
                await self._safe_edit(cq, self._t("ui_migrate_packing"),
                                      reply_markup=None)
                owner = cq.from_user.id if not self._is_admin(cq.from_user.id) else None
                doc = await self._backup.create_snapshot(owner)
                text = serialize_bundle(doc)
                fname = "atf-backup.json"
                if passphrase:
                    text = encrypt_bundle(text, passphrase)
                    fname = "atf-backup.enc"
                path = os.path.join(tempfile.gettempdir(), f"atf_{cq.from_user.id}_{fname}")
                await asyncio.to_thread(Path(path).write_text, text, encoding="utf-8")
                counts = doc.get("counts") or {}
                lines = [self._t("ui_migrate_exported"),
                         "─────────────────────"]
                for k in ("sessions", "forward_rules", "filter_rules",
                          "ai_configs", "prompt_templates", "api_credentials"):
                    if counts.get(k):
                        lines.append(f"• {k}: {counts[k]}")
                if passphrase:
                    lines.insert(1, "🔒 AES-GCM " + self._t("ui_migrate_encrypted"))
                await self.bot.send_document(
                    chat_id=cq.message.chat.id, document=path,
                    caption="\n".join(lines),
                )
                os.remove(path)
                try:
                    self._log.info("bot", "backup", "migration snapshot exported")
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("migration export failed")
                await self._record_error("bot", exc, user_id=cq.from_user.id)
                await cq.message.reply_text(
                    self._t("ui_migrate_fail", error=tg_detail(exc)))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["migrate"] + "$"))
        @self._localized_handler
        async def _cb_migrate(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            await self._safe_edit(cq, self._t("ui_migrate_title"),
                                  reply_markup=self._migrate_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["migrate_export"] + "$"))
        @self._localized_handler
        async def _cb_migrate_export(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            await cq.answer()
            await _do_migration_export(cq, "")

        @b.on_callback_query(filters.regex("^" + CB["migrate_export_enc"] + "$"))
        @self._localized_handler
        async def _cb_migrate_export_enc(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            await cq.answer()
            st = self._state(cq.from_user.id)
            st.step = "migrate_passphrase"
            st.buffer["migrate_chat_id"] = cq.message.chat.id
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, self._t("ui_migrate_ask_passphrase"),
                                  reply_markup=self._kbd(
                                      [[(self._btn("ui_cancel"), CB["cancel"])]]))

        @b.on_callback_query(filters.regex("^" + CB["migrate_import"] + "$"))
        @self._localized_handler
        async def _cb_migrate_import(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            await cq.answer()
            st = self._state(cq.from_user.id)
            st.step = "migrate_await_file"
            st.buffer["migrate_overwrite"] = False
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, self._t("ui_migrate_await_file"),
                                  reply_markup=self._kbd(
                                      [[(self._btn("ui_migrate_overwrite"), CB["migrate_overwrite"])],
                                       [(self._btn("ui_cancel"), CB["cancel"])]]))

        @b.on_callback_query(filters.regex("^" + CB["migrate_overwrite"] + "$"))
        @self._localized_handler
        async def _cb_migrate_overwrite(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            await cq.answer()
            st = self._state(cq.from_user.id)
            if st.step == "migrate_await_file":
                st.buffer["migrate_overwrite"] = not st.buffer.get("migrate_overwrite", False)
                await self._persist(cq.from_user.id, st)
                await self._safe_edit(
                    cq,
                    self._t("ui_migrate_await_file")
                    + ("\n\n⚠️ " + self._t("ui_migrate_overwrite_on")
                       if st.buffer.get("migrate_overwrite") else ""),
                    reply_markup=self._kbd(
                        [[(self._btn("ui_migrate_overwrite"), CB["migrate_overwrite"])],
                         [(self._btn("ui_cancel"), CB["cancel"])]]))

        @b.on_callback_query(filters.regex("^" + CB["migrate_help"] + "$"))
        @self._localized_handler
        async def _cb_migrate_help(_, cq: CallbackQuery):
            await self._safe_edit(cq, self._t("ui_migrate_help_text"),
                                  reply_markup=self._migrate_menu())
            await cq.answer()

        # ---------------- logs panel ----------------
        @b.on_callback_query(filters.regex("^" + CB["logs"] + "$"))
        @self._localized_handler
        async def _cb_logs(_, cq: CallbackQuery):
            await self._safe_edit(cq, self._t("ui_logs_title"), reply_markup=self._logs_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["logs_recent"] + "$"))
        @self._localized_handler
        async def _cb_logs_recent(_, cq: CallbackQuery):
            lines = [self._t("ui_logs_recent"), ""]
            if self._error_log is not None:
                try:
                    rows = await self._error_log.recent(10)
                    if not rows:
                        lines.append(self._t("ui_none"))
                    for r in rows:
                        lines.append(diagnostics.error_line(self._t, r))
                        det = (r.get("detail") or "")[:90]
                        if det:
                            lines.append(f"     {det}")
                except Exception as exc:
                    lines.append("⚠️ " + tg_detail(exc))
            else:
                lines.append(self._t("ui_none"))
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._logs_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["logs_errors"] + "$"))
        @self._localized_handler
        async def _cb_logs_errors(_, cq: CallbackQuery):
            lines = [self._t("ui_logs_errors"), ""]
            if self._error_log is not None:
                try:
                    rows = await self._error_log.recent(8, severity=SEV_FATAL)
                    rows += await self._error_log.recent(8, severity=SEV_ERROR)
                    if not rows:
                        lines.append(self._t("ui_none"))
                    for r in rows:
                        lines.append(diagnostics.error_line(
                            self._t, r, retryable=bool(r.get("recoverable"))))
                        det = (r.get("detail") or "")[:90]
                        if det:
                            lines.append(f"     {det}")
                except Exception as exc:
                    lines.append("⚠️ " + tg_detail(exc))
            else:
                lines.append(self._t("ui_none"))
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._logs_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["logs_stats"] + "$"))
        @self._localized_handler
        async def _cb_logs_stats(_, cq: CallbackQuery):
            lines = [self._t("ui_logs_stats"), ""]
            if self._error_log is not None:
                try:
                    since = int(time.time()) - 24 * 3600
                    counts = await self._error_log.counts_by_severity(since)
                    if not counts:
                        lines.append(self._t("ui_none"))
                    for sev, n in sorted(counts.items()):
                        lines.append(diagnostics.severity_count_line(self._t, sev, n))
                except Exception as exc:
                    lines.append("⚠️ " + tg_detail(exc))
            else:
                lines.append(self._t("ui_none"))
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._logs_menu())
            await cq.answer()

        # ---------------- debug ----------------
        @b.on_callback_query(filters.regex("^" + CB["debug"] + "$"))
        @self._localized_handler
        async def _cb_debug(_, cq: CallbackQuery):
            await self._safe_edit(cq, 
                await self._debug_body(), reply_markup=self._debug_menu())
            await cq.answer()

        # ---------------- web dashboard URL configuration ----------------
        @b.on_callback_query(filters.regex("^" + CB["weburl"] + "$"))
        @self._localized_handler
        async def _cb_weburl(_, cq: CallbackQuery):
            await self._safe_edit(
                cq, self._weburl_body(), reply_markup=self._weburl_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["weburl_set"] + "$"))
        @self._localized_handler
        async def _cb_weburl_set(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "weburl_input"
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(
                cq, self._t("ui_weburl_prompt"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^weburl_clear$"))
        @self._localized_handler
        async def _cb_weburl_clear(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_rule_not_found_or_not_owner", lang=self._resolve_lang(cq.from_user.id)), show_alert=True)
            if self._settings is not None:
                try:
                    await self._settings.set("web_url", "")
                except Exception as exc:
                    logger.warning("clear web_url failed: %s", exc)
            self._web_url = ""
            await self._safe_edit(
                cq, self._t("ui_weburl_cleared"), reply_markup=self._main_menu())
            await cq.answer()

        # ---------------- multi-source / multi-target routes ----------------
        # One task routes M sources to N targets. `/route` is the one-shot entry
        # point; the callbacks below drive the inline-keyboard wizard and share
        # every helper with it.
        @b.on_message(filters.command("route") & filters.private)
        @self._localized_handler
        async def _route_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /route from uid=%s", uid)
            if not uid:
                return
            try:
                await self._handle_route_command(message, uid)
            except Exception as exc:
                logger.exception("/route failed for uid=%s: %s", uid, exc)
                try:
                    await message.reply_text(
                        self._t("ui_route_failed", lang=self._resolve_lang(uid),
                                error=tg_detail(exc)),
                        reply_markup=self._rules_menu(),
                    )
                except Exception:
                    pass

        @b.on_callback_query(filters.regex(
            r"^(?:" + CB["route_src_list"] + r"|" + CB["route_tgt_list"] + r"):([st]):(.+)$"))
        @self._localized_handler
        async def _cb_route_open(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            lang = self._resolve_lang(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":", 2)
            role = parts[1] if len(parts) > 1 and parts[1] in ("s", "t") else "s"
            scope = parts[2] if len(parts) > 2 else ROUTE_SCOPE_NEW
            rule = None
            if scope != ROUTE_SCOPE_NEW:
                rule = await self._route_find_rule(uid, scope)
                if rule is None:
                    return await cq.answer(
                        self._t("ui_rule_not_found_or_not_owner_alert", lang=lang), show_alert=True)
            st = self._state(uid)
            await self._route_seed(uid, st, scope)
            await self._persist(uid, st)
            text, markup = self._route_render(st, role, self._route_open(uid, st, role), rule=rule)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^" + CB["route_fix_entry"] + r":([st]):(.+)$"))
        @self._localized_handler
        async def _cb_route_fix(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            lang = self._resolve_lang(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":", 2)
            role = parts[1] if len(parts) > 1 and parts[1] in ("s", "t") else "s"
            scope = parts[2] if len(parts) > 2 else self._route_scope(self._state(uid))
            st = self._state(uid)
            if self._route_scope(st) != scope:
                await self._route_seed(uid, st, scope)
            st.step = "route_edit_s" if role == "s" else "route_edit_t"
            await self._persist(uid, st)
            rule = await self._route_find_rule(uid, scope) if scope != ROUTE_SCOPE_NEW else None
            await self._safe_edit(
                cq, self._t("ui_route_edit_one_ask", lang=lang),
                reply_markup=self._route_keyboard(st, role, lang, rule))
            await cq.answer(self._t("ui_route_edit_one", lang=lang))

        @b.on_callback_query(filters.regex(r"^" + CB["route_save_lists"] + r":(.+)$"))
        @self._localized_handler
        async def _cb_route_save(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            lang = self._resolve_lang(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            scope = raw.split(":", 1)[1]
            st = self._state(uid)
            if self._route_scope(st) != scope:
                if scope != ROUTE_SCOPE_NEW and await self._route_find_rule(uid, scope) is None:
                    return await cq.answer(
                        self._t("ui_rule_not_found_or_not_owner_alert", lang=lang), show_alert=True)
                await self._route_seed(uid, st, scope)
            await self._route_save(cq, uid, st, lang)

        @b.on_callback_query(filters.regex(r"^" + CB["route_back"] + r":(.+)$"))
        @self._localized_handler
        async def _cb_route_back(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            lang = self._resolve_lang(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            scope = raw.split(":", 1)[1]
            await self._route_back(uid, self._state(uid), lang, cq=cq, scope=scope)

        # ---------------- command / FSM arbitration ----------------
        # Mounted at group -3, ahead of the command router (-2), the free-text
        # router (-1) and BotManager (1) — for EVERY command token the bot
        # answers, including the ones other modules own. A command is a statement
        # of intent, so it settles whatever prompt was pending before it renders:
        # without this a step armed on a previous screen stayed armed and the
        # caller's next message was stored as the answer to a question they had
        # already walked away from. The logic, the command list and the reasons
        # live in `fsm_arbiter` (tested there against real pyrogram exceptions).
        fsm_arbiter.register(b, self)

        # ---------------- free-text FSM router ----------------
        # Every command token is excluded here as well, and the list is the same
        # registry-derived one the arbiter registers: the command router runs at
        # group -2 and stops propagation, but a command must never be able to
        # fall into an FSM step (where it would be stored as input) if that
        # registration ever changes. A hand-copied list drifts; this one cannot.
        @b.on_message(
            filters.private & filters.text & ~filters.command(list(ARBITRATED_COMMANDS)),
            group=-1,
        )
        @self._localized_handler
        async def _text(_, message: Message):
            # One message at a time per user (see `_user_lock`): the step read,
            # the routing decision and the persist that follows it must not
            # interleave with another update for the same user, or two updates
            # act on the same step and the later write wins with stale data.
            uid = int(message.from_user.id) if message.from_user else 0
            async with self._user_lock(uid):
                await self._handle_free_text(message)

        @b.on_message(filters.private & filters.document, group=-2)
        @self._localized_handler
        async def _document(_, message: Message):
            """Receive a migration bundle file and restore it."""
            uid = message.from_user.id if message.from_user else 0
            step = ""
            try:
                step = self._state(uid).step or ""
            except Exception:
                pass
            if step != "migrate_await_file":
                # Not ours: raise the propagation signal unwrapped (see _text).
                message.continue_propagation()
            await self._route_document(message)

    # ------------------------------------------------------------------ #
    # error recording
    # ------------------------------------------------------------------ #
    async def _record_error(
        self, category: str, exc: BaseException, user_id: Optional[int] = None,
        session_id: Optional[str] = None, rule_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        """Store the *exact* Telegram error and broadcast it to the log service."""
        key, sev, recoverable = tg_error(exc)
        detail = tg_detail(exc)
        if self._error_log is not None:
            try:
                await self._error_log.record(
                    category=category, error_name=type(exc).__name__,
                    severity=sev, detail=detail, recoverable=recoverable,
                    user_id=user_id, session_id=session_id, rule_id=rule_id,
                    chat_id=chat_id,
                )
            except Exception:
                logger.debug("error_log write failed", exc_info=True)
        if self._metrics is not None and sev in (SEV_ERROR, SEV_FATAL):
            try:
                await self._metrics.bump(errors=1)
            except Exception:
                pass
        try:
            self._log.error("bot", category, f"{type(exc).__name__}: {detail}")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # dashboard bodies
    # ------------------------------------------------------------------ #
    async def _dashboard_body(self) -> str:
        s = self._pipeline.stats
        lines = [self._t("ui_stats_title"), ""]
        lines.append(f"📥 {self._t('ui_stat_processed')}: {s.processed}")
        lines.append(f"⚡ {self._t('ui_stat_forwarded')}: {s.forwarded}")
        lines.append(f"🧹 {self._t('ui_stat_filtered')}: {s.filtered}")
        lines.append(f"🤖 {self._t('ui_stat_rewritten')}: {s.rewritten}")
        lines.append(f"❌ {self._t('ui_stat_errors')}: {s.errors}")
        lines.append(f"⏱ {self._t('ui_stat_uptime')}: {int(time.time()) - s.started_at}s")
        if self._metrics is not None:
            try:
                lines.append("")
                lines.append(f"🗂 {self._t('ui_stat_total')}: {await self._metrics.total_str()}")
            except Exception:
                pass
        return "\n".join(lines)

    # -------------------------------------------------------------- web access
    @staticmethod
    def _detect_public_host() -> str:
        """Best-effort public host for the web dashboard.

        Used only when the operator has not configured ``web_url`` explicitly.
        ``localhost`` is useless in Telegram: the link is opened on the user's
        own device, so it must point at a reachable address of *this* server.
        Returns the first global (non-loopback / non-private) address seen on
        the host's interfaces; falls back to ``localhost`` only if none found.
        """
        try:
            import ipaddress
            import socket

            seen: list[str] = []
            for info in socket.getaddrinfo(socket.gethostname(), None):
                addr = str(info[4][0])
                if addr in seen:
                    continue
                seen.append(addr)
                try:
                    ip = ipaddress.ip_address(addr)
                except ValueError:
                    continue
                if not ip.is_global or ip.is_loopback or ip.is_link_local:
                    continue
                if ip.version == 6:
                    return f"[{ip}]"
                return str(ip)
        except Exception:
            pass
        return "localhost"

    def _web_base_url(self) -> str:
        if self._web_url:
            return self._web_url
        return f"http://{self._detect_public_host()}:8088"

    def _weburl_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [((self._btn("ui_weburl_set")), CB["weburl_set"])],
            [((self._btn("ui_weburl_clear")), "weburl_clear")],
            [((self._btn("ui_back")), CB["main"])],
        ])

    def _weburl_body(self) -> str:
        current = self._web_url or self._t("ui_weburl_none")
        return self._t("ui_weburl_body").replace("{cur}", current)

    async def _step_rename_rule(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
        rule.name = text.strip()
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_rule_detail(rule)
        await message.reply_text(self._t("ui_step_task_name_updated", name=rule.name), reply_markup=kbd)

    async def _step_set_topic(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
        clean_txt = text.strip().lstrip("#")
        try:
            tid = int(clean_txt)
            rule.topic_id = tid if tid > 0 else 0
        except ValueError:
            return await message.reply_text(self._t("ui_step_topic_invalid_error"))
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_rule_detail(rule)
        await message.reply_text(
            self._t(
                "ui_step_topic_saved",
                lang=self._lang_for(rule),
                topic=rule.topic_id or self._t("ui_none_label", lang=self._lang_for(rule)),
            ),
            reply_markup=kbd,
        )

    async def _step_set_delay(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
        try:
            # Parse first so a typo is still reported as invalid, then clamp the
            # number to a delay the workers can honour (0 .. 24h, finite).
            sec = clamp_delay_seconds(float(text.strip()))
            rule.delay_seconds = sec
        except ValueError:
            return await message.reply_text(self._t("ui_step_delay_invalid_error"))
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_delay_menu(rule)
        await message.reply_text(self._t("ui_step_delay_saved", delay=rule.delay_seconds), reply_markup=kbd)

    async def _step_set_every_n(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        """Free-text interval entry: whole numbers only, 1..1000."""
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
        raw = (text or "").strip()
        try:
            # `int()` and not `float()`: "2.5 messages" is not a thing, and a
            # silent truncation would be a cadence the user never chose.
            every_n = int(raw)
        except (TypeError, ValueError):
            return await message.reply_text(self._t("ui_step_every_n_invalid_error"))
        if not 1 <= every_n <= 1000:
            return await message.reply_text(self._t("ui_step_every_n_invalid_error"))
        rule.forward_every_n = every_n
        # The add-on flag stays the catalogue's own switch (a boolean), so the
        # grid and this screen cannot disagree about whether the feature is on.
        addons = rule.paid_addons
        addons["every_n"] = every_n > 1
        rule.paid_addons = addons
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_every_n_menu(rule)
        await message.reply_text(
            self._t("ui_step_every_n_saved", n=every_n), reply_markup=kbd
        )

    async def _step_add_bl(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
        new_words = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
        bl = list(rule.blacklist or [])
        for w in new_words:
            if w not in bl:
                bl.append(w)
        rule.blacklist = bl
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_blacklist_menu(rule)
        await message.reply_text(self._t("ui_step_blacklist_saved", count=len(new_words)), reply_markup=kbd)

    async def _step_add_wl(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
        new_words = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
        wl = list(rule.whitelist or [])
        for w in new_words:
            if w not in wl:
                wl.append(w)
        rule.whitelist = wl
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_whitelist_menu(rule)
        await message.reply_text(self._t("ui_step_whitelist_saved", count=len(new_words)), reply_markup=kbd)

    async def _step_set_wm(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
        rule.watermark = text.strip()
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_watermark_menu(rule)
        await message.reply_text(self._t("ui_step_watermark_saved", watermark=rule.watermark), reply_markup=kbd)

    async def _step_ai_prompt_name(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        name = (text or "").strip()
        if not name or len(name) > 80:
            return await message.reply_text(self._t("ui_ai_prompt_name_invalid"))
        st.buffer["ai_new_prompt_name"] = name
        st.step = f"ai_prompt_sys_{rule_id}"
        await self._persist(uid, st)
        kbd = self._kbd([[(self._t("ui_cancel_back"), f"r_ai_prompt:{rule_id}")]])
        await message.reply_text(self._t("ui_ai_prompt_enter_system_instruction", name=name), reply_markup=kbd)

    async def _step_ai_prompt_sys(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        sys_text = (text or "").strip()
        if not sys_text:
            return await message.reply_text(self._t("ui_ai_prompt_sys_empty_error"))
        name = st.buffer.get("ai_new_prompt_name") or f"Prompt_{int(time.time())}"
        st.step = ""
        st.buffer.pop("ai_new_prompt_name", None)
        tpl_id = None
        if self._prompts:
            try:
                tpl = await self._prompts.create_template(
                    name=name,
                    system_prompt=sys_text,
                    user_prompt_template="{text}",
                    target_language=self._resolve_lang(uid),
                    owner_user_id=uid,
                )
                tpl_id = tpl.id
            except Exception as exc:
                logger.warning("Failed to create prompt template: %s", exc)
        draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
        if not isinstance(draft, dict) or st.buffer.get("rule_id") != rule_id:
            rule = await self._owned_rule(rule_id, uid)
            if rule:
                draft = rule.to_draft()
                st.buffer["rule_id"] = rule_id
                st.buffer["draft"] = draft
        if draft and tpl_id:
            draft["prompt_id"] = tpl_id
            rule = await self._owned_rule(rule_id, uid)
            if rule:
                rule.prompt_id = tpl_id
                await self._save_owned_rule(rule, uid)
        await self._persist(uid, st)
        if draft:
            cfg_id = str(draft.get("ai_config_id") or "")
            prompt_id = str(draft.get("prompt_id") or "")
            pol = str(draft.get("ai_fallback_policy") or "DROP")
            tmo = draft.get("ai_timeout_seconds") or 15.0
            lines = [self._t("ui_ai_menu_title"), ""]
            if cfg_id:
                try:
                    cfg = await self._ai.get(cfg_id)
                    lines.append(f"🧠 {self._t('ui_ai_title')}: `{cfg.name}` [{self._provider_label(cfg.provider.value)}/{cfg.model}]" if cfg else f"🧠 {self._t('ui_ai_title')}: `{cfg_id[:8]}`")
                except Exception:
                    lines.append(f"🧠 {self._t('ui_ai_title')}: `{cfg_id[:8]}`")
            else:
                lines.append(self._t("ui_ai_cfg_status_off"))
            if prompt_id and self._prompts:
                try:
                    tpl = await self._prompts.get_template(prompt_id)
                    lines.append(f"📝 {self._t('ui_ai_prompt_label')}: `{tpl.name}` (v{tpl.current_version})" if tpl else f"📝 {self._t('ui_ai_prompt_label')}: `{prompt_id[:8]}`")
                except Exception:
                    lines.append(f"📝 {self._t('ui_ai_prompt_label')}: `{prompt_id[:8]}`")
            else:
                lines.append(f"📝 {self._t('ui_ai_prompt_label')}: {self._t('ui_ai_prompt_default')}")

            pol_desc = self._ai_policy_text(pol)
            lines += [
                f"⚠️ {self._t('ui_ai_policy_label')}: `{pol}`"
                + (f" ({pol_desc})" if pol_desc else ""),
                f"⏱ {self._t('ui_ai_timeout_label')}: {tmo}s",
            ]
            toggle_btn = (
                (self._btn('ui_ai_btn_disable'), f"r_aitoggle:{rule_id}:off")
                if cfg_id
                else (self._btn('ui_ai_btn_enable'), f"r_aitoggle:{rule_id}:on")
            )
            rows = [
                [toggle_btn],
                [(self._t("ui_ai_btn_pick_config"), f"r_ai_cfg:{rule_id}")],
                [(self._t("ui_ai_btn_pick_prompt"), f"r_ai_prompt:{rule_id}")],
                [(self._t("ui_ai_btn_change_policy"), f"r_ai_policy:{rule_id}")],
                [(self._t("ui_ai_btn_test_sample"), f"r_ai_test:{rule_id}")],
                [(self._t("ui_ai_btn_back_to_editor"), f"rd:{rule_id}")],
            ]
            await message.reply_text(f"{self._t('ui_ai_prompt_created_success', name=name)}\n\n" + "\n".join(lines), reply_markup=self._kbd(rows))
        else:
            await message.reply_text(self._t("ui_ai_prompt_created_success", name=name), reply_markup=self._main_menu())

    async def _step_weburl_input(
        self, message: Message, st: "UiState", text: str
    ) -> None:
        lang = self._lang_for(st)
        url = (text or "").strip()
        st.step = ""
        await self._persist(message.from_user.id, st)
        if not (url.startswith("http://") or url.startswith("https://")) or " " in url:
            return await message.reply_text(
                self._t("ui_weburl_invalid", lang=lang), reply_markup=self._cancel_kbd())
        url = url.rstrip("/")
        if self._settings is not None:
            try:
                await self._settings.set("web_url", url)
            except Exception as exc:
                logger.warning("save web_url failed: %s", exc)
                return await message.reply_text(
                    self._t("ui_internal_error", lang=lang), reply_markup=self._main_menu())
        self._web_url = url
        await message.reply_text(
            self._t("ui_weburl_saved", lang=lang).replace("{url}", url),
            reply_markup=self._main_menu())

    async def _step_client_api_id(self, message: Message, st: "UiState", text: str) -> None:
        clean = (text or "").strip()
        if not clean.isdigit() or int(clean) <= 0:
            return await message.reply_text(
                self._t("ui_step_api_id_prompt"),
                reply_markup=self._cancel_kbd(),
            )
        st.buffer["client_api_id"] = int(clean)
        st.step = "client_api_hash"
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_step_api_hash_prompt"),
            reply_markup=self._cancel_kbd(),
        )

    async def _step_client_api_hash(self, message: Message, st: "UiState", text: str) -> None:
        api_hash = (text or "").strip()
        uid = message.from_user.id
        if len(api_hash) != 32:
            return await message.reply_text(
                self._t("ui_step_api_hash_invalid"),
                reply_markup=self._cancel_kbd(),
            )
        api_id = int(st.buffer.get("client_api_id", 0))
        st.step = ""
        st.buffer.clear()
        await self._persist(uid, st)

        if self._credentials:
            try:
                owned = await self._credentials.list_by_owner(uid)
                for c in owned:
                    await self._credentials.delete(c.id)
                await self._credentials.create(
                    label=f"user_{uid}_cred",
                    api_id=api_id,
                    api_hash=api_hash,
                    is_default=True,
                    owner_user_id=uid,
                )
            except Exception as exc:
                logger.error("Failed saving user credentials: %s", exc)

        if self._pool:
            try:
                self._pool.register_credentials(f"user_{uid}", api_id, api_hash)
            except Exception:
                pass

        await message.reply_text(
            self._t("ui_step_custom_api_saved"),
            reply_markup=self._client_settings_menu(uid),
        )

    async def _step_admin_broadcast_text(self, message: Message, st: "UiState", text: str) -> None:
        uid = message.from_user.id
        if not self._is_admin(uid):
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_need_admin"))

        bcast_text = (message.text or "").strip()
        if not bcast_text:
            return await message.reply_text(self._t("ui_step_empty_text_error"), reply_markup=self._cancel_kbd())

        st.buffer["bcast_text"] = bcast_text
        st.step = ""
        await self._persist(uid, st)

        count = 0
        if self._users:
            try:
                count = await self._users.count_all()
            except Exception:
                pass

        preview_text = self._t("ui_step_broadcast_preview", text=bcast_text, count=count)
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton(self._t("ui_step_broadcast_btn_confirm"), callback_data=CB["adm_bcast_confirm"])],
            [InlineKeyboardButton(self._t("ui_cancel"), callback_data=CB["adm_bcast_cancel"])],
        ])
        await message.reply_text(preview_text, reply_markup=kbd)

    async def _step_admin_forcesub_channel(self, message: Message, st: "UiState", text: str) -> None:
        uid = message.from_user.id
        if not self._is_admin(uid):
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_need_admin"))

        channel = (text or "").strip()
        if not channel.startswith("@") and not channel.startswith("-100"):
            channel = "@" + channel.lstrip("@")

        now = int(time.time())
        if self._db:
            try:
                self._db.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('force_sub_channel', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (channel, now),
                )
                self._db.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('force_sub_enabled', '1', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (now,),
                )
            except Exception as exc:
                logger.error("Failed saving force_sub_channel: %s", exc)

        st.step = ""
        await self._persist(uid, st)
        text_panel, kbd_panel = await self._render_admin_panel(uid)
        await message.reply_text(self._t("ui_step_force_sub_channel_saved", channel=channel), reply_markup=kbd_panel)

    async def _step_admin_add_id(self, message: Message, st: "UiState", text: str) -> None:
        uid = message.from_user.id
        if not self._is_admin(uid):
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_need_admin"))

        clean = (text or "").strip()
        if not clean.isdigit() or int(clean) <= 0:
            return await message.reply_text(self._t("ui_step_invalid_user_id"), reply_markup=self._cancel_kbd())

        target_uid = int(clean)
        st.step = ""
        await self._persist(uid, st)

        if self._admin_service:
            try:
                self._admin_service.add_admin(target_uid, added_by=uid)
            except Exception as exc:
                return await message.reply_text(self._t("ui_step_admin_add_error", exc=exc))

        text_panel, kbd_panel = await self._render_admin_panel(uid)
        await message.reply_text(self._t("ui_step_admin_added", uid=target_uid), reply_markup=kbd_panel)

    async def _run_broadcast_job(self, bcast_id: str, admin_uid: int, text: str) -> None:
        recipients = []
        if self._db:
            try:
                rows = self._db.query_all("SELECT user_id FROM users WHERE status != 'suspended'")
                for r in rows or []:
                    recipients.append(int(r["user_id"]))
            except Exception as exc:
                logger.error("Failed querying users for broadcast: %s", exc)

        sent = 0
        failed = 0
        for uid in recipients:
            try:
                await self.bot.send_message(uid, text)
                sent += 1
            except Exception as exc:
                failed += 1
                exc_str = str(exc).lower()
                if "flood" in exc_str:
                    await asyncio.sleep(2.0)
            await asyncio.sleep(0.04)

        if self._db:
            try:
                self._db.execute(
                    "UPDATE broadcast_queue SET status='completed', sent_count=?, failed_count=?, updated_at=? WHERE id=?",
                    (sent, failed, int(time.time()), bcast_id),
                )
            except Exception:
                pass

        if admin_uid > 0:
            try:
                await self.bot.send_message(
                    admin_uid,
                    self._t("ui_admin_broadcast_done", lang=self._resolve_lang(admin_uid), sent=sent, failed=failed),
                )
            except Exception:
                pass

    async def _show_dashboard(self, cq: CallbackQuery, force_reset: bool = False) -> None:
        """Dashboard button: delivers secure Username, Password, and 1-tap Login button."""
        uid = cq.from_user.id
        first_name = getattr(cq.from_user, "first_name", "") or ""
        base = self._web_base_url()

        username = f"tg_{uid}"
        password = ""
        code = ""
        if self._accounts:
            try:
                username, password, _token = await self._accounts.get_or_create_credentials(
                    uid, display_name=first_name, force_reset=force_reset
                )
            except Exception as e:
                logger.error("Failed to provision credentials for %s: %s", uid, e)
            try:
                # The link carries a single-use code (10 minutes), never the
                # 7-day session token: a link lives in the chat history, so it
                # must stop working the moment it is used.
                code, _exp = self._accounts.issue_login_code(uid)
            except Exception as e:
                logger.error("Failed to mint a login code for %s: %s", uid, e)

        magic_link = f"{base}/?code={quote(code, safe='')}" if code else base

        lines = [
            self._t("ui_dashboard_body"),
            "",
            f"👤 {self._t('ui_webpass_username')}: <code>{username}</code>",
        ]
        if password and not password.startswith("("):
            lines.append(f"🔑 {self._t('ui_webpass_password')}: <code>{password}</code>")
        elif password:
            lines.append(f"🔑 {self._t('ui_webpass_password')}: <i>{password}</i>")
        lines.append(f"🌐 {self._t('ui_webpass_url')}: <code>{base}</code>")
        lines.append("")
        lines.append(self._t("ui_dashboard_tip"))

        text = "\n".join(lines)
        buttons = [
            [(self._btn("ui_dashboard_open"), magic_link)],
            [(self._btn("ui_webpass_reset_btn"), CB["webpass_reset"])],
            [(self._btn("ui_back"), CB["main"])],
        ]
        kbd = self._kbd(buttons)
        try:
            await self._safe_edit(cq, text, reply_markup=kbd, disable_web_page_preview=True)
        except Exception:
            try:
                await cq.message.reply_text(text, reply_markup=kbd, disable_web_page_preview=True)
            except Exception:
                pass
        try:
            self._log.info("bot", "system", f"web dashboard link and credentials issued to {uid}")
        except Exception:
            pass
        await cq.answer()

    async def _issue_web_credentials(self, cq: CallbackQuery) -> None:
        """Create / refresh the web account credentials for this Telegram user."""
        uid = cq.from_user.id
        if self._accounts is None:
            await cq.answer(self._t("ui_dashboard_offline"), show_alert=True)
            return
        try:
            from core.proto import pb
            resp = await self._accounts.IssueWebToken(
                pb.IssueWebTokenRequest(user_id=uid), None
            )
            if getattr(resp, "success", False) and resp.account:
                acc = resp.account
                body = (
                    self._t("ui_webpass_body") + "\n\n"
                    f"👤 {self._t('ui_webpass_username')}: `{acc.username}`\n"
                    f"🆔 {self._t('ui_webpass_userid')}: `{acc.user_id}`\n\n"
                    + self._t("ui_webpass_note")
                )
                await self._safe_edit(cq, body, reply_markup=self._main_menu())
                try:
                    self._log.info("bot", "system",
                                   f"web credentials issued to {uid} ({acc.username})")
                except Exception:
                    pass
                await cq.answer()
                return
            await cq.answer(resp.message or self._t("ui_webpass_fail"), show_alert=True)
        except Exception as exc:
            await cq.answer(str(exc)[:180], show_alert=True)

    async def _perf_body(self) -> str:
        s = self._pipeline.stats
        uptime = max(int(time.time()) - s.started_at, 1)
        rate = s.processed / uptime * 60
        lines = [self._t("ui_perf_title"), ""]
        lines.append(f"⚡ {self._t('ui_perf_rate')}: {rate:.1f}/min")
        lines.append(f"📥 {self._t('ui_stat_processed')}: {s.processed}")
        lines.append(f"❌ {self._t('ui_stat_errors')}: {s.errors}")
        try:
            live = len(self._pool.all_clients())
        except Exception:
            live = 0
        lines.append(f"📱 {self._t('ui_perf_sessions')}: {live}")
        if self._metrics is not None:
            try:
                hourly = await self._metrics.hourly(24)
                if hourly:
                    total = sum(h["forwarded"] for h in hourly)
                    lines.append(f"📈 {self._t('ui_perf_window')}: {total} "
                                 f"{self._t('ui_stat_forwarded').lower()}")
            except Exception:
                pass
        return "\n".join(lines)

    async def _debug_body(self) -> str:
        lines = [self._t("ui_debug_title"), ""]
        try:
            live = len(self._pool.all_clients())
        except Exception:
            live = 0
        lines.append(f"📱 {self._t('ui_perf_sessions')}: {live}")
        if self._dispatcher is not None:
            q = getattr(self._dispatcher, "queue_size", None)
            if q is not None:
                try:
                    lines.append(f"📦 {self._t('ui_debug_queue_depth')}: {q()}")
                except Exception:
                    pass
        if self._error_log is not None:
            try:
                since = int(time.time()) - 24 * 3600
                counts = await self._error_log.counts_by_severity(since)
                lines.append(f"⚠️ {self._t('ui_debug_errors_24h')}: {sum(counts.values())}")
            except Exception:
                pass
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # text routing (FSM)
    # ------------------------------------------------------------------ #
    async def _route_text(self, message: Message) -> None:
        uid = message.from_user.id
        st = self._state(uid)
        text = (message.text or "").strip()
        if not st.step:
            # Command-driven flows (e.g. /login via BotManager) own this user
            # right now — stay silent instead of spamming "use the menu".
            if self._login.has_pending(uid):
                return
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text_ob, kbd_ob = self._render_welcome_onboarding(uid, user=message.from_user)
                return await message.reply_text(text_ob, reply_markup=kbd_ob)
            return await message.reply_text(
                self._t("ui_use_menu"), reply_markup=self._main_menu()
            )

        if st.step.startswith("ar_"):
            if await self.ar_route_text(message, st, text):
                return
        if st.step.startswith("rename_rule_"):
            rule_id = st.step.split("rename_rule_", 1)[1]
            return await self._step_rename_rule(message, st, text, rule_id)
        if st.step.startswith("rge_edit_"):
            rule_id = st.step.split("rge_edit_", 1)[1]
            return await self._step_registry_edit(message, st, text, rule_id)
        if st.step.startswith("rge_search_"):
            rule_id = st.step.split("rge_search_", 1)[1]
            return await self._step_registry_search(message, st, text, rule_id)
        if st.step.startswith("bot_route_token_"):
            rule_id = st.step.split("bot_route_token_", 1)[1]
            return await self._step_bot_route_token(message, st, text, rule_id)
        if st.step.startswith("set_topic_"):
            rule_id = st.step.split("set_topic_", 1)[1]
            return await self._step_set_topic(message, st, text, rule_id)
        if st.step.startswith("set_delay_"):
            rule_id = st.step.split("set_delay_", 1)[1]
            return await self._step_set_delay(message, st, text, rule_id)
        if st.step.startswith("set_every_n_"):
            rule_id = st.step.split("set_every_n_", 1)[1]
            return await self._step_set_every_n(message, st, text, rule_id)
        if st.step.startswith("add_bl_"):
            rule_id = st.step.split("add_bl_", 1)[1]
            return await self._step_add_bl(message, st, text, rule_id)
        if st.step.startswith("add_wl_"):
            rule_id = st.step.split("add_wl_", 1)[1]
            return await self._step_add_wl(message, st, text, rule_id)
        if st.step.startswith("set_wm_"):
            rule_id = st.step.split("set_wm_", 1)[1]
            return await self._step_set_wm(message, st, text, rule_id)
        if st.step.startswith("ai_prompt_name_"):
            rule_id = st.step.split("ai_prompt_name_", 1)[1]
            return await self._step_ai_prompt_name(message, st, text, rule_id)
        if st.step.startswith("ai_prompt_sys_"):
            rule_id = st.step.split("ai_prompt_sys_", 1)[1]
            return await self._step_ai_prompt_sys(message, st, text, rule_id)

        handler = getattr(self, f"_step_{st.step}", None)
        if handler is None:
            st.step = ""
            await self._persist(uid, st)
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text_ob, kbd_ob = self._render_welcome_onboarding(uid, user=message.from_user)
                return await message.reply_text(text_ob, reply_markup=kbd_ob)
            return await message.reply_text(
                self._t("ui_use_menu"), reply_markup=self._main_menu()
            )
        await handler(message, st, text)

    async def _route_document(self, message: Message) -> None:
        """Handle a document sent during the migration-import flow."""
        uid = message.from_user.id
        st = self._state(uid)
        if self._backup is None:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_migrate_unavailable"), reply_markup=self._main_menu())

        doc = message.document
        if doc is None:
            return await message.reply_text(self._t("ui_migrate_need_file"))
        size_mb = (doc.file_size or 0) / (1024 * 1024)
        if size_mb > 20:
            return await message.reply_text(self._t("ui_migrate_file_too_big"))

        path = os.path.join(tempfile.gettempdir(), f"atf_import_{uid}_{doc.file_id[:12]}")
        try:
            await message.download(file_name=path)
            blob = await asyncio.to_thread(Path(path).read_text, encoding="utf-8", errors="replace")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        if is_encrypted_bundle(blob):
            # ask for the passphrase, keep the bundle in the FSM buffer
            st.buffer["migrate_blob"] = blob
            st.step = "migrate_import_passphrase"
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_migrate_ask_passphrase"),
                reply_markup=self._kbd([[(self._btn("ui_cancel"), CB["cancel"])]]))

        await self._finish_migration_import(message, st, blob, "")

    async def _finish_migration_import(
        self, message: Message, st: UiState, blob: str, passphrase: str,
    ) -> None:
        lang = self._lang_for(st)
        uid = message.from_user.id
        overwrite = bool(st.buffer.get("migrate_overwrite", False))
        try:
            text = decrypt_bundle(blob, passphrase)
            doc = deserialize_bundle(text)
        except ValueError as exc:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(self._t("ui_backup_import_error", error=exc))

        try:
            stats = await self._backup.restore_snapshot(
                doc, None, overwrite_existing=overwrite)
        except Exception as exc:
            logger.exception("migration import failed")
            await self._record_error("bot", exc, user_id=uid)
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_migrate_fail", lang=lang, error=tg_detail(exc)))

        st.step = ""
        st.buffer.pop("migrate_blob", None)
        await self._persist(uid, st)
        lines = [self._t("ui_migrate_imported", lang=lang), "─────────────────────"]
        total_in = total_sk = total_fl = 0
        for k, v in stats.items():
            total_in += v.get("imported", 0)
            total_sk += v.get("skipped", 0)
            total_fl += v.get("failed", 0)
            lines.append(f"• {k}: ✅{v.get('imported', 0)}"
                         + (f" ⏭{v.get('skipped', 0)}" if v.get("skipped") else "")
                         + (f" ❌{v.get('failed', 0)}" if v.get("failed") else ""))
        lines.append(f"\n**{self._t('ui_stat_total')}:** ✅ {total_in} · ⏭ {total_sk} · ❌ {total_fl}")
        try:
            self._log.info("bot", "backup",
                           f"migration snapshot imported by {uid}: {stats}")
        except Exception:
            pass
        await message.reply_text("\n".join(lines), reply_markup=self._main_menu())

    # ------------------------------------------------------------------ #
    # Step handlers (FSM)
    # ------------------------------------------------------------------ #
    async def _step_migrate_passphrase(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        """Passphrase for the snapshot being EXPORTED right now."""
        uid = message.from_user.id
        passphrase = (text or "").strip()
        if not passphrase:
            return await message.reply_text(self._t("ui_migrate_passphrase_required", lang=lang))
        st.step = ""
        await self._persist(uid, st)
        await self._do_migration_export_from_message(message, passphrase)

    async def _step_session_backup_passphrase(
        self, message: Message, st: UiState, text: str
    ) -> None:
        """Export one owned Telegram session as an authenticated encrypted bundle."""
        uid = message.from_user.id
        lang = self._lang_for(st)
        passphrase = (text or "").strip()
        session_id = str(st.buffer.get("backup_session_id") or "")
        if len(passphrase) < 12:
            return await message.reply_text(
                self._t("ui_migrate_passphrase_required", lang=lang)
            )
        st.step = ""
        st.buffer.pop("backup_session_id", None)
        await self._persist(uid, st)
        path = None
        try:
            sessions = await self._sessions.list_by_owner(uid) if self._sessions else []
            target = next((item for item in sessions if str(item.id) == session_id), None)
            if target is None:
                return await message.reply_text(
                    self._t("ui_rule_not_found_or_not_owner", lang=lang)
                )
            bundle = {
                "format": 1,
                "phone": target.phone_number,
                "session_string": target.session_string_encrypted,
                "user_id": target.user_id,
                "username": target.username,
                "first_name": target.first_name,
            }
            encrypted = encrypt_bundle(
                json.dumps(bundle, ensure_ascii=False, separators=(",", ":")),
                passphrase,
            )
            fd, path = tempfile.mkstemp(
                prefix=f"atf_session_{uid}_", suffix=".atf",
                dir=tempfile.gettempdir(),
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(encrypted)
            await self.bot.send_document(
                chat_id=message.chat.id,
                document=path,
                caption=self._t("ui_backup_done", phone=target.phone_number, lang=lang)
                + "\n"
                + self._t("ui_backup_cipher", lang=lang),
            )
            try:
                self._log.info("bot", "session", "encrypted session backup exported")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("encrypted session backup failed")
            await self._record_error("bot", exc, user_id=uid)
            await message.reply_text(
                self._t("ui_backup_fail", error=tg_detail(exc), lang=lang)
            )
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    logger.warning("unable to remove temporary session backup file")

    async def _step_migrate_import_passphrase(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        """Passphrase for an ENCRYPTED bundle that was just uploaded."""
        uid = message.from_user.id
        passphrase = (text or "").strip()
        blob = st.buffer.get("migrate_blob") or ""
        if not blob:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_migrate_need_file", lang=lang), reply_markup=self._migrate_menu())
        if not passphrase:
            return await message.reply_text(self._t("ui_migrate_passphrase_required", lang=lang))
        await self._finish_migration_import(message, st, blob, passphrase)

    async def _do_migration_export_from_message(self, message: Message, passphrase: str) -> None:
        """Export with a passphrase, invoked once the user has typed one."""
        uid = message.from_user.id
        if self._backup is None:
            return await message.reply_text(
                self._t("ui_migrate_unavailable"), reply_markup=self._migrate_menu())
        try:
            owner = uid if not self._is_admin(uid) else None
            doc = await self._backup.create_snapshot(owner)
            text = serialize_bundle(doc)
            fname = "atf-backup.enc"
            text = encrypt_bundle(text, passphrase)
            path = os.path.join(tempfile.gettempdir(), f"atf_{uid}_{fname}")
            await asyncio.to_thread(Path(path).write_text, text, encoding="utf-8")
            counts = doc.get("counts") or {}
            lines = [self._t("ui_migrate_exported"),
                     "🔒 AES-GCM " + self._t("ui_migrate_encrypted"),
                     "─────────────────────"]
            for k in ("sessions", "forward_rules", "filter_rules",
                      "ai_configs", "prompt_templates", "api_credentials"):
                if counts.get(k):
                    lines.append(f"• {k}: {counts[k]}")
            await self.bot.send_document(
                chat_id=message.chat.id, document=path, caption="\n".join(lines))
            os.remove(path)
            try:
                self._log.info("bot", "backup", "migration snapshot exported (encrypted)")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("migration export failed")
            await self._record_error("bot", exc, user_id=uid)
            await message.reply_text(self._t("ui_migrate_fail", error=tg_detail(exc)))

    async def _step_login_phone(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        uid = message.from_user.id
        raw_text = (text or "").strip()
        masked_input = (raw_text[:4] + "****" + raw_text[-4:]) if len(raw_text) > 8 else "***"
        logger.info("Processing login phone input from uid=%s: input=%s", uid, masked_input)
        try:
            self._log.info("bot", "login", f"received phone input from {uid}: {masked_input}")
        except Exception:
            pass

        if not self._login.valid_phone(raw_text):
            logger.warning("Invalid phone format from uid=%s: %s", uid, masked_input)
            await message.reply_text(
                self._t("ui_login_phone_bad", lang=lang), reply_markup=self._cancel_kbd())
            return

        phone = self._login.normalize_phone(raw_text)
        logger.info("Normalized phone for uid=%s: %s. Initiating login flow...", uid, phone[:4] + "****" + phone[-4:])

        try:
            result = await self._login.start(uid, phone)
        except Exception as exc:
            logger.exception("LoginFlow.start exception for uid=%s phone=%s: %s", uid, phone, exc)
            try:
                self._log.error("bot", "login", f"start exception for uid={uid}: {type(exc).__name__}: {exc}")
            except Exception:
                pass
            await message.reply_text(
                self._t("ui_login_code_error", lang=lang),
                reply_markup=self._cancel_kbd(),
            )
            return

        logger.info("LoginFlow.start returned result=%r for uid=%s", result, uid)

        if result != "send_code":
            code, _, detail = result.partition(":")
            err_msg = f"send_code failed for {phone}: {result}"
            logger.error(err_msg)
            try:
                self._log.error("bot", "login", err_msg)
            except Exception:
                pass

            # A code with no catalog entry gets the generic line, never the raw
            # key; the detail is internal (exception text) and stays in the log.
            fail_text = self._login_fail_text(code, detail, lang=lang)

            await message.reply_text(fail_text, reply_markup=self._cancel_kbd())
            return

        st.step = "login_code"
        st.login_phone = phone
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        st.buffer["code_digits"] = ""
        await self._persist(uid, st)
        logger.info("Login code sent successfully to %s. State updated to login_code for uid=%s", phone[:4] + "****", uid)
        try:
            self._log.info("bot", "login", f"code sent to {phone[:4]}****, awaiting code from {uid}")
        except Exception:
            pass
        await message.reply_text(self._login_code_text(st), reply_markup=self._code_keypad(st))

    async def _cb_keypad(self, cq: CallbackQuery) -> None:
        uid = cq.from_user.id if cq.from_user else 0
        st = self._state(uid)
        if st.step != "login_code":
            await cq.answer()
            return

        raw_data = cq.data or ""
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8", errors="ignore")
        action = str(raw_data)[2:]  # "1", "2", ... or "del"
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        digits = str(st.buffer.get("code_digits", "") or "")

        if action == "del":
            digits = digits[:-1]
            st.buffer["code_digits"] = digits
            await self._persist(uid, st)
            await cq.answer(f"{digits or self._t('ui_login_code_cleared')}")
            try:
                await self._safe_edit(cq, 
                    self._login_code_text(st),
                    reply_markup=self._code_keypad(st),
                )
            except Exception:
                pass
            return

        if action.isdigit():
            if len(digits) < 5:
                digits += str(action)
                st.buffer["code_digits"] = digits
                await self._persist(uid, st)
                await cq.answer(f"{digits}")

            if len(digits) < 5:
                try:
                    await self._safe_edit(cq, 
                        self._login_code_text(st),
                        reply_markup=self._code_keypad(st),
                    )
                except Exception:
                    pass
            else:
                try:
                    cur_lang = self._resolve_lang(uid)
                    await self._safe_edit(cq, 
                        self._t("ui_verifying_code", lang=cur_lang, code=' '.join(digits)),
                    )
                except Exception:
                    pass
                await self._submit_code_flow(cq.message, st, digits, uid=uid)

    async def _step_login_code(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        norm_text = (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        clean = re.sub(r"\D", "", norm_text)
        logger.info("Received login code input from uid=%s (raw_len=%d, clean_len=%d)",
                    uid, len(text), len(clean))
        if len(clean) not in (5, 6):
            cur_lang = self._resolve_lang(uid)
            await message.reply_text(
                self._t("ui_login_code_invalid", lang=cur_lang),
                reply_markup=self._code_keypad(st),
            )
            return
        await self._submit_code_flow(message, st, clean, uid=uid)

    async def _submit_code_flow(
        self, message: Message, st: UiState, clean_code: str, uid: Optional[int] = None
    ) -> None:
        lang = self._lang_for(st)
        if uid is None:
            uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        logger.info("Submitting login code for uid=%s (len=%d)", uid, len(clean_code))
        try:
            result = await self._login.submit_code(uid, clean_code)
        except Exception as exc:
            logger.exception("submit_code failed for uid=%s: %s", uid, exc)
            await message.reply_text(
                self._t("ui_login_code_verify_error", lang=lang),
                reply_markup=self._cancel_kbd(),
            )
            return

        logger.info("submit_code result for uid=%s: %r", uid, result)
        if result == "send_password":
            st.step = "login_password"
            if isinstance(st.buffer, dict):
                st.buffer.pop("code_digits", None)
            await self._persist(uid, st)
            await message.reply_text(
                self._t("ui_login_password", lang=lang), reply_markup=self._cancel_kbd())
            return
        await self._finish_login(message, st, result, uid=uid)

    async def _step_login_password(self, message: Message, st: UiState, text: str) -> None:
        uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        logger.info("Received 2FA password input from uid=%s", uid)
        try:
            result = await self._login.submit_password(uid, text)
        except Exception as exc:
            logger.exception("submit_password failed for uid=%s: %s", uid, exc)
            await message.reply_text(
                self._t("ui_login_2fa_verify_error", lang=self._lang_for(st)),
                reply_markup=self._cancel_kbd(),
            )
            return
        await self._finish_login(message, st, result, uid=uid)

    async def _finish_login(
        self, message: Message, st: UiState, result: str, uid: Optional[int] = None
    ) -> None:
        lang = self._lang_for(st)
        if uid is None:
            uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        phone = getattr(st, "login_phone", "") or ""

        if result in ("auth_expired", "code_expired"):
            st.step = "login_phone"
            if isinstance(st.buffer, dict):
                st.buffer.pop("code_digits", None)
            await self._persist(uid, st)
            retry_btn = self._kbd([[(self._btn("ui_login_retry", lang=lang), CB["login_retry"])]]) if phone else self._cancel_kbd()
            if result == "code_expired":
                text = self._t("ui_login_code_expired_prompt")
                await message.reply_text(text, reply_markup=retry_btn)
            else:
                await message.reply_text(self._t("ui_login_expired", lang=lang, phone=phone), reply_markup=retry_btn)
            return

        if result == "invalid_code":
            if isinstance(st.buffer, dict):
                st.buffer["code_digits"] = ""
            await self._persist(uid, st)
            await message.reply_text(
                self._t("ui_login_code_invalid", lang=lang),
                reply_markup=self._code_keypad(st),
            )
            return

        st.step = ""
        if isinstance(st.buffer, dict):
            st.buffer.clear()
        await self._persist(uid, st)
        if result.startswith("login_success"):
            try:
                self._log.info("bot", "login", f"login success for {phone}")
            except Exception:
                pass
            await message.reply_text(
                self._t("ui_login_done", lang=lang, phone=phone),
                reply_markup=self._main_menu(),
            )
        else:
            code, _, detail = result.partition(":")
            await message.reply_text(
                self._login_fail_text(code, detail, lang=lang),
                reply_markup=self._main_menu(),
            )

    # ---------------- credential FSM ----------------
    async def _step_cred_label(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.cred_label = text[:64]
        st.step = "cred_api_id"
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_cred_api_id", lang=lang), reply_markup=self._cancel_kbd())

    async def _step_cred_api_id(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        raw = text.strip()
        if not raw.isdigit():
            return await message.reply_text(
                self._t("ui_cred_api_id_bad", lang=lang), reply_markup=self._cancel_kbd())
        st.cred_api_id = raw
        st.step = "cred_api_hash"
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_cred_api_hash", lang=lang), reply_markup=self._cancel_kbd())

    async def _step_cred_api_hash(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.cred_api_hash = text.strip()
        st.step = ""
        if self._credentials is None:
            await self._persist(message.from_user.id, st)
            return await message.reply_text(
                self._t("ui_cred_fail", lang=lang,
                        error=self._t("ui_cred_vault_unavailable", lang=lang)),
                reply_markup=self._creds_menu(),
            )
        try:
            cred = await self._credentials.create(
                label=st.cred_label or "default",
                api_id=int(st.cred_api_id),
                api_hash=st.cred_api_hash,
                is_default=True,
                # v12: stamp the Telegram user who added this credential so the
                # owner-scoped queries (backup export, listing) can see it.
                owner_user_id=int(message.from_user.id or 0),
            )
            try:
                self._pool.register_credentials(cred.id, cred.api_id, cred.api_hash)
                self._pool.register_credentials("default", cred.api_id, cred.api_hash)
            except Exception:
                pass
            try:
                self._log.info("bot", "cred", f"credential {cred.id[:8]} added")
            except Exception:
                pass
            await self._persist(message.from_user.id, st)
            await message.reply_text(
                self._t("ui_cred_done", lang=lang, id=cred.id[:8]),
                reply_markup=self._creds_menu(),
            )
        except Exception as exc:
            await self._record_error("bot", exc, user_id=message.from_user.id)
            await message.reply_text(
                self._t("ui_cred_fail", lang=lang, error=tg_detail(exc)),
                reply_markup=self._creds_menu(),
            )
        finally:
            try:
                await message.delete()
            except Exception:
                pass  # api_hash is sensitive — best-effort cleanup only

    # ---------------- rule FSM ----------------
    async def _step_rule_source(self, message: Message, st: UiState, text: str) -> None:
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        matched_title = raw
        if isinstance(st.buffer, dict) and "chats" in st.buffer:
            for c in st.buffer["chats"]:
                if c["id"] == raw or (c.get("username") and c["username"].lower() == raw.lstrip("@").lower()):
                    matched_title = f"{c['emoji']} {c['title']}"
                    raw = c["id"]
                    break

        if matched_title == raw:
            try:
                _fsm_uid = self._scope_uid(uid)
                sessions = (await self._sessions.list_all() if _fsm_uid is None
                            else await self._sessions.list_by_owner(uid))
                if sessions:
                    client = self._pool.get(sessions[0].id)
                    if client and getattr(client, "is_connected", False):
                        chat_obj = await client.get_chat(raw)
                        c_id = getattr(chat_obj, "id", None)
                        if chat_obj and c_id is not None:
                            raw = str(c_id)
                            c_title = getattr(chat_obj, "title", None) or getattr(chat_obj, "first_name", None) or raw
                            matched_title = f"📢 {c_title}"
            except Exception:
                pass

        st.rule_source = raw
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        st.buffer["source_name"] = matched_title
        # Target-first flow: a pending target from the catalog makes this
        # free-text source answer the completion trigger, not another question.
        pending = st.buffer.get("pending_target") if isinstance(st.buffer, dict) else None
        if isinstance(pending, dict) and pending.get("id"):
            st.rule_target = str(pending.get("id"))
            target_title = str(pending.get("title") or st.rule_target)
            st.buffer.pop("pending_target", None)
            await self._finish_rule_creation(message, st, matched_title, target_title)
            return
        st.step = "rule_target"
        st.buffer["page"] = 0
        await self._persist(uid, st)

        picker_text, markup = self._render_chat_picker(st, role="target", page=0)
        await message.reply_text(picker_text, reply_markup=markup)

    async def _step_rule_target(self, message: Message, st: UiState, text: str) -> None:
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        matched_title = raw
        if isinstance(st.buffer, dict) and "chats" in st.buffer:
            for c in st.buffer["chats"]:
                if c["id"] == raw or (c.get("username") and c["username"].lower() == raw.lstrip("@").lower()):
                    matched_title = f"{c['emoji']} {c['title']}"
                    raw = c["id"]
                    break

        if matched_title == raw:
            try:
                _fsm_uid = self._scope_uid(uid)
                sessions = (await self._sessions.list_all() if _fsm_uid is None
                            else await self._sessions.list_by_owner(uid))
                if sessions:
                    client = self._pool.get(sessions[0].id)
                    if client and getattr(client, "is_connected", False):
                        chat_obj = await client.get_chat(raw)
                        c_id = getattr(chat_obj, "id", None)
                        if chat_obj and c_id is not None:
                            raw = str(c_id)
                            c_title = getattr(chat_obj, "title", None) or getattr(chat_obj, "first_name", None) or raw
                            matched_title = f"📢 {c_title}"
            except Exception:
                pass

        st.rule_target = raw
        source_title = st.buffer.get("source_name") or st.rule_source
        await self._finish_rule_creation(message, st, source_title, matched_title)

    async def _step_rule_edit_source(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        in_draft = bool(st.buffer.get("in_draft")) if isinstance(st.buffer, dict) else False
        draft_obj = st.buffer.get("draft") if (in_draft and isinstance(st.buffer, dict)) else None
        draft: Dict[str, Any] = draft_obj if isinstance(draft_obj, dict) else rule.to_draft()
        if in_draft and not isinstance(draft_obj, dict) and isinstance(st.buffer, dict):
            st.buffer["draft"] = draft

        if raw.lower() in cancel_aliases():
            st.step = "rule_editing_draft" if in_draft else ""
            if not in_draft and isinstance(st.buffer, dict):
                st.buffer = {}
            await self._persist(uid, st)
            if in_draft:
                text_out, kbd = self._render_draft_editor(rule, draft)
                await message.reply_text(text_out, reply_markup=kbd)
            else:
                text_out, kbd = self._render_rule_detail(rule)
                await message.reply_text(text_out, reply_markup=kbd)
            return

        # One resolution path for the whole bot: the buffered dialog list first,
        # then the connected client — the same `_resolve_chat_token` the route
        # wizard and `/route` use.
        raw, matched_title, _status = await self._resolve_chat_token(uid, st, raw)

        target_id = draft.get("target_chat_id") if in_draft else rule.target_chat_id
        if str(raw).strip() == str(target_id).strip():
            cancel_cb = f"re:{rule.id}" if in_draft else f"rd:{rule.id}"
            await message.reply_text(
                self._t("ui_loop_prevention_src", lang=lang),
                reply_markup=self._kbd([[(self._t("ui_cancel_back", lang=lang), cancel_cb)]]),
            )
            return

        if in_draft:
            draft["source_chat_id"] = str(raw)
            draft["source_name"] = matched_title
            st.step = "rule_editing_draft"
            await self._persist(uid, st)
            await message.reply_text(self._t("ui_step_src_draft_updated", title=matched_title))
            text_out, markup = self._render_draft_editor(rule, draft)
            await message.reply_text(text_out, reply_markup=markup)
        else:
            rule.source_chat_id = str(raw)
            rule.source_chat_name = matched_title
            expected_ver = st.buffer.get("version") if isinstance(st.buffer, dict) else None
            try:
                await self._save_owned_rule(rule, uid, expected_ver)
            except ConcurrencyError:
                await message.reply_text(self._t("ui_step_rule_concurrent_edit_error"), reply_markup=self._main_menu())
                return
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text(self._t("ui_step_src_rule_updated", title=matched_title))
            text_out, markup = self._render_rule_detail(rule)
            await message.reply_text(text_out, reply_markup=markup)

    async def _step_rule_edit_target(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        in_draft = bool(st.buffer.get("in_draft")) if isinstance(st.buffer, dict) else False
        draft_obj = st.buffer.get("draft") if (in_draft and isinstance(st.buffer, dict)) else None
        draft: Dict[str, Any] = draft_obj if isinstance(draft_obj, dict) else rule.to_draft()
        if in_draft and not isinstance(draft_obj, dict) and isinstance(st.buffer, dict):
            st.buffer["draft"] = draft

        if raw.lower() in cancel_aliases():
            st.step = "rule_editing_draft" if in_draft else ""
            if not in_draft and isinstance(st.buffer, dict):
                st.buffer = {}
            await self._persist(uid, st)
            if in_draft:
                text_out, kbd = self._render_draft_editor(rule, draft)
                await message.reply_text(text_out, reply_markup=kbd)
            else:
                text_out, kbd = self._render_rule_detail(rule)
                await message.reply_text(text_out, reply_markup=kbd)
            return

        # Same single resolution path as the source step.
        raw, matched_title, _status = await self._resolve_chat_token(uid, st, raw)

        source_id = draft.get("source_chat_id") if in_draft else rule.source_chat_id
        if str(raw).strip() == str(source_id).strip():
            cancel_cb = f"re:{rule.id}" if in_draft else f"rd:{rule.id}"
            await message.reply_text(
                self._t("ui_loop_prevention_tgt", lang=lang),
                reply_markup=self._kbd([[(self._t("ui_cancel_back", lang=lang), cancel_cb)]]),
            )
            return

        if in_draft:
            draft["target_chat_id"] = str(raw)
            draft["target_name"] = matched_title
            st.step = "rule_editing_draft"
            await self._persist(uid, st)
            await message.reply_text(self._t("ui_step_dst_draft_updated", title=matched_title))
            text_out, markup = self._render_draft_editor(rule, draft)
            await message.reply_text(text_out, reply_markup=markup)
        else:
            rule.target_chat_id = str(raw)
            rule.target_chat_name = matched_title
            expected_ver = st.buffer.get("version") if isinstance(st.buffer, dict) else None
            try:
                await self._save_owned_rule(rule, uid, expected_ver)
            except ConcurrencyError:
                await message.reply_text(self._t("ui_step_rule_concurrent_edit_error"), reply_markup=self._main_menu())
                return
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text(self._t("ui_step_dst_rule_updated", title=matched_title))
            text_out, markup = self._render_rule_detail(rule)
            await message.reply_text(text_out, reply_markup=markup)

    async def _step_rule_editing_draft(self, message: Message, st: UiState, text: str) -> None:
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))
        lang = self._resolve_lang(uid)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if raw.lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            text_out, kbd = self._render_rule_detail(rule)
            await message.reply_text(self._t("ui_step_edit_cancelled_draft_discarded"), reply_markup=kbd)
            return

        draft_obj = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
        draft: Dict[str, Any] = draft_obj if isinstance(draft_obj, dict) else rule.to_draft()
        if not isinstance(draft_obj, dict) and isinstance(st.buffer, dict):
            st.buffer["draft"] = draft

        text_out, kbd = self._render_draft_editor(rule, draft)
        await message.reply_text(self._t("ui_draft_hint", lang=lang), reply_markup=kbd)

    # ---------------- rule replace & header/footer FSM ----------------
    async def _step_rule_replace_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        lang = self._lang_for(st)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_replace_menu(rule)
            await message.reply_text(self._t("ui_step_replace_cancelled"), reply_markup=menu_kbd)
            return

        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        new_replacements, problems = parse_freeform(text)

        if not new_replacements:
            # A completely unusable input keeps the old behaviour: explain the
            # format. A partially unusable one below names the rejected lines.
            await message.reply_text(
                self._t("ui_replace_format_invalid", lang=lang),
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rtx:{rule_id}")]]),
            )
            return

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        current_reps = dict(rule.replacements)
        try:
            for src, value in new_replacements.items():
                current_reps = merge_entry(current_reps, entry_from_value(src, value))
        except ReplaceError as exc:
            await message.reply_text(
                self._t(error_key(exc.code), lang=lang, max=MAX_ENTRIES),
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rtx:{rule_id}")]]),
            )
            return
        rule.metadata["replacements"] = current_reps
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        deleted_label = self._t("ui_rep_word_deleted", lang=lang)
        added_list = "\n".join([
            "• " + describe_entry(entry_from_value(k, v), deleted_label)
            for k, v in new_replacements.items()
        ])
        await message.reply_text(self._t("ui_replace_added", lang=lang, added_list=added_list))
        if problems:
            rejected = "\n".join(
                f"• `{src}` — {self._t(error_key(code), lang=lang)}"
                for src, code in problems
            )
            await message.reply_text(
                self._t("ui_rep_rejected", lang=lang) + "\n" + rejected
            )
        menu_text, menu_kbd = self._render_replace_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    async def _step_rule_header_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_header_footer_menu(rule)
            await message.reply_text(self._t("ui_step_header_cancelled"), reply_markup=menu_kbd)
            return

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["header"] = text.strip()
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(self._t("ui_step_header_saved"))
        menu_text, menu_kbd = self._render_header_footer_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    async def _step_rule_footer_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_header_footer_menu(rule)
            await message.reply_text(self._t("ui_step_footer_cancelled"), reply_markup=menu_kbd)
            return

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["footer"] = text.strip()
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(self._t("ui_step_footer_saved"))
        menu_text, menu_kbd = self._render_header_footer_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    async def _step_rule_clean_words_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_cleaner_menu(rule)
            await message.reply_text(self._t("ui_cancelled"), reply_markup=menu_kbd)
            return

        words = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["clean_words"] = words
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(self._t("ui_step_clean_words_saved", count=len(words)))
        menu_text, menu_kbd = self._render_cleaner_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    async def _step_rule_max_edit_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        clean = text.strip()
        if clean.lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_max_time_edit_menu(rule)
            await message.reply_text(self._t("ui_cancelled"), reply_markup=menu_kbd)
            return

        if not clean.isdigit():
            await message.reply_text(
                self._t("ui_step_seconds_integer_error"),
                reply_markup=self._cancel_kbd(),
            )
            return

        val = int(clean)
        rule.max_time_edit = val
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(self._t("ui_step_mte_saved", val=val))
        menu_text, menu_kbd = self._render_max_time_edit_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    # ---------------- smart routing FSM steps ----------------
    async def _step_smart_set_intermediate(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        lang = self._resolve_lang(uid)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        raw_input = text.strip()
        if raw_input.lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text(self._t("ui_step_interm_cancelled"), reply_markup=m_kbd)
            return

        # Loop prevention check
        norm_input = raw_input.replace("https://t.me/", "").replace("t.me/", "")
        if (raw_input == str(rule.source_chat_id) or norm_input == str(rule.source_chat_name or "").lstrip("@") or
                raw_input == str(rule.target_chat_id) or norm_input == str(rule.target_chat_name or "").lstrip("@")):
            await message.reply_text(
                self._t("ui_loop_detected_title", lang=lang),
                reply_markup=self._kbd([[(self._t("ui_cancel"), f"rsm:{rule.id}")]]),
            )
            return

        # Resolve peer info if client available
        resolved_name = raw_input
        try:
            clients = self._pool.all_clients()
            client = next(iter(clients.values()), None) if clients else None
            if client and hasattr(client, "is_connected") and client.is_connected:
                peer = raw_input
                if peer.startswith("https://t.me/+") or peer.startswith("t.me/+"):
                    pass
                else:
                    if peer.startswith("@"):
                        chat = await client.get_chat(peer)
                    elif peer.startswith("-100") or (peer.startswith("-") and peer[1:].isdigit()):
                        chat = await client.get_chat(int(peer))
                    else:
                        chat = await client.get_chat(peer)
                    if chat:
                        raw_input = str(chat.id)
                        resolved_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id)
        except Exception:
            pass

        rule.intermediate_channel_id = str(raw_input)
        rule.intermediate_channel_name = str(resolved_name)
        rule.use_intermediate = True
        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["intermediate_channel_id"] = str(raw_input)
        rule.metadata["intermediate_channel_name"] = str(resolved_name)
        rule.metadata["use_intermediate"] = True
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(self._t("ui_smart_mid_channel", lang=lang, mid=resolved_name))
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_header(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text(self._t("ui_step_custom_hdr_cancelled"), reply_markup=m_kbd)
            return

        rule.custom_header = text.strip()
        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["custom_header"] = text.strip()
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(self._t("ui_step_custom_copy_header_saved"))
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_keywords(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text(self._t("ui_step_keywords_cancelled"), reply_markup=m_kbd)
            return

        raw = text.strip()
        if not isinstance(rule.detection_criteria, dict):
            rule.detection_criteria = {}

        if raw.lower() in _input_aliases("ui_input_clear_aliases"):
            rule.detection_criteria.pop("text_contains", None)
            rule.detection_criteria.pop("keywords", None)
            ans = self._t("ui_step_keywords_removed")
        else:
            kws = [k.strip() for k in raw.replace("،", ",").split(",") if k.strip()]
            rule.detection_criteria["text_contains"] = kws
            rule.detection_criteria["keywords"] = kws
            ans = self._t("ui_step_keywords_saved", count=len(kws), keywords=", ".join(f"`{k}`" for k in kws))

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["detection_criteria"] = rule.detection_criteria
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(ans)
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_brand_origins(self, message: Message, st: UiState, text: str) -> None:
        """Set the forward-origin filter: channel names (or ids / @usernames).

        Matched against Telegram's "Forwarded from <channel>" header — the origin
        channel, not text inside the message body.
        """
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text(self._t("ui_cancel"), reply_markup=m_kbd)
            return

        raw = text.strip()
        cfg = branding_policy.read_config(rule)
        cleared = raw.casefold() in BRAND_CLEAR_WORDS
        if cleared:
            cfg["origin_titles"] = []
            cfg["origin_ids"] = []
            ans = self._t("ui_brand_cleared")
        else:
            titles: List[str] = []
            ids: List[str] = []
            for token in raw.replace("،", ",").replace("\n", ",").split(","):
                item = token.strip()
                if not item:
                    continue
                looks_like_ref = item.startswith("@") or item.startswith("-") or item.lstrip("-").isdigit()
                (ids if looks_like_ref else titles).append(item)
            cfg["origin_titles"] = titles
            cfg["origin_ids"] = ids
            values = ", ".join(f"`{v}`" for v in (titles + ids))
            ans = self._t("ui_brand_origins_saved", value=values)
        cfg["triggers_enabled"] = bool(cfg["origin_titles"] or cfg["origin_ids"] or cfg["keywords"])
        branding_policy.write_config(rule, cfg)
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(ans, reply_markup=self._main_menu())
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_brand_words(self, message: Message, st: UiState, text: str) -> None:
        """Set the word list that must appear in the message body for the hop."""
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text(self._t("ui_cancel"), reply_markup=m_kbd)
            return

        raw = text.strip()
        cfg = branding_policy.read_config(rule)
        if raw.casefold() in BRAND_CLEAR_WORDS:
            cfg["keywords"] = []
            ans = self._t("ui_brand_cleared")
        else:
            words = [w.strip() for w in raw.replace("،", ",").replace("\n", ",").split(",") if w.strip()]
            cfg["keywords"] = words
            ans = self._t("ui_brand_words_saved", value=", ".join(f"`{w}`" for w in words))
        cfg["triggers_enabled"] = bool(cfg["origin_titles"] or cfg["origin_ids"] or cfg["keywords"])
        branding_policy.write_config(rule, cfg)
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(ans, reply_markup=self._main_menu())
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_regex(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        lang = self._resolve_lang(uid)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text(self._t("ui_step_regex_cancelled"), reply_markup=m_kbd)
            return

        raw = text.strip()
        if not isinstance(rule.detection_criteria, dict):
            rule.detection_criteria = {}

        if raw.lower() in _input_aliases("ui_input_clear_aliases"):
            rule.detection_criteria.pop("regex_pattern", None)
            rule.detection_criteria.pop("text_regex", None)
            ans = self._t("ui_step_regex_removed")
        else:
            import re
            try:
                re.compile(raw)
            except re.error as exc:
                await message.reply_text(self._t("ui_smart_regex_invalid", lang=lang, error=str(exc)))
                return
            rule.detection_criteria["regex_pattern"] = raw
            rule.detection_criteria["text_regex"] = raw
            ans = self._t("ui_step_regex_saved", raw=raw)

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["detection_criteria"] = rule.detection_criteria
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(ans)
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_test_rule(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text(self._t("ui_rule_not_found"), reply_markup=self._main_menu())
            return

        if text.strip().lower() in cancel_aliases():
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text(self._t("ui_step_sim_cancelled"), reply_markup=m_kbd)
            return

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        from ...domain.services import SmartRoutingEngine
        from ...domain.entities import MessagePayload

        # Build simulated payload
        payload = MessagePayload(
            message_id=999999,
            chat_id=str(rule.source_chat_id),
            text=text.strip(),
            date=int(time.time()),
        )

        # Check if forwarded message attributes were present in user's test message
        f_origin = getattr(message, "forward_origin", None)
        f_chat = getattr(message, "forward_from_chat", None)
        if f_origin:
            chat_obj = getattr(f_origin, "chat", None)
            payload.forward_origin = {
                "type": getattr(f_origin, "type", "channel"),
                "from_chat_id": str(chat_obj.id) if chat_obj else "",
                "from_chat_title": getattr(chat_obj, "title", ""),
                "sender_name": getattr(f_origin, "sender_user_name", ""),
            }
        elif f_chat:
            payload.forward_origin = {
                "type": "channel",
                "from_chat_id": str(f_chat.id),
                "from_chat_title": getattr(f_chat, "title", ""),
            }

        engine = SmartRoutingEngine()
        decision, route_reason = engine.decide_route(payload, rule)
        matched, reason = engine.evaluate_rule(payload, rule)
        is_vip, vip_reason = engine.is_vip_origin(payload, rule)
        detected_cat = "VIP" if is_vip else ("NORMAL" if not payload.forward_origin else "UNKNOWN_ORIGIN")
        cat_labels = {"ALL": self._t("ui_cat_all"), "VIP": self._t("ui_cat_vip"), "NORMAL": self._t("ui_cat_normal"), "UNKNOWN_ORIGIN": "❓"}

        crit = getattr(rule, "detection_criteria", {}) or {}
        m_mode = (crit.get("match_mode") or "ANY").upper()
        kws = crit.get("text_contains") or crit.get("keywords") or []
        rx = crit.get("regex_pattern") or crit.get("text_regex") or ""

        route_label = {
            RoutingDecision.BRANDING_VIA_C: self._t("ui_sim_decision_c"),
            RoutingDecision.DIRECT_COPY: self._t("ui_sim_decision_copy"),
            RoutingDecision.DIRECT_FORWARD: self._t("ui_sim_decision_fwd"),
            RoutingDecision.CUSTOM_HEADER_COPY: self._t("ui_sim_decision_custom_hdr"),
            RoutingDecision.DROP: self._t("ui_sim_decision_drop"),
        }.get(decision, str(decision))

        action_lines = []
        if matched and decision != RoutingDecision.DROP:
            if decision == RoutingDecision.BRANDING_VIA_C:
                action_lines.append(self._t("ui_sim_step_c1", target=rule.intermediate_channel_name or rule.intermediate_channel_id))
                action_lines.append(self._t("ui_sim_step_c2", target=rule.target_chat_name or rule.target_chat_id))
            elif decision == RoutingDecision.DIRECT_FORWARD:
                action_lines.append(self._t("ui_sim_step_fwd", target=rule.target_chat_name or rule.target_chat_id))
            else:
                action_lines.append(self._t("ui_sim_step_copy", target=rule.target_chat_name or rule.target_chat_id))
        else:
            action_lines.append(self._t("ui_sim_step_none"))

        match_status_text = self._t("ui_sim_match_ok") if matched else self._t("ui_sim_match_fail")
        result_text = (
            self._t("ui_sim_dry_run_title") +
            "────────────────────\n"
            + self._t("ui_sim_task_line", task=rule.id[:8], source=rule.source_chat_name or rule.source_chat_id, target=rule.target_chat_name or rule.target_chat_id)
            + self._t("ui_sim_match_summary", match=match_status_text, cat=cat_labels.get(detected_cat, detected_cat), route=route_label)
            + self._t("ui_sim_analysis", mode=self._logic_word(m_mode), kws=', '.join(kws) if kws else self._t("ui_clean_not_set"), rx=rx if rx else self._t("ui_clean_not_set"), reason=reason_label(route_reason or reason, self._t))
            + "\n".join(action_lines) + "\n\n"
            + self._t("ui_sim_footer_notes")
        )
        await message.reply_text(result_text)
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    # ---------------- filter FSM ----------------
    async def _step_filter_name(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.step = "filter_blacklist"
        st.buffer["name"] = text.strip()
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_filter_blacklist", lang=lang), reply_markup=self._cancel_kbd())

    async def _step_filter_blacklist(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.step = ""
        blacklist = [w.strip() for w in text.replace("،", ",").split(",") if w.strip()]
        try:
            fr = FilterRule(
                name=st.buffer.get("name", "filter"), blacklist_keywords=blacklist,
                # v12.1: stamp the creator so owner-scoped reads can see it.
                owner_user_id=int(message.from_user.id or 0) if message.from_user else 0)
            await self._filters.create(fr)
            try:
                self._log.info(
                    "bot", "filter",
                    f"filter {fr.name} created with {len(blacklist)} blacklist words")
            except Exception:
                pass
            await self._persist(message.from_user.id, st)
            await message.reply_text(
                self._t("ui_filter_done", lang=lang, name=fr.name), reply_markup=self._filters_menu())
        except Exception as exc:
            await self._record_error("bot", exc, user_id=message.from_user.id)
            await message.reply_text(
                self._t("ui_filter_fail", lang=lang, error=tg_detail(exc)),
                reply_markup=self._filters_menu(),
            )

    # ---------------- AI config FSM ----------------
    async def _step_ai_name(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.step = "ai_provider"
        st.ai_name = text.strip()
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_ai_provider", lang=lang), reply_markup=self._provider_picker())

    async def _step_ai_provider(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        provider = text.strip().lower()
        if provider not in {p.value for p in AIProviderType}:
            return await message.reply_text(
                self._t("ui_ai_provider_bad", lang=lang), reply_markup=self._cancel_kbd())
        st.ai_provider = provider
        st.step = "ai_base_url"
        from core.infrastructure.ai.providers import OpenAICompatibleProvider
        default_url = OpenAICompatibleProvider.default_base_urls.get(provider, "")
        st.buffer["default_base_url"] = default_url
        prompt_text = self._t("ui_step_prompt_server_url", provider=self._provider_label(provider), default_url=default_url or self._t("ui_none_manual"))
        kbd = self._kbd([
            [(self._t("ui_btn_use_default_host"), "ai_url_default")],
            [(self._btn("ui_cancel", lang=lang), CB["cancel"])],
        ])
        await self._persist(message.from_user.id, st)
        await message.reply_text(prompt_text, reply_markup=kbd)

    async def _step_ai_base_url(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        url = text.strip()
        if url.lower() in _input_aliases("ui_input_default_aliases"):
            url = str(st.buffer.get("default_base_url", ""))
        elif not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url
        problem = self._endpoint_problem(url)
        if problem:
            # Stay on this step: the user is one message away from a working one.
            kbd = self._kbd([
                [(self._t("ui_btn_use_default_host"), "ai_url_default")],
                [(self._btn("ui_cancel", lang=lang), CB["cancel"])],
            ])
            await self._persist(message.from_user.id, st)
            await message.reply_text(
                self._t("ui_ai_endpoint_blocked", lang=lang), reply_markup=kbd)
            return
        st.ai_base_url = url
        st.step = "ai_model"
        kb = self._model_picker(st, st.ai_provider)
        await self._persist(message.from_user.id, st)
        msg_text = (
            self._t("ui_step_host_saved_banner", url=url or "default")
            + self._t("ui_ai_model", lang=lang)
            + self._t("ui_prompt_select_model_or_type")
        )
        if kb is not None:
            await message.reply_text(msg_text, reply_markup=kb)
        else:
            await message.reply_text(msg_text, reply_markup=self._cancel_kbd())

    async def _step_ai_model(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.ai_model = text.strip()
        st.step = "ai_api_key"
        await self._persist(message.from_user.id, st)
        await message.reply_text(self._t("ui_ai_key", lang=lang), reply_markup=self._cancel_kbd())

    async def _step_ai_api_key(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.step = ""
        user_id = message.from_user.id
        try:
            cfg = AIConfig(
                name=st.ai_name,
                provider=AIProviderType(st.ai_provider),
                model=st.ai_model,
                base_url=st.ai_base_url or "",
                api_key=text.strip(),
                # v12.1: stamp the creator so owner-scoped reads can see it.
                owner_user_id=int(user_id or 0),
            )
            created = await self._ai.create(cfg)
            try:
                self._log.info(
                    "bot", "ai", f"AI config {cfg.name} created ({cfg.provider.value})")
            except Exception:
                pass
            await self._persist(user_id, st)

            # Immediately test health
            ok, reply_or_err, latency = await self._ai.health_check(created.id)
            health_info = {
                "ok": ok,
                "latency_ms": latency,
                "reply": reply_or_err if ok else "",
                "error": reply_or_err if not ok else "",
            }
            detail_text, detail_kbd = self._render_ai_detail(created, health_info=health_info)
            banner = self._t("ui_step_ai_registered_tested") if ok else self._t("ui_step_ai_saved_test_failed")
            await message.reply_text(banner + detail_text, reply_markup=detail_kbd)
        except Exception as exc:
            await self._record_error("ai", exc, user_id=user_id)
            cfgs = await (self._ai.list_all() if self._is_admin(user_id) else self._ai.list_by_owner(user_id))
            if "endpoint refused" in str(exc):
                # The core refused the URL: the generic failure banner would
                # hide the one thing the user has to change.
                await message.reply_text(
                    self._t("ui_ai_endpoint_blocked", lang=lang),
                    reply_markup=self._ai_menu(cfgs))
                return
            await message.reply_text(
                self._t("ui_ai_fail", lang=lang, error=tg_detail(exc)),
                reply_markup=self._ai_menu(cfgs))
        finally:
            try:
                await message.delete()
            except Exception:
                pass  # api key is sensitive — best-effort cleanup

    async def _step_ai_edit_host(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        aid = str(st.buffer.get("ai_edit_id", ""))
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        cfg = await self._ai.get_owned(aid, self._scope_uid(message.from_user.id if message.from_user else 0)) if aid else None
        _step_uid = message.from_user.id if message.from_user else 0
        if not cfg:
            cfgs = await (self._ai.list_all() if self._is_admin(_step_uid) else self._ai.list_by_owner(_step_uid))
            await message.reply_text(self._t("ui_ai_config_not_found"), reply_markup=self._ai_menu(cfgs))
            return
        new_url = text.strip()
        if new_url.lower() in _input_aliases("ui_input_default_aliases"):
            new_url = ""
        elif not new_url.startswith("http://") and not new_url.startswith("https://"):
            new_url = "http://" + new_url
        if self._endpoint_problem(new_url):
            await message.reply_text(self._t("ui_ai_endpoint_blocked", lang=lang))
            text_prev, kbd_prev = self._render_ai_detail(cfg)
            await message.reply_text(text_prev, reply_markup=kbd_prev)
            return
        cfg.base_url = new_url
        try:
            await self._ai.update_owned(cfg, self._scope_uid(message.from_user.id if message.from_user else 0))
        except PermissionError:
            return await message.reply_text(self._t("ui_need_admin", lang=lang))
        except ValueError:
            # The use case applies the same guard; say so instead of a traceback.
            return await message.reply_text(self._t("ui_ai_endpoint_blocked", lang=lang))
        ok, reply_or_err, latency = await self._ai.health_check_owned(cfg.id, self._scope_uid(message.from_user.id if message.from_user else 0))
        health_info = {
            "ok": ok,
            "latency_ms": latency,
            "reply": reply_or_err if ok else "",
            "error": reply_or_err if not ok else "",
        }
        detail_text, detail_kbd = self._render_ai_detail(cfg, health_info=health_info)
        await message.reply_text(self._t("ui_step_host_updated") + "\n\n" + detail_text, reply_markup=detail_kbd)

    async def _step_ai_edit_model(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        aid = str(st.buffer.get("ai_edit_id", ""))
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        cfg = await self._ai.get_owned(aid, self._scope_uid(message.from_user.id if message.from_user else 0)) if aid else None
        _step_uid = message.from_user.id if message.from_user else 0
        if not cfg:
            cfgs = await (self._ai.list_all() if self._is_admin(_step_uid) else self._ai.list_by_owner(_step_uid))
            await message.reply_text(self._t("ui_ai_config_not_found"), reply_markup=self._ai_menu(cfgs))
            return
        cfg.model = text.strip()
        try:
            await self._ai.update_owned(cfg, self._scope_uid(message.from_user.id if message.from_user else 0))
        except PermissionError:
            return await message.reply_text(self._t("ui_need_admin", lang=lang))
        ok, reply_or_err, latency = await self._ai.health_check_owned(cfg.id, self._scope_uid(message.from_user.id if message.from_user else 0))
        health_info = {
            "ok": ok,
            "latency_ms": latency,
            "reply": reply_or_err if ok else "",
            "error": reply_or_err if not ok else "",
        }
        detail_text, detail_kbd = self._render_ai_detail(cfg, health_info=health_info)
        await message.reply_text(self._t("ui_step_model_updated") + "\n\n" + detail_text, reply_markup=detail_kbd)

    async def _step_ai_edit_key(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        aid = str(st.buffer.get("ai_edit_id", ""))
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        try:
            await message.delete()
        except Exception:
            pass
        cfg = await self._ai.get_owned(aid, self._scope_uid(message.from_user.id if message.from_user else 0)) if aid else None
        _step_uid = message.from_user.id if message.from_user else 0
        if not cfg:
            cfgs = await (self._ai.list_all() if self._is_admin(_step_uid) else self._ai.list_by_owner(_step_uid))
            await message.reply_text(self._t("ui_ai_config_not_found"), reply_markup=self._ai_menu(cfgs))
            return
        cfg.api_key = text.strip()
        try:
            await self._ai.update_owned(cfg, self._scope_uid(message.from_user.id if message.from_user else 0))
        except PermissionError:
            return await message.reply_text(self._t("ui_need_admin", lang=lang))
        ok, reply_or_err, latency = await self._ai.health_check_owned(cfg.id, self._scope_uid(message.from_user.id if message.from_user else 0))
        health_info = {
            "ok": ok,
            "latency_ms": latency,
            "reply": reply_or_err if ok else "",
            "error": reply_or_err if not ok else "",
        }
        detail_text, detail_kbd = self._render_ai_detail(cfg, health_info=health_info)
        await message.reply_text(self._t("ui_step_api_key_updated") + "\n\n" + detail_text, reply_markup=detail_kbd)

    async def _step_pv_persona_input(self, message: Message, st: UiState, text: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        st.step = ""
        st.buffer = {}
        await self._persist(uid, st)
        resp = getattr(self, "_pv_responder", None)
        if not resp:
            await message.reply_text(self._t("ui_step_pv_module_not_found"), reply_markup=self._main_menu())
            return
        lang = await self._get_user_lang(uid)
        if text.strip().lower() in cancel_aliases():
            text_out, kbd = self._render_pv_menu(uid, lang=lang)
            await message.reply_text(self._t("ui_pv_prompt_cancelled"), reply_markup=kbd)
            return

        cfg = resp.get_config_for_user(uid)
        cfg.persona_prompt = text.strip()
        resp.save_config_for_user(uid, cfg)
        await message.reply_text(self._t("ui_step_pv_prompt_saved"))
        text_out, kbd = self._render_pv_menu(uid, lang=lang)
        await message.reply_text(text_out, reply_markup=kbd)

    async def _step_pv_test_input(self, message: Message, st: UiState, text: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        st.step = ""
        st.buffer = {}
        await self._persist(uid, st)
        resp = getattr(self, "_pv_responder", None)
        if not resp:
            await message.reply_text(self._t("ui_step_pv_module_not_found"), reply_markup=self._main_menu())
            return
        lang = await self._get_user_lang(uid)
        if text.strip().lower() in cancel_aliases():
            text_out, kbd = self._render_pv_menu(uid, lang=lang)
            await message.reply_text(self._t("ui_pv_test_cancelled"), reply_markup=kbd)
            return

        cfg = resp.get_config_for_user(uid)
        ai_cfg = await resp.get_active_ai_config(uid)
        if not ai_cfg:
            text_out, kbd = self._render_pv_menu(uid, lang=lang)
            await message.reply_text(self._t("ui_step_pv_no_active_ai_error"), reply_markup=kbd)
            return

        wait_msg = await message.reply_text(self._t("ui_step_pv_simulating_typing"))
        try:
            from core.infrastructure.ai.providers import OpenAICompatibleProvider, AIProviderFactory
            prov_key = ai_cfg.provider.value if hasattr(ai_cfg.provider, "value") else str(ai_cfg.provider)
            factory = resp._ai_factory or AIProviderFactory()
            provider_cls = factory._registry.get(prov_key, OpenAICompatibleProvider)
            provider = provider_cls()

            messages = [
                {"role": "system", "content": cfg.persona_prompt},
                {"role": "user", "content": text.strip()},
            ]
            if hasattr(provider, "chat_complete"):
                reply_text = await provider.chat_complete(ai_cfg, messages, temperature=0.7)
            else:
                reply_text = await provider.rewrite(ai_cfg, text.strip())

            body = self._t(
                "ui_pv_sim_result_body",
                text=text.strip(),
                model=ai_cfg.model,
                reply=reply_text.strip(),
                min_t=cfg.typing_delay_min,
                max_t=cfg.typing_delay_max,
            )
            try:
                await wait_msg.delete()
            except Exception:
                pass
            text_out, kbd = self._render_pv_menu(uid, lang=lang)
            await message.reply_text(body, reply_markup=kbd)
        except Exception as exc:
            try:
                await wait_msg.delete()
            except Exception:
                pass
            text_out, kbd = self._render_pv_menu()
            await message.reply_text(self._t("ui_step_pv_test_error", exc=exc), reply_markup=kbd)
