"""Production-Grade Durable Message Pipeline (P1).

Implements the 9-stage flow:
Ingest -> Normalize -> Deduplicate -> Filter -> Transform -> Enqueue -> Dispatch -> Confirm -> MessageMap

Guarantees:
- Fully non-blocking ingestion path.
- Restart-safe and crash-recoverable at every intermediate stage.
- Independent per-target delivery state and backpressure.
- Clean separation between pure domain rules and infrastructure I/O.
- Observability with correlation IDs, latency tracking, and metrics.
"""

from dataclasses import dataclass, field
from enum import Enum
import logging
import time
from typing import Any, Callable, Dict, List, Optional
import uuid

from ..domain import branding
from ..domain.entities import (
    DeliveryJob,
    DeliveryStatus,
    EvaluationResult,
    FilterRule,
    ForwardRule,
    MessageMapping,
    MessagePayload,
)
from ..domain.content_policy import ContentPolicy, has_link
from ..domain.services import (
    AdvancedSettingsEngine,
    FilterEngine,
    LinkManagementEngine,
    RoutingPolicy,
    TransformationService,
    utf16_len,
)
from .effective_settings import materialize
from .log_policy import LogPolicy
from ..domain.value_objects import (
    AIFallbackPolicy,
    ContentMode,
    FilterAction,
    ForwardMode,
    MediaType,
)

logger = logging.getLogger("atf.pipeline")


class PipelineStage(str, Enum):
    INGEST = "INGEST"
    NORMALIZE = "NORMALIZE"
    DEDUPLICATE = "DEDUPLICATE"
    FILTER = "FILTER"
    TRANSFORM = "TRANSFORM"
    ENQUEUE = "ENQUEUE"
    DISPATCH = "DISPATCH"
    CONFIRM = "CONFIRM"
    MESSAGEMAP = "MESSAGEMAP"


@dataclass
class StageResult:
    stage: PipelineStage
    success: bool
    reason: str = ""
    duration_ms: float = 0.0
    data: Any = None
    error: Optional[str] = None


@dataclass
class PipelineContext:
    correlation_id: str = field(default_factory=lambda: f"corr_{uuid.uuid4().hex[:12]}")
    trigger: str = "NEW_MESSAGE"
    raw_message: Any = None
    payload: Optional[MessagePayload] = None
    rule: Optional[ForwardRule] = None
    filter_result: Optional[EvaluationResult] = None
    transformed_text: Optional[str] = None
    target_jobs: Dict[str, DeliveryJob] = field(default_factory=dict)
    stage_results: List[StageResult] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # Rule id -> content fingerprint claimed at the DEDUPLICATE stage but not
    # yet earned by a delivery. Consumed when the message is accepted for
    # delivery, released otherwise (see DurableMessagePipeline._release_content_claim).
    content_claims: Dict[str, str] = field(default_factory=dict)

    def record_stage(self, stage: PipelineStage, success: bool, reason: str = "", duration_ms: float = 0.0, data: Any = None, error: Optional[str] = None) -> None:
        self.stage_results.append(StageResult(
            stage=stage,
            success=success,
            reason=reason,
            duration_ms=duration_ms,
            data=data,
            error=error,
        ))

    @property
    def is_failed(self) -> bool:
        return any(not r.success for r in self.stage_results if r.stage not in (PipelineStage.FILTER, PipelineStage.DEDUPLICATE))


@dataclass
class PipelineMetrics:
    ingested_total: int = 0
    normalized_total: int = 0
    dedup_skipped_total: int = 0
    filtered_total: int = 0
    transformed_total: int = 0
    enqueued_total: int = 0
    dispatched_total: int = 0
    confirmed_total: int = 0
    failed_total: int = 0
    unknown_total: int = 0
    backpressure_drops: int = 0
    ai_transforms_total: int = 0
    ai_transforms_success: int = 0
    ai_transforms_failed: int = 0
    ai_fallback_send_original: int = 0
    ai_fallback_drops: int = 0
    ai_fallback_retries: int = 0
    ai_fallback_quarantine: int = 0

    def snapshot(self) -> dict:
        return {
            "ingested_total": self.ingested_total,
            "normalized_total": self.normalized_total,
            "dedup_skipped_total": self.dedup_skipped_total,
            "filtered_total": self.filtered_total,
            "transformed_total": self.transformed_total,
            "enqueued_total": self.enqueued_total,
            "dispatched_total": self.dispatched_total,
            "confirmed_total": self.confirmed_total,
            "failed_total": self.failed_total,
            "unknown_total": self.unknown_total,
            "backpressure_drops": self.backpressure_drops,
            "ai_transforms_total": self.ai_transforms_total,
            "ai_transforms_success": self.ai_transforms_success,
            "ai_transforms_failed": self.ai_transforms_failed,
            "ai_fallback_send_original": self.ai_fallback_send_original,
            "ai_fallback_drops": self.ai_fallback_drops,
            "ai_fallback_retries": self.ai_fallback_retries,
            "ai_fallback_quarantine": self.ai_fallback_quarantine,
        }


# =====================================================================
# Normalizer
# =====================================================================

class Normalizer:
    """Sanitizes raw telegram inputs into a validated canonical MessagePayload."""

    @staticmethod
    def normalize(raw: Any) -> MessagePayload:
        if isinstance(raw, MessagePayload):
            return Normalizer._clean_payload(raw)

        # Pyrogram Message object or dictionary
        chat_id = str(getattr(getattr(raw, "chat", None), "id", "") or (raw.get("chat_id") if isinstance(raw, dict) else ""))
        msg_id = int(getattr(raw, "id", 0) or (raw.get("message_id", 0) if isinstance(raw, dict) else 0))
        date_val = getattr(raw, "date", None)
        if date_val is not None and hasattr(date_val, "timestamp"):
            date_ts = int(date_val.timestamp())
        elif isinstance(date_val, (int, float)):
            date_ts = int(date_val)
        else:
            date_ts = int(time.time())

        text = getattr(raw, "text", "") or (raw.get("text", "") if isinstance(raw, dict) else "")
        caption = getattr(raw, "caption", "") or (raw.get("caption", "") if isinstance(raw, dict) else "")
        media_group_id = getattr(raw, "media_group_id", None) or (raw.get("media_group_id") if isinstance(raw, dict) else None)
        is_service = bool(getattr(raw, "service", False) or (raw.get("is_service", False) if isinstance(raw, dict) else False))
        is_edit = bool(getattr(raw, "edit_date", None) is not None or (raw.get("is_edit", False) if isinstance(raw, dict) else False))

        # Media identification
        has_media = False
        m_type = MediaType.TEXT

        for attr, enum_val in [
            ("photo", MediaType.PHOTO),
            ("video", MediaType.VIDEO),
            ("document", MediaType.DOCUMENT),
            ("audio", MediaType.AUDIO),
            ("voice", MediaType.VOICE),
            ("sticker", MediaType.STICKER),
            ("animation", MediaType.ANIMATION),
        ]:
            val = getattr(raw, attr, None) if not isinstance(raw, dict) else raw.get(attr)
            if val is not None:
                has_media = True
                m_type = enum_val
                break

        payload = MessagePayload(
            chat_id=chat_id,
            message_id=msg_id,
            date=date_ts,
            text=Normalizer._sanitize_str(text),
            caption=Normalizer._sanitize_str(caption),
            has_media=has_media,
            media_type=m_type,
            media_group_id=str(media_group_id) if media_group_id else None,
            is_service=is_service,
            is_edit=is_edit,
            edit_date=int(getattr(raw, "edit_date", 0) or 0) or None,
            original_date=date_ts,
            # v2.0 app update — Reply feature: a reply's target is needed by
            # SyncEngine.handle_reply to mirror the conversation in the target.
            reply_to_message_id=(
                int(getattr(raw, "reply_to_message_id", 0) or 0) or None
            ) if not isinstance(raw, dict) else (
                int(raw.get("reply_to_message_id", 0) or 0) or None
            ),
        )
        return Normalizer._clean_payload(payload)

    @staticmethod
    def _sanitize_str(s: Optional[str]) -> str:
        if not s:
            return ""
        # Remove null bytes and corrupted control characters
        return str(s).replace("\x00", "")

    @staticmethod
    def _clean_payload(p: MessagePayload) -> MessagePayload:
        p.chat_id = str(p.chat_id).strip()
        p.message_id = int(p.message_id)
        p.text = Normalizer._sanitize_str(p.text)
        p.caption = Normalizer._sanitize_str(p.caption)
        return p


# =====================================================================
# DurableMessagePipeline Coordinator
# =====================================================================

def _content_fingerprint(payload: "MessagePayload") -> str:
    """Stable content fingerprint for the Duplicate Filters feature.

    Normalizes whitespace and case so a re-post with added newlines or a
    different casing still matches. Media is keyed by kind + album id, not by
    binary content (the pipeline never downloads media at filter time).
    """
    import hashlib

    body = (payload.text or "") + "||" + (payload.caption or "")
    normalized = " ".join(body.split()).casefold()
    media_key = str(getattr(payload.media_type, "value", payload.media_type))
    if payload.media_group_id:
        media_key += f":album:{payload.media_group_id}"
    raw = f"{normalized}|{media_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DurableMessagePipeline:
    """Production-Grade message pipeline coordinator implementing P1."""

    def __init__(
        self,
        rule_repo: Any,
        filter_repo: Any,
        queue_manager: Optional[Any] = None,
        msg_map_repo: Optional[Any] = None,
        processed_repo: Optional[Any] = None,
        filter_engine: Optional[FilterEngine] = None,
        routing_policy: Optional[RoutingPolicy] = None,
        ai_factory: Optional[Any] = None,
        ai_repo: Optional[Any] = None,
        ai_transformer: Optional[Any] = None,
        prompt_service: Optional[Any] = None,
        pool: Optional[Any] = None,
        log_client: Optional[Any] = None,
        audit_repo: Optional[Any] = None,
        log_policy: Optional["LogPolicy"] = None,
        branding_hop: Optional[Any] = None,
        bot_tokens: Optional[Any] = None,
    ) -> None:
        self._rules = rule_repo
        self._filters = filter_repo
        self._queue = queue_manager
        self._map = msg_map_repo
        self._processed = processed_repo
        self._filter_engine = filter_engine or FilterEngine()
        self._routing = routing_policy or RoutingPolicy()
        self._ai_factory = ai_factory
        self._ai_repo = ai_repo
        self._ai_transformer = ai_transformer
        self._prompt_service = prompt_service
        self._pool = pool
        self._log_client = log_client
        self._audit_repo = audit_repo
        # Branding hop performer (port): the dispatcher, wired by main.py. Kept
        # optional so a bare pipeline — unit tests, embedded use — still runs;
        # without it a rule that asks for the hop simply delivers directly.
        self._branding_hop = branding_hop
        # Per-bot defaults: a task inherits the behaviour settings of the bot it
        # has attached. Read here, at the moment a message is decided, because
        # everything downstream — the transform *and* the delivery job, which
        # carries delivery_transport, bot_token_id and auto_delete_seconds — is
        # derived from the rule object this pipeline hands along.
        self._bot_tokens = bot_tokens
        self.metrics = PipelineMetrics()
        # Which lifecycle events may leave this process, and how often. The
        # Logger has no server-side filter yet, so the documented severity
        # defaults (INFO/SUCCESS/DEBUG off, WARN and above on) are enforced here
        # — otherwise every successfully forwarded message writes an INFO row.
        self.log_policy = log_policy if log_policy is not None else LogPolicy.from_env()

    # -----------------------------------------------------------------
    # Main Entry Point
    # -----------------------------------------------------------------
    async def _audit(self, ctx: "PipelineContext", event_type: str, outcome: str,
                     severity: str = "INFO", reason_code: str = "", rule_id: str = "",
                     feature_id: str = "FWD_MESSAGE_DELIVERY", **kw) -> None:
        """Emit one structured audit event through the Central Logger (best-effort,
        never blocks forwarding). Audit events carry ids and reason codes — never
        message content (mission §12 minimum-necessary principle)."""
        if self._log_client is None:
            return
        # End-to-end latency belongs on every event: reconstructing one failed
        # forwarding operation means knowing both what happened and how long the
        # pipeline took to decide it. Callers may override it.
        kw.setdefault("duration_ms", int((time.time() - ctx.created_at) * 1000))
        # The severity decision lives at the door (LogClient), so it governs this
        # path and the stdlib-logging bridge alike. What this helper must do is
        # NAME the event, so a security-relevant one can be exempted from the
        # threshold — and the durable audit write below stays unconditional: the
        # verbosity gate must never share a path with the evidence.
        try:
            from core.infrastructure.persistence.audit_events import AuditEvent
            evt = AuditEvent(
                event_type=event_type, service="core", outcome=outcome,
                severity=severity, reason_code=reason_code,
                correlation_id=ctx.correlation_id, rule_id=rule_id,
                feature_id=feature_id, **kw,
            )
            self._log_client.log(
                severity,
                "core", "audit",
                f"{event_type} outcome={outcome}" + (f" reason={reason_code}" if reason_code else ""),
                evt.to_dict(),
                # v18: the ids travel as columns too, so "this task's history"
                # and "this message's trail" are index lookups rather than
                # free-text scans over the detail blob.
                rule_id=rule_id or "",
                user_id=int(kw.get("owner_user_id", 0) or kw.get("user_id", 0) or 0),
                correlation_id=ctx.correlation_id or "",
                # Naming the event is what lets the door exempt a
                # security-relevant event from the severity threshold.
                event_type=event_type,
                reason_code=reason_code,
            )
        except Exception:
            pass  # never break the forwarding path for observability
        # Durable audit write (PostgreSQL audit_events) when available — also
        # best-effort: a Logger outage must never stop forwarding (§9.1).
        if self._audit_repo is not None:
            try:
                from core.infrastructure.persistence.audit_events import AuditEvent
                await self._audit_repo.record(AuditEvent(
                    event_type=event_type, service="core", outcome=outcome,
                    severity=severity, reason_code=reason_code,
                    correlation_id=ctx.correlation_id, rule_id=rule_id,
                    feature_id=feature_id, **kw,
                ))
            except Exception:
                pass

    # -----------------------------------------------------------------
    def set_branding_hop(self, performer: Optional[Any]) -> None:
        """Wire the branding hop performer (the dispatcher) after construction.

        ``main.py`` builds the pipeline before the dispatcher exists, and the hop
        needs the dispatcher's live session pool and posting routine; this setter
        is how the two halves meet without the application layer importing the
        infrastructure layer.
        """
        self._branding_hop = performer

    async def _perform_brand_hop(self, ctx: "PipelineContext", rule: ForwardRule,
                                 text: str) -> Optional[Dict[str, Any]]:
        """Ask the performer to post ``text`` into C; never raise at the caller."""
        try:
            plan = await self._branding_hop.perform_branding_hop(  # type: ignore[union-attr]
                ctx.payload, rule, text,
            )
        except Exception as exc:
            logger.warning(
                "[PIPELINE:BRANDING] [%s] Rule %s hop failed (%s) — delivering directly",
                ctx.correlation_id[:8], rule.id[:8], exc,
            )
            return None
        if plan:
            logger.info(
                "[PIPELINE:BRANDING] [%s] Rule %s msg %s posted to C=%s msg=%s ids=%s (%s)",
                ctx.correlation_id[:8], rule.id[:8], ctx.payload.message_id,
                plan.get("chat_id"), plan.get("message_id"), plan.get("message_ids"),
                plan.get("reason", ""),
            )
        else:
            logger.warning(
                "[PIPELINE:BRANDING] [%s] Rule %s msg %s: nothing posted into C — delivering directly",
                ctx.correlation_id[:8], rule.id[:8], getattr(ctx.payload, "message_id", "?"),
            )
        return plan

    async def process(self, raw_message: Any, trigger: str = "NEW_MESSAGE") -> PipelineContext:
        ctx = PipelineContext(trigger=trigger, raw_message=raw_message)
        t_start = time.monotonic()

        # 1. INGEST
        t0 = time.monotonic()
        if raw_message is None:
            ctx.record_stage(PipelineStage.INGEST, False, reason="null_message")
            self.metrics.failed_total += 1
            return ctx
        ctx.record_stage(PipelineStage.INGEST, True, reason="accepted", duration_ms=(time.monotonic() - t0) * 1000)
        self.metrics.ingested_total += 1

        # 2. NORMALIZE
        t0 = time.monotonic()
        try:
            payload = Normalizer.normalize(raw_message)
            ctx.payload = payload
            ctx.record_stage(PipelineStage.NORMALIZE, True, reason="normalized", duration_ms=(time.monotonic() - t0) * 1000)
            self.metrics.normalized_total += 1
            logger.info(
                "[PIPELINE:INGEST] [%s] Ingested message id=%s chat=%s (%s) media=%s text_len=%d trigger=%s",
                ctx.correlation_id[:8], payload.message_id, payload.chat_id,
                payload.chat_title or "unknown", getattr(payload.media_type, "value", payload.media_type),
                len(payload.effective_text or ""), trigger,
            )
        except Exception as exc:
            ctx.record_stage(PipelineStage.NORMALIZE, False, reason="normalize_error", error=str(exc))
            self.metrics.failed_total += 1
            logger.exception("[%s] Normalize error: %s", ctx.correlation_id, exc)
            return ctx

        if payload.is_service:
            ctx.record_stage(PipelineStage.FILTER, False, reason="service_message_dropped")
            self.metrics.filtered_total += 1
            logger.info("[PIPELINE:DROP] [%s] Dropping service message id=%s", ctx.correlation_id[:8], payload.message_id)
            return ctx

        # Find matching rules
        active_rules = await self._rules.list_active_by_source(payload.chat_id)
        matching = self._routing.matching_rules(payload, active_rules)
        valid_rules = [r for r in matching if trigger in (getattr(r, "trigger_events", None) or ["NEW_MESSAGE"])]

        if not valid_rules:
            # A paused task is not a "no rule matched": the rule is there and it
            # watches this source, it is simply stopped by the user. Saying
            # "none of N matched" sends an operator looking for a routing bug
            # that does not exist (Part 2 STEER 05/09).
            if active_rules and all(
                bool(getattr(r, "is_currently_paused", lambda: False)()) for r in active_rules
            ):
                ctx.record_stage(PipelineStage.FILTER, False, reason="rule_paused")
                await self._audit(ctx, "forward.dropped", "FAILED", reason_code="RULE_PAUSED",
                                  message_id=str(payload.message_id),
                                  result_summary=f"every task watching {payload.chat_id} is paused")
                logger.info(
                    "[PIPELINE:PAUSED] [%s] Message %s from chat %s: the task(s) watching this source are paused",
                    ctx.correlation_id[:8], payload.message_id, payload.chat_id,
                )
                return ctx
            ctx.record_stage(PipelineStage.FILTER, False, reason="no_matching_rules")
            reason_code = "RULE_NOT_MATCHED" if active_rules else "RULE_DISABLED"
            await self._audit(ctx, "forward.dropped", "FAILED", reason_code=reason_code,
                              message_id=str(payload.message_id),
                              result_summary=f"no matching rule for chat {payload.chat_id}")
            if not active_rules:
                logger.info(
                    "[PIPELINE:NO_RULES] [%s] Message %s from chat %s: No active forward rules exist for this source",
                    ctx.correlation_id[:8], payload.message_id, payload.chat_id,
                )
            else:
                logger.info(
                    "[PIPELINE:NO_MATCH] [%s] Message %s from chat %s: None of %d active rule(s) matched (matching=%d, trigger=%s)",
                    ctx.correlation_id[:8], payload.message_id, payload.chat_id, len(active_rules), len(matching), trigger,
                )
            return ctx

        logger.info(
            "[PIPELINE:ROUTE] [%s] Message %s matched %d valid forwarding rule(s) for source chat %s",
            ctx.correlation_id[:8], payload.message_id, len(valid_rules), payload.chat_id,
        )

        for rule in valid_rules:
            try:
                await self._process_for_rule(ctx, rule)
            finally:
                # A content-dedup claim is only earned when the message reached
                # the delivery queue. Anything left here was dropped after the
                # dedup stage, so give the fingerprint back.
                await self._release_content_claim(ctx, rule)

        logger.info(
            "[PIPELINE:COMPLETE] [%s] Pipeline finished in %.2fms. Target jobs enqueued=%d",
            ctx.correlation_id[:8],
            (time.monotonic() - t_start) * 1000,
            len(ctx.target_jobs),
        )
        return ctx

    @staticmethod
    def _content_fingerprint(payload: "MessagePayload") -> str:
        return _content_fingerprint(payload)

    async def _release_content_claim(self, ctx: PipelineContext, rule: ForwardRule) -> None:
        """Give back a Duplicate Filters claim the message never earned.

        The claim is taken at the dedup stage so concurrent workers cannot both
        forward identical content, and it is consumed once the message is
        accepted for delivery. If processing ended any other way — filtered
        out, keyword rule, AI drop, backpressure, an exception — nothing
        reached the targets, so the fingerprint is returned. Without this the
        task would treat the next legitimate post of the same text as a
        duplicate and silently drop it.
        """
        claim = ctx.content_claims.pop(rule.id, None)
        if not claim or self._map is None:
            return
        try:
            await self._map.release_content_hash(rule.id, claim)
            logger.info(
                "[PIPELINE:DUP_FILTER] [%s] Rule %s released content claim %s (message not delivered)",
                ctx.correlation_id[:8], rule.id[:8], claim[:8],
            )
        except Exception as exc:
            logger.warning(
                "[PIPELINE:DUP_FILTER] [%s] Rule %s could not release content claim %s: %s",
                ctx.correlation_id[:8], rule.id[:8], claim[:8], exc,
            )

    async def _process_for_rule(self, ctx: PipelineContext, rule: ForwardRule) -> None:
        payload = ctx.payload
        if payload is None:
            return

        # The rule as the forwarder should see it: the attached bot's defaults
        # filled in for everything this task does not set itself. A copy, so
        # nothing here can be written back as if the task had chosen it.
        rule = await materialize(rule, self._bot_tokens)

        if getattr(rule, "is_paused", False) and getattr(rule, "is_currently_paused", lambda: True)():
            ctx.record_stage(PipelineStage.FILTER, False, reason="rule_paused")
            await self._audit(ctx, "forward.dropped", "FAILED", reason_code="RULE_PAUSED",
                              rule_id=rule.id, message_id=str(payload.message_id),
                              result_summary="rule is paused")
            logger.info(
                "[PIPELINE:DROP] [%s] Rule %s is paused. Skipping forwarding for msg %s",
                ctx.correlation_id[:8], rule.id[:8], payload.message_id,
            )
            return

        # 3. DEDUPLICATE (Event level)
        t0 = time.monotonic()
        since = getattr(rule, "since_ts", 0) or 0
        if since and payload.date and payload.date < since:
            ctx.record_stage(PipelineStage.DEDUPLICATE, False, reason="too_old", duration_ms=(time.monotonic() - t0) * 1000)
            self.metrics.dedup_skipped_total += 1
            logger.info(
                "[%s] Rule %s skipped msg %s: too_old (date=%s < since_ts=%s)",
                ctx.correlation_id[:8], rule.id[:8], payload.message_id, payload.date, since,
            )
            return

        if payload.is_edit and getattr(rule, "ignore_edits", False):
            ctx.record_stage(PipelineStage.DEDUPLICATE, False, reason="edit_ignored")
            self.metrics.dedup_skipped_total += 1
            logger.info(
                "[%s] Rule %s skipped msg %s: edit_ignored",
                ctx.correlation_id[:8], rule.id[:8], payload.message_id,
            )
            return

        # Phase 2: edit-time window. An edit arriving outside the configured
        # window is ignored, which stops a reconnect-time burst of very old
        # edits from flooding the target. The check is defensive: a missing or
        # malformed date never drops a legitimate edit.
        if payload.is_edit and not AdvancedSettingsEngine.is_edit_within_window(
            getattr(payload, "original_date", None),
            getattr(payload, "edit_date", None) or getattr(payload, "date", None),
            rule,
        ):
            ctx.record_stage(PipelineStage.DEDUPLICATE, False, reason="edit_outside_time_window")
            self.metrics.dedup_skipped_total += 1
            logger.info(
                "[PIPELINE:EDIT_WINDOW] [%s] Rule %s skipped edit msg %s: outside edit_time_limit_seconds=%s",
                ctx.correlation_id[:8], rule.id[:8], payload.message_id, rule.edit_time_limit_seconds,
            )
            return

        if not payload.is_edit and getattr(rule, "skip_history", True) and self._processed is not None:
            seen = await self._processed.was_processed(rule.id, payload.chat_id, payload.message_id)
            if seen:
                ctx.record_stage(PipelineStage.DEDUPLICATE, False, reason="duplicate_event")
                self.metrics.dedup_skipped_total += 1
                logger.info(
                    "[%s] Rule %s skipped msg %s: already processed (duplicate_event)",
                    ctx.correlation_id[:8], rule.id[:8], payload.message_id,
                )
                return

        # v2.0 app update — Duplicate Filters: drop a message whose *content*
        # was already forwarded under this rule, even when it is a different
        # message id. Two different ids carrying byte-identical text (a channel
        # re-posting the same announcement) would otherwise both be delivered.
        # The fingerprint is scoped to the rule (its body + media kind), so the
        # same text under another task is a different message and is forwarded.
        #
        # The claim is atomic (the store's UNIQUE(rule_id, content_hash) index
        # picks the winner) but it is only *earned* when the message is accepted
        # for delivery; see `_release_content_claim`. Recording it here and
        # never giving it back marked content as forwarded that never reached a
        # target, so a later legitimate post of the same text was silently
        # dropped.
        if bool(getattr(rule, "duplicate_filters", False)) and self._map is not None:
            content_hash = self._content_fingerprint(payload)
            if content_hash:
                already = False
                try:
                    claim = getattr(self._map, "claim_content_hash", None)
                    if claim is None:
                        # A store written before the atomic claim existed: check
                        # without claiming, so there is nothing to release.
                        already = await self._map.was_content_forwarded(rule.id, content_hash)
                    else:
                        claimed = await claim(rule.id, content_hash)
                        if claimed:
                            ctx.content_claims[rule.id] = content_hash
                        else:
                            already = True
                except Exception:
                    already = False  # a dedup-store hiccup must never drop a message
                if already:
                    ctx.record_stage(PipelineStage.DEDUPLICATE, False, reason="duplicate_content")
                    self.metrics.dedup_skipped_total += 1
                    logger.info(
                        "[PIPELINE:DUP_FILTER] [%s] Rule %s skipped msg %s: duplicate content (hash=%s)",
                        ctx.correlation_id[:8], rule.id[:8], payload.message_id, content_hash[:8],
                    )
                    return

        ctx.record_stage(PipelineStage.DEDUPLICATE, True, reason="unique", duration_ms=(time.monotonic() - t0) * 1000)

        # 4. FILTER
        t0 = time.monotonic()
        filter_rule = None
        if rule.filter_rule_id:
            try:
                filter_rule = await self._filters.get_by_id(rule.filter_rule_id)
            except Exception as exc:
                logger.warning("[%s] Filter fetch failed: %s", ctx.correlation_id, exc)
            # The id comes from the task's own row, but the row it points at is
            # read by id alone: a task must never be filtered by another
            # tenant's rule. A rule owned by somebody else is ignored (and
            # logged); an id with no owner (0) is a pre-ownership row and is
            # still the task's own configuration.
            owner = getattr(filter_rule, "owner_user_id", 0) if filter_rule is not None else 0
            task_owner = getattr(rule, "owner_user_id", 0)
            if filter_rule is not None and owner not in (0, task_owner):
                logger.warning(
                    "[PIPELINE:FILTER_OWNERSHIP] [%s] Rule %s (owner %s) references filter %s owned "
                    "by %s — ignoring the filter",
                    ctx.correlation_id[:8], rule.id[:8], task_owner,
                    str(rule.filter_rule_id)[:8], owner,
                )
                await self._audit(
                    ctx, "filter.denied", "REJECTED", reason_code="FILTER_NOT_OWNED",
                    rule_id=rule.id, result_summary=f"filter_owner={owner}",
                )
                filter_rule = None

        eval_res = self._filter_engine.evaluate(payload, filter_rule)
        ctx.filter_result = eval_res

        if eval_res.action == FilterAction.DROP:
            ctx.record_stage(PipelineStage.FILTER, False, reason=eval_res.reason, duration_ms=(time.monotonic() - t0) * 1000)
            self.metrics.filtered_total += 1
            logger.warning(
                "[PIPELINE:FILTER_DROP] [%s] Rule %s DROPPED msg %s by filter rule %s: %s",
                ctx.correlation_id[:8], rule.id[:8], payload.message_id, rule.filter_rule_id or "none", eval_res.reason,
            )
            return

        # Check if session is time-limited
        if self._pool is not None and hasattr(self._pool, "is_time_limited"):
            sid = getattr(rule, "session_id", "")
            if sid:
                try:
                    is_lim = self._pool.is_time_limited(sid)
                    if type(is_lim).__name__ != "MagicMock" and is_lim:
                        raw_rem = self._pool.remaining_limit_seconds(sid)
                        rem = float(raw_rem) if isinstance(raw_rem, (int, float)) else 0.0
                        ctx.record_stage(PipelineStage.FILTER, False, reason="session_time_limited")
                        logger.info(
                            "[PIPELINE:TIME_LIMITED] [%s] Session %s is time-limited (%.1fs remaining). Dropping msg %s",
                            ctx.correlation_id[:8], sid[:8], rem, payload.message_id,
                        )
                        return
                except Exception as exc:
                    logger.debug("Failed checking time limit for %s: %s", sid, exc)

        # Additional media & content filters
        m_type = str(getattr(payload.media_type, "value", payload.media_type)).lower()
        if getattr(rule, "block_voice", False) and m_type in ("voice", "audio"):
            ctx.record_stage(PipelineStage.FILTER, False, reason="voice_blocked")
            self.metrics.filtered_total += 1
            logger.info("[PIPELINE:FILTER] [%s] Rule %s dropped msg %s: voice blocked", ctx.correlation_id[:8], rule.id[:8], payload.message_id)
            return
        if getattr(rule, "block_stickers", False) and m_type == "sticker":
            ctx.record_stage(PipelineStage.FILTER, False, reason="sticker_blocked")
            self.metrics.filtered_total += 1
            logger.info("[PIPELINE:FILTER] [%s] Rule %s dropped msg %s: sticker blocked", ctx.correlation_id[:8], rule.id[:8], payload.message_id)
            return
        if getattr(rule, "block_video", False) and m_type in ("video", "video_note"):
            ctx.record_stage(PipelineStage.FILTER, False, reason="video_blocked")
            self.metrics.filtered_total += 1
            logger.info("[PIPELINE:FILTER] [%s] Rule %s dropped msg %s: video blocked", ctx.correlation_id[:8], rule.id[:8], payload.message_id)
            return
        if getattr(rule, "block_audio", False) and m_type in ("audio", "voice"):
            ctx.record_stage(PipelineStage.FILTER, False, reason="audio_blocked")
            self.metrics.filtered_total += 1
            logger.info("[PIPELINE:FILTER] [%s] Rule %s dropped msg %s: audio blocked", ctx.correlation_id[:8], rule.id[:8], payload.message_id)
            return
        if getattr(rule, "block_text", False) and m_type == "text":
            ctx.record_stage(PipelineStage.FILTER, False, reason="text_blocked")
            self.metrics.filtered_total += 1
            logger.info("[PIPELINE:FILTER] [%s] Rule %s dropped msg %s: text blocked", ctx.correlation_id[:8], rule.id[:8], payload.message_id)
            return
        if getattr(rule, "block_links", False):
            if has_link(payload.effective_text or ""):
                ctx.record_stage(PipelineStage.FILTER, False, reason="link_blocked")
                self.metrics.filtered_total += 1
                logger.info("[PIPELINE:FILTER] [%s] Rule %s dropped msg %s: link blocked", ctx.correlation_id[:8], rule.id[:8], payload.message_id)
                return

        # 4b. PER-TASK CONTENT RULES (blacklist / whitelist-only / block links),
        # source-scoped. Runs after the media gates and *before* any transform:
        # a message that will not be forwarded must not pay for a rewrite.
        # The chain is the task's own policy plus the override for this source
        # chat; every policy in it must allow the message (an override can only
        # add constraints, never relax the task's).
        policy_chain = (
            rule.policy_chain(source_chat_id=payload.chat_id)
            if hasattr(rule, "policy_chain") else []
        )
        for policy in policy_chain:
            decision = policy.evaluate(payload.effective_text or "", m_type)
            if not decision.allowed:
                ctx.record_stage(PipelineStage.FILTER, False, reason=decision.reason)
                self.metrics.filtered_total += 1
                logger.info(
                    "[PIPELINE:POLICY] [%s] Rule %s dropped msg %s at %s: %s",
                    ctx.correlation_id[:8], rule.id[:8], payload.message_id,
                    policy.source or "task", decision.reason,
                )
                await self._audit(ctx, "forward.filtered", "REJECTED",
                                  reason_code="CONTENT_POLICY",
                                  rule_id=rule.id, message_id=str(payload.message_id),
                                  result_summary=f"{policy.source or 'task'}:{decision.reason}")
                return
        active_policies = [p for p in policy_chain if p.has_rules]
        if active_policies:
            logger.info(
                "[PIPELINE:POLICY] [%s] Rule %s msg %s passed %d content polic(ies): %s",
                ctx.correlation_id[:8], rule.id[:8], payload.message_id,
                len(active_policies), [p.describe() for p in active_policies],
            )

        ctx.record_stage(PipelineStage.FILTER, True, reason=eval_res.reason, duration_ms=(time.monotonic() - t0) * 1000)
        logger.info(
            "[PIPELINE:FILTER] [%s] Rule %s PASSED msg %s (filter=%s, reason=%s, took %.2fms)",
            ctx.correlation_id[:8], rule.id[:8], payload.message_id, rule.filter_rule_id or "none",
            eval_res.reason or "allow", (time.monotonic() - t0) * 1000,
        )

        # 5. TRANSFORM
        t0 = time.monotonic()
        try:
            transformed = await self._apply_transforms(ctx, payload, rule, eval_res)
            if transformed is None:
                # Message was dropped or deferred by transform policy (e.g. AI fallback DROP or RETRY)
                self.metrics.failed_total += 1
                logger.warning(
                    "[PIPELINE:TRANSFORM] [%s] Rule %s transform policy returned None for msg %s (dropped or deferred)",
                    ctx.correlation_id[:8], rule.id[:8], payload.message_id,
                )
                return
            # Output validation: validate max length
            max_limit = 1024 if payload.has_media else 4096
            if len(transformed) > max_limit:
                transformed = transformed[:max_limit]
            ctx.transformed_text = transformed
            ctx.record_stage(PipelineStage.TRANSFORM, True, reason="transformed", duration_ms=(time.monotonic() - t0) * 1000)
            self.metrics.transformed_total += 1
            logger.info(
                "[PIPELINE:TRANSFORM] [%s] Rule %s transformed msg %s: text_len %d -> %d (ai=%s, took %.2fms)",
                ctx.correlation_id[:8], rule.id[:8], payload.message_id,
                len(payload.effective_text or ""), len(transformed),
                bool(getattr(rule, "ai_rewrite_active", bool(rule.ai_config_id))), (time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            ctx.record_stage(PipelineStage.TRANSFORM, False, reason="transform_error", error=str(exc))
            self.metrics.failed_total += 1
            logger.exception("[%s] Transform error: %s", ctx.correlation_id, exc)
            return

        # 6. ENQUEUE (Durable Delivery Jobs per target)
        t0 = time.monotonic()
        targets = list(getattr(rule, "all_targets", [rule.target_chat_id]) or [rule.target_chat_id])
        if not targets:
            ctx.record_stage(PipelineStage.ENQUEUE, False, reason="no_targets")
            logger.warning(
                "[PIPELINE:ENQUEUE] [%s] Rule %s has no target chats configured — cannot enqueue msg %s",
                ctx.correlation_id[:8], rule.id[:8], payload.message_id,
            )
            return

        enqueued_any = False
        # --- Branding hop (A -> C -> B) ------------------------------------- #
        # The hop runs *after* every transform, on the text this target would
        # otherwise have received, so C holds the processed message (Replace
        # rules, AI rewrite, header/footer, watermark included) and B's forward
        # of C therefore carries the branded *and* processed content. The
        # decision comes from the same shared source the dashboard/bot preview
        # uses; the posting itself is the dispatcher's `perform_branding_hop`, so
        # the queue and direct paths cannot drift apart.
        hop_enabled, hop_reason = False, "branding_hop_unwired"
        if self._branding_hop is not None:
            try:
                hop_enabled, hop_reason = self._branding_hop.branding_decision(payload, rule)
            except Exception as exc:  # a decision failure must never block delivery
                hop_enabled, hop_reason = False, f"branding_decision_failed:{exc}"
        delivery_targets = list(targets)
        if hop_enabled:
            brand_channel = branding.channel_id(rule).strip()
            remaining = [t for t in targets if str(t).strip() != brand_channel]
            if not brand_channel or not remaining or brand_channel == str(payload.chat_id).strip():
                # C is the source, or the rule's only destination: there is no
                # stage 2 to brand, so the message is delivered directly rather
                # than posted into C and left there.
                logger.warning(
                    "[PIPELINE:BRANDING] [%s] Rule %s: hop not possible for msg %s "
                    "(C=%s, remaining targets=%d) — delivering directly",
                    ctx.correlation_id[:8], rule.id[:8], payload.message_id,
                    brand_channel, len(remaining),
                )
                hop_enabled = False
            else:
                # C receives exactly one post; it is not a stage-2 destination.
                delivery_targets = remaining
        hop_plans: Dict[str, Optional[Dict[str, Any]]] = {}

        for target in delivery_targets:
            # Anti-loop per target: with many targets, one of them may be the
            # source itself (or the original forward origin). Skip that one
            # instead of dropping the message for every other target, and say so
            # in the log rather than failing silently.
            try:
                if hasattr(self._routing, "is_loop"):
                    is_loop, loop_reason = self._routing.is_loop(payload, rule, target)
                    if is_loop:
                        # A skip is not a filter: the message still goes to every
                        # other target, so counting it as "filtered" would
                        # overstate what the content rules dropped.
                        logger.info(
                            "[PIPELINE:LOOP_SKIP] [%s] Rule %s msg %s not sent to target %s: %s",
                            ctx.correlation_id[:8], rule.id[:8], payload.message_id, target, loop_reason,
                        )
                        continue
            except Exception as exc:  # never let a guard crash a delivery
                logger.warning("[PIPELINE:LOOP_SKIP] [%s] loop check failed for %s: %s",
                               ctx.correlation_id[:8], target, exc)

            # Per-target content override: one task can route A,B -> X,Y where X
            # and Y want different rules. A target that rejects the message is
            # skipped, not failed — the other targets still receive it, and the
            # task's own transforms are already applied (only the override's
            # rewrite runs here, on this target's copy of the text).
            target_text = ctx.transformed_text
            # Already decided: the task's own policy (and the source override)
            # ran in the FILTER stage, on the *source* message. Re-deciding it
            # here would judge the task's own transforms — a header/footer or a
            # replacement that contains one of the task's own blacklist words
            # used to reject every message the task ever forwarded, silently
            # (Part 2 STEER 05). Only the target-scoped override, which the
            # FILTER stage never saw, is decided here, on this target's copy.
            chain = rule.policy_chain(source_chat_id=payload.chat_id, target_chat_id=target)
            target_reject = None
            for policy in chain:
                if not str(getattr(policy, "source", "") or "").startswith("target:"):
                    continue
                decision = policy.evaluate(target_text or "", m_type)
                if not decision.allowed:
                    target_reject = (policy, decision)
                    break
            if target_reject is not None:
                policy, decision = target_reject
                self.metrics.filtered_total += 1
                ctx.record_stage(PipelineStage.ENQUEUE, False, reason=f"target_filtered:{target}")
                await self._audit(ctx, "forward.filtered", "REJECTED",
                                  reason_code="CONTENT_POLICY_TARGET",
                                  rule_id=rule.id, message_id=str(payload.message_id),
                                  result_summary=f"{policy.source or 'task'}:{decision.reason} -> {target}")
                logger.info(
                    "[PIPELINE:POLICY_TARGET] [%s] Rule %s msg %s NOT sent to target %s (%s at %s)",
                    ctx.correlation_id[:8], rule.id[:8], payload.message_id, target,
                    decision.reason, policy.source or "task",
                )
                continue
            for policy in chain[1:]:
                if policy.is_active:
                    rewritten, notes = policy.transform(target_text or "")
                    if notes:
                        target_text = rewritten
                        logger.info(
                            "[PIPELINE:POLICY_TARGET] [%s] Rule %s rewrote msg %s for target %s at %s: %s",
                            ctx.correlation_id[:8], rule.id[:8], payload.message_id, target,
                            policy.source or "target", notes,
                        )
            # Branding hop: post this target's processed text into C once, then
            # hand the plan to the queue so the worker reproduces the hop. The
            # outcome (including a failure) is cached per distinct text — C must
            # never receive two copies of one message, and a hop miss must fall
            # back to a direct delivery rather than drop the message.
            branding_plan = None
            if hop_enabled and self._branding_hop is not None:
                hop_key = target_text or ""
                if hop_key not in hop_plans:
                    hop_plans[hop_key] = await self._perform_brand_hop(ctx, rule, target_text)
                branding_plan = hop_plans[hop_key]

            if self._queue is not None:
                ok = await self._queue.enqueue_delivery(
                    rule, payload, str(target), target_text, eval_res,
                    correlation_id=ctx.correlation_id,
                    branding=branding_plan,
                )
                if ok:
                    enqueued_any = True
                    job_id = f"{rule.id}:{payload.chat_id}:{payload.message_id}:{target}"
                    ctx.target_jobs[str(target)] = DeliveryJob(
                        id=job_id,
                        rule_id=rule.id,
                        source_chat_id=payload.chat_id,
                        source_message_id=payload.message_id,
                        target_chat_id=str(target),
                    )
                    self.metrics.enqueued_total += 1
                    await self._audit(ctx, "forward.enqueued", "ACCEPTED",
                                      rule_id=rule.id, message_id=str(payload.message_id),
                                      queue_job_id=job_id, result_summary=f"queued to {target}")
                    logger.info(
                        "[PIPELINE:ENQUEUE] [%s] Rule %s enqueued msg %s -> target %s (job_id=%s)",
                        ctx.correlation_id[:8], rule.id[:8], payload.message_id, target, job_id,
                    )
                else:
                    self.metrics.backpressure_drops += 1
                    await self._audit(ctx, "forward.enqueue_failed", "FAILED",
                                      severity="ERROR", reason_code="QUEUE_ENQUEUE_FAILED",
                                      rule_id=rule.id, message_id=str(payload.message_id),
                                      result_summary=f"backpressure drop for target {target}")
                    logger.error(
                        "[PIPELINE:ENQUEUE_DROP] [%s] Rule %s failed to enqueue msg %s for target %s (queue full / backpressure drop)",
                        ctx.correlation_id[:8], rule.id[:8], payload.message_id, target,
                    )
            else:
                # No queue manager configured (standalone/mock mode)
                enqueued_any = True

        # Phase 2: keyword-confirmation / keyword-reply add-on. A matched
        # whitelist keyword schedules the configured reply text to the same
        # targets, as its own durable job. This runs *after* the primary
        # enqueue so a reply failure can never drop or delay the forward
        # itself; the reply is a secondary, best-effort side effect.
        try:
            reply_text = AdvancedSettingsEngine.classify_keyword_trigger(
                payload.effective_text or ctx.transformed_text or "", rule,
            )
            if reply_text:
                # Replies go to the same destinations as the forward, minus C:
                # branding is about the *forward header* of the forwarded
                # message, and a keyword reply is our own plain text (it carries
                # no header to brand), so it must not be posted into C.
                for target in delivery_targets:
                    if self._queue is None:
                        break
                    # job_kind="keyword_reply" gives the reply its own
                    # deliverable key (id + the table's UNIQUE key include the
                    # kind) so it is not deduplicated against the primary
                    # forward for the same target.
                    queued = await self._queue.enqueue_delivery(
                        rule, payload, str(target), reply_text, eval_res,
                        job_kind="keyword_reply",
                        correlation_id=ctx.correlation_id,
                    )
                    if queued is False:
                        # A refused enqueue used to be invisible: the only log
                        # line claimed the reply had shipped. Say what really
                        # happened so an operator can see it in the console.
                        logger.warning(
                            "[PIPELINE:KEYWORD_REPLY] [%s] Rule %s msg %s — reply to %s was refused by the queue",
                            ctx.correlation_id[:8], rule.id[:8], payload.message_id, target,
                        )
                ctx.record_stage(
                    PipelineStage.TRANSFORM, True,
                    reason="keyword_reply:matched",
                )
                logger.info(
                    "[PIPELINE:KEYWORD_REPLY] [%s] Rule %s keyword matched on msg %s — reply enqueued to %d target(s)",
                    ctx.correlation_id[:8], rule.id[:8], payload.message_id, len(delivery_targets),
                )
        except Exception as exc:
            # The keyword reply is an add-on side effect: a failure here must
            # never turn an already-enqueued forward into a pipeline failure.
            logger.warning(
                "[PIPELINE:KEYWORD_REPLY] [%s] Rule %s keyword reply failed (%s) — forward unaffected",
                ctx.correlation_id[:8], rule.id[:8], exc,
            )

        ctx.record_stage(
            PipelineStage.ENQUEUE,
            enqueued_any,
            reason="enqueued" if enqueued_any else "backpressure_or_duplicate",
            duration_ms=(time.monotonic() - t0) * 1000,
        )

        # Mark processed in processed_repo for event-level replay protection
        if enqueued_any and self._processed is not None:
            try:
                await self._processed.mark_processed(
                    rule.id, payload.chat_id, payload.message_id, payload.media_group_id or ""
                )
            except Exception:
                # v12.3 (ACID observability): this marker IS the replay-protection
                # guarantee. Failing to write it does not lose this message
                # (it is already enqueued), but it silently disables idempotency
                # for the replay — a subsequent delivery could be duplicated with
                # no signal anywhere. Log it with the correlation id instead of
                # swallowing it; do not re-raise, as the enqueue already succeeded
                # and must not be rolled back.
                logger.exception(
                    "%s replay-marker write failed — idempotency degraded for "
                    "rule=%s src=%s:%s; duplicate delivery possible on replay",
                    getattr(ctx, "correlation_id", "?"),
                    rule.id[:8], payload.chat_id, payload.message_id,
                )

        # The message reached the delivery queue, so the content fingerprint is
        # earned: forget the claim (it stays in the store) and a later replay of
        # the same content under this rule stays a duplicate.
        if enqueued_any:
            ctx.content_claims.pop(rule.id, None)

    async def _apply_transforms(
        self, ctx: PipelineContext, payload: MessagePayload, rule: ForwardRule, evaluation: EvaluationResult
    ) -> Optional[str]:
        text = payload.effective_text or ""

        # v2.0 app update — Cleaner: strip configured content (patterns, URLs,
        # @mentions, #hashtags, emoji) before any other transform sees the text.
        # Runs first so downstream replacement/AI stages operate on cleaned
        # input and cannot resurrect removed content.
        cleaner = rule.build_cleaner() if hasattr(rule, "build_cleaner") else None
        if cleaner is not None:
            try:
                cleaned = cleaner.clean(text)
                if cleaned != text:
                    logger.info(
                        "[PIPELINE:CLEANER] [%s] Rule %s cleaned msg %s: len %d -> %d",
                        ctx.correlation_id[:8], rule.id[:8], payload.message_id, len(text), len(cleaned),
                    )
                    text = cleaned
                # The inline keyboard is markup, not text: the cleaner cannot
                # reach it from `clean()`, so the option is applied here.
                if getattr(cleaner, "strip_buttons", False) and getattr(payload, "reply_markup", None) is not None:
                    payload.reply_markup = None
                    logger.info(
                        "[PIPELINE:CLEANER] [%s] Rule %s removed the inline keyboard of msg %s",
                        ctx.correlation_id[:8], rule.id[:8], payload.message_id,
                    )
            except Exception as exc:
                # A cleaner failure must never drop the message — deliver
                # uncleaned text and record why.
                logger.warning(
                    "[PIPELINE:CLEANER] [%s] Rule %s cleaner failed (%s) — delivering uncleaned text",
                    ctx.correlation_id[:8], rule.id[:8], exc,
                )

        # Link Management Engine (Entity-aware, domain allowlist/blocklist, SSRF protected)
        link_policy = getattr(rule, "link_policy", "PRESERVE_ALL")
        if (link_policy and link_policy != "PRESERVE_ALL") or getattr(rule, "remove_links", False):
            clean_text, updated_ents, links_mod = LinkManagementEngine.apply_to_payload(
                payload, rule, text=text
            )
            if clean_text is not None:
                # Defensive: the engine must never answer a payload with None.
                text = clean_text
            if updated_ents is not None:
                # The engine shifts UTF-16 offsets for the entities it kept;
                # carrying them on the payload keeps rich formatting aligned
                # with the text that will actually be sent.
                if payload.has_media:
                    payload.caption_entities = updated_ents
                else:
                    payload.entities = updated_ents
            if links_mod:
                evaluation.links_removed = True
        elif getattr(rule, "link_replacement", None):
            text = self._filter_engine.replace_links(text, rule.link_replacement)
            evaluation.links_removed = True

        # Emoji removal
        if getattr(rule, "remove_emojis", False):
            text = self._filter_engine.remove_emojis(text)

        # The cleaner is a text-only stage, so an entity whose range was inside
        # removed text can end up pointing past the end of the body. Telegram
        # rejects such an entity, so it is dropped (or clipped) here.
        self._clamp_entities(payload, text)

        if (payload.effective_text or "").strip() and not text.strip():
            # The stages above removed the whole body (for example the only
            # content was a link the task asked to strip). Forwarding an empty
            # message is not an option and neither is dropping it silently: the
            # message is dropped with a reason code and an audit record.
            ctx.record_stage(PipelineStage.TRANSFORM, False, reason="empty_after_clean")
            self.metrics.filtered_total += 1
            logger.info(
                "[PIPELINE:CLEANER] [%s] Rule %s dropped msg %s: nothing left after cleaning "
                "(reason=empty_after_clean, link_policy=%s)",
                ctx.correlation_id[:8], rule.id[:8], payload.message_id,
                getattr(rule, "link_policy", "PRESERVE_ALL"),
            )
            await self._audit(
                ctx, "forward.filtered", "REJECTED", reason_code="EMPTY_AFTER_CLEAN",
                rule_id=rule.id, message_id=str(payload.message_id),
                result_summary="cleaner:empty_after_clean",
            )
            return None

        # Replacements are applied once, in the final pass below (after the AI
        # stage), so an expanding rule (`# -> ##`) is not applied twice and a
        # rewrite cannot reintroduce text the task asked to replace.

        # Per-task content rules: remove-links / strip words / replacements.
        # Runs after the legacy cleaner + link engine so the policy sees the
        # text that will actually be sent, and before the AI stage so a rewrite
        # can never resurrect content the task asked to remove.
        if hasattr(rule, "content_policy"):
            try:
                policy_text, policy_notes = rule.content_policy.transform(text)
                if policy_notes:
                    logger.info(
                        "[PIPELINE:POLICY] [%s] Rule %s rewrote msg %s at task policy: %s (len %d -> %d)",
                        ctx.correlation_id[:8], rule.id[:8], payload.message_id,
                        policy_notes, len(text), len(policy_text),
                    )
                    if policy_notes.get("links_removed"):
                        evaluation.links_removed = True
                    text = policy_text
            except Exception as exc:
                # A content rule must never drop a message by crashing: deliver
                # the text we have and record the failure.
                logger.warning(
                    "[PIPELINE:POLICY] [%s] Rule %s content rules failed (%s) — delivering unmodified text",
                    ctx.correlation_id[:8], rule.id[:8], exc,
                )

        # AI Transformation (Modular, non-blocking, multi-layer defended).
        # The gate is the rule's single AI authority: a configured config id is
        # not enough, because the task panel can switch rewriting off (and the
        # paid-addon switch can be closed) while the config stays attached.
        ai_cfg_id = getattr(rule, "ai_config_id", None)
        if ai_cfg_id and not getattr(rule, "ai_rewrite_active", True):
            logger.info(
                "[PIPELINE:AI_REWRITE:OFF] [%s] Rule %s keeps cfg=%s but rewrite is disabled "
                "(smart_ai_rewrite=%s, paid_addons=%s) — delivering unmodified text",
                ctx.correlation_id[:8], rule.id[:8], ai_cfg_id,
                bool(getattr(rule, "smart_ai_rewrite", False)),
                (getattr(rule, "paid_addons", {}) or {}).get("ai_rewrite")
                if isinstance(getattr(rule, "paid_addons", None), dict) else None,
            )
            ai_cfg_id = None
        if ai_cfg_id:
            ai_transformed = await self._run_ai_transform(ctx, text, rule, payload, evaluation)
            if ai_transformed is None:
                # Dropped or quarantined according to fallback policy
                return None
            text = ai_transformed

        # Final Replacements: the single authority for `rule.replacements`.
        # Applied after the AI stage (so a rewrite cannot resurrect replaced
        # text) and before the header/footer (so a task rule never rewrites the
        # task's own header).
        if getattr(rule, "replacements", None):
            text = self._filter_engine.replace_text(text, rule.replacements)

        # Header & Footer
        header = getattr(rule, "header", "") or getattr(rule, "header_text", "")
        footer = getattr(rule, "footer", "") or getattr(rule, "footer_text", "")
        if header or footer:
            text = self._filter_engine.apply_header_footer(text, header=header, footer=footer)

        # Phase 2: advanced task-overview content transforms (watermark, link
        # buttons, affiliate kit). These run AFTER the header/footer so the
        # watermark anchors the very end of the rendered body.
        try:
            text, applied = AdvancedSettingsEngine.apply_content_transforms(text, rule, payload)
            if applied:
                ctx.record_stage(
                    PipelineStage.TRANSFORM,
                    True,
                    reason="advanced_settings:" + ",".join(applied),
                )
                logger.info(
                    "[PIPELINE:ADVANCED] [%s] Rule %s applied advanced transforms %s to msg %s",
                    ctx.correlation_id[:8], rule.id[:8], applied, payload.message_id,
                )
        except Exception as exc:
            # A content transform must never drop the message: log the failure
            # and deliver the pre-transform text.
            logger.warning(
                "[PIPELINE:ADVANCED] [%s] Rule %s advanced transform failed (%s) — delivering untransformed text",
                ctx.correlation_id[:8], rule.id[:8], exc,
            )

        # Every text transform above (AI rewrite, replacements, header/footer,
        # watermark) can move the body under the entity ranges again; clamp once
        # more so what leaves the pipeline is always sendable.
        self._clamp_entities(payload, text)

        return text

    @staticmethod
    def _clamp_entities(payload: MessagePayload, text: str) -> None:
        """Keep the payload's entities inside the body they annotate.

        The cleaner and the other text stages are text-only, so they can delete
        a span an entity pointed at. An entity that runs past the end of the
        body makes Telegram reject the whole send, so an out-of-range entity is
        dropped and an entity that straddles the end is clipped.
        """
        attr = "caption_entities" if payload.has_media else "entities"
        entities = getattr(payload, attr, None)
        if not entities:
            return
        limit = utf16_len(text or "")
        kept = []
        for ent in entities:
            try:
                offset = int(getattr(ent, "offset", 0) or 0)
                length = int(getattr(ent, "length", 0) or 0)
            except (TypeError, ValueError):
                continue
            if offset < 0 or offset >= limit or length <= 0:
                continue
            if offset + length > limit:
                try:
                    ent.length = limit - offset
                except Exception:
                    continue
            kept.append(ent)
        try:
            setattr(payload, attr, kept or None)
        except Exception:
            # A read-only payload shim must not break delivery.
            return

    async def _run_ai_transform(
        self,
        ctx: PipelineContext,
        text: str,
        rule: ForwardRule,
        payload: MessagePayload,
        evaluation: EvaluationResult,
    ) -> Optional[str]:
        self.metrics.ai_transforms_total += 1

        # Check if production AITransformer is available
        if self._ai_transformer is None:
            # Fallback legacy provider if available
            if self._ai_repo and self._ai_factory:
                try:
                    ai_cfg = await self._ai_repo.get_by_id(rule.ai_config_id)
                    if ai_cfg and ai_cfg.is_enabled:
                        provider = self._ai_factory.get(ai_cfg)
                        rewritten = await provider.rewrite(ai_cfg, text)
                        if rewritten:
                            evaluation.rewritten_text = rewritten
                            self.metrics.ai_transforms_success += 1
                            return rewritten
                except Exception as exc:
                    logger.warning("[%s] Legacy AI transform failed: %s", ctx.correlation_id, exc)
            return self._handle_ai_fallback(ctx, text, rule, "AI_UNAVAILABLE", "AI transformer not initialized")

        # Resolve Prompt Template and Version Pinning
        system_prompt = ""
        user_template = "{text}"
        prompt_ver = getattr(rule, "ai_prompt_version", 0) or 0
        prompt_id = ""
        if isinstance(rule.metadata, dict) and rule.metadata.get("prompt_id"):
            prompt_id = str(rule.metadata.get("prompt_id"))

        if self._prompt_service and prompt_id:
            try:
                preview = await self._prompt_service.preview_prompt(
                    prompt_id, text, version=prompt_ver if prompt_ver > 0 else None
                )
                system_prompt = preview.get("system_prompt", "")
                user_template = preview.get("rendered_user_prompt", "{text}")
                prompt_ver = preview.get("version", prompt_ver or 1)
            except Exception as exc:
                logger.warning("[%s] Could not resolve prompt '%s': %s", ctx.correlation_id, prompt_id, exc)

        from ..infrastructure.ai.transformer import AIRequest

        req = AIRequest(
            text=text,
            system_prompt=system_prompt,
            user_template=user_template,
            prompt_id=prompt_id,
            prompt_version=prompt_ver or 1,
            target_language="auto",
            max_length=1024 if payload.has_media else 4096,
            timeout_seconds=getattr(rule, "ai_timeout_seconds", 15.0) or 15.0,
            correlation_id=ctx.correlation_id,
            is_caption=payload.has_media,
        )

        logger.info(
            "[PIPELINE:AI_REWRITE:START] [%s] Rule %s initiating AI rewrite (cfg=%s, prompt=%s:v%s, tmo=%.1fs, caption=%s)",
            ctx.correlation_id[:8],
            rule.id[:8],
            rule.ai_config_id,
            prompt_id or "default",
            prompt_ver or 1,
            req.timeout_seconds,
            payload.has_media,
        )

        resp = await self._ai_transformer.transform(
            req,
            primary_config_id=rule.ai_config_id,
            secondary_config_id=getattr(rule, "ai_secondary_config_id", None),
        )

        if resp.success:
            self.metrics.ai_transforms_success += 1
            evaluation.rewritten_text = resp.transformed_text
            logger.info(
                "[PIPELINE:AI_REWRITE:SUCCESS] [%s] Rule %s transformed %d chars in %.2fms (model=%s, failover=%s)",
                ctx.correlation_id[:8],
                rule.id[:8],
                len(resp.transformed_text or ""),
                getattr(resp, "execution_duration_ms", 0.0),
                getattr(resp, "model_used", "") or "unknown",
                getattr(resp, "is_failover", False),
            )
            return resp.transformed_text

        # AI Transformation Failed -> Execute fallback policy
        self.metrics.ai_transforms_failed += 1
        logger.warning(
            "[PIPELINE:AI_REWRITE:FAILED] [%s] Rule %s AI transform failed (code=%s, err=%s)",
            ctx.correlation_id[:8],
            rule.id[:8],
            resp.error_code,
            resp.error_message,
        )
        return self._handle_ai_fallback(ctx, text, rule, resp.error_code, resp.error_message)

    def _handle_ai_fallback(
        self,
        ctx: PipelineContext,
        original_text: str,
        rule: ForwardRule,
        error_code: str,
        error_message: str,
    ) -> Optional[str]:
        policy = getattr(rule, "ai_fallback_policy", AIFallbackPolicy.DROP)
        if isinstance(policy, str):
            try:
                policy = AIFallbackPolicy(policy)
            except ValueError:
                policy = AIFallbackPolicy.DROP

        if policy == AIFallbackPolicy.SEND_ORIGINAL:
            self.metrics.ai_fallback_send_original += 1
            logger.warning(
                "[PIPELINE:AI_REWRITE:FALLBACK] [%s] Rule %s AI failed (%s: %s). Policy: SEND_ORIGINAL -> forwarding original text (%d chars).",
                ctx.correlation_id[:8],
                rule.id[:8],
                error_code,
                error_message,
                len(original_text or ""),
            )
            return original_text

        if policy == AIFallbackPolicy.DROP:
            self.metrics.ai_fallback_drops += 1
            logger.warning(
                "[PIPELINE:AI_REWRITE:FALLBACK] [%s] Rule %s AI failed (%s: %s). Policy: DROP -> dropping message fail-closed.",
                ctx.correlation_id[:8],
                rule.id[:8],
                error_code,
                error_message,
            )
            ctx.record_stage(
                PipelineStage.TRANSFORM,
                False,
                reason=f"ai_drop:{error_code}",
                error=error_message,
            )
            return None

        if policy == AIFallbackPolicy.RETRY:
            self.metrics.ai_fallback_retries += 1
            logger.warning(
                "[PIPELINE:AI_REWRITE:FALLBACK] [%s] Rule %s AI failed (%s: %s). Policy: RETRY.",
                ctx.correlation_id[:8],
                rule.id[:8],
                error_code,
                error_message,
            )
            ctx.record_stage(
                PipelineStage.TRANSFORM,
                False,
                reason=f"ai_retry:{error_code}",
                error=error_message,
            )
            return None

        if policy == AIFallbackPolicy.QUARANTINE:
            self.metrics.ai_fallback_quarantine += 1
            logger.warning(
                "[%s] AI transform failed (%s: %s). Policy: QUARANTINE.",
                ctx.correlation_id,
                error_code,
                error_message,
            )
            ctx.record_stage(
                PipelineStage.TRANSFORM,
                False,
                reason=f"ai_quarantine:{error_code}",
                error=error_message,
            )
            return None

        return None
