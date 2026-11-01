"""Bot API adapter — the Bot transport execution layer of the Telegram Runtime.

The Go Worker never holds a bot token. It names the *stored* token by id and
the tenant the delivery belongs to, and this adapter is where that name is
resolved inside the trusted Python boundary:

1. look the token up through the Core's **owner-scoped** repository
   (``get_owned(token_id, owner)`` — the owner is in the WHERE clause, so a
   transplanted id never round-trips the secret at all);
2. re-check the ownership on every call, **cache hit included** — a token id
   travels in the job payload and anything that travels can be transplanted,
   which is exactly the hole a pool keyed only by token id would open;
3. decrypt the token only here, and only to build the pooled
   ``BotApiTransport`` (one ``httpx.AsyncClient`` per token, reused across
   messages — a forwarder that opens a TLS connection per message cannot
   hold tens of thousands of concurrent users);
4. execute the requested Bot Api operation and report what Telegram said as
   the shared typed error vocabulary. Go keeps the retry decision.

What this adapter deliberately does NOT do:

* it never chooses a transport, falls back to the account, or retries — those
  are business decisions and they belong to the Go Worker;
* it never logs, echoes or returns the token — only the token *id* and the
  resolved bot username appear in logs and responses;
* it never accepts a raw token from the wire. The wire carries ids only.

The implementation reuses the Core's existing ``BotApiTransport`` /
``BotTransportCache`` (``core/infrastructure/telegram/bot_transport.py``)
rather than standing up a second Bot Api client: one pooled client per token
is the whole point, and a second implementation would drift (its button
parsing, its ``retry_after`` handling, its connection reuse).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from telegram_runtime import errors

logger = logging.getLogger("atf.telegram_runtime.bot_api")

__all__ = ["BotApiAdapter", "BotTokenUnavailable"]


class BotTokenUnavailable(RuntimeError):
    """The named token cannot be used by this tenant.

    Raised (and normalized into the shared vocabulary by the caller) when the
    token is missing, inactive, malformed, or **not owned by the requesting
    tenant**. The last case is a security boundary, not a transient failure:
    "not yours" and "does not exist" answer identically so an id cannot be
    probed for existence.
    """


def _mask(token_id: str) -> str:
    """A token id is not a secret, but only its head belongs in a log."""
    return str(token_id or "")[:8]


class BotApiAdapter:
    """The pooled, owner-validated Bot Api execution surface.

    One adapter per runtime process; it holds one ``BotApiTransport`` (and
    its ``httpx.AsyncClient`` connection pool) per stored token, exactly like
    the Core's own ``BotTransportCache``. The token repository is supplied by
    the process that owns the Core's composition root — the standalone
    runtime bootstraps one from the shared SQLite store.
    """

    def __init__(self, token_repo: Any = None, ttl_seconds: float = 300.0) -> None:
        self._repo = token_repo
        self._ttl = ttl_seconds
        # token_id -> (fetched_at, BotApiTransport, owner_user_id)
        self._entries: Dict[str, Tuple[float, Any, int]] = {}
        # token_id -> negative-cache until (missing/inactive/unusable)
        self._negative: Dict[str, float] = {}
        # (token_id, tenant) -> refused-until: a foreign-owner refusal must
        # not stop the real owner from using their own bot 30s later.
        self._denied: Dict[Tuple[str, int], float] = {}

    # -- wiring ------------------------------------------------------------- #

    def bind_repo(self, token_repo: Any) -> None:
        """Attach the Core's IBotTokenRepository (owner-scoped reads)."""
        self._repo = token_repo

    async def aclose(self) -> None:
        """Close every pooled transport on shutdown."""
        for _entry in list(self._entries.values()):
            try:
                await _entry[1].aclose()
            except Exception:  # pragma: no cover - shutdown best effort
                pass
        self._entries.clear()

    # -- ownership ---------------------------------------------------------- #

    def _may_use(self, token_id: str, owner: int, wanted: int) -> bool:
        """Whether ``wanted`` may deliver through a token stored as ``owner``'s.

        Compared on EVERY call — including a cache hit, which is exactly where
        a pool keyed only by token id would hand one tenant's connection to
        another tenant's job.
        """
        if not owner:
            if wanted:
                logger.warning(
                    "[BOT ROUTE] token %s was stored without an owner — account %s "
                    "cannot claim it; re-attach the bot from this account to use it",
                    _mask(token_id), wanted,
                )
                return False
            return True
        if not wanted or owner == wanted:
            return True
        logger.warning(
            "[BOT ROUTE] token %s belongs to account %s but this task's owner is %s — "
            "refusing the bot route rather than posting as another tenant's bot",
            _mask(token_id), owner, wanted,
        )
        return False

    async def _resolve(self, token_id: str, owner_user_id: int) -> Any:
        """The pooled transport for a token, after owner re-validation.

        Raises ``BotTokenUnavailable`` when the token cannot be used by this
        tenant. The lookup itself goes through the repository's owner-scoped
        path so a transplanted id never even round-trips the secret.
        """
        from core.infrastructure.telegram.bot_transport import (
            BotApiTransport, BotTokenInvalid,
        )

        token_id = str(token_id or "").strip()
        if not token_id:
            raise BotTokenUnavailable("empty bot token id")
        if self._repo is None:
            raise BotTokenUnavailable("no bot token repository wired into the runtime")

        wanted = int(owner_user_id or 0)
        now = time.monotonic()

        entry = self._entries.get(token_id)
        if entry and now - entry[0] < self._ttl:
            # Cache hit: the transport exists, but the TENANT still has to be
            # re-checked. This is the cache-hit ownership check the security
            # model depends on.
            if self._may_use(token_id, entry[2], wanted):
                return entry[1]
            # A recent refusal for this exact tenant is cached so a loop of
            # retries does not hammer the database.
            self._denied[(token_id, wanted)] = now + 30.0
            raise BotTokenUnavailable(f"token {_mask(token_id)} is not owned by account {wanted}")

        if not entry and self._negative.get(token_id, 0.0) > now:
            raise BotTokenUnavailable(f"bot token {_mask(token_id)} is missing, inactive or empty")
        if self._denied.get((token_id, wanted), 0.0) > now:
            raise BotTokenUnavailable(f"token {_mask(token_id)} was recently refused for account {wanted}")

        try:
            if wanted:
                # Ask for *this tenant's* row rather than reading any row and
                # comparing afterwards: a transplanted id then never
                # round-trips the secret at all.
                record = await self._repo.get_owned(token_id, wanted)
            else:
                record = await self._repo.get_by_id(token_id)
        except Exception as exc:
            logger.warning("[BOT ROUTE] token %s lookup failed: %s", _mask(token_id), exc)
            record = None

        if record is None:
            if wanted:
                # "Not yours" and "does not exist" answer identically, so an
                # id cannot be probed for existence.
                self._denied[(token_id, wanted)] = now + 30.0
                raise BotTokenUnavailable(
                    f"token {_mask(token_id)} is not owned by account {wanted} (or does not exist)"
                )
            self._negative[token_id] = now + 30.0
            raise BotTokenUnavailable(f"bot token {_mask(token_id)} is missing, inactive or empty")

        if not getattr(record, "is_active", False) or not getattr(record, "token", ""):
            self._negative[token_id] = now + 30.0
            raise BotTokenUnavailable(f"bot token {_mask(token_id)} is missing, inactive or empty")

        try:
            transport = BotApiTransport(record.token, token_id=token_id)
        except BotTokenInvalid as exc:
            logger.error("[BOT ROUTE] stored bot token %s is unusable: %s", _mask(token_id), exc)
            self._negative[token_id] = now + 60.0
            raise BotTokenUnavailable(f"stored bot token {_mask(token_id)} is unusable") from exc

        owner = int(getattr(record, "owner_user_id", 0) or 0)
        # Pooled even when this caller may not use it: the refusal is about
        # the caller, not about the connection.
        self._entries[token_id] = (now, transport, owner)
        self._negative.pop(token_id, None)
        if not self._may_use(token_id, owner, wanted):
            self._denied[(token_id, wanted)] = now + 30.0
            raise BotTokenUnavailable(f"token {_mask(token_id)} is not owned by account {wanted}")
        return transport

    # -- availability ------------------------------------------------------- #

    async def availability(self, token_id: str, owner_user_id: int) -> Tuple[bool, str]:
        """Is this token valid, owned by this tenant, and the bot usable?

        Returns ``(ok, username)``. Never raises: an unusable bot is a typed
        outcome for the caller to classify, not an exception.
        """
        try:
            transport = await self._resolve(token_id, owner_user_id)
        except BotTokenUnavailable as exc:
            logger.warning("[BOT ROUTE] availability refused: %s", exc)
            return False, ""
        try:
            me = await transport.get_me()
            username = str((me or {}).get("username") or "")
            transport.username = username
            return True, username
        except Exception as exc:
            # A Bot Api failure on getMe is normalized by the caller; here it
            # simply means "not available right now".
            logger.warning("[BOT ROUTE] getMe for token %s failed: %s", _mask(token_id), exc)
            return False, transport.username or ""

    # -- Bot Api operations ------------------------------------------------- #
    #
    # Every method maps ONE contract call onto the pooled transport and
    # normalizes whatever Telegram said into the shared typed vocabulary.
    # Buttons are parsed by the transport's own parser (the same one the
    # Core's delivery path uses), and invalid buttons degrade to no markup
    # rather than failing the message.

    async def send_text(
        self, token_id: str, owner_user_id: int, chat_id: Any, text: str,
        *, thread_id: int = 0, inline_buttons: Optional[List[str]] = None,
    ) -> Tuple[int, Optional[errors.TelegramOperationError]]:
        transport, ferr = await self._resolve_typed(token_id, owner_user_id)
        if ferr is not None:
            return 0, ferr
        try:
            sent = await transport.send_message(
                chat_id=chat_id, text=text,
                buttons=_buttons(inline_buttons), message_thread_id=int(thread_id or 0),
            )
            return _sent_id(sent), None
        except Exception as exc:
            return 0, _bot_error(exc, "sendMessage")

    async def copy_message(
        self, token_id: str, owner_user_id: int, chat_id: Any, from_chat_id: Any,
        message_id: int, *, caption: Optional[str] = None, thread_id: int = 0,
        inline_buttons: Optional[List[str]] = None,
    ) -> Tuple[int, Optional[errors.TelegramOperationError]]:
        transport, ferr = await self._resolve_typed(token_id, owner_user_id)
        if ferr is not None:
            return 0, ferr
        try:
            sent = await transport.copy_message(
                chat_id=chat_id, from_chat_id=from_chat_id, message_id=int(message_id),
                caption=caption,
                buttons=_buttons(inline_buttons), message_thread_id=int(thread_id or 0),
            )
            return _sent_id(sent), None
        except Exception as exc:
            return 0, _bot_error(exc, "copyMessage")

    async def send_media(
        self, token_id: str, owner_user_id: int, chat_id: Any, file_reference: str,
        caption: str, media_type: str, *, thread_id: int = 0,
        inline_buttons: Optional[List[str]] = None,
    ) -> Tuple[int, Optional[errors.TelegramOperationError]]:
        """Send a staged file through the bot.

        ``file_reference`` is the opaque token the runtime returned from
        PrepareMediaUpload; empty means the caller already decided to
        degrade to text (the contract makes that Go's decision).
        """
        transport, ferr = await self._resolve_typed(token_id, owner_user_id)
        if ferr is not None:
            return 0, ferr
        method = _send_method_for(media_type)
        try:
            sent = await getattr(transport, method)(
                chat_id=chat_id, photo=file_reference, caption=caption or "",
            )
            return _sent_id(sent), None
        except Exception as exc:
            return 0, _bot_error(exc, method)

    async def delete_messages(
        self, token_id: str, owner_user_id: int, chat_id: Any, message_ids: List[int],
    ) -> Tuple[bool, Optional[errors.TelegramOperationError]]:
        """Delete through the bot. An already-deleted message is a success.

        ``deleteMessages`` on the Bot Api is idempotent in the sense that
        matters here: Telegram reports the messages are gone, which is the
        cleanup outcome the scheduler needs, not a duplicate of a prior
        delete.
        """
        transport, ferr = await self._resolve_typed(token_id, owner_user_id)
        if ferr is not None:
            return False, ferr
        try:
            await transport.delete_messages(chat_id, [int(m) for m in message_ids])
            return True, None
        except Exception as exc:
            normalized = _bot_error(exc, "deleteMessages")
            # "Message to delete not found" proves the message is gone: the
            # cleanup already happened, which is the scheduler's success
            # condition.
            if _is_already_deleted(normalized):
                return True, None
            return False, normalized

    # -- helpers ------------------------------------------------------------ #

    async def _resolve_typed(
        self, token_id: str, owner_user_id: int,
    ) -> Tuple[Optional[Any], Optional[errors.TelegramOperationError]]:
        """Resolve without raising: an unusable token is a typed outcome.

        A token that is missing, inactive, malformed or **not owned by the
        requesting tenant** is reported as an authentication-level typed
        failure. Go then decides (per its own fallback policy) whether the
        job defers, fails or re-routes; the runtime never silently posts as
        a different identity.
        """
        try:
            transport = await self._resolve(token_id, owner_user_id)
            return transport, None
        except BotTokenUnavailable as exc:
            return None, errors.failed(
                errors.AUTHENTICATION_ERROR, str(exc),
                error_type="BOT_TOKEN_UNAVAILABLE",
            )


def _buttons(inline_buttons: Optional[List[str]]) -> Any:
    """The raw per-task button spec, passed to the transport's own parser.

    The wire carries the task's stored spec verbatim (rows of dicts serialized
    as JSON strings, or "label|url" strings). The Core's ``parse_button_rows``
    accepts both, and invalid buttons degrade to no markup rather than
    failing the message — losing a decoration is acceptable, losing the
    message is not.
    """
    if not inline_buttons:
        return None
    import json as _json

    parsed: List[Any] = []
    for row in inline_buttons:
        if not row:
            continue
        try:
            decoded = _json.loads(row)
        except (TypeError, ValueError):
            decoded = row  # a plain "label|url" string
        parsed.append(decoded)
    return parsed


def _sent_id(sent: Any) -> int:
    """The message id a Bot Api result carries."""
    if sent is None:
        return 0
    return int(getattr(sent, "id", None) or getattr(sent, "message_id", None) or 0)


def _send_method_for(media_type: str) -> str:
    """Pick the Bot Api send method for a media classification.

    Bot Api has one method per media kind; the classification is the one the
    ingest side already recorded on the job payload, so the runtime does not
    re-classify.
    """
    kind = str(media_type or "").strip().lower()
    return {
        "photo": "send_photo",
        "video": "send_video",
        "audio": "send_audio",
        "voice": "send_voice",
        "video_note": "send_video_note",
        "animation": "send_animation",
        "sticker": "send_sticker",
    }.get(kind, "send_document")


def _is_already_deleted(failure: errors.TelegramOperationError) -> bool:
    """Telegram's proof that the message is already gone."""
    text = " ".join(filter(None, [failure.error_type, failure.message])).lower()
    return (
        "message to delete not found" in text
        or "message_not_found" in text
        or "message_id_invalid" in text
    )


def _bot_error(exc: Exception, method: str) -> errors.TelegramOperationError:
    """Normalize a Bot Api failure into the shared typed vocabulary.

    The Core's ``BotApiError`` already extracted Telegram's error code and
    ``retry_after``; everything else is mapped by the runtime's normalizer,
    so a Bot Api refusal and an MTProto refusal speak the same vocabulary.
    """
    from core.infrastructure.telegram.bot_transport import BotApiError, BotTransportUnsupported

    if isinstance(exc, BotTransportUnsupported):
        # The Bot Api genuinely cannot express this operation. Reported as a
        # permanent, method-level refusal: Go decides whether the feature
        # permits a USER-transport fallback.
        return errors.failed(
            errors.TELEGRAM_PERMANENT_ERROR,
            f"bot api cannot express {method}: {exc}",
            error_type="BOT_METHOD_INVALID",
        )
    if isinstance(exc, BotApiError):
        # Telegram answered with a typed error body. Bot Api uses HTTP status
        # codes (401 unauthorized, 403 forbidden, 429 too many requests, 400
        # bad request) plus a human description; map the well-known ones and
        # let the generic classifier handle the rest.
        detail = str(exc.description or str(exc))
        code = int(exc.code or 0)
        retry_after = int(float(exc.retry_after or 0.0))
        if code == 401 or "unauthorized" in detail.lower() or "token" in detail.lower():
            return errors.TelegramOperationError(
                code=errors.AUTHENTICATION_ERROR,
                error_type=f"BOT_API_{code}", error_code=str(code),
                retry_after_seconds=retry_after, message=errors._redact(detail),
            )
        if code == 403 or "forbidden" in detail.lower() or "not enough rights" in detail.lower():
            return errors.TelegramOperationError(
                code=errors.PERMISSION_DENIED,
                error_type=f"BOT_API_{code}", error_code=str(code),
                retry_after_seconds=retry_after, message=errors._redact(detail),
            )
        if code == 429 or "too many requests" in detail.lower():
            return errors.TelegramOperationError(
                code=errors.FLOOD_WAIT,
                error_type=f"BOT_API_{code}", error_code=str(code),
                retry_after_seconds=retry_after or _retry_after_from(detail),
                message=errors._redact(detail),
            )
        if "chat not found" in detail.lower() or "chat_id_invalid" in detail.lower():
            return errors.TelegramOperationError(
                code=errors.CHAT_NOT_FOUND,
                error_type=f"BOT_API_{code}", error_code=str(code),
                retry_after_seconds=retry_after, message=errors._redact(detail),
            )
        if "message not found" in detail.lower() or "message_id_invalid" in detail.lower():
            return errors.TelegramOperationError(
                code=errors.MESSAGE_NOT_FOUND,
                error_type=f"BOT_API_{code}", error_code=str(code),
                retry_after_seconds=retry_after, message=errors._redact(detail),
            )
        if "button_data_invalid" in detail.lower() or "inline_keyboard" in detail.lower():
            return errors.TelegramOperationError(
                code=errors.TELEGRAM_PERMANENT_ERROR,
                error_type=f"BOT_API_{code}", error_code=str(code),
                retry_after_seconds=retry_after, message=errors._redact(detail),
            )
        return errors.TelegramOperationError(
            code=errors.TELEGRAM_PERMANENT_ERROR if 400 <= code < 500 else errors.TELEGRAM_TEMPORARY_ERROR,
            error_type=f"BOT_API_{code}", error_code=str(code),
            retry_after_seconds=retry_after, message=errors._redact(detail),
        )
    # A transport-level failure (httpx timeout / connection error) is the
    # ambiguous case: the request may have reached Telegram.
    return errors.normalize(exc, operation=method)


def _retry_after_from(detail: str) -> int:
    """Telegram's "Too Many Requests: retry after N" body, parsed best-effort."""
    import re as _re

    m = _re.search(r"retry after (\d+)", str(detail or ""), _re.IGNORECASE)
    return int(m.group(1)) if m else 0
