"""Durable queue consumer: generates the reply and delivers it.

Design constraints that shape this module:

* **Hot path separation.** The inbound dispatcher enqueues and returns; the
  model call happens here, so a 45-second provider timeout cannot stall
  message ingestion.
* **Per-chat ordering.** ``claim_next`` refuses to claim a job while an
  earlier job for the same chat is still waiting, so a burst is answered in
  the order it arrived.
* **Concurrency control.** ``max_concurrency`` is enforced two ways — the
  semaphore bounds in-flight work, and the claim's per-chat lane guard keeps
  two workers from ever answering the same chat at once.
* **Crash safety.** A lease that expires without a completion puts the job
  back on the queue (``recover_expired_leases``), so a killed worker cannot
  hold a conversation hostage.
* **Retries with backoff**, and a dead-letter state inside the same table
  once attempts are exhausted.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from ...domain.autoreply import AutoReplyJob, AutoReplyPrompt, AutoReplyRule, JobStatus
from .context import (
    PromptContext,
    build_messages,
    build_system_prompt,
    redact_for_prompt,
    resolve_language,
)

logger = logging.getLogger("atf.autoreply.worker")

#: Pause between empty-queue polls (the worker does not spin).
IDLE_POLL_SECONDS = 1.5
#: Pause between maintenance sweeps (lease recovery + retention).
MAINTENANCE_EVERY_SECONDS = 30.0
#: How often an orphaned RUNNING job may be requeued before it is failed.
MAX_RECOVERIES = 4
#: Failure codes that a retry cannot fix: the tenant has to change something.
#: They skip the backoff ladder and go straight to the dead-letter list, where
#: the dashboard shows them with their reason and offers a replay.
_PERMANENT_ERROR_CODES = frozenset({
    "no_ai_config", "send_refused", "chat_unavailable", "auth_error",
})


@dataclass
class GenerationResult:
    """What the provider returned, plus everything a diagnostic needs."""

    ok: bool
    text: str = ""
    error: str = ""
    error_code: str = ""
    latency_ms: int = 0
    provider: str = ""
    model: str = ""
    prompt_id: str = ""
    token_usage: Optional[Dict[str, int]] = None


class OutboundSender:
    """Protocol/adapter for actually delivering a reply.

  owning nothing but a callable, so tests substitute a stub and the worker
  never knows whether it is talking to Pyrogram or a list.
  """

    def __init__(self, send: Callable[[str, str], Awaitable[Optional[int]]],
                 typing: Optional[Callable[[str], Awaitable[None]]] = None) -> None:
        self._send = send
        self._typing = typing

    async def send(self, chat_id: str, text: str) -> Optional[int]:
        return await self._send(chat_id, text)

    async def typing(self, chat_id: str) -> None:
        if self._typing is not None:
            try:
                await self._typing(chat_id)
            except Exception:
                pass


class AutoReplyWorker:
    """Consumes the auto-reply queue for every tenant.

    A single worker instance serves the whole process. Concurrency is bounded
    per account by ``max_concurrency`` (each account's settings), and ordering
    is preserved per chat by the claim itself.
    """

    def __init__(
        self,
        repo: Any,
        ai_repo: Any = None,
        prompt_repo: Any = None,
        provider_factory: Any = None,
        test_generator: Optional[Callable[[PromptContext], Awaitable[GenerationResult]]] = None,
        log_sink: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repo = repo
        self._ai_repo = ai_repo
        self._prompt_repo = prompt_repo
        self._factory = provider_factory
        self._test_generator = test_generator
        self._log = log_sink
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._semaphores: Dict[int, asyncio.Semaphore] = {}
        self._sem_lock = asyncio.Lock()
        self._sends_attempted = 0
        self._sends_ok = 0
        self._failures = 0
        self._last_maintenance = 0.0
        self._senders: Dict[str, OutboundSender] = {}

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #
    def _emit(self, level: str, event: str, message: str, **fields: Any) -> None:
        detail = {"event": event, **{k: v for k, v in fields.items() if v not in (None, "")}}
        try:
            if self._log is not None:
                self._log.log(
                    level, "autoreply", "auto_reply",
                    message, detail=detail,
                    rule_id=str(fields.get("rule_id") or ""),
                    user_id=int(fields.get("owner_user_id") or 0),
                    correlation_id=str(fields.get("correlation_id") or ""),
                    event_type=f"autoreply.{event}",
                )
                return
        except Exception:
            pass
        getattr(logger, level.lower(), logger.info)(
            "[AUTOREPLY:%s] %s %s", event, message, detail
        )

    def counters(self) -> Dict[str, int]:
        return {
            "sends_attempted": self._sends_attempted,
            "sends_ok": self._sends_ok,
            "failures": self._failures,
        }

    def attach_log(self, log_sink: Any) -> None:
        """Bind the structured sink once it exists (it is built after this graph)."""
        self._log = log_sink

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def _semaphore_for(self, owner_user_id: int, limit: int) -> asyncio.Semaphore:
        key = int(owner_user_id)
        async with self._sem_lock:
            existing = self._semaphores.get(key)
            if existing is None or getattr(existing, "_bound_value", limit) != limit:
                existing = asyncio.Semaphore(max(1, int(limit)))
                self._semaphores[key] = existing
            return existing

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.ensure_future(self._run())
        logger.info("auto-reply worker started")

    async def stop(self, timeout: float = 10.0) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("auto-reply worker stopped")

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._maintenance()
                claimed = await self._claim_and_process()
                if claimed is None:
                    await asyncio.sleep(IDLE_POLL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("auto-reply worker loop failed")
                await asyncio.sleep(IDLE_POLL_SECONDS)

    async def _maintenance(self) -> None:
        """Lease recovery + retention, on a clock, not on every poll."""
        now = self._clock()
        if now - self._last_maintenance < MAINTENANCE_EVERY_SECONDS:
            return
        self._last_maintenance = now
        try:
            recovered = self._repo.recover_expired_leases(int(now))
            if recovered:
                self._emit("warning", "leases_recovered",
                           "auto-reply jobs recovered after expired leases",
                           recovered=recovered)
            self._repo.cleanup_terminal()
            self._repo.cleanup_messages()
            self._repo.cleanup_expired_memory(int(now))
        except Exception:
            logger.debug("auto-reply maintenance failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Claiming
    # ------------------------------------------------------------------ #
    async def _claim_and_process(self) -> Optional[AutoReplyJob]:
        job = self._repo.claim_next(worker_id="autoreply-worker", now=int(self._clock()))
        if job is None:
            return None
        owner = int(job.owner_user_id)
        settings = self._repo.get_settings(owner)
        semaphore = await self._semaphore_for(owner, settings.max_concurrency)
        # claim_next is a blocking SQLite call, so the claim itself already
        # happened synchronously; the processing is what the semaphore bounds.
        async with semaphore:
            await self._process(job, settings)
        return job

    # ------------------------------------------------------------------ #
    # Processing one job
    # ------------------------------------------------------------------ #
    def _load_prompt(self, owner: int, prompt_id: Optional[str],
                     settings: Any) -> Optional[AutoReplyPrompt]:
        if prompt_id and self._prompt_repo is not None:
            prompt = self._prompt_repo.get_prompt(prompt_id, owner)
            if prompt is not None:
                return prompt
        if settings.prompt_id and self._prompt_repo is not None:
            return self._prompt_repo.get_prompt(settings.prompt_id, owner)
        return None

    def _load_rule(self, owner: int, rule_id: Optional[str]) -> Optional[AutoReplyRule]:
        if not rule_id:
            return None
        try:
            return self._repo.get_rule(rule_id, owner)
        except Exception:
            return None

    def _load_ai_config(self, owner: int, config_id: Optional[str]) -> Optional[Any]:
        if not config_id or self._ai_repo is None:
            return None
        try:
            get_owned = getattr(self._ai_repo, "get_owned", None)
            if get_owned is not None:
                return get_owned(config_id, owner)
            return self._ai_repo.get(config_id)
        except Exception:
            return None

    def _build_context(self, job: AutoReplyJob, settings: Any) -> PromptContext:
        owner = int(job.owner_user_id)
        prompt = self._load_prompt(owner, job.prompt_id or settings.prompt_id, settings)
        rule = self._load_rule(owner, job.rule_id)
        language = resolve_language(settings, prompt, rule, job.inbound_text)
        memory: list = []
        if settings.memory_enabled:
            rows = self._repo.list_memory(owner, job.chat_id, settings.memory_max_facts)
            memory = [str(r.get("content")) for r in rows if r.get("content")]
        history = self._repo.list_messages(owner, job.chat_id,
                                           settings.context_messages) if settings.context_messages else []
        system_prompt = build_system_prompt(settings, prompt, rule, memory, language, job.inbound_text)
        tone = (rule.tone.value if (rule and rule.tone) else
                prompt.tone.value if (prompt and prompt.tone) else settings.tone.value)
        length = (rule.response_length.value if (rule and rule.response_length) else
                  prompt.response_length.value if (prompt and prompt.response_length) else
                  settings.response_length.value)
        template = prompt.user_prompt_template if prompt else "{text}"
        return PromptContext(
            system_prompt=system_prompt,
            tone=tone,
            response_length=length,
            language=language,
            prompt_id=prompt.id if prompt else None,
            ai_config_id=job.ai_config_id or settings.ai_config_id,
            rule_id=rule.id if rule else None,
            cooldown_seconds=settings.cooldown_seconds,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            max_reply_chars=settings.max_reply_chars,
            user_template=template,
            history=history,
            memory=memory,
            correlation_id=job.correlation_id,
            inbound=job.inbound_text,
            sender=job.chat_title,
        )

    async def _generate(self, ctx: PromptContext, ai_config: Any) -> GenerationResult:
        """Call the provider, mapping failures to stable error codes."""
        if self._test_generator is not None:
            return await self._test_generator(ctx)
        if ai_config is None:
            return GenerationResult(False, error_code="no_ai_config",
                                    error="no AI configuration is linked to this account")
        started = self._clock()
        try:
            from ...infrastructure.ai.providers import AIProviderFactory, OpenAICompatibleProvider
            factory = self._factory or AIProviderFactory()
            provider_key = (ai_config.provider.value if hasattr(ai_config.provider, "value")
                            else str(ai_config.provider))
            provider_cls = getattr(factory, "_registry", {}).get(provider_key, OpenAICompatibleProvider)
            provider = provider_cls()
            messages = build_messages(
                ctx.system_prompt, ctx.history, ctx.inbound or "", ctx.user_template
            )
            if hasattr(provider, "chat_complete"):
                raw = await provider.chat_complete(ai_config, messages, temperature=ctx.temperature)
            else:
                raw = await provider.rewrite(
                    ai_config,
                    ctx.user_template.replace("{text}", ctx.inbound or ""),
                )
            text = (raw or "").strip()
            latency = int((self._clock() - started) * 1000)
            if not text:
                return GenerationResult(False, error="empty_completion",
                                        error_code="empty_completion", latency_ms=latency)
            if len(text) > ctx.max_reply_chars:
                text = text[: ctx.max_reply_chars].rsplit(" ", 1)[0].strip()
            return GenerationResult(True, text=text, latency_ms=latency,
                                    provider=provider_key, model=str(getattr(ai_config, "model", "")),
                                    prompt_id=ctx.prompt_id or "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            latency = int((self._clock() - started) * 1000)
            code = _classify_error(exc)
            return GenerationResult(False, error=str(exc)[:300], error_code=code,
                                    latency_ms=latency)

    async def _send_reply(self, job: AutoReplyJob, text: str, settings: Any,
                          sender: Optional[OutboundSender]) -> Optional[int]:
        """Type, pause like a human, then send — or send without the pause."""
        if sender is None:
            return None
        chat_id = job.chat_id
        delay = 0.0
        if settings.typing_simulation:
            delay = min(
                max(len(job.inbound_text) * 0.02 + random.uniform(0.6, 1.6),
                    settings.typing_delay_min),
                settings.typing_delay_max,
            )
        if delay > 0:
            await sender.typing(chat_id)
            await asyncio.sleep(delay)
        return await sender.send(chat_id, text)

    async def _process(self, job: AutoReplyJob, settings: Any) -> None:
        owner = int(job.owner_user_id)
        self._sends_attempted += 1
        # The send adapter lives on the job's session; the pool keeps one map.
        raw_sender = self._senders.get(str(job.session_id))
        sender: Optional[OutboundSender] = (
            raw_sender if isinstance(raw_sender, OutboundSender)
            else OutboundSender(raw_sender) if raw_sender else None
        )
        ai_config = self._load_ai_config(owner, job.ai_config_id or settings.ai_config_id)
        ctx = self._build_context(job, settings)
        ctx.inbound = job.inbound_text

        prompt = self._load_prompt(owner, job.prompt_id or settings.prompt_id, settings)
        if prompt is not None:
            try:
                self._repo.touch_prompt_usage(prompt.id, owner)
            except Exception:
                pass

        result = await self._generate(ctx, ai_config)

        if not result.ok:
            self._failures += 1
            await self._handle_failure(job, settings, result)
            return

        try:
            sent_id = await self._send_reply(job, result.text, settings, sender)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures += 1
            await self._handle_failure(job, settings, GenerationResult(
                False, error=str(exc)[:300], error_code=_classify_error(exc)))
            return

        if sent_id is None and sender is not None:
            # The adapter refused (banned chat, blocked user). Not retryable.
            self._failures += 1
            await self._handle_failure(job, settings, GenerationResult(
                False, error="send refused by transport", error_code="send_refused"))
            return

        latency = result.latency_ms
        try:
            self._repo.complete_sent(job.id, job.lease_token,
                                     sent_message_id=int(sent_id or 0),
                                     reply_text=result.text, latency_ms=latency)
        except Exception:
            logger.debug("autoreply complete_sent failed", exc_info=True)
        self._sends_ok += 1
        # The exchange belongs to the rolling context: the next reply in this
        # chat sees what was said, not just what was asked.
        try:
            self._repo.append_message(owner, job.chat_id, "assistant",
                                      redact_for_prompt(result.text), 0)
            if settings.memory_enabled:
                _remember_from_exchange(self._repo, owner, job.chat_id,
                                        job.inbound_text, result.text)
        except Exception:
            logger.debug("autoreply context append failed", exc_info=True)
        self._emit(
            "info", "sent", "auto-reply sent",
            owner_user_id=owner, chat_id=job.chat_id, job_id=job.id,
            rule_id=job.rule_id, prompt_id=job.prompt_id,
            correlation_id=job.correlation_id, latency_ms=latency,
            chars=len(result.text),
        )

    async def _handle_failure(self, job: AutoReplyJob, settings: Any,
                              result: GenerationResult) -> None:
        job.attempt += 1
        # Retrying cannot fix these: the tenant has to act (configure a
        # provider, unblock the bot, re-authorise the key). They land in the
        # dead-letter list where they are visible and replayable.
        permanent = result.error_code in _PERMANENT_ERROR_CODES
        if job.attempt >= job.max_attempts or permanent:
            dead_letter = True
            try:
                self._repo.mark_failed(job.id, job.lease_token, result.error,
                                       result.error_code, dead_letter=dead_letter)
            except Exception:
                logger.debug("autoreply mark_failed failed", exc_info=True)
            self._emit(
                "error", "dead_letter" if dead_letter else "failed",
                "auto-reply failed",
                owner_user_id=job.owner_user_id, chat_id=job.chat_id, job_id=job.id,
                attempt=job.attempt, error=result.error, error_code=result.error_code,
                correlation_id=job.correlation_id,
            )
            return
        backoff = settings.retry_backoff_seconds * (2 ** max(0, job.attempt - 1))
        backoff = min(backoff, 600.0) + random.uniform(0, 2.0)
        try:
            self._repo.mark_retry(job.id, job.lease_token, result.error,
                                  result.error_code, backoff, job.attempt)
        except Exception:
            logger.debug("autoreply mark_retry failed", exc_info=True)
        self._emit(
            "warning", "retry", "auto-reply retry scheduled",
            owner_user_id=job.owner_user_id, chat_id=job.chat_id, job_id=job.id,
            attempt=job.attempt, backoff_seconds=round(backoff, 1),
            error=result.error, error_code=result.error_code,
            correlation_id=job.correlation_id,
        )

    # ------------------------------------------------------------------ #
    # Send adapter registration (the transport layer plugs in here)
    # ------------------------------------------------------------------ #
    def register_session_sender(self, session_id: str, sender: Any) -> None:
        """Bind a transport to a session so the worker can deliver replies.

        ``sender`` is either an :class:`OutboundSender` or a bare coroutine
        function ``send(chat_id, text) -> Optional[int]``; both shapes are
        accepted so the dispatcher can register Pyrogram directly.
        """
        self._senders[str(session_id)] = sender

    def unregister_session_sender(self, session_id: str) -> None:
        self._senders.pop(str(session_id), None)


def _classify_error(exc: Exception) -> str:
    """Stable error codes for diagnostics and retry decisions."""
    name = type(exc).__name__
    text = str(exc or "").lower()
    # Chat-level refusals come first: "Forbidden: bot was blocked by the user"
    # contains "forbidden", but retrying it forever would only spam the log.
    if any(marker in text for marker in (
        "bot was blocked", "blocked by the user", "user is deactivated",
        "chat not found", "peer_id_invalid", "bot can't initiate",
        "chat_write_forbidden", "have no rights", "not a member",
    )):
        return "chat_unavailable"
    if "timeout" in name.lower() or "timeout" in text:
        return "timeout"
    if "auth" in text or "401" in text or "unauthorized" in text:
        return "auth_error"
    if "rate" in text or "429" in text or "flood" in text:
        return "rate_limited"
    if "connection" in text or "network" in text or "dns" in text:
        return "network_error"
    if "empty" in text:
        return "empty_completion"
    return "provider_error"


def _remember_from_exchange(repo: Any, owner: int, chat_id: str, inbound: str,
                            reply: str) -> None:
    """Extract at most one durable fact from an exchange.

    Deliberately conservative: the responder is a chat agent, not a CRM. It
    remembers explicit, short, declarative statements that appear in the
    *contact's* message ("i'm a designer", "call me next week"), never in its
    own reply, and never anything that looks like a secret.
    """
    text = (inbound or "").strip()
    if not text or len(text) > 300:
        return
    lowered = text.lower()
    if any(word in lowered for word in ("password", "token", "api key", "card", "cvv", "secret")):
        return
    if not any(marker in lowered for marker in ("my name is", "i am ", "i'm ", "call me",
                                                "remember", "next week", "on monday",
                                                "on tuesday", "on friday", "my phone")):
        return
    try:
        repo.remember(owner, chat_id, text[:300])
    except Exception:
        logger.debug("autoreply remember failed", exc_info=True)


__all__ = ["AutoReplyWorker", "GenerationResult", "OutboundSender"]
