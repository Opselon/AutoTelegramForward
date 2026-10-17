"""Deliver a task's copies through a bot the owner attached, not their account.

The default transport is the owner's own Telegram account (``USER``). A task can
instead post through a bot whose token the owner supplied: the bot becomes the
middle hop, so copies arrive as bot messages, the owner's account is never the
visible sender, and Bot API features that MTProto-copying cannot reach become
available — inline buttons in particular.

Design notes that matter in production:

* The class deliberately duck-types the pyrogram calls the delivery worker
  already makes (``send_message``, ``copy_message``). The worker picks a
  transport once and everything downstream stays identical, so there is one
  delivery code path rather than two that drift.
* One ``httpx.AsyncClient`` per token, reused across messages. A forwarder that
  opens a TLS connection per message cannot hold 30-40k concurrent users; the
  pool is the difference between "works in the demo" and "works at scale".
* The token is never logged, echoed or put in an exception message. Only the
  token *id* and the resolved bot username appear in logs.
* A malformed button definition disables buttons; it never fails the forward.
  Losing a decoration is acceptable, losing a message is not.
* ``retry_after`` from a 429/``Too Many Requests`` is surfaced as
  ``retry_after`` on the exception so the queue can back off with Telegram's
  own number instead of a guess.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx

logger = logging.getLogger("atf.bot_transport")

BOT_API = "https://api.telegram.org/bot{token}/{method}"

# A bot token is "<numeric bot id>:<35-ish urlsafe chars>". Validating the shape
# locally turns "token was pasted with a stray space" into an instant, readable
# error instead of an opaque 401 on the first forward.
_TOKEN_RE = re.compile(r"^[0-9]{5,}:[A-Za-z0-9_-]{30,}$")
_MAX_BUTTON_TEXT = 64
_MAX_ROWS = 8


class BotTokenInvalid(ValueError):
    """The supplied token is not even shaped like a bot token."""


class BotTransportUnsupported(RuntimeError):
    """Bot API cannot express this operation; the caller must fall back."""


class BotApiError(RuntimeError):
    """Telegram rejected the call. ``retry_after`` is set for 429 responses."""

    def __init__(self, method: str, code: int, description: str, retry_after: float = 0.0) -> None:
        super().__init__(f"{method} failed ({code}): {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after


def token_looks_valid(token: str) -> bool:
    return bool(_TOKEN_RE.match((token or "").strip()))


def mask_token(token: str) -> str:
    """A token is a password. Show only its id and the fact it exists."""
    token = (token or "").strip()
    if not token:
        return "(empty)"
    return f"{token.split(':', 1)[0]}:…" if ":" in token else "…"


def parse_button_rows(raw: Any) -> Optional[List[List[Dict[str, str]]]]:
    """Normalise a task's button config into Bot API ``inline_keyboard`` rows.

    Accepts the canonical form (rows of dicts: ``{"text": "Open", "url": "..."}``)
    and the friendly form owners actually type (``"Open|https://example.com"``
    per row, or a single ``"|"``-separated string). Returns ``None`` when the
    config cannot be used, so a typo downgrades the message rather than breaking
    the forward.
    """
    if not raw:
        return None

    if isinstance(raw, str):
        candidates: List[Any] = [row for row in raw.splitlines() if row.strip()]
    elif isinstance(raw, (list, tuple)):
        candidates = list(raw)
    else:
        return None

    rows: List[List[Dict[str, str]]] = []
    for row in candidates:
        if isinstance(row, dict):
            button = _button_from_mapping(row)
            if button:
                rows.append([button])
            else:
                return None
            continue
        if isinstance(row, (list, tuple)):
            built = [b for b in (_button_from_mapping(b) for b in row) if b]
            if len(built) != len(row) or not built:
                return None
            rows.append(built)
            continue
        if isinstance(row, str):
            parts = [p.strip() for p in row.split("|")]
            if len(parts) < 2:
                return None
            button = _button_from_mapping({"text": parts[0], "url": parts[1]})
            if not button:
                return None
            rows.append([button])
            continue
        return None

    if not rows or len(rows) > _MAX_ROWS:
        return None
    if any(len(r[0].get("text", "")) > _MAX_BUTTON_TEXT for r in rows):
        return None
    return rows


def _button_from_mapping(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not text or not text.isprintable():
        return None
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
        return None
    return {"text": text, "url": url}


class BotApiTransport:
    """Minimal Bot API client with the same call shape as the pyrogram client.

    Only the calls the delivery worker makes are implemented. Anything the Bot
    API genuinely cannot express raises ``BotTransportUnsupported`` so the
    caller can pick another transport for that job instead of failing silently.
    """

    def __init__(self, token: str, token_id: str = "", timeout: float = 30.0) -> None:
        token = (token or "").strip()
        if not token_looks_valid(token):
            raise BotTokenInvalid(f"bot token {mask_token(token)} is not shaped like a token")
        self._token = token
        self.token_id = token_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
        self.username = ""
        self.bot_id = int(token.split(":", 1)[0])
        self._closed = False

    # -- lifecycle ---------------------------------------------------------- #

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    async def __aenter__(self) -> "BotApiTransport":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- plumbing ----------------------------------------------------------- #

    async def _call(self, method: str, payload: Dict[str, Any], files: Optional[Dict[str, Any]] = None) -> Any:
        url = BOT_API.format(token=self._token, method=method)
        try:
            resp = await self._client.post(url, json=None if files else payload, data=payload if files else None, files=files)
        except httpx.HTTPError as exc:
            # Connection-level failure: the queue retries with its own backoff.
            raise BotApiError(method, 0, f"transport error: {exc}") from exc

        try:
            body = resp.json()
        except ValueError:
            raise BotApiError(method, resp.status_code, f"non-JSON response ({resp.status_code})")

        if body.get("ok"):
            return body.get("result")

        description = str(body.get("description") or f"HTTP {resp.status_code}")
        retry_after = 0.0
        params = body.get("parameters") or {}
        if isinstance(params, dict) and params.get("retry_after"):
            try:
                retry_after = float(params["retry_after"])
            except (TypeError, ValueError):
                retry_after = 0.0
        raise BotApiError(method, int(body.get("error_code") or resp.status_code), description, retry_after)

    @staticmethod
    def _buttons_markup(buttons: Any) -> Optional[Dict[str, Any]]:
        rows = parse_button_rows(buttons)
        return {"inline_keyboard": rows} if rows else None

    # -- validation --------------------------------------------------------- #

    async def get_me(self) -> Dict[str, Any]:
        me = await self._call("getMe", {})
        self.username = str(me.get("username") or "")
        return me

    # -- the calls the worker makes ----------------------------------------- #

    async def send_message(
        self,
        chat_id: Any,
        text: str,
        *,
        buttons: Any = None,
        disable_web_page_preview: bool = True,
        message_thread_id: int = 0,
        reply_markup: Any = None,
        **_: Any,
    ) -> Any:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text or "",
            "link_preview_options": {"is_disabled": bool(disable_web_page_preview)},
        }
        if message_thread_id:
            payload["message_thread_id"] = int(message_thread_id)
        markup = reply_markup or self._buttons_markup(buttons)
        if markup:
            payload["reply_markup"] = markup
        result = await self._call("sendMessage", payload)
        return _SentMessage(result)

    async def copy_message(
        self,
        chat_id: Any,
        from_chat_id: Any,
        message_id: int,
        *,
        caption: Optional[str] = None,
        buttons: Any = None,
        message_thread_id: int = 0,
        **_: Any,
    ) -> Any:
        """Copy through the Bot API.

        Works when the attached bot can itself read the source message (it is a
        member of that channel/chat). When it cannot, this raises a Bot API
        error and the queue retries — it never silently posts nothing.
        """
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": int(message_id),
        }
        if caption is not None:
            payload["caption"] = caption
        markup = self._buttons_markup(buttons)
        if markup:
            payload["reply_markup"] = markup
        if message_thread_id:
            payload["message_thread_id"] = int(message_thread_id)
        result = await self._call("copyMessage", payload)
        return _SentMessage(result)

    async def send_media_group(self, *args: Any, **kwargs: Any) -> Any:
        raise BotTransportUnsupported(
            "media groups have no Bot API equivalent that preserves an album from a "
            "source the bot cannot read; use the account transport for this task"
        )

    async def edit_message_text(self, chat_id: Any, message_id: int, text: str, **_: Any) -> Any:
        result = await self._call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": int(message_id), "text": text or ""},
        )
        return _SentMessage(result or {})

    async def delete_messages(self, chat_id: Any, message_ids: Sequence[int]) -> bool:
        await self._call("deleteMessages", {"chat_id": chat_id, "message_ids": [int(m) for m in message_ids]})
        return True

    async def send_photo(
        self,
        chat_id: Any,
        photo: Any,
        *,
        caption: str = "",
        buttons: Any = None,
        **_: Any,
    ) -> Any:
        payload: Dict[str, Any] = {"chat_id": chat_id, "photo": photo, "caption": caption or ""}
        markup = self._buttons_markup(buttons)
        if markup:
            payload["reply_markup"] = markup
        return _SentMessage(await self._call("sendPhoto", payload))


class _SentMessage:
    """The two attributes the worker reads off a pyrogram message."""

    __slots__ = ("id", "message_id", "raw")

    def __init__(self, raw: Any) -> None:
        raw = raw if isinstance(raw, dict) else {}
        self.raw = raw
        self.id = int(raw.get("message_id") or 0)
        self.message_id = self.id


class BotTransportCache:
    """Keeps one transport (and its connection pool) per stored bot token.

    Tokens are decrypted once and cached; the database is not consulted per
    message and the TLS connection is not rebuilt per message.
    """

    def __init__(self, token_repo: Any = None, ttl_seconds: float = 300.0) -> None:
        self._repo = token_repo
        self._ttl = ttl_seconds
        self._entries: Dict[str, tuple[float, BotApiTransport]] = {}
        self._negative: Dict[str, float] = {}

    def bind_repo(self, token_repo: Any) -> None:
        self._repo = token_repo

    async def get(self, token_id: str) -> Optional[BotApiTransport]:
        token_id = str(token_id or "").strip()
        if not token_id or self._repo is None:
            return None

        now = time.monotonic()
        entry = self._entries.get(token_id)
        if entry and now - entry[0] < self._ttl:
            return entry[1]
        if not entry and self._negative.get(token_id, 0.0) > now:
            return None  # recent failure: do not hammer the database

        try:
            record = await self._repo.get_by_id(token_id)
        except Exception as exc:
            logger.warning("[BOT ROUTE] token %s lookup failed: %s", token_id[:8], exc)
            record = None

        if record is None or not getattr(record, "is_active", False) or not getattr(record, "token", ""):
            logger.warning("[BOT ROUTE] bot token %s is missing, inactive or empty", token_id[:8])
            self._negative[token_id] = now + 30.0
            return None

        try:
            transport = BotApiTransport(record.token, token_id=token_id)
        except BotTokenInvalid as exc:
            logger.error("[BOT ROUTE] stored bot token %s is unusable: %s", token_id[:8], exc)
            self._negative[token_id] = now + 60.0
            return None

        self._entries[token_id] = (now, transport)
        self._negative.pop(token_id, None)
        logger.info("[BOT ROUTE] attached bot %s (token %s) ready", transport.username or transport.bot_id, token_id[:8])
        return transport

    async def validate(self, token: str, token_id: str = "probe") -> Dict[str, Any]:
        """Reject a bad token at the moment it is entered, not on first forward."""
        async with BotApiTransport(token, token_id=token_id) as transport:
            return await transport.get_me()

    async def aclose(self) -> None:
        for _, transport in self._entries.values():
            await transport.aclose()
        self._entries.clear()


def iter_button_texts(rows: Optional[Iterable[Iterable[Dict[str, str]]]]) -> List[str]:
    """Flat list of button labels — for logging and panel previews."""
    out: List[str] = []
    for row in rows or []:
        for button in row:
            out.append(str(button.get("text", "")))
    return out


class BotRouteAdapter:
    """Per-job view over a shared transport, carrying *this* job's buttons.

    The transport itself is pooled and shared across jobs (that is the whole
    point of the connection reuse), so per-job settings must not be written onto
    it — two tasks forwarding at once would paint each other's buttons onto the
    other's messages. The adapter is the thin, per-job object the worker passes
    around; it exposes exactly the calls the dispatch path already makes.
    """

    def __init__(self, transport: BotApiTransport, buttons: Any = None, thread_id: int = 0) -> None:
        self._t = transport
        self._buttons = buttons
        self._thread_id = int(thread_id or 0)

    @property
    def bot_username(self) -> str:
        return self._t.username

    @property
    def token_id(self) -> str:
        return self._t.token_id

    async def send_message(self, chat_id: Any, text: str, **kw: Any) -> Any:
        kw.setdefault("message_thread_id", self._thread_id)
        return await self._t.send_message(chat_id, text, buttons=self._buttons, **kw)

    async def copy_message(self, chat_id: Any, from_chat_id: Any, message_id: int, **kw: Any) -> Any:
        kw.setdefault("message_thread_id", self._thread_id)
        return await self._t.copy_message(chat_id, from_chat_id, message_id, buttons=self._buttons, **kw)

    async def edit_message_text(self, chat_id: Any, message_id: int, text: str, **kw: Any) -> Any:
        return await self._t.edit_message_text(chat_id, message_id, text)

    async def send_media_group(self, *args: Any, **kwargs: Any) -> Any:
        # Albums cannot be preserved through the Bot API when the bot cannot read
        # the source; the worker falls back to the account transport for this job.
        raise BotTransportUnsupported("media group delivery is not available on the bot route")
