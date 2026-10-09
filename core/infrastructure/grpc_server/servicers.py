"""gRPC servicers implementing the shared proto contract over the use cases."""

import time

from ...proto import pb, pb_grpc  # noqa: F401


def _status(success: bool, message: str = "", error_code: str = "") -> pb.StatusResponse:
    return pb.StatusResponse(success=success, message=message, error_code=error_code)


def _session_info(s) -> pb.SessionInfo:
    return pb.SessionInfo(
        id=s.id, phone_number=s.phone_number, user_id=s.user_id,
        username=s.username, first_name=s.first_name,
        is_active=s.is_active, is_authorized=s.is_authorized,
        created_at=s.created_at, updated_at=s.updated_at,
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
    )


def _ai_pb(c, mask_key: bool = True) -> pb.AIConfig:
    key = ("***" + c.api_key[-4:]) if (mask_key and c.api_key) else c.api_key
    return pb.AIConfig(
        id=c.id, name=c.name, provider=c.provider.value, model=c.model,
        api_key=key, base_url=c.base_url, system_prompt=c.system_prompt,
        user_prompt_template=c.user_prompt_template, temperature=c.temperature,
        is_enabled=c.is_enabled, target_language=c.target_language,
    )


class SessionControlServicer(pb_grpc.SessionControlServiceServicer):
    def __init__(self, sessions, pool) -> None:
        self._sessions = sessions
        self._pool = pool

    async def ListSessions(self, request, context):
        rows = await self._sessions.list_all()
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
        s = await self._sessions.get(request.session_id)
        if not s:
            return pb.BackupSessionResponse(success=False, message="session_not_found")
        return pb.BackupSessionResponse(success=True, encrypted_session_data=s.session_string_encrypted)

    async def RestoreSession(self, request, context):
        try:
            s = await self._sessions.create(phone_number="", session_string_encrypted=request.encrypted_session_data)
            s.activate()
            await self._sessions._repo.update(s)
            return pb.RestoreSessionResponse(success=True, session_id=s.id)
        except Exception as exc:
            return pb.RestoreSessionResponse(success=False, message=str(exc))

    async def TerminateSession(self, request, context):
        ok = await self._sessions.delete(request.session_id)
        await self._pool.stop(request.session_id)
        return _status(ok, "terminated" if ok else "not_found")


class ForwardRuleControlServicer(pb_grpc.ForwardRuleControlServiceServicer):
    def __init__(self, rules) -> None:
        self._rules = rules

    async def ListRules(self, request, context):
        rows = (
            await self._rules.list_by_session(request.session_id)
            if request.session_id
            else await self._rules.list_all()
        )
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
        )
        created = await self._rules.create(entity)
        return _rule_pb(created)

    async def UpdateRule(self, request, context):
        import json as _json
        from ...domain.value_objects import ForwardMode as FM
        existing = await self._rules.get(request.rule.id)
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

        existing.mark_updated()
        updated = await self._rules.update(existing)
        return _rule_pb(updated)

    async def DeleteRule(self, request, context):
        ok = await self._rules.delete(request.id)
        return _status(ok, "deleted" if ok else "not_found")

    async def TestRule(self, request, context):
        """Dry-run a rule against a sample message without sending anything."""
        import json as _json
        from ...domain.entities import MessagePayload, MediaType
        from ...domain.services import SmartRoutingEngine, TransformationService

        rule = await self._rules.get(request.rule_id)
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
        rule = await self._rules.get(request.rule_id)
        if rule is None:
            return pb.RuleActionResponse(success=False, message="rule_not_found", error_code="not_found")
        rule.is_paused = True
        rule.paused_until = int(request.until or 0)
        rule.mark_updated()
        await self._rules.update(rule)
        return pb.RuleActionResponse(success=True, message="paused")

    async def ResumeRule(self, request, context):
        rule = await self._rules.get(request.rule_id)
        if rule is None:
            return pb.RuleActionResponse(success=False, message="rule_not_found", error_code="not_found")
        rule.is_paused = False
        rule.paused_until = 0
        rule.mark_updated()
        await self._rules.update(rule)
        return pb.RuleActionResponse(success=True, message="resumed")


class DeliveryControlServicer(pb_grpc.DeliveryControlServiceServicer):
    """Live delivery stats, dead-letter queue inspection and replay."""

    def __init__(self, db, rule_repo, pipeline=None) -> None:
        self._db = db
        self._rules = rule_repo
        self._pipeline = pipeline

    def _q(self, sql: str, params: tuple = ()):
        try:
            cur = self._db.connection.cursor()
            cur.execute(sql, params)
            return cur.fetchall()
        except Exception:
            return []

    async def GetDeliveryStats(self, request, context):
        processed = forwarded = failed = dedup = filtered = in_queue = retries = 0
        for row in self._q("SELECT forwarded, filtered, errors FROM rule_stats"):
            forwarded += int(row[0] or 0)
            filtered += int(row[1] or 0)
            failed += int(row[2] or 0)
        for row in self._q("SELECT COALESCE(SUM(forwarded),0), COALESCE(SUM(filtered),0), COALESCE(SUM(errors),0) FROM metrics_hourly"):
            forwarded += int(row[0] or 0)
            filtered += int(row[1] or 0)
            failed += int(row[2] or 0)
        for row in self._q("SELECT COALESCE(SUM(filtered),0), COALESCE(SUM(errors),0) FROM metrics_hourly"):
            pass
        for row in self._q("SELECT COUNT(*) FROM delivery_jobs WHERE status IN ('PENDING','LEASED')"):
            in_queue = int(row[0] or 0)
        for row in self._q("SELECT COALESCE(SUM(attempts),0) FROM delivery_jobs WHERE attempts > 1"):
            retries = int(row[0] or 0)
        processed = forwarded + filtered + failed

        rules = []
        try:
            all_rules = await self._rules.list_all()
        except Exception:
            all_rules = []
        for r in all_rules:
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
            "FROM error_log ORDER BY ts DESC LIMIT 25"
        ):
            errors.append(pb.RecentError(
                ts=int(row[0] or 0), rule_id=str(row[1] or ""), category=str(row[2] or ""),
                error_name=str(row[3] or ""), severity=str(row[4] or ""),
                detail=str(row[5] or "")[:500], chat_id=str(row[6] or ""),
            ))

        dlq_total = 0
        for row in self._q("SELECT COUNT(*) FROM dead_letter_queue"):
            dlq_total = int(row[0] or 0)

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
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY dead_lettered_at DESC LIMIT ?"
        params = params + (limit,)

        entries = []
        for row in self._q(sql, params):
            entries.append(pb.DeadLetterEntry(
                id=str(row[0]), rule_id=str(row[1]), source_chat_id=str(row[2]),
                source_message_id=int(row[3] or 0), target_chat_id=str(row[4]),
                attempts=int(row[5] or 0), last_error=str(row[6] or "")[:500],
                error_category=str(row[7] or ""), dead_lettered_at=int(row[8] or 0),
            ))
        return pb.ListDeadLetterResponse(entries=entries, total=len(entries))

    async def ReplayDeadLetter(self, request, context):
        rows = self._q("SELECT * FROM dead_letter_queue WHERE id=?", (request.id,))
        if not rows:
            return pb.RuleActionResponse(success=False, message="not_found", error_code="not_found")
        row = rows[0]
        try:
            self._db.execute(
                "INSERT OR IGNORE INTO delivery_jobs "
                "(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json, "
                "status, attempts, max_attempts, next_retry_at, lease_until, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?, 'PENDING', 0, 3, 0, 0, ?, ?)",
                (f"replay-{row['id']}", row["rule_id"], row["source_chat_id"],
                 int(row["source_message_id"]), row["target_chat_id"], row["payload_json"] or "{}",
                 int(time.time()), int(time.time())),
            )
            self._db.execute("DELETE FROM dead_letter_queue WHERE id=?", (request.id,))
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
        if where:
            sql += " WHERE " + " AND ".join(where)
        try:
            cur = self._db.execute(sql, params)
            return pb.RuleActionResponse(success=True, message=f"purged {cur.rowcount}")
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
        rows = await self._filters.list_all()
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
        )
        created = await self._filters.create(entity)
        return _filter_pb(created)

    async def UpdateFilter(self, request, context):
        existing = await self._filters.get(request.filter.id)
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
        updated = await self._filters.update(existing)
        return _filter_pb(updated)

    async def DeleteFilter(self, request, context):
        ok = await self._filters.delete(request.id)
        return _status(ok, "deleted" if ok else "not_found")


class AIControlServicer(pb_grpc.AIControlServiceServicer):
    def __init__(self, ai_uc) -> None:
        self._ai = ai_uc

    async def ListAIConfigs(self, request, context):
        rows = await self._ai.list_all()
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
        )
        created = await self._ai.create(entity)
        return _ai_pb(created)

    async def UpdateAIConfig(self, request, context):
        existing = await self._ai.get(request.config.id)
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
        updated = await self._ai.update(existing)
        return _ai_pb(updated)

    async def DeleteAIConfig(self, request, context):
        ok = await self._ai.delete(request.id)
        return _status(ok, "deleted" if ok else "not_found")

    async def TestAIRewrite(self, request, context):
        ok, text = await self._ai.test_rewrite(request.ai_config_id, request.sample_text)
        return pb.TestAIRewriteResponse(success=ok, rewritten_text=text if ok else "", error_message="" if ok else text)


class SystemStatusControlServicer(pb_grpc.SystemStatusControlServiceServicer):
    def __init__(self, sessions, rules, pipeline, bot_username: str, version: str) -> None:
        self._sessions = sessions
        self._rules = rules
        self._pipeline = pipeline
        self._bot_username = bot_username
        self._version = version

    async def GetSystemStats(self, request, context):
        s = self._pipeline.stats
        active = await self._sessions.list_active()
        all_rules = await self._rules.list_all()
        return pb.SystemStatsResponse(
            core_running=True,
            uptime_seconds=int(time.time()) - s.started_at,
            active_sessions_count=len(active),
            active_rules_count=sum(1 for r in all_rules if r.is_active),
            total_messages_processed=s.processed,
            total_messages_forwarded=s.forwarded,
            total_messages_filtered=s.filtered,
            total_messages_rewritten=s.rewritten,
            bot_username=self._bot_username,
            version=self._version,
        )
