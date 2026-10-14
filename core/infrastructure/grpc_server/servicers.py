"""gRPC servicers implementing the shared proto contract over the use cases."""

import time

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import grpc

logger = logging.getLogger(__name__)

from ...proto import pb, pb_grpc  # noqa: F401
from ...application.backup_service import (  # noqa: E402
    decrypt_bundle,
    deserialize_bundle,
    encrypt_bundle,
    serialize_bundle,
)


def _status(success: bool, message: str = "", error_code: str = "") -> pb.StatusResponse:
    return pb.StatusResponse(success=success, message=message, error_code=error_code)


def _session_info(s) -> pb.SessionInfo:
    return pb.SessionInfo(
        id=s.id, phone_number=s.phone_number, user_id=s.user_id,
        username=s.username, first_name=s.first_name,
        is_active=s.is_active, is_authorized=s.is_authorized,
        created_at=s.created_at, updated_at=s.updated_at,
        owner_user_id=int(getattr(s, "owner_user_id", 0) or 0),
    )


def _rule_pb(r) -> pb.ForwardRule:
    import json as _json
    from ...domain.value_objects import ForwardMode as _FM

    def _mode_val(v):
        if v is None:
            return pb.ForwardMode.FORWARD_MODE_UNKNOWN
        name = getattr(v, "name", None)
        if name and _FM.__members__.get(name) is not None:
            try:
                return pb.ForwardMode.Value(name)
            except ValueError:
                pass
        raw = str(getattr(v, "value", v))
        mapping = {
            "DIRECT_FORWARD": pb.ForwardMode.DIRECT_FORWARD,
            "COPY_MESSAGE": pb.ForwardMode.COPY_MESSAGE,
            "CUSTOM_HEADER_COPY": pb.ForwardMode.CUSTOM_HEADER_COPY,
        }
        return mapping.get(raw, pb.ForwardMode.COPY_MESSAGE)

    criteria = getattr(r, "detection_criteria", None)
    criteria_json = _json.dumps(criteria) if isinstance(criteria, dict) else "{}"

    try:
        tgt_ids = list(getattr(r, "target_chat_ids", []) or [])
    except Exception:
        tgt_ids = []

    return pb.ForwardRule(
        id=r.id, session_id=r.session_id,
        source_chat_id=r.source_chat_id, source_chat_name=r.source_chat_name,
        target_chat_id=r.target_chat_id, target_chat_name=r.target_chat_name,
        routing_type=pb.RoutingType.Value(r.routing_type.name),
        forward_mode=_mode_val(getattr(r, "forward_mode", None)),
        is_active=r.is_active, filter_rule_id=r.filter_rule_id or "",
        ai_config_id=r.ai_config_id or "", remove_links=r.remove_links,
        custom_caption_template=r.custom_caption_template,
        created_at=r.created_at, updated_at=r.updated_at,
        # Smart Forwarding Rules (v8)
        name=getattr(r, "name", "") or "",
        description=getattr(r, "description", "") or "",
        target_chat_ids=tgt_ids,
        message_category=getattr(r, "message_category", "ALL") or "ALL",
        intermediate_channel_id=getattr(r, "intermediate_channel_id", "") or "",
        intermediate_channel_name=getattr(r, "intermediate_channel_name", "") or "",
        use_intermediate=bool(getattr(r, "use_intermediate", False)),
        fallback_mode=_mode_val(getattr(r, "fallback_mode", None)),
        fallback_enabled=bool(getattr(r, "fallback_enabled", True)),
        detection_criteria_json=criteria_json,
        priority=int(getattr(r, "priority", 10) or 10),
        execution_order=int(getattr(r, "execution_order", 0) or 0),
        multi_route=bool(getattr(r, "multi_route", False)),
        media_handling=getattr(r, "media_handling", "AUTO") or "AUTO",
        dedupe_policy=getattr(r, "dedupe_policy", "STRICT") or "STRICT",
        max_retries=int(getattr(r, "max_retries", 3) or 3),
        retry_backoff_base=float(getattr(r, "retry_backoff_base", 2.0) or 2.0),
        rate_limit_per_minute=int(getattr(r, "rate_limit_per_minute", 0) or 0),
        rate_limit_burst=int(getattr(r, "rate_limit_burst", 0) or 0),
        custom_header=getattr(r, "custom_header", "") or "",
        custom_footer=getattr(r, "custom_footer", "") or "",
        header_enabled=bool(getattr(r, "header_enabled", False)),
        template_text=getattr(r, "template_text", "") or "",
        template_media=getattr(r, "template_media", "") or "",
        template_album=getattr(r, "template_album", "") or "",
        caption_max_length=int(getattr(r, "caption_max_length", 0) or 0),
        preserve_signature=bool(getattr(r, "preserve_signature", False)),
        is_paused=bool(getattr(r, "is_paused", False)),
        paused_until=int(getattr(r, "paused_until", 0) or 0),
        version=int(getattr(r, "version", 1) or 1),
        owner_user_id=int(getattr(r, "owner_user_id", 0) or 0),
        custom_metadata_json=_json.dumps(getattr(r, "metadata", {}) or {}),
    )


def _filter_pb(f) -> pb.FilterRule:
    return pb.FilterRule(
        id=f.id, name=f.name,
        whitelist_keywords=f.whitelist_keywords,
        blacklist_keywords=f.blacklist_keywords,
        regex_patterns=f.regex_patterns,
        allowed_media_types=f.allowed_media_types,
        blocked_media_types=f.blocked_media_types,
        drop_service_messages=f.drop_service_messages,
        min_message_length=f.min_message_length,
        max_message_length=f.max_message_length,
        owner_user_id=int(getattr(f, "owner_user_id", 0) or 0),
    )


def _ai_pb(c, mask_key: bool = True) -> pb.AIConfig:
    key = ("***" + c.api_key[-4:]) if (mask_key and c.api_key) else c.api_key
    return pb.AIConfig(
        id=c.id, name=c.name, provider=c.provider.value, model=c.model,
        api_key=key, base_url=c.base_url, system_prompt=c.system_prompt,
        user_prompt_template=c.user_prompt_template, temperature=c.temperature,
        is_enabled=c.is_enabled, target_language=c.target_language,
        owner_user_id=int(getattr(c, "owner_user_id", 0) or 0),
    )


class SessionControlServicer(pb_grpc.SessionControlServiceServicer):
    def __init__(self, sessions, pool) -> None:
        self._sessions = sessions
        self._pool = pool

    async def ListSessions(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ListSessionsResponse(sessions=[])
        rows = await self._sessions.list_by_owner(owner)
        return pb.ListSessionsResponse(sessions=[_session_info(s) for s in rows])

    async def StartLogin(self, request, context):
        # Interactive login lives in the bot; gRPC returns guidance.
        return pb.StartLoginResponse(
            success=True,
            message="Use the Telegram bot /login command for interactive login.",
        )

    async def SubmitCode(self, request, context):
        return pb.SubmitCodeResponse(success=False, message="Interactive login is handled by the bot.")

    async def SubmitPassword(self, request, context):
        return pb.SubmitPasswordResponse(success=False, message="Interactive login is handled by the bot.")

    async def BackupSession(self, request, context):
        # v12.2: the exported blob is full account-takeover material — only
        # the session's owner may retrieve it. Fail closed on missing/zero identity.
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.BackupSessionResponse(success=False, message="unauthorized")
        s = await self._sessions.get_owned(request.session_id, owner)
        if not s:
            return pb.BackupSessionResponse(success=False, message="session_not_found")
        return pb.BackupSessionResponse(success=True, encrypted_session_data=s.session_string_encrypted)

    async def RestoreSession(self, request, context):
        # v12.2: bind the restored session to the importing account so it
        # cannot be dropped into the shared/legacy scope and later picked up
        # by another tenant's rules.
        try:
            owner = int(request.owner_user_id or 0)
            if not owner:
                return pb.RestoreSessionResponse(success=False, message="unauthorized")
            s = await self._sessions.create(
                phone_number="",
                session_string_encrypted=request.encrypted_session_data,
                owner_user_id=owner,
            )
            s.activate()
            await self._sessions._repo.update(s)
            return pb.RestoreSessionResponse(success=True, session_id=s.id)
        except Exception as exc:
            return pb.RestoreSessionResponse(success=False, message=str(exc))

    async def TerminateSession(self, request, context):
        # v12.2: deleting a session also tears down its live Telegram client —
        # require ownership first. A foreign delete raises PermissionError,
        # which we surface as a denial rather than a not_found so the caller
        # cannot use the response to probe for session existence.
        owner = int(request.owner_user_id or 0)
        if not owner:
            return _status(False, "unauthorized")
        try:
            ok = await self._sessions.delete_owned(request.session_id, owner)
        except PermissionError:
            logger.warning(
                "TerminateSession: owner=%s denied termination of session=%s",
                owner, request.session_id,
            )
            return _status(False, "denied")
        if ok:
            if self._pool is not None:
                await self._pool.stop(request.session_id)
        return _status(ok, "terminated" if ok else "not_found")

    async def ResolveChat(self, request, context):
        """Resolve a human handle (username / t.me link / numeric id) to a
        canonical numeric chat id using a live Telegram session. The dispatcher
        only understands numeric ids, so the API layer calls this before saving
        a rule to guarantee no username ever lands in chat_id columns."""
        import re as _re

        handle = (request.handle or "").strip()
        session_id = (request.session_id or "").strip()
        if not handle:
            return pb.ResolveChatResponse(found=False, error="handle_empty")

        # Normalize the handle: links and @-prefixes down to a bare username/id.
        raw = handle
        raw = _re.sub(r"^https?://t\.me/", "", raw, flags=_re.IGNORECASE)
        raw = _re.sub(r"^t\.me/", "", raw, flags=_re.IGNORECASE)
        raw = raw.lstrip("@").strip()
        if not raw:
            return pb.ResolveChatResponse(found=False, error="handle_empty")

        # Numeric ids (with optional -100 channel prefix) pass through.
        if _re.fullmatch(r"-?\d+", raw):
            return pb.ResolveChatResponse(
                found=True, chat_id=str(int(raw)), title=handle, chat_type="numeric"
            )

        # Pick the requested session, or the first active+authorized one owned
        # by the caller. Fail closed: no arbitrary tenant client driving.
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ResolveChatResponse(found=False, error="unauthorized")
        if session_id:
            session = await self._sessions.get_owned(session_id, owner)
        else:
            sessions = await self._sessions.list_by_owner(owner)
            active = [s for s in sessions if s.is_active and s.is_authorized]
            session = active[0] if active else (sessions[0] if sessions else None)
        if not session:
            return pb.ResolveChatResponse(found=False, error="no_active_session")

        client = self._pool.get(session.id)
        if client is None:
            return pb.ResolveChatResponse(
                found=False, error="session_not_connected"
            )

        try:
            chat = await client.get_chat(raw)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
            return pb.ResolveChatResponse(
                found=False, error=f"{type(exc).__name__}: {exc}"
            )

        chat_type = type(chat).__name__.lower()
        if hasattr(chat, "type") and chat.type is not None:
            chat_type = getattr(chat.type, "value", str(chat.type)).lower()
        username = getattr(chat, "username", "") or ""
        title = getattr(chat, "title", "") or getattr(chat, "first_name", "") or username
        return pb.ResolveChatResponse(
            found=True,
            chat_id=str(chat.id),
            title=str(title),
            username=username,
            chat_type=chat_type,
        )


class ForwardRuleControlServicer(pb_grpc.ForwardRuleControlServiceServicer):
    def __init__(self, rules, queue=None) -> None:
        self._rules = rules
        self._queue = queue

    async def ListRules(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ListRulesResponse(rules=[])
        if request.session_id:
            rows = await self._rules.list_by_session(request.session_id, owner)
        else:
            rows = await self._rules.list_by_owner(owner)
        return pb.ListRulesResponse(rules=[_rule_pb(r) for r in rows])

    async def CreateRule(self, request, context):
        from ...domain.entities import ForwardRule as Entity
        from ...domain.value_objects import ForwardMode as FM, RoutingType as RT
        import json as _json
        r = request.rule
        entity = Entity(
            session_id=r.session_id, source_chat_id=r.source_chat_id,
            source_chat_name=r.source_chat_name, target_chat_id=r.target_chat_id,
            target_chat_name=r.target_chat_name,
            routing_type=RT[routing_name(r.routing_type)],
            forward_mode=FM[mode_name(r.forward_mode)],
            is_active=r.is_active or True,
            remove_links=r.remove_links,
            custom_caption_template=r.custom_caption_template,
            # Smart fields
            name=r.name or "",
            description=r.description or "",
            target_chat_ids=list(r.target_chat_ids or []),
            message_category=r.message_category or "ALL",
            intermediate_channel_id=r.intermediate_channel_id or "",
            intermediate_channel_name=r.intermediate_channel_name or "",
            use_intermediate=bool(r.use_intermediate),
            fallback_mode=FM[mode_name(r.fallback_mode)] if r.fallback_mode else FM.COPY_MESSAGE,
            fallback_enabled=bool(r.fallback_enabled) if r.fallback_enabled else True,
            detection_criteria=_json.loads(r.detection_criteria_json or "{}") or {},
            priority=int(r.priority or 10),
            execution_order=int(r.execution_order or 0),
            multi_route=bool(r.multi_route),
            media_handling=r.media_handling or "AUTO",
            dedupe_policy=r.dedupe_policy or "STRICT",
            max_retries=int(r.max_retries or 3),
            retry_backoff_base=float(r.retry_backoff_base or 2.0),
            rate_limit_per_minute=int(r.rate_limit_per_minute or 0),
            rate_limit_burst=int(r.rate_limit_burst or 0),
            custom_header=r.custom_header or "",
            custom_footer=r.custom_footer or "",
            header_enabled=bool(r.header_enabled),
            template_text=r.template_text or "",
            template_media=r.template_media or "",
            template_album=r.template_album or "",
            caption_max_length=int(r.caption_max_length or 0),
            preserve_signature=bool(r.preserve_signature),
            is_paused=bool(r.is_paused),
            paused_until=int(r.paused_until or 0),
            owner_user_id=int(r.owner_user_id or 0),
        )
        try:
            created = await self._rules.create(entity)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return pb.ForwardRule()
        return _rule_pb(created)

    async def UpdateRule(self, request, context):
        import json as _json
        from ...domain.value_objects import ForwardMode as FM
        # v12.2: verify the caller owns the rule before mutating it.
        caller = int(request.rule.owner_user_id or 0)
        if not caller:
            return pb.ForwardRule()
        existing = await self._rules.get_owned(request.rule.id, caller)
        if not existing:
            return pb.ForwardRule()
        r = request.rule
        if r.source_chat_id:
            existing.source_chat_id = r.source_chat_id
        if r.target_chat_id:
            existing.target_chat_id = r.target_chat_id
        if r.source_chat_name:
            existing.source_chat_name = r.source_chat_name
        if r.target_chat_name:
            existing.target_chat_name = r.target_chat_name
        existing.is_active = r.is_active
        existing.remove_links = r.remove_links
        if r.custom_caption_template:
            existing.custom_caption_template = r.custom_caption_template

        # Smart fields (only applied when present in the request)
        if r.name:
            existing.name = r.name
        if r.description:
            existing.description = r.description
        if r.target_chat_ids:
            existing.target_chat_ids = list(r.target_chat_ids)
        if r.message_category:
            existing.message_category = r.message_category
        if r.intermediate_channel_id:
            existing.intermediate_channel_id = r.intermediate_channel_id
        if r.intermediate_channel_name:
            existing.intermediate_channel_name = r.intermediate_channel_name
        if r.use_intermediate:
            existing.use_intermediate = bool(r.use_intermediate)
        if r.fallback_mode:
            try:
                existing.fallback_mode = FM[mode_name(r.fallback_mode)]
            except KeyError:
                existing.fallback_mode = FM.COPY_MESSAGE
        existing.fallback_enabled = bool(r.fallback_enabled) if r.fallback_enabled else existing.fallback_enabled
        if r.detection_criteria_json:
            try:
                existing.detection_criteria = _json.loads(r.detection_criteria_json) or {}
            except Exception:
                pass
        if r.priority:
            existing.priority = int(r.priority)
        if r.execution_order:
            existing.execution_order = int(r.execution_order)
        if r.multi_route:
            existing.multi_route = bool(r.multi_route)
        if r.media_handling:
            existing.media_handling = r.media_handling
        if r.dedupe_policy:
            existing.dedupe_policy = r.dedupe_policy
        if r.max_retries:
            existing.max_retries = int(r.max_retries)
        if r.retry_backoff_base:
            existing.retry_backoff_base = float(r.retry_backoff_base)
        if r.rate_limit_per_minute:
            existing.rate_limit_per_minute = int(r.rate_limit_per_minute)
        if r.rate_limit_burst:
            existing.rate_limit_burst = int(r.rate_limit_burst)
        if r.custom_header:
            existing.custom_header = r.custom_header
        if r.custom_footer:
            existing.custom_footer = r.custom_footer
        if r.header_enabled:
            existing.header_enabled = bool(r.header_enabled)
        if r.template_text:
            existing.template_text = r.template_text
        if r.template_media:
            existing.template_media = r.template_media
        if r.template_album:
            existing.template_album = r.template_album
        if r.caption_max_length:
            existing.caption_max_length = int(r.caption_max_length)
        if r.preserve_signature:
            existing.preserve_signature = bool(r.preserve_signature)
        if r.is_paused:
            existing.is_paused = bool(r.is_paused)
        if r.paused_until:
            existing.paused_until = int(r.paused_until)
        if r.custom_metadata_json:
            try:
                merged = _json.loads(r.custom_metadata_json)
                if isinstance(merged, dict):
                    if not isinstance(existing.metadata, dict):
                        existing.metadata = {}
                    existing.metadata.update(merged)
            except Exception:
                pass

        existing.mark_updated()
        try:
            updated = await self._rules.update(existing)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return pb.ForwardRule()
        return _rule_pb(updated)

    async def DeleteRule(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return _status(False, "unauthorized")
        try:
            ok = await self._rules.delete_owned(request.id, owner)
        except PermissionError:
            logger.warning(
                "DeleteRule: owner=%s denied delete of rule=%s",
                owner, request.id,
            )
            return _status(False, "denied")
        return _status(ok, "deleted" if ok else "not_found")

    async def TestRule(self, request, context):
        """Dry-run a rule against a sample message without sending anything."""
        import json as _json
        from ...domain.entities import MessagePayload, MediaType
        from ...domain.services import SmartRoutingEngine, TransformationService

        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.TestRuleResponse(matched=False, match_reason="unauthorized")
        rule = await self._rules.get_owned(request.rule_id, owner)
        if rule is None:
            return pb.TestRuleResponse(matched=False, match_reason="rule_not_found")

        media_raw = (request.sample_media_type or "text").lower()
        try:
            media_type = MediaType(media_raw)
        except ValueError:
            media_type = MediaType.TEXT

        forward_origin = None
        if request.forward_origin_chat_id or request.forward_origin_username or request.forward_origin_title:
            forward_origin = {
                "type": "channel",
                "from_chat_id": request.forward_origin_chat_id or "",
                "from_chat_username": request.forward_origin_username or "",
                "from_chat_title": request.forward_origin_title or "",
                "from_message_id": 0,
                "date": 0,
                "sender_name": request.forward_origin_username or request.forward_origin_title or "",
            }

        payload = MessagePayload(
            message_id=1,
            chat_id=request.sample_chat_id or rule.source_chat_id or "-1000",
            chat_type="channel",
            sender_id=request.sample_sender_id or None,
            text=request.sample_text or "",
            caption=request.sample_text or "",
            media_type=media_type,
            has_media=bool(request.sample_has_media),
            forward_origin=forward_origin,
        )

        engine = SmartRoutingEngine()
        matched, reason = engine.evaluate_rule(payload, rule)
        is_vip, vip_reason = engine.is_vip_origin(payload, rule)
        looped, loop_reason = engine.check_loop(payload, rule, rule.target_chat_id)

        rendered = ""
        header_rendered = ""
        footer_rendered = ""
        if matched:
            svc = TransformationService()
            rendered = svc.transform_text(payload.effective_text, rule, payload)
            header_rendered = svc.apply_header_footer(
                "", header=rule.header, footer="",
            ) if rule.header else ""
            footer_rendered = rule.footer or ""

        return pb.TestRuleResponse(
            matched=matched,
            match_reason=reason,
            rule_name=rule.name or f"Rule-{rule.id[:8]}",
            message_category=rule.message_category or "ALL",
            is_vip=is_vip,
            vip_reason=vip_reason,
            loop_detected=looped,
            loop_reason=loop_reason,
            forward_mode=str(getattr(rule.forward_mode, "value", rule.forward_mode)),
            fallback_mode=str(getattr(rule.fallback_mode, "value", rule.fallback_mode)),
            rendered_text=rendered,
            rendered_header=header_rendered,
            rendered_footer=footer_rendered,
            delivery_targets=list(rule.all_targets),
            intermediate_channel_id=rule.intermediate_channel_id or "",
            use_intermediate=bool(rule.use_intermediate),
        )

    async def PauseRule(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.RuleActionResponse(success=False, message="unauthorized", error_code="unauthorized")
        rule = await self._rules.get_owned(request.rule_id, owner)
        if rule is None:
            return pb.RuleActionResponse(success=False, message="rule_not_found", error_code="not_found")
        rule.is_paused = True
        rule.paused_until = int(request.until or 0)
        rule.mark_updated()
        await self._rules.update(rule)
        return pb.RuleActionResponse(success=True, message="paused")

    async def ResumeRule(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.RuleActionResponse(success=False, message="unauthorized", error_code="unauthorized")
        rule = await self._rules.get_owned(request.rule_id, owner)
        if rule is None:
            return pb.RuleActionResponse(success=False, message="rule_not_found", error_code="not_found")
        rule.is_paused = False
        rule.paused_until = 0
        rule.mark_updated()
        await self._rules.update(rule)
        if self._queue is not None:
            try:
                if hasattr(self._queue, "_db") and not hasattr(self._queue, "_db_path"):
                    from ..persistence.postgres_repositories import _execute_db
                    await _execute_db(
                        self._queue._db,
                        "UPDATE delivery_jobs SET next_retry_at = 0 WHERE rule_id = %s AND status = 'RETRY_WAIT'",
                        (rule.id,),
                    )
                elif hasattr(self._queue, "_db") and hasattr(self._queue._db, "execute"):
                    self._queue._db.execute(
                        "UPDATE delivery_jobs SET next_retry_at = 0 WHERE rule_id = ? AND status = 'RETRY_WAIT'",
                        (rule.id,),
                    )
            except Exception:
                pass
        return pb.RuleActionResponse(success=True, message="resumed")


class DeliveryControlServicer(pb_grpc.DeliveryControlServiceServicer):
    """Live delivery stats, dead-letter queue inspection and replay."""

    def __init__(
        self, db, rule_repo, pipeline=None, queue_repo=None, message_map_repo=None
    ) -> None:
        self._db = db
        self._rules = rule_repo
        self._pipeline = pipeline
        self._queue_repo = queue_repo
        self._message_map_repo = message_map_repo

    def _q(self, sql: str, params: tuple = ()):
        try:
            cur = self._db.connection.cursor()
            cur.execute(sql, params)
            return cur.fetchall()
        except Exception:
            return []

    async def _query_queue(self, sql: str, params: tuple = ()) -> list:
        """Execute a query against delivery_jobs or dead_letter_queue across PostgreSQL and SQLite."""
        if self._queue_repo is not None and hasattr(self._queue_repo, "_fetch_all"):
            pg_sql = sql.replace("?", "%s")
            return await self._queue_repo._fetch_all(pg_sql, params)
        return self._q(sql, params)

    async def _execute_queue(self, sql: str, params: tuple = ()) -> Any:
        """Execute an insert/update/delete on delivery_jobs or dead_letter_queue across backends."""
        if self._queue_repo is not None and hasattr(self._queue_repo, "_db") and not hasattr(self._queue_repo, "_db_path"):
            from ..persistence.postgres_repositories import _execute_db
            pg_sql = sql.replace("?", "%s")
            if "INSERT OR IGNORE" in pg_sql:
                pg_sql = pg_sql.replace("INSERT OR IGNORE", "INSERT")
                if "VALUES" in pg_sql and "ON CONFLICT" not in pg_sql:
                    pg_sql += " ON CONFLICT DO NOTHING"
            return await _execute_db(self._queue_repo._db, pg_sql, params)
        if hasattr(self._db, "execute"):
            return self._db.execute(sql, params)
        cur = self._db.connection.cursor()
        cur.execute(sql, params)
        self._db.connection.commit()
        return cur

    async def _owner_rule_ids(self, owner_user_id: int) -> Optional[set]:
        """v12: resolve the rule ids a tenant may see. None = unscoped (admin)."""
        if not owner_user_id:
            return None
        try:
            owned = await self._rules.list_by_owner(int(owner_user_id))
        except Exception:
            return set()
        return {r.id for r in owned}

    async def GetDeliveryStats(self, request, context):
        # v12: a tenant only sees stats for its own rules, queue depth and
        # errors included. Without this GET /api/queue exposed every tenant's
        # rule names, error text and dead-letter backlog.
        scope = await self._owner_rule_ids(int(request.owner_user_id or 0))
        owner_clause = ""
        owner_params: tuple = ()
        if scope is not None:
            if not scope:
                return pb.DeliveryStatsResponse(rules=[], errors=[])
            placeholders = ",".join("?" for _ in scope)
            owner_clause = f" AND rule_id IN ({placeholders})"
            owner_params = tuple(scope)

        processed = forwarded = failed = dedup = filtered = in_queue = retries = 0
        for row in self._q(f"SELECT forwarded, filtered, errors FROM rule_stats WHERE 1=1{owner_clause}", owner_params):
            forwarded += int(row[0] or 0)
            filtered += int(row[1] or 0)
            failed += int(row[2] or 0)
        for row in self._q(f"SELECT COALESCE(SUM(forwarded),0), COALESCE(SUM(filtered),0), COALESCE(SUM(errors),0) FROM metrics_hourly WHERE 1=1{owner_clause}", owner_params):
            forwarded += int(row[0] or 0)
            filtered += int(row[1] or 0)
            failed += int(row[2] or 0)

        # Active jobs in queue: PENDING, CLAIMED, RETRY_WAIT
        queue_sql = f"SELECT COUNT(*) FROM delivery_jobs WHERE status IN ('PENDING', 'CLAIMED', 'RETRY_WAIT'){owner_clause}"
        for row in await self._query_queue(queue_sql, owner_params):
            val = row[0] if isinstance(row, (tuple, list)) else list(row.values())[0] if hasattr(row, "values") else (row["count"] if isinstance(row, dict) else 0)
            in_queue = int(val or 0)

        retries_sql = f"SELECT COALESCE(SUM(attempts),0) FROM delivery_jobs WHERE attempts > 1{owner_clause}"
        for row in await self._query_queue(retries_sql, owner_params):
            val = row[0] if isinstance(row, (tuple, list)) else list(row.values())[0] if hasattr(row, "values") else (row["coalesce"] if isinstance(row, dict) else 0)
            retries = int(val or 0)

        processed = forwarded + filtered + failed

        rules = []
        try:
            all_rules = await self._rules.list_all()
        except Exception:
            all_rules = []
        for r in all_rules:
            if scope is not None and r.id not in scope:
                continue
            stat = self._q(
                "SELECT forwarded, filtered, errors, last_forward_ts FROM rule_stats WHERE rule_id=?",
                (r.id,),
            )
            if stat:
                s = stat[0]
                rules.append(pb.RuleLiveStat(
                    rule_id=r.id, rule_name=r.name or f"Rule-{r.id[:8]}",
                    is_active=bool(r.is_active), is_paused=bool(getattr(r, "is_paused", False)),
                    forwarded=int(s[0] or 0), filtered=int(s[1] or 0), errors=int(s[2] or 0),
                    last_forward_ts=int(s[3] or 0),
                ))
            else:
                rules.append(pb.RuleLiveStat(
                    rule_id=r.id, rule_name=r.name or f"Rule-{r.id[:8]}",
                    is_active=bool(r.is_active), is_paused=bool(getattr(r, "is_paused", False)),
                ))

        errors = []
        for row in self._q(
            "SELECT ts, rule_id, category, error_name, severity, detail, chat_id "
            "FROM error_log WHERE 1=1" + owner_clause + " ORDER BY ts DESC LIMIT 25",
            owner_params,
        ):
            errors.append(pb.RecentError(
                ts=int(row[0] or 0), rule_id=str(row[1] or ""), category=str(row[2] or ""),
                error_name=str(row[3] or ""), severity=str(row[4] or ""),
                detail=str(row[5] or "")[:500], chat_id=str(row[6] or ""),
            ))

        dlq_total = 0
        dlq_sql = "SELECT COUNT(*) FROM dead_letter_queue WHERE 1=1" + owner_clause
        for row in await self._query_queue(dlq_sql, owner_params):
            val = row[0] if isinstance(row, (tuple, list)) else list(row.values())[0] if hasattr(row, "values") else (row["count"] if isinstance(row, dict) else 0)
            dlq_total = int(val or 0)

        return pb.DeliveryStatsResponse(
            processed_total=processed, forwarded_total=forwarded, failed_total=failed,
            dedup_skipped_total=dedup, filtered_total=filtered, in_queue=in_queue,
            dead_lettered_total=dlq_total, retry_total=retries,
            rules=rules, errors=errors,
        )

    async def ListDeadLetter(self, request, context):
        limit = int(request.limit or 50)
        if limit <= 0 or limit > 500:
            limit = 50
        params: tuple = ()
        sql = (
            "SELECT id, rule_id, source_chat_id, source_message_id, target_chat_id, "
            "attempts, last_error, error_category, dead_lettered_at "
            "FROM dead_letter_queue"
        )
        where = []
        if request.rule_id:
            where.append("rule_id=?")
            params = (request.rule_id,)
        if request.since:
            where.append("dead_lettered_at>=?")
            params = params + (int(request.since),)
        # v12: scope to the caller's rules.
        scope = await self._owner_rule_ids(int(request.owner_user_id or 0))
        if scope is not None:
            if not scope:
                return pb.ListDeadLetterResponse(entries=[], total=0)
            placeholders = ",".join("?" for _ in scope)
            where.append(f"rule_id IN ({placeholders})")
            params = params + tuple(scope)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY dead_lettered_at DESC LIMIT ?"
        params = params + (limit,)

        entries = []
        for r in await self._query_queue(sql, params):
            if isinstance(r, (tuple, list)):
                entries.append(pb.DeadLetterEntry(
                    id=str(r[0]), rule_id=str(r[1]), source_chat_id=str(r[2]),
                    source_message_id=int(r[3] or 0), target_chat_id=str(r[4]),
                    attempts=int(r[5] or 0), last_error=str(r[6] or "")[:500],
                    error_category=str(r[7] or ""), dead_lettered_at=int(r[8] or 0),
                ))
            elif hasattr(r, "keys"):
                d = dict(r)
                entries.append(pb.DeadLetterEntry(
                    id=str(d.get("id", "")), rule_id=str(d.get("rule_id", "")),
                    source_chat_id=str(d.get("source_chat_id", "")),
                    source_message_id=int(d.get("source_message_id", 0) or 0),
                    target_chat_id=str(d.get("target_chat_id", "")),
                    attempts=int(d.get("attempts", 0) or 0),
                    last_error=str(d.get("last_error") or d.get("error_message") or "")[:500],
                    error_category=str(d.get("error_category", "")),
                    dead_lettered_at=int(d.get("dead_lettered_at", 0) or 0),
                ))
        return pb.ListDeadLetterResponse(entries=entries, total=len(entries))

    async def ReplayDeadLetter(self, request, context):
        rows = await self._query_queue("SELECT * FROM dead_letter_queue WHERE id=?", (request.id,))
        if not rows:
            return pb.RuleActionResponse(success=False, message="not_found", error_code="not_found")
        r = rows[0]
        if isinstance(r, (tuple, list)):
            row_id = r[0]
            rule_id = r[2] if len(r) > 2 else ""
            src_chat = r[3] if len(r) > 3 else ""
            src_msg = r[4] if len(r) > 4 else 0
            tgt_chat = r[5] if len(r) > 5 else ""
            payload_json = r[6] if len(r) > 6 else "{}"
        else:
            d = dict(r) if hasattr(r, "keys") else r
            row_id = d.get("id", "")
            rule_id = d.get("rule_id", "")
            src_chat = d.get("source_chat_id", "")
            src_msg = d.get("source_message_id", 0)
            tgt_chat = d.get("target_chat_id", "")
            payload_json = d.get("payload_json", "{}")

        # v12: a tenant may only replay entries whose rule it owns.
        scope = await self._owner_rule_ids(int(request.owner_user_id or 0))
        if scope is not None and rule_id not in scope:
            logger.warning(
                "ReplayDeadLetter: owner=%s denied replay of entry=%s rule=%s",
                request.owner_user_id, request.id, rule_id,
            )
            return pb.RuleActionResponse(success=False, message="not_found", error_code="not_found")
        try:
            import json as _json
            import time as _time
            payload_str = payload_json if isinstance(payload_json, str) else _json.dumps(payload_json or {})
            now = int(_time.time())
            await self._execute_queue(
                "INSERT OR IGNORE INTO delivery_jobs "
                "(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json, "
                "status, attempts, max_attempts, next_retry_at, lease_until, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, 3, 0, 0, ?, ?)",
                (f"replay-{row_id}", str(rule_id), str(src_chat),
                 int(src_msg or 0), str(tgt_chat), payload_str, now, now),
            )
            await self._execute_queue("DELETE FROM dead_letter_queue WHERE id=?", (request.id,))
            return pb.RuleActionResponse(success=True, message="requeued")
        except Exception as exc:
            return pb.RuleActionResponse(success=False, message=str(exc)[:200], error_code="replay_failed")

    async def PurgeDeadLetter(self, request, context):
        params: tuple = ()
        sql = "DELETE FROM dead_letter_queue"
        where = []
        if request.rule_id:
            where.append("rule_id=?")
            params = (request.rule_id,)
        if request.older_than:
            where.append("dead_lettered_at<?")
            params = params + (int(request.older_than),)
        # v12: purge only the caller's own rules.
        scope = await self._owner_rule_ids(int(request.owner_user_id or 0))
        if scope is not None:
            if not scope:
                return pb.RuleActionResponse(success=True, message="purged 0")
            placeholders = ",".join("?" for _ in scope)
            where.append(f"rule_id IN ({placeholders})")
            params = params + tuple(scope)
        if where:
            sql += " WHERE " + " AND ".join(where)
        try:
            await self._execute_queue(sql, params)
            return pb.RuleActionResponse(success=True, message="purged")
        except Exception as exc:
            return pb.RuleActionResponse(success=False, message=str(exc)[:200], error_code="purge_failed")


def routing_name(value) -> str:
    try:
        return pb.RoutingType.Name(value)
    except ValueError:
        return "CHANNEL_TO_CHANNEL"


def mode_name(value) -> str:
    try:
        return pb.ForwardMode.Name(value)
    except ValueError:
        return "COPY_MESSAGE"


class FilterControlServicer(pb_grpc.FilterControlServiceServicer):
    def __init__(self, filters_uc) -> None:
        self._filters = filters_uc

    async def ListFilters(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ListFiltersResponse(filters=[])
        rows = await self._filters.list_by_owner(owner)
        return pb.ListFiltersResponse(filters=[_filter_pb(f) for f in rows])

    async def CreateFilter(self, request, context):
        from ...domain.entities import FilterRule as Entity
        f = request.filter
        entity = Entity(
            name=f.name,
            whitelist_keywords=list(f.whitelist_keywords),
            blacklist_keywords=list(f.blacklist_keywords),
            regex_patterns=list(f.regex_patterns),
            allowed_media_types=list(f.allowed_media_types),
            blocked_media_types=list(f.blocked_media_types),
            drop_service_messages=f.drop_service_messages,
            min_message_length=f.min_message_length,
            max_message_length=f.max_message_length,
            owner_user_id=int(f.owner_user_id or 0),
        )
        created = await self._filters.create(entity)
        return _filter_pb(created)

    async def UpdateFilter(self, request, context):
        # v12.2: ownership guard at the use-case boundary.
        caller = int(request.filter.owner_user_id or 0)
        if not caller:
            return pb.FilterRule()
        existing = await self._filters.get_owned(request.filter.id, caller)
        if not existing:
            return pb.FilterRule()
        f = request.filter
        existing.name = f.name or existing.name
        existing.whitelist_keywords = list(f.whitelist_keywords)
        existing.blacklist_keywords = list(f.blacklist_keywords)
        existing.regex_patterns = list(f.regex_patterns)
        existing.allowed_media_types = list(f.allowed_media_types)
        existing.blocked_media_types = list(f.blocked_media_types)
        existing.drop_service_messages = f.drop_service_messages
        existing.min_message_length = f.min_message_length
        existing.max_message_length = f.max_message_length
        existing.owner_user_id = int(f.owner_user_id or existing.owner_user_id or 0)
        updated = await self._filters.update(existing)
        return _filter_pb(updated)

    async def DeleteFilter(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return _status(False, "unauthorized")
        try:
            ok = await self._filters.delete_owned(request.id, owner)
        except PermissionError:
            logger.warning(
                "DeleteFilter: owner=%s denied delete of filter=%s",
                owner, request.id,
            )
            return _status(False, "denied")
        return _status(ok, "deleted" if ok else "not_found")


class AIControlServicer(pb_grpc.AIControlServiceServicer):
    def __init__(self, ai_uc, prompt_service=None, ai_transformer=None) -> None:
        self._ai = ai_uc
        self._prompts = prompt_service
        self._transformer = ai_transformer

    async def ListAIConfigs(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ListAIConfigsResponse(configs=[])
        rows = await self._ai.list_by_owner(owner)
        return pb.ListAIConfigsResponse(configs=[_ai_pb(c) for c in rows])

    async def CreateAIConfig(self, request, context):
        from ...domain.entities import AIConfig as Entity
        from ...domain.value_objects import AIProviderType as PT
        c = request.config
        entity = Entity(
            name=c.name, provider=PT(c.provider.lower()) if c.provider else PT.OPENAI,
            model=c.model or "gpt-4o-mini", api_key=c.api_key, base_url=c.base_url,
            system_prompt=c.system_prompt, user_prompt_template=c.user_prompt_template or "{text}",
            temperature=c.temperature or 0.7, is_enabled=c.is_enabled or True,
            target_language=c.target_language or "en",
            owner_user_id=int(c.owner_user_id or 0),
        )
        created = await self._ai.create(entity)
        return _ai_pb(created)

    async def UpdateAIConfig(self, request, context):
        # v12.2: ownership guard — a forged config id must not let a tenant
        # overwrite another account's provider key.
        caller = int(request.config.owner_user_id or 0)
        if not caller:
            return pb.AIConfig()
        existing = await self._ai.get_owned(request.config.id, caller)
        if not existing:
            return pb.AIConfig()
        c = request.config
        if c.model:
            existing.model = c.model
        if c.api_key and not c.api_key.startswith("***"):
            existing.api_key = c.api_key
        if c.base_url:
            existing.base_url = c.base_url
        if c.system_prompt:
            existing.system_prompt = c.system_prompt
        if c.user_prompt_template:
            existing.user_prompt_template = c.user_prompt_template
        existing.temperature = c.temperature or existing.temperature
        existing.is_enabled = c.is_enabled
        if c.target_language:
            existing.target_language = c.target_language
        existing.owner_user_id = int(c.owner_user_id or existing.owner_user_id or 0)
        updated = await self._ai.update(existing)
        return _ai_pb(updated)

    async def DeleteAIConfig(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return _status(False, "unauthorized")
        try:
            ok = await self._ai.delete_owned(request.id, owner)
        except PermissionError:
            logger.warning(
                "DeleteAIConfig: owner=%s denied delete of config=%s",
                owner, request.id,
            )
            return _status(False, "denied")
        return _status(ok, "deleted" if ok else "not_found")

    async def TestAIRewrite(self, request, context):
        # v12.2: a rewrite burns the provider's quota — refuse to run it
        # against a config the caller does not own.
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.TestAIRewriteResponse(success=False, error_message="unauthorized")
        ok, text = await self._ai.test_rewrite_owned(
            request.ai_config_id, request.sample_text, owner
        )
        return pb.TestAIRewriteResponse(success=ok, rewritten_text=text if ok else "", error_message="" if ok else text)

    # ------------------------------------------------------------------
    # Prompt templates (AI Rewrite) — shared by bot & web dashboard
    # ------------------------------------------------------------------

    @staticmethod
    def _prompt_pb(t) -> pb.PromptTemplate:
        return pb.PromptTemplate(
            id=t.id, name=t.name, description=t.description or "",
            system_prompt=t.system_prompt,
            user_prompt_template=t.user_prompt_template or "{text}",
            target_language=t.target_language or "en",
            current_version=t.current_version,
            is_system=bool(t.is_system),
            created_at=int(t.created_at or 0), updated_at=int(t.updated_at or 0),
        )

    async def ListPromptTemplates(self, request, context):
        if self._prompts is None:
            return pb.ListPromptTemplatesResponse()
        # v12: scope to the caller. An empty owner returns the shared system
        # prompts only; list_templates() (everything) is reserved for the
        # unscoped admin path in BackupService.
        owner = int(request.owner_user_id or 0)
        if owner:
            rows = await self._prompts.list_templates_by_owner(owner)
        else:
            rows = [t for t in await self._prompts.list_templates() if t.is_system]
        return pb.ListPromptTemplatesResponse(templates=[self._prompt_pb(t) for t in rows])

    async def SavePromptTemplate(self, request, context):
        if self._prompts is None:
            return pb.PromptTemplate()
        c = request.template
        owner = int(request.owner_user_id or 0)
        try:
            if c.id:
                existing = await self._prompts.get_template(c.id)
                if existing and existing.is_system:
                    return pb.PromptTemplate()  # system prompts are immutable
                # v12: a tenant may only overwrite its own (non-system) prompts.
                if existing and owner and int(getattr(existing, "owner_user_id", 0) or 0) not in (0, owner):
                    logger.warning(
                        "SavePromptTemplate: owner=%s denied write to prompt=%s owned_by=%s",
                        owner, c.id, getattr(existing, "owner_user_id", 0),
                    )
                    return pb.PromptTemplate()
                if existing and request.expected_version and existing.current_version != request.expected_version:
                    return pb.PromptTemplate()  # OCC conflict
                updated = await self._prompts.update_template(
                    c.id,
                    name=c.name or existing.name,
                    system_prompt=c.system_prompt,
                    user_prompt_template=c.user_prompt_template or "{text}",
                    description=c.description or "",
                    expected_version=request.expected_version or None,
                )
                if updated is False or updated is None:
                    return pb.PromptTemplate()
                fresh = await self._prompts.get_template(c.id)
                return self._prompt_pb(fresh) if fresh else pb.PromptTemplate()
            created = await self._prompts.create_template(
                name=c.name or "Untitled prompt",
                system_prompt=c.system_prompt,
                user_prompt_template=c.user_prompt_template or "{text}",
                description=c.description or "",
                target_language=c.target_language or "auto",
                owner_user_id=owner,
            )
            return self._prompt_pb(created)
        except Exception as exc:
            logger.warning("SavePromptTemplate failed: %s", exc)
            return pb.PromptTemplate()

    async def DeletePromptTemplate(self, request, context):
        if self._prompts is None:
            return _status(False, "prompt_service_unavailable")
        existing = await self._prompts.get_template(request.id)
        if not existing:
            return _status(False, "not_found")
        if existing.is_system:
            return _status(False, "system_prompt_immutable")
        # v12: a tenant may only delete its own prompts.
        owner = int(request.owner_user_id or 0)
        if owner and int(getattr(existing, "owner_user_id", 0) or 0) not in (0, owner):
            logger.warning(
                "DeletePromptTemplate: owner=%s denied delete of prompt=%s owned_by=%s",
                owner, request.id, getattr(existing, "owner_user_id", 0),
            )
            return _status(False, "not_found")
        ok = await self._prompts.delete_template(request.id)
        return _status(ok, "deleted" if ok else "not_found")

    async def TestPrompt(self, request, context):
        """Runs the real transform path in dry-run — no message is sent."""
        if self._prompts is None or self._transformer is None:
            return pb.TestPromptResponse(success=False, error_code="service_unavailable")
        # v12.2: the transform consumes the AI config's quota — only allow a
        # config the caller owns (shared is_system configs stay available to
        # everyone, as on the read path).
        owner = int(request.owner_user_id or 0)
        if owner and request.ai_config_id:
            cfg = await self._ai.get_owned(request.ai_config_id, owner) if hasattr(self._ai, "get_owned") else None
            if cfg is None:
                logger.warning(
                    "TestPrompt: owner=%s denied use of ai_config=%s",
                    owner, request.ai_config_id,
                )
                return pb.TestPromptResponse(success=False, error_code="config_not_found")
        try:
            result = await self._prompts.test_prompt(
                request.prompt_id, request.sample_text,
                request.ai_config_id, self._transformer,
                version=request.version or None,
            )
            return pb.TestPromptResponse(
                success=bool(result.get("success")),
                transformed_text=result.get("transformed_text") or "",
                error_message=result.get("error_message") or "",
                error_code=result.get("error_code") or "",
                execution_duration_ms=result.get("execution_duration_ms") or 0.0,
                provider_used=result.get("provider_used") or "",
                model_used=result.get("model_used") or "",
                prompt_version_used=int(result.get("prompt_version_used") or 0),
                circuit_state=result.get("circuit_state") or "",
            )
        except Exception as exc:
            logger.warning("TestPrompt failed: %s", exc)
            return pb.TestPromptResponse(success=False, error_code=str(type(exc).__name__), error_message=str(exc))


class SystemStatusControlServicer(pb_grpc.SystemStatusControlServiceServicer):
    def __init__(self, sessions, rules, pipeline, bot_username: str, version: str) -> None:
        self._sessions = sessions
        self._rules = rules
        self._pipeline = pipeline
        self._bot_username = bot_username
        self._version = version

    async def GetSystemStats(self, request, context):
        s = self._pipeline.stats
        owner = int(request.owner_user_id or 0)
        if owner:
            rules_q = await self._rules.list_by_owner(owner)
        else:
            rules_q = []
        active = [r for r in rules_q if r.is_active]
        return pb.SystemStatsResponse(
            core_running=True,
            uptime_seconds=int(time.time()) - s.started_at,
            active_sessions_count=len(active),
            active_rules_count=len(active),
            total_messages_processed=s.processed,
            total_messages_forwarded=s.forwarded,
            total_messages_filtered=s.filtered,
            total_messages_rewritten=s.rewritten,
            bot_username=self._bot_username,
            version=self._version,
        )


class BackupControlServicer(pb_grpc.BackupControlServiceServicer):
    """Server migration: export/import the whole install or one tenant.

    The bundle carries live credentials, so the caller should pass a passphrase
    for anything that leaves the box.
    """

    def __init__(self, backup_service) -> None:
        self._backup = backup_service

    async def BackupExport(self, request, context):
        try:
            owner = int(request.owner_user_id or 0)
            doc = await self._backup.create_snapshot(owner or None)
            text = serialize_bundle(doc)
            if request.passphrase:
                text = encrypt_bundle(text, request.passphrase)
            return pb.BackupExportResponse(
                success=True,
                bundle=text,
                counts={k: int(v) for k, v in (doc.get("counts") or {}).items()},
            )
        except Exception as exc:
            logger.warning("BackupExport failed: %s", exc)
            return pb.BackupExportResponse(success=False, error=str(exc))

    async def BackupImport(self, request, context):
        try:
            text = decrypt_bundle(request.bundle, request.passphrase)
            doc = deserialize_bundle(text)
            target = int(request.target_owner_user_id or 0)
            stats = await self._backup.restore_snapshot(
                doc,
                target or None,
                overwrite_existing=bool(request.overwrite_existing),
            )
            return pb.BackupImportResponse(
                success=True,
                stats={
                    k: pb.EntityImportStats(
                        imported=int(v.get("imported", 0)),
                        skipped=int(v.get("skipped", 0)),
                        failed=int(v.get("failed", 0)),
                    )
                    for k, v in stats.items()
                },
            )
        except Exception as exc:
            logger.warning("BackupImport failed: %s", exc)
            return pb.BackupImportResponse(success=False, error=str(exc))


class AuditControlServicer(pb_grpc.AuditControlServiceServicer):
    """Tenant-scoped audit event queries backed by the PostgreSQL audit store
    (mission Phase Three). The Go API resolves the authorized tenant from the
    authenticated principal and passes it as owner_user_id — this servicer
    never trusts client-side tenant claims beyond that contract."""

    def __init__(self, audit_repo):
        self._audit = audit_repo

    async def QueryAuditEvents(self, request, context):
        if self._audit is None:
            return pb.QueryAuditEventsResponse(events=[], total=0)
        rows = await self._audit.query(
            tenant_id=str(request.owner_user_id) if request.owner_user_id else "",
            correlation_id=request.correlation_id or "",
            operation_id=request.operation_id or "",
            feature_id=request.feature_id or "",
            service=request.service or "",
            severity=request.severity or "",
            event_category=request.event_category or "",
            outcome=request.outcome or "",
            since=request.since or 0,
            until=request.until or 0,
            limit=request.limit or 100,
            offset=request.offset or 0,
        )

        def _s(v):
            return "" if v is None else str(v)

        events = [
            pb.AuditEventEntry(
                event_id=_s(r.get("event_id")),
                schema_version=_s(r.get("schema_version")),
                event_type=_s(r.get("event_type")),
                event_category=_s(r.get("event_category")),
                severity=_s(r.get("severity")),
                outcome=_s(r.get("outcome")),
                ts_utc=int(r.get("ts_utc") or 0),
                service=_s(r.get("service")),
                correlation_id=_s(r.get("correlation_id")),
                operation_id=_s(r.get("operation_id")),
                tenant_id=_s(r.get("tenant_id")),
                actor_type=_s(r.get("actor_type")),
                actor_id=_s(r.get("actor_id")),
                feature_id=_s(r.get("feature_id")),
                action=_s(r.get("action")),
                resource_type=_s(r.get("resource_type")),
                resource_id=_s(r.get("resource_id")),
                rule_id=_s(r.get("rule_id")),
                queue_job_id=_s(r.get("queue_job_id")),
                reason_code=_s(r.get("reason_code")),
                duration_ms=int(r.get("duration_ms") or 0),
                error_code=_s(r.get("error_code")),
                error_class=_s(r.get("error_class")),
                retryable=bool(r.get("retryable") or False),
                result_summary=_s(r.get("result_summary")),
                metadata_json=_s(r.get("metadata")),
            )
            for r in rows
        ]
        return pb.QueryAuditEventsResponse(events=events, total=len(events))
