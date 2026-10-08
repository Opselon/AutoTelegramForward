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
    return pb.ForwardRule(
        id=r.id, session_id=r.session_id,
        source_chat_id=r.source_chat_id, source_chat_name=r.source_chat_name,
        target_chat_id=r.target_chat_id, target_chat_name=r.target_chat_name,
        routing_type=pb.RoutingType.Value(r.routing_type.name),
        forward_mode=pb.ForwardMode.Value(r.forward_mode.name),
        is_active=r.is_active, filter_rule_id=r.filter_rule_id or "",
        ai_config_id=r.ai_config_id or "", remove_links=r.remove_links,
        custom_caption_template=r.custom_caption_template,
        created_at=r.created_at, updated_at=r.updated_at,
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
        )
        created = await self._rules.create(entity)
        return _rule_pb(created)

    async def UpdateRule(self, request, context):
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
        updated = await self._rules.update(existing)
        return _rule_pb(updated)

    async def DeleteRule(self, request, context):
        ok = await self._rules.delete(request.id)
        return _status(ok, "deleted" if ok else "not_found")


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
