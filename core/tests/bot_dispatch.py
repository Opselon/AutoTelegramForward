"""Drive the *real* bot callbacks from a test.

The task screens and their buttons are registered in `ProBotUI._register()` as
pyrogram handlers with `filters.regex(...)` patterns. A test that calls a
private helper instead of the handler proves nothing about the user's boundary:
the helper has no `from_user`. This harness runs `_register()` against a fake
bot that records the registry, then replays a callback payload through the same
handlers the dispatcher would choose, as the user id the test decides.

It is deliberately framework-shaped, not framework-dependent:

* `filters.regex` is replaced by a small object that keeps the pattern and can
  match a payload, so a handler is found by the same regex the product uses;
* handlers are run in pyrogram's group order and `continue_propagation()`
  propagates exactly as it does in production (the trace handler uses it);
* `cq.answer` / `cq.message.edit_text` / `reply_text` records what the user
  would have seen, so a refusal can be asserted as *visible*, not just silent.
"""

from __future__ import annotations

import importlib
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.application.use_cases import (
    AIConfigUseCases,
    BotTokenUseCases,
    FilterRuleUseCases,
    ForwardRuleUseCases,
    SessionUseCases,
)
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAIConfigRepository,
    SqliteApiCredentialRepository,
    SqliteBotTokenRepository,
    SqliteDeliveryQueueRepository,
    SqliteErrorLogRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteMetricsRepository,
    SqliteProcessedMessageRepository,
    SqliteRuleStatsRepository,
    SqliteSessionRepository,
    SqliteUiStateRepository,
    SqliteUserRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.i18n import I18n


class ContinuePropagation(Exception):
    """Stands in for pyrogram's signal that the next group must handle this."""


class _Answers:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls: list[tuple[str, dict]] = []

    def record(self, text, **kwargs) -> None:
        self.calls.append((str(text or ""), kwargs))

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.calls]

    def __bool__(self) -> bool:
        return bool(self.calls)


class _RecordingCall:
    def __init__(self, owner: "_FakeMessage", name: str) -> None:
        self._owner = owner
        self._name = name

    async def __call__(self, *args, **kwargs):
        self._owner.sent.append((self._name, args[0] if args else None, kwargs))
        return SimpleNamespace(message_id=len(self._owner.sent))


class _FakeMessage:
    def __init__(self, chat_id: int = 5094837833, message_id: int = 1) -> None:
        self.chat = SimpleNamespace(id=chat_id, type="private", title="")
        self.message_id = message_id
        self.text = ""
        self.caption = None
        self.sent: list[tuple[str, object, dict]] = []

    async def edit_text(self, text=None, **kwargs):
        self.sent.append(("edit_text", text, kwargs))

    async def edit_reply_markup(self, *args, **kwargs):
        self.sent.append(("edit_reply_markup", None, kwargs))

    async def reply_text(self, text=None, **kwargs):
        self.sent.append(("reply_text", text, kwargs))

    async def delete(self, *args, **kwargs):
        self.sent.append(("delete", None, kwargs))

    def __getattr__(self, name):  # any other call the UI makes, recorded
        return _RecordingCall(self, name)


class CallbackQuery:
    """The shape the UI reads, plus a record of what it told the user."""

    def __init__(self, data: str, user_id: int, chat_id: int = 5094837833, matches=None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, first_name="U", username="u",
                                         language_code="en", is_bot=False)
        self.message = _FakeMessage(chat_id=chat_id)
        #: pyrogram fills this from the matcher that claimed the update; handlers
        #: read the capture groups out of it (`int(cq.matches[0].group(1))`).
        self.matches = list(matches or [])
        self.answers = _Answers("answer")
        self.propagated = False

    async def answer(self, text=None, **kwargs):
        self.answers.record(text, **kwargs)

    async def edit_message_text(self, text=None, **kwargs):
        self.message.sent.append(("edit_message_text", text, kwargs))

    async def continue_propagation(self):
        self.propagated = True
        raise ContinuePropagation()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _RecordingCall(self.message, name)


class _RegexFilter:
    def __init__(self, pattern: str, flags: int = 0) -> None:
        self.pattern = pattern
        self.flags = flags
        self._re = re.compile(pattern, flags)

    def matches(self, data: str) -> bool:
        return bool(self._re.match(data or ""))

    def match(self, data: str):
        """The match object pyrogram would attach to the update."""
        return self._re.match(data or "")


class _AnythingFilter:
    """`filters.command("start") & filters.private` — usable, never a callback hit.

    The UI registers 244 callback handlers, every one of them with
    `filters.regex(...)`; the only unfiltered one is the group -1 trace handler
    (which propagates). So a non-regex filter is a message filter here, and
    matching it for a callback payload would let an unrelated handler swallow
    the press.
    """

    def matches(self, data: str) -> bool:  # pragma: no cover - message filters
        return False

    def __call__(self, *args, **kwargs):
        return _AnythingFilter()

    def __and__(self, other):
        return self

    def __or__(self, other):
        return self

    def __invert__(self):
        return self


class _FakeFilters:
    def __init__(self) -> None:
        self.patterns: list[str] = []

    def regex(self, pattern, flags=0):
        self.patterns.append(pattern)
        return _RegexFilter(pattern, flags)

    def __getattr__(self, name):
        # Message filters (`private`, `text`, `command([...])`) must compose like
        # pyrogram filters do: `&`, `|`, `~`, and be callable for `command(...)`.
        return _AnythingFilter()


class FakeBot:
    def __init__(self) -> None:
        self.filters = _FakeFilters()
        self.callback_handlers: list[tuple[int, object, object]] = []
        self.message_handlers: list[tuple[int, object, object]] = []

    def on_callback_query(self, filter=None, group=0):
        def decorator(func):
            self.callback_handlers.append((group, filter, func))
            return func

        return decorator

    def on_message(self, filter=None, group=0):
        def decorator(func):
            self.message_handlers.append((group, filter, func))
            return func

        return decorator

    def on_inline_query(self, filter=None, group=0):
        return self.on_message(filter=filter, group=group)

    def on_chosen_inline_result(self, filter=None, group=0):
        return self.on_message(filter=filter, group=group)


class _LogClient:
    """Records rather than discards: a swallowed exception is a finding."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level: str, args) -> None:
        self.records.append((level, " ".join(str(a) for a in args)))

    def info(self, *a, **_k):
        self._record("info", a)

    def error(self, *a, **_k):
        self._record("error", a)

    def exception(self, *a, **_k):
        self._record("error", a)

    def warn(self, *a, **_k):
        self._record("warn", a)

    def warning(self, *a, **_k):
        self._record("warn", a)

    def debug(self, *a, **_k):
        self._record("debug", a)

    @property
    def errors(self) -> list[str]:
        return [text for level, text in self.records if level == "error"]


#: The modules that mount callback handlers with their own `filters` import.
#: A new mixin belongs here, or its regexes are invisible to the harness.
REGISTERING_MODULES = (
    "core.infrastructure.telegram.pro_bot_ui",
    "core.infrastructure.telegram.autoreply_bot_ui",
    "core.infrastructure.telegram.replace_bot_ui",
)


class BotHarness:
    """A registered ProBotUI wired to a real SQLite store."""

    def __init__(self, db_path: str, admin_ids: list[int] | None = None) -> None:
        from core.infrastructure.telegram import pro_bot_ui as ui_module

        self.db = SqliteDatabase(db_path)
        self.crypto = CryptoService("test-key")
        self.db_path = db_path
        self.owner = (admin_ids or [5094837833])[0]

        self.sessions_repo = SqliteSessionRepository(self.db, self.crypto)
        self.rules_repo = SqliteForwardRuleRepository(self.db)
        self.filters_repo = SqliteFilterRuleRepository(self.db)
        self.ai_repo = SqliteAIConfigRepository(self.db, self.crypto)
        self.tokens_repo = SqliteBotTokenRepository(self.db, self.crypto)
        self.creds_repo = SqliteApiCredentialRepository(self.db, self.crypto)
        self.state_repo = SqliteUiStateRepository(self.db)
        self.error_repo = SqliteErrorLogRepository(self.db)
        self.metrics_repo = SqliteMetricsRepository(self.db)
        self.stats_repo = SqliteRuleStatsRepository(self.db)
        self.users_repo = SqliteUserRepository(self.db)
        self.queue_repo = SqliteDeliveryQueueRepository(self.db)
        self.map_repo = SqliteMessageMapRepository(self.db)
        self.processed_repo = SqliteProcessedMessageRepository(self.db)

        self.sessions = SessionUseCases(self.sessions_repo)
        self.rules = ForwardRuleUseCases(self.rules_repo, self.sessions_repo)
        self.filter_rules = FilterRuleUseCases(self.filters_repo)
        self.ai_configs = AIConfigUseCases(self.ai_repo, provider_factory=lambda *a, **k: MagicMock())
        self.bot_tokens = BotTokenUseCases(self.tokens_repo)

        self.bot = FakeBot()
        self.log_client = _LogClient()
        # Every module that registers callbacks owns its own `filters` import:
        # the UI itself, and each mixin it inherits. All of them must see the
        # fake filter object, or a module's real pyrogram `RegexFilter` (which
        # exposes `.p`, not `.matches`) is treated as "matches everything" and
        # its handlers swallow every press before the intended one runs.
        registering_modules = [
            importlib.import_module(name)
            for name in REGISTERING_MODULES
        ]
        original_filters = [m.filters for m in registering_modules]
        for module in registering_modules:
            module.filters = self.bot.filters
        try:
            self.ui = ui_module.ProBotUI(
                bot=self.bot,
                i18n=I18n(),
                sessions=self.sessions,
                rules=self.rules,
                filter_rules=self.filter_rules,
                ai_configs=self.ai_configs,
                pipeline=MagicMock(),
                pool=MagicMock(),
                login=MagicMock(),
                log_client=self.log_client,
                admin_ids=list(admin_ids or [self.owner]),
                credentials=self.creds_repo,
                bot_tokens=self.bot_tokens,
                ui_state_repo=self.state_repo,
                error_log=self.error_repo,
                metrics=self.metrics_repo,
                rule_stats=self.stats_repo,
                delivery_queue=self.queue_repo,
                users=self.users_repo,
                db=self.db,
                web_url="https://api.legoten.com",
            )
            # `__init__` already calls `_register()` (line ~489), so the registry
            # is complete here. Calling it again would double-register every
            # handler and make a test press the same button twice.
        finally:
            for module, original in zip(registering_modules, original_filters):
                module.filters = original

    # ------------------------------------------------------------------ #
    # The store
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self.db.close()

    async def seed_session(self, session_id: str, owner: int) -> None:
        from core.domain.entities import TelegramSession

        await self.sessions_repo.add(
            TelegramSession(id=session_id, phone_number="+100****0000", is_active=True,
                            is_authorized=True, owner_user_id=owner)
        )

    async def seed_rule(self, rule_id: str, owner: int, **kwargs):
        from core.domain.entities import ForwardRule

        rule = ForwardRule(
            id=rule_id,
            session_id=kwargs.pop("session_id", "sess-a"),
            source_chat_id=kwargs.pop("source_chat_id", "-1004400615270"),
            target_chat_id=kwargs.pop("target_chat_id", "-1003915313919"),
            name=kwargs.pop("name", rule_id),
            owner_user_id=owner,
            **kwargs,
        )
        await self.rules_repo.add(rule)
        return rule

    def rule_row(self, rule_id: str) -> dict:
        """Every persisted column of a task, as a comparable snapshot."""
        cur = self.db.connection.execute("SELECT * FROM forward_rules WHERE id = ?", (rule_id,))
        row = cur.fetchone()
        if row is None:
            return {}
        return dict(zip([d[0] for d in cur.description], row))

    def rule_count(self, owner: int | None = None) -> int:
        if owner is None:
            cur = self.db.connection.execute("SELECT COUNT(*) FROM forward_rules")
        else:
            cur = self.db.connection.execute(
                "SELECT COUNT(*) FROM forward_rules WHERE owner_user_id = ?", (owner,))
        return int(cur.fetchone()[0])

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def handlers_and_matches(self, data: str) -> list:
        """`(handler, match)` pairs, in the order pyrogram would try them.

        The match object is per handler — pyrogram attaches the one that claimed
        the update — and handlers read their capture groups out of it
        (`int(cq.matches[0].group(1))`), so a harness that omits it cannot drive
        the payload-taking callbacks at all.
        """
        hits = [
            (group, index, flt, func)
            for index, (group, flt, func) in enumerate(self.bot.callback_handlers)
            if group >= 0 and (flt is None or getattr(flt, "matches", lambda _d: True)(data))
        ]
        hits.sort(key=lambda item: (item[0], item[1]))
        out = []
        for _group, _index, flt, func in hits:
            matcher = getattr(flt, "match", None)
            out.append((func, matcher(data) if callable(matcher) else None))
        return out

    def handlers_for(self, data: str) -> list:
        """The handlers a dispatcher would reach for this payload.

        Group -1 is skipped: it is the trace logger (it records the click and
        returns without handling anything), so running it tells the test
        nothing. Everything else is returned in the order pyrogram would try it.
        """
        return [func for func, _match in self.handlers_and_matches(data)]

    async def press(self, data: str, user_id: int, chat_id: int | None = None):
        """Press a button as `user_id`; returns the callback query as it ended.

        Every handler that claims the payload runs, in group order, so the test
        sees the same side effects the user's press would cause. `cq.matches` is
        set to the claiming handler's own match, as the framework does.
        """
        cq = CallbackQuery(data, user_id, chat_id=chat_id or user_id)
        for func, match in self.handlers_and_matches(data):
            cq.matches = [match] if match is not None else []
            try:
                await func(self.bot, cq)
            except ContinuePropagation:
                continue
        return cq

    async def press_expecting_error(self, data: str, user_id: int, chat_id: int | None = None):
        try:
            await self.press(data, user_id, chat_id=chat_id)
        except Exception as exc:
            return exc
        return None
