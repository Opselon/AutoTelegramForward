"""AutoReplyControlServicer — the gRPC surface of the v28 auto-reply subsystem.

Every RPC here is tenant-scoped: ``owner_user_id`` arrives already resolved by
the API's authenticated identity and is passed straight through to the
application service, which refuses ``0``. Nothing in this file reads another
tenant's row, and nothing returns "not found" for an id that belongs to
somebody else in a way that would confirm it exists.

Error mapping is deliberate: a client must be able to tell "you haven't linked
a provider" (FAILED_PRECONDITION) from "that id is not yours" (NOT_FOUND from a
foreign id, because it does not exist *for you*).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import grpc

from ...proto import autoforward_pb2 as pb
from ...proto import autoforward_pb2_grpc as pb_grpc

logger = logging.getLogger("atf.autoreply.grpc")

#: AutoReplyError.code -> gRPC status. Anything unmapped is INVALID_ARGUMENT:
#: a request the service understood and refused is the caller's to fix.
_CODE_TO_STATUS = {
    "chat_required": grpc.StatusCode.INVALID_ARGUMENT,
    "content_required": grpc.StatusCode.INVALID_ARGUMENT,
    "sample_required": grpc.StatusCode.INVALID_ARGUMENT,
    "query_required": grpc.StatusCode.INVALID_ARGUMENT,
    "owner_required": grpc.StatusCode.UNAUTHENTICATED,
    "prompt_not_found": grpc.StatusCode.NOT_FOUND,
    "rule_not_found": grpc.StatusCode.NOT_FOUND,
    "job_not_found": grpc.StatusCode.NOT_FOUND,
    "target_not_found": grpc.StatusCode.NOT_FOUND,
    "session_not_found": grpc.StatusCode.NOT_FOUND,
    "ai_config_not_found": grpc.StatusCode.NOT_FOUND,
    "prompt_name_taken": grpc.StatusCode.ALREADY_EXISTS,
    "prompt_invalid": grpc.StatusCode.INVALID_ARGUMENT,
    "import_invalid": grpc.StatusCode.INVALID_ARGUMENT,
    "import_too_large": grpc.StatusCode.RESOURCE_EXHAUSTED,
    "job_not_cancellable": grpc.StatusCode.FAILED_PRECONDITION,
    "no_ai_config": grpc.StatusCode.FAILED_PRECONDITION,
}


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _settings_pb(s: Any) -> "pb.AutoReplySettings":
    return pb.AutoReplySettings(
        owner_user_id=_int(_field(s, "owner_user_id")),
        enabled=bool(_field(s, "enabled", False)),
        ai_config_id=_str(_field(s, "ai_config_id")),
        prompt_id=_str(_field(s, "prompt_id")),
        target_mode=_str(getattr(_field(s, "target_mode", ""), "value",
                                 _field(s, "target_mode", ""))),
        language=_str(_field(s, "language")),
        tone=_str(getattr(_field(s, "tone", ""), "value", _field(s, "tone", ""))),
        response_length=_str(getattr(_field(s, "response_length", ""), "value",
                                     _field(s, "response_length", ""))),
        temperature=float(_field(s, "temperature", 0.7) or 0.7),
        max_tokens=_int(_field(s, "max_tokens", 300)),
        context_messages=_int(_field(s, "context_messages", 6)),
        memory_enabled=bool(_field(s, "memory_enabled", True)),
        memory_max_facts=_int(_field(s, "memory_max_facts", 12)),
        extra_instructions=_str(_field(s, "extra_instructions")),
        max_reply_chars=_int(_field(s, "max_reply_chars", 1200)),
        typing_simulation=bool(_field(s, "typing_simulation", True)),
        typing_delay_min=float(_field(s, "typing_delay_min", 1.5) or 0.0),
        typing_delay_max=float(_field(s, "typing_delay_max", 4.0) or 0.0),
        cooldown_seconds=_int(_field(s, "cooldown_seconds", 15)),
        daily_limit_per_chat=_int(_field(s, "daily_limit_per_chat", 0)),
        rate_limit_per_minute=_int(_field(s, "rate_limit_per_minute", 20)),
        max_concurrency=_int(_field(s, "max_concurrency", 3)),
        max_attempts=_int(_field(s, "max_attempts", 3)),
        retry_backoff_seconds=float(_field(s, "retry_backoff_seconds", 20.0) or 0.0),
        quiet_hours_start=_int(_field(s, "quiet_hours_start", -1)),
        quiet_hours_end=_int(_field(s, "quiet_hours_end", -1)),
        timezone_offset_minutes=_int(_field(s, "timezone_offset_minutes", 0)),
        active_days=[_int(d) for d in (_field(s, "active_days", []) or [])],
        fallback_text=_str(_field(s, "fallback_text")),
        ignore_bots=bool(_field(s, "ignore_bots", True)),
        version=_int(_field(s, "version", 1)),
        created_at=_int(_field(s, "created_at")),
        updated_at=_int(_field(s, "updated_at")),
    )


def _prompt_pb(p: Any) -> "pb.AutoReplyPrompt":
    return pb.AutoReplyPrompt(
        id=_str(_field(p, "id")),
        name=_str(_field(p, "name")),
        description=_str(_field(p, "description")),
        system_prompt=_str(_field(p, "system_prompt")),
        user_prompt_template=_str(_field(p, "user_prompt_template"), "{text}"),
        language=_str(_field(p, "language")),
        tone=_str(getattr(_field(p, "tone", ""), "value", _field(p, "tone", ""))),
        response_length=_str(getattr(_field(p, "response_length", ""), "value",
                                     _field(p, "response_length", ""))),
        tags=[_str(t) for t in (_field(p, "tags", []) or [])],
        category=_str(_field(p, "category")),
        is_default=bool(_field(p, "is_default", False)),
        is_archived=bool(_field(p, "is_archived", False)),
        usage_count=_int(_field(p, "usage_count", 0)),
        created_at=_int(_field(p, "created_at")),
        updated_at=_int(_field(p, "updated_at")),
    )


def _target_pb(t: Any) -> "pb.AutoReplyTarget":
    return pb.AutoReplyTarget(
        chat_id=_str(_field(t, "chat_id")),
        title=_str(_field(t, "title")),
        username=_str(_field(t, "username")),
        is_enabled=bool(_field(t, "is_enabled", True)),
        prompt_id=_str(_field(t, "prompt_id")),
        note=_str(_field(t, "note")),
        priority=_int(_field(t, "priority")),
        added_at=_int(_field(t, "created_at")),
    )


def _rule_pb(r: Any) -> "pb.AutoReplyRule":
    return pb.AutoReplyRule(
        id=_str(_field(r, "id")),
        name=_str(_field(r, "name")),
        is_enabled=bool(_field(r, "is_enabled", True)),
        priority=_int(_field(r, "priority")),
        action=_str(getattr(_field(r, "action", ""), "value", _field(r, "action", "REPLY"))),
        conditions_json=json.dumps(_field(r, "conditions", {}) or {}, ensure_ascii=False),
        exclusions_json=json.dumps(_field(r, "exclusions", {}) or {}, ensure_ascii=False),
        prompt_id=_str(_field(r, "prompt_id")),
        ai_config_id=_str(_field(r, "ai_config_id")),
        cooldown_seconds=_int(_field(r, "cooldown_seconds", -1)),
        language=_str(_field(r, "language")),
        tone=_str(getattr(_field(r, "tone", "") or "", "value", _field(r, "tone", ""))),
        response_length=_str(getattr(_field(r, "response_length", "") or "", "value",
                                     _field(r, "response_length", ""))),
        temperature=float(_field(r, "temperature", 0.0) or 0.0),
        max_tokens=_int(_field(r, "max_tokens", 0)),
        created_at=_int(_field(r, "created_at")),
        updated_at=_int(_field(r, "updated_at")),
    )


def _job_pb(j: Any) -> "pb.AutoReplyJob":
    return pb.AutoReplyJob(
        id=_str(_field(j, "id")),
        chat_id=_str(_field(j, "chat_id")),
        chat_title=_str(_field(j, "chat_title")),
        status=_str(getattr(_field(j, "status", ""), "value", _field(j, "status", ""))),
        attempt=_int(_field(j, "attempt")),
        max_attempts=_int(_field(j, "max_attempts", 3)),
        error=_str(_field(j, "error")),
        error_code=_str(_field(j, "error_code")),
        inbound_text=_str(_field(j, "inbound_text")),
        reply_text=_str(_field(j, "reply_text")),
        correlation_id=_str(_field(j, "correlation_id")),
        latency_ms=_int(_field(j, "latency_ms")),
        created_at=_int(_field(j, "created_at")),
        sent_at=_int(_field(j, "sent_at")),
        source_message_id=_int(_field(j, "source_message_id")),
    )


#: Settings fields a client may patch through `update_mask`. Kept explicit so a
#: typo cannot silently write nothing, and so no unknown key ever reaches SQL.
_SETTINGS_FIELDS = (
    "enabled", "ai_config_id", "prompt_id", "target_mode", "language", "tone",
    "response_length", "temperature", "max_tokens", "context_messages",
    "memory_enabled", "memory_max_facts", "extra_instructions", "max_reply_chars",
    "typing_simulation", "typing_delay_min", "typing_delay_max", "cooldown_seconds",
    "daily_limit_per_chat", "rate_limit_per_minute", "max_concurrency",
    "max_attempts", "retry_backoff_seconds", "quiet_hours_start", "quiet_hours_end",
    "timezone_offset_minutes", "active_days", "fallback_text", "ignore_bots",
)


class AutoReplyControlServicer(pb_grpc.AutoReplyControlServiceServicer):
    def __init__(self, service: Any, engine: Any = None) -> None:
        self._service = service
        self._engine = engine

    # -- plumbing ---------------------------------------------------------
    async def _abort(self, context: Any, exc: Exception) -> None:
        """Translate an AutoReplyError into a status and stop the call.

        ``context.abort`` is a coroutine in grpc.aio and only raises once it is
        awaited, so every call site must ``await`` this helper — returning
        without aborting would send a normal reply built from ``None``.
        """
        code = getattr(exc, "code", "invalid")
        message = getattr(exc, "message", "") or str(exc)
        status = _CODE_TO_STATUS.get(code, grpc.StatusCode.INVALID_ARGUMENT)
        await context.abort(status, f"{code}: {message}" if message else code)

    @staticmethod
    def _owner(request: Any) -> int:
        return _int(getattr(request, "owner_user_id", 0), 0)

    # -- settings ---------------------------------------------------------
    async def GetAutoReplySettings(self, request, context):
        owner = self._owner(request)
        if not owner:
            return pb.AutoReplySettings(owner_user_id=0, target_mode="EVERYONE",
                                        tone="friendly", response_length="short")
        try:
            return _settings_pb(self._service.get_settings(owner))
        except Exception as exc:  # noqa: BLE001 - mapped, never swallowed
            await self._abort(context, exc)

    async def UpdateAutoReplySettings(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        s = request.settings
        mask = [m for m in (request.update_mask or []) if m in _SETTINGS_FIELDS]
        if not mask:
            # No mask: treat every non-zero field as present, which is what a
            # client that filled the form and sent it expects.
            mask = [f for f in _SETTINGS_FIELDS if _is_present(s, f)]
        patch: Dict[str, Any] = {}
        for field_name in mask:
            patch[field_name] = getattr(s, field_name)
        try:
            return _settings_pb(self._service.update_settings(owner, patch))
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)

    # -- prompts ----------------------------------------------------------
    async def ListAutoReplyPrompts(self, request, context):
        owner = self._owner(request)
        if not owner:
            return pb.ListAutoReplyPromptsResponse(prompts=[])
        try:
            rows = self._service.list_prompts(
                owner, search=_str(request.search),
                include_archived=bool(request.include_archived),
            )
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.ListAutoReplyPromptsResponse(prompts=[_prompt_pb(p) for p in rows])

    async def SaveAutoReplyPrompt(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        data = _prompt_from_pb(request.prompt)
        try:
            return _prompt_pb(self._service.save_prompt(owner, data))
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)

    async def DeleteAutoReplyPrompt(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            self._service.delete_prompt(owner, request.prompt_id)
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.StatusResponse(success=True, message="deleted")

    async def DuplicateAutoReplyPrompt(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            clone = self._service.duplicate_prompt(owner, request.prompt_id, _str(request.name))
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return _prompt_pb(clone)

    async def ExportAutoReplyPrompts(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            document = self._service.export_prompts(owner, list(request.prompt_ids or []))
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.ExportAutoReplyPromptsResponse(
            document_json=json.dumps(document, ensure_ascii=False)
        )

    async def ImportAutoReplyPrompts(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            document = json.loads(str(request.document_json or ""))
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "import_invalid: not json")
        try:
            result = self._service.import_prompts(owner, document)
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.ImportAutoReplyPromptsResponse(
            imported=_int(result.get("imported")),
            skipped=_int(result.get("skipped")),
            prompts=[_prompt_pb(p) for p in result.get("prompts", [])],
        )

    # -- targets ----------------------------------------------------------
    async def ListAutoReplyTargets(self, request, context):
        owner = self._owner(request)
        if not owner:
            return pb.ListAutoReplyTargetsResponse(targets=[], total=0, enabled=0)
        mode = _str(request.mode).lower()
        try:
            rows = self._service.list_targets(
                owner, search=_str(request.search), enabled_only=(mode == "enabled"),
            )
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        if mode == "disabled":
            rows = [t for t in rows if not t.is_enabled]
        enabled = sum(1 for t in rows if t.is_enabled)
        total = len(rows)
        offset = max(0, _int(request.offset))
        limit = _int(request.limit) or total
        window = rows[offset:offset + max(1, limit)] if limit else rows[offset:]
        return pb.ListAutoReplyTargetsResponse(
            targets=[_target_pb(t) for t in window], total=total, enabled=enabled,
        )

    async def AddAutoReplyTargets(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        entries = []
        for t in request.targets:
            entries.append({
                "chat_id": t.chat_id, "title": t.title, "username": t.username,
                "is_enabled": bool(t.is_enabled), "prompt_id": t.prompt_id or "",
                "note": t.note, "priority": _int(t.priority),
            })
        try:
            self._service.add_targets(owner, entries)
            rows = self._service.list_targets(owner)
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.ListAutoReplyTargetsResponse(
            targets=[_target_pb(t) for t in rows],
            total=len(rows), enabled=sum(1 for t in rows if t.is_enabled),
        )

    async def UpdateAutoReplyTarget(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        t = request.target
        try:
            updated = self._service.set_target_enabled(owner, t.chat_id, bool(t.is_enabled))
            # The remaining editable fields are patched through add_targets'
            # upsert, so one call updates title/note/prompt/priority too.
            if t.title or t.note or t.prompt_id or t.priority:
                self._service.add_targets(owner, [{
                    "chat_id": t.chat_id, "title": t.title, "username": t.username,
                    "is_enabled": bool(t.is_enabled), "prompt_id": t.prompt_id or "",
                    "note": t.note, "priority": _int(t.priority),
                }])
                updated = self._service.list_targets(owner)
                updated = next((x for x in updated if x.chat_id == t.chat_id), updated[0]
                               if updated else None)
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        if updated is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "target_not_found")
        return _target_pb(updated)

    async def RemoveAutoReplyTargets(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            removed = self._service.remove_targets(owner, list(request.chat_ids or []))
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.StatusResponse(success=True, message=f"removed={removed}")

    async def ResolveAutoReplyChat(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            found = await self._service.resolve_chat(
                owner, request.query, session_id=_str(request.session_id),
            )
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        if not found:
            return pb.ResolveAutoReplyChatResponse(found=False, error_code="not_found")
        target = pb.AutoReplyTarget(
            chat_id=_str(found.get("chat_id")), title=_str(found.get("title")),
            username=_str(found.get("username")),
        )
        return pb.ResolveAutoReplyChatResponse(found=True, target=target)

    # -- rules ------------------------------------------------------------
    async def ListAutoReplyRules(self, request, context):
        owner = self._owner(request)
        if not owner:
            return pb.ListAutoReplyRulesResponse(rules=[])
        try:
            rows = self._service.list_rules(owner)
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.ListAutoReplyRulesResponse(rules=[_rule_pb(r) for r in rows])

    async def SaveAutoReplyRule(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        r = request.rule
        data = {
            "id": r.id, "name": r.name, "is_enabled": bool(r.is_enabled),
            "priority": _int(r.priority), "action": r.action or "REPLY",
            "conditions": _loads(r.conditions_json), "exclusions": _loads(r.exclusions_json),
            "prompt_id": r.prompt_id or "", "ai_config_id": r.ai_config_id or "",
            "cooldown_seconds": _int(r.cooldown_seconds, -1),
            "language": r.language, "tone": r.tone or "",
            "response_length": r.response_length or "",
            "temperature": float(r.temperature or 0.0),
            "max_tokens": _int(r.max_tokens),
        }
        try:
            return _rule_pb(self._service.save_rule(owner, data))
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)

    async def DeleteAutoReplyRule(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            self._service.delete_rule(owner, request.rule_id)
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.StatusResponse(success=True, message="deleted")

    async def ReorderAutoReplyRules(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            rows = self._service.reorder_rules(owner, list(request.rule_ids or []))
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.ListAutoReplyRulesResponse(rules=[_rule_pb(r) for r in rows])

    # -- jobs -------------------------------------------------------------
    async def ListAutoReplyJobs(self, request, context):
        owner = self._owner(request)
        if not owner:
            return pb.ListAutoReplyJobsResponse(jobs=[], total=0)
        try:
            jobs, total = self._service.list_jobs(
                owner, status=_str(request.status), limit=_int(request.limit) or 50,
                offset=_int(request.offset), chat_id=_str(request.chat_id),
            )
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.ListAutoReplyJobsResponse(jobs=[_job_pb(j) for j in jobs], total=total)

    async def CancelAutoReplyJob(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            self._service.cancel_job(owner, request.job_id)
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.StatusResponse(success=True, message="cancelled")

    async def RetryAutoReplyJob(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            self._service.retry_job(owner, request.job_id)
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.StatusResponse(success=True, message="requeued")

    # -- stats / preview --------------------------------------------------
    async def GetAutoReplyStats(self, request, context):
        owner = self._owner(request)
        if not owner:
            return pb.AutoReplyStatsResponse(status_json="{}")
        try:
            stats = self._service.stats(owner)
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        counts = stats.get("jobs", {}) or {}
        return pb.AutoReplyStatsResponse(
            sent_today=_int(stats.get("sent_24h")),
            sent_total=_int(stats.get("sent_total")),
            pending=_int(counts.get("PENDING")),
            retrying=_int(counts.get("RETRY_WAIT")),
            dead_letter=_int(counts.get("DEAD_LETTER")),
            chats_enabled=_int(stats.get("chats_enabled")),
            prompts=_int(stats.get("prompts")),
            rules=_int(stats.get("rules")),
            average_latency_ms=float(stats.get("avg_latency_ms") or 0.0),
            last_sent_at=_int(stats.get("last_sent_at")),
            status_json=json.dumps(counts, ensure_ascii=False),
        )

    async def PreviewAutoReply(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_required")
        try:
            result = await self._service.preview(
                owner, request.inbound_text,
                prompt_id=_str(request.prompt_id), ai_config_id=_str(request.ai_config_id),
            )
        except Exception as exc:  # noqa: BLE001
            await self._abort(context, exc)
        return pb.PreviewAutoReplyResponse(
            ok=bool(result.ok), reply_text=_str(result.text), error=_str(result.error),
            error_code=_str(result.error_code), system_prompt="",
            provider=_str(result.provider), model=_str(result.model),
            latency_ms=_int(result.latency_ms),
            tokens=_int((result.token_usage or {}).get("total_tokens")),
        )


def _loads(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _prompt_from_pb(p: Any) -> Dict[str, Any]:
    data = {
        "name": p.name, "description": p.description,
        "system_prompt": p.system_prompt,
        "user_prompt_template": p.user_prompt_template or "{text}",
        "language": p.language, "tone": p.tone, "response_length": p.response_length,
        "tags": list(p.tags or []), "category": p.category,
        "is_default": bool(p.is_default), "is_archived": bool(p.is_archived),
    }
    if p.id:
        data["id"] = p.id
    return data


def _is_present(settings: Any, field_name: str) -> bool:
    """True when the client actually set this field (non-zero / non-empty)."""
    value = getattr(settings, field_name, None)
    if isinstance(value, bool):
        return value is True
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, tuple)):
        return len(value) > 0
    return bool(str(value or "").strip())


__all__ = ["AutoReplyControlServicer"]
