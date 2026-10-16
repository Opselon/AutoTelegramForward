"""AutoTelegramForward Core — composition root.

Wires config -> persistence -> use cases -> adapters (bot, gRPC, dispatcher)
and runs the asyncio event loop.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import Config  # noqa: E402
from core.application.use_cases import (  # noqa: E402
    AIConfigUseCases,
    FilterRuleUseCases,
    ForwardRuleUseCases,
    MessageForwardingUseCase,
    SessionUseCases,
)
from core.domain.services import FilterEngine, RoutingPolicy  # noqa: E402
from core.infrastructure.ai.providers import AIProviderFactory  # noqa: E402
from core.infrastructure.grpc_server import servicers  # noqa: E402
from core.infrastructure.persistence.database import SqliteDatabase  # noqa: E402
from core.infrastructure.persistence.postgres_repositories import (  # noqa: E402
    PostgresDeliveryQueueRepository,
    PostgresForwardRuleRepository,
    PostgresMessageMapRepository,
    PostgresPromptRepository,
    initialize_postgres_schema,
)
from core.infrastructure.persistence.sqlite_repositories import (  # noqa: E402
    SqliteAIConfigRepository,
    SqliteApiCredentialRepository,
    SqliteAuthFlowRepository,
    SqliteBotTokenRepository,
    SqliteDeliveryQueueRepository,
    SqliteErrorLogRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteMetricsRepository,
    SqliteProcessedMessageRepository,
    SqlitePromptRepository,
    SqliteRuleStatsRepository,
    SqliteSessionRepository,
    SqliteSettingsRepository,
    SqliteUiStateRepository,
    SqliteUserRepository,
)
from core.application.prompt_service import PromptService  # noqa: E402
from core.infrastructure.ai.circuit_breaker import CircuitBreakerRegistry  # noqa: E402
from core.infrastructure.ai.transformer import AITransformer  # noqa: E402
from core.infrastructure.ai.validator import AIOutputValidator  # noqa: E402
from core.infrastructure.security.crypto import CryptoService  # noqa: E402
from core.infrastructure.telegram.bot_manager import BotManager  # noqa: E402
from core.infrastructure.telegram.login_flow import LOGIN_TTL  # noqa: E402
from core.infrastructure.telegram.client_pool import ClientPool  # noqa: E402
from core.infrastructure.telegram.dispatcher import MessageDispatcher  # noqa: E402
from core.infrastructure.telegram.durable_queue import DurableQueueManager  # noqa: E402
from core.infrastructure.telegram.sync_engine import SyncEngine  # noqa: E402
from core.infrastructure.telegram.pro_bot_ui import ProBotUI  # noqa: E402
from core.infrastructure.telegram.i18n import I18n  # noqa: E402

logging.basicConfig(
    level=os.environ.get("ATF_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Temporary diagnostics: allow a verbose pyrogram update stream via env var.
_pyro_level = os.environ.get("PYROGRAM_LOG_LEVEL")
if _pyro_level:
    logging.getLogger("pyrogram").setLevel(_pyro_level.upper())
logger = logging.getLogger("atf.core")


def _create_postgres_pool(database_url: str):
    """Create an asyncpg connection pool for the durable queue (v14).

    The Postgres repositories were written against the PostgresConnection
    protocol but nothing ever constructed a pool — the code path was dead.
    asyncpg acquires a connection per operation; the pool sets the
    statement cache and bounds concurrent DB work.
    """
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "ATF_DATABASE_URL is set but asyncpg is not installed. "
            "Install it (pip install asyncpg) or unset ATF_DATABASE_URL to "
            "keep using SQLite."
        ) from exc

    import asyncio

    async def _build():
        pool = await asyncpg.create_pool(
            dsn=database_url,
            min_size=2,
            max_size=10,
            max_queries=50000,
            timeout=30.0,
            command_timeout=30.0,
        )
        await initialize_postgres_schema(pool)
        return pool

    try:
        loop = asyncio.get_running_loop()
        # Called from inside a running loop (normal async startup path):
        # return the coroutine so the caller awaits it on the same loop.
        return _build()
    except RuntimeError:
        pass
    # Called before any loop exists (early sync bootstrap path).
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_build())
    except Exception as exc:
        raise RuntimeError(
            f"Failed to initialize configured PostgreSQL database at {database_url}: {exc}"
        ) from exc


def build_container(cfg: Config) -> dict:
    crypto = CryptoService(cfg.master_key)

    # v14: PostgreSQL is optional. When ATF_DATABASE_URL is set the durable
    # queue runs against Postgres (FOR UPDATE SKIP LOCKED, real connection
    # pooling) — the SQLite repositories remain the default for single-node
    # installs. The queue/message-map repositories are the only ones switched:
    # the rest of the schema is per-service and unchanged.
    pg_pool = None
    if getattr(cfg, "database_url", ""):
        import urllib.parse
        try:
            parsed = urllib.parse.urlsplit(cfg.database_url)
            safe_netloc = parsed.hostname or "localhost"
            if parsed.port:
                safe_netloc = f"{safe_netloc}:{parsed.port}"
            redacted_url = f"{parsed.scheme}://[REDACTED]@{safe_netloc}{parsed.path}"
        except Exception:
            redacted_url = "postgresql://[REDACTED]"
        logger.info("Selected authoritative database backend: PostgreSQL (%s)", redacted_url)
        pg_pool = _create_postgres_pool(cfg.database_url)
        # When called from inside a running loop, _create_postgres_pool
        # returns an un-awaitable *coroutine* — repositories must NOT bind it
        # directly (their DB calls would silently no-op). Stash it here and
        # let _resolve_pg_pool() await + rebind before anything queries.
        if asyncio.iscoroutine(pg_pool):
            logger.info("PostgreSQL pool deferred: awaiting in _resolve_pg_pool")
        # Durable audit outbox shares the PostgreSQL boundary (mission Phase Five).
        from core.infrastructure.persistence.audit_outbox import AuditOutboxRepository
        audit_repo = AuditOutboxRepository(pg_pool)
    else:
        logger.info("Selected authoritative database backend: SQLite (%s)", cfg.db_path)
        audit_repo = None

    db = SqliteDatabase(cfg.db_path)
    session_repo = SqliteSessionRepository(db, crypto)
    if pg_pool is not None and (getattr(cfg, "postgres_all", False) or os.environ.get("ATF_POSTGRES_ALL", "").lower() in ("1", "true")):
        rule_repo = PostgresForwardRuleRepository(pg_pool)
        prompt_repo = PostgresPromptRepository(pg_pool)
    else:
        rule_repo = SqliteForwardRuleRepository(db)
        prompt_repo = SqlitePromptRepository(db)
    filter_repo = SqliteFilterRuleRepository(db)
    ai_repo = SqliteAIConfigRepository(db, crypto)
    cred_repo = SqliteApiCredentialRepository(db, crypto)
    token_repo = SqliteBotTokenRepository(db, crypto)
    processed_repo = SqliteProcessedMessageRepository(db)
    ui_state_repo = SqliteUiStateRepository(db)
    auth_flow_repo = SqliteAuthFlowRepository(db)
    error_log_repo = SqliteErrorLogRepository(db)
    metrics_repo = SqliteMetricsRepository(db)
    rule_stats_repo = SqliteRuleStatsRepository(db)
    user_repo = SqliteUserRepository(db)
    settings_repo = SqliteSettingsRepository(db)
    if pg_pool is not None:
        msg_map_repo = PostgresMessageMapRepository(pg_pool)
        queue_repo = PostgresDeliveryQueueRepository(pg_pool)
    else:
        msg_map_repo = SqliteMessageMapRepository(db)
        queue_repo = SqliteDeliveryQueueRepository(db)
    factory = AIProviderFactory()

    prompt_service = PromptService(prompt_repo)
    ai_circuit_registry = CircuitBreakerRegistry()
    ai_validator = AIOutputValidator()
    ai_transformer = AITransformer(
        ai_config_repo=ai_repo,
        circuit_registry=ai_circuit_registry,
        validator=ai_validator,
        provider_factory=factory,
    )

    sessions = SessionUseCases(session_repo)
    rules = ForwardRuleUseCases(rule_repo, session_repo=session_repo)
    filter_rules = FilterRuleUseCases(filter_repo)
    ai_configs = AIConfigUseCases(ai_repo, factory)
    from core.application.use_cases import (  # noqa: E402
        ApiCredentialUseCases,
        BotTokenUseCases,
    )
    credentials = ApiCredentialUseCases(cred_repo)
    bot_tokens = BotTokenUseCases(token_repo)

    from core.application.backup_service import BackupService  # noqa: E402
    backup = BackupService(
        sessions=session_repo,
        rules=rule_repo,
        filters=filter_repo,
        ai_configs=ai_repo,
        credentials=cred_repo,
        prompts=prompt_repo,
        crypto=crypto,
    )

    pipeline = MessageForwardingUseCase(
        rule_repo, filter_repo, ai_repo, factory,
        filter_engine=FilterEngine(), routing_policy=RoutingPolicy(),
        processed_repo=processed_repo,
    )
    pool = ClientPool(cfg.api_id, cfg.api_hash)
    sync_engine = SyncEngine(msg_map_repo, rule_repo, pool, filter_repo=filter_repo)
    # LogClient is created later (line ~291); queue_manager receives it via a
    # late attribute set right after the client exists (see below).
    queue_manager = DurableQueueManager(
        queue_repo=queue_repo,
        message_map_repo=msg_map_repo,
        client_pool=pool,
        rule_repo=rule_repo,
        num_workers=getattr(cfg, "queue_num_workers", 3),
        batch_size=getattr(cfg, "queue_batch_size", 8),
        max_queue_size=getattr(cfg, "queue_max_size", 10000),
        lease_duration=getattr(cfg, "queue_lease_seconds", 30.0),
        max_per_minute_per_target=getattr(cfg, "queue_max_per_minute_per_target", 25),
        metrics_repo=metrics_repo,
    )
    from core.application.pipeline import DurableMessagePipeline  # noqa: E402
    durable_pipeline = DurableMessagePipeline(
        rule_repo=rule_repo,
        filter_repo=filter_repo,
        queue_manager=queue_manager,
        msg_map_repo=msg_map_repo,
        processed_repo=processed_repo,
        ai_factory=factory,
        ai_repo=ai_repo,
        ai_transformer=ai_transformer,
        prompt_service=prompt_service,
        pool=pool,
        audit_repo=audit_repo,
    )
    from core.infrastructure.telegram.pv_responder import AIPVResponder  # noqa: E402
    pv_responder = AIPVResponder(ai_repo=ai_repo, ai_factory=factory, db=db)

    dispatcher = MessageDispatcher(
        pool, pipeline,
        error_log=error_log_repo, metrics=metrics_repo,
        rule_stats=rule_stats_repo,
        queue_manager=queue_manager,
        sync_engine=sync_engine,
        durable_pipeline=durable_pipeline,
        pv_responder=pv_responder,
    )
    i18n = I18n()
    i18n.set_language(cfg.language)

    from core.domain.authorization import AdminService
    admin_service = AdminService(db, cfg.admin_ids, owner_user_id=cfg.admin_user_id)

    from core.infrastructure.grpc_server.account_servicer import AccountControlServicer
    account_servicer = AccountControlServicer(
        db, users=user_repo, is_admin_user=cfg.admin_ids, admin_service=admin_service
    )

    from core.infrastructure.grpc_server.admin_servicer import AdminControlServicer
    admin_servicer = AdminControlServicer(
        db=db,
        users=user_repo,
        rules=rules,
        admin_service=admin_service,
        credentials=credentials,
        pool=pool,
        error_log=error_log_repo,
        queue_manager=queue_manager,
        bot=None,
        web_url=cfg.web_url,
        i18n=i18n,
    )

    return {
        "config": cfg, "db": db, "crypto": crypto,
        "admin_service": admin_service, "account_servicer": account_servicer,
        "admin_servicer": admin_servicer,
        "sessions": sessions, "rules": rules, "filters": filter_rules,
        "ai": ai_configs, "credentials": credentials, "bot_tokens": bot_tokens,
        "rule_repo": rule_repo,
        "processed": processed_repo, "pipeline": pipeline, "pool": pool,
        "dispatcher": dispatcher, "i18n": i18n, "factory": factory,
        "logger_addr": cfg.logger_addr,
        "ui_state_repo": ui_state_repo, "auth_flow_repo": auth_flow_repo,
        "error_log": error_log_repo,
        "metrics": metrics_repo, "rule_stats": rule_stats_repo,
        "users": user_repo,
        "msg_map": msg_map_repo,
        "audit_repo": audit_repo,
        "queue_repo": queue_repo,
        "queue_manager": queue_manager,
        "sync_engine": sync_engine,
        "durable_pipeline": durable_pipeline,
        "prompt_repo": prompt_repo,
        "prompt_service": prompt_service,
        "backup": backup,
        "ai_transformer": ai_transformer,
        "ai_circuit_registry": ai_circuit_registry,
        "pv_responder": pv_responder,
        "account_servicer": account_servicer,
        "settings": settings_repo,
        # The raw pool (or its deferred coroutine) — awaited by _resolve_pg_pool.
        "pg_pool_holder": pg_pool,
    }


async def _resolve_pg_pool(container: dict) -> None:
    """Await the deferred PostgreSQL pool and rebind every PG-backed object.

    ``build_container`` is sync but ``_create_postgres_pool`` returns a
    coroutine when a loop is already running, so the repositories initially
    hold an un-awaited coroutine. Without this rebind their DB calls would
    silently no-op (``_fetch_all_db`` falls through to ``return []``), which
    is exactly the swallowed-connection-failure class the observability
    mission forbids. Idempotent: no-op when the pool is already a pool.
    """
    pool = container.get("pg_pool_holder")
    if pool is None or not asyncio.iscoroutine(pool):
        return
    resolved = await pool
    # Rebind every repo that was constructed against the coroutine.
    for key in ("rule_repo", "prompt_repo", "msg_map_repo", "queue_repo"):
        obj = container.get(key)
        if obj is not None and asyncio.iscoroutine(getattr(obj, "_db", None)):
            obj._db = resolved
    audit_repo = container.get("audit_repo")
    if audit_repo is not None and asyncio.iscoroutine(getattr(audit_repo, "_db", None)):
        audit_repo._db = resolved
    container["pg_pool_holder"] = resolved
    logger.info("PostgreSQL pool resolved and bound to repositories")


async def serve_grpc(container: dict, cfg: Config):
    import grpc
    from core.proto import pb_grpc

    server = grpc.aio.server()
    pb_grpc.add_SessionControlServiceServicer_to_server(
        servicers.SessionControlServicer(container["sessions"], container["pool"]), server
    )
    pb_grpc.add_ForwardRuleControlServiceServicer_to_server(
        servicers.ForwardRuleControlServicer(container["rules"], queue=container.get("queue_repo")), server
    )
    pb_grpc.add_FilterControlServiceServicer_to_server(
        servicers.FilterControlServicer(container["filters"]), server
    )
    pb_grpc.add_AIControlServiceServicer_to_server(
        servicers.AIControlServicer(
            container["ai"],
            prompt_service=container["prompt_service"],
            ai_transformer=container["ai_transformer"],
        ),
        server,
    )
    pb_grpc.add_SystemStatusControlServiceServicer_to_server(
        servicers.SystemStatusControlServicer(
            container["sessions"], container["rules"],
            container["pipeline"], "", cfg.version,
        ),
        server,
    )
    pb_grpc.add_DeliveryControlServiceServicer_to_server(
        servicers.DeliveryControlServicer(
            container["db"],
            container["rules"],
            container.get("pipeline"),
            queue_repo=container.get("queue_repo"),
            message_map_repo=container.get("msg_map_repo"),
        ),
        server,
    )
    # Centralized audit events (Phase Three): tenant-scoped durable queries.
    pb_grpc.add_AuditControlServiceServicer_to_server(
        servicers.AuditControlServicer(container.get("audit_repo")), server,
    )
    from core.infrastructure.grpc_server.account_servicer import AccountControlServicer

    pb_grpc.add_AccountControlServiceServicer_to_server(
        container.get("account_servicer") or AccountControlServicer(
            container["db"],
            users=container.get("users"),
            is_admin_user=cfg.admin_ids,
            admin_service=container.get("admin_service"),
        ),
        server,
    )
    pb_grpc.add_BackupControlServiceServicer_to_server(
        servicers.BackupControlServicer(container["backup"]), server,
    )
    if "admin_servicer" in container:
        pb_grpc.add_AdminControlServiceServicer_to_server(
            container["admin_servicer"], server,
        )
    addr = f"{cfg.grpc_host}:{cfg.grpc_port}"
    server.add_insecure_port(addr)
    await server.start()
    logger.info("gRPC server listening on %s", addr)
    return server


async def seed_default_credential(container: dict, cfg: Config) -> None:
    """Persist the config.yaml api_id/api_hash pair into the vault once so
    sessions created before this update keep working."""
    try:
        creds_uc = container.get("credentials")
        if creds_uc is None or not cfg.api_id or not cfg.api_hash:
            return
        existing = await creds_uc.list_all()
        if any(c.api_id == cfg.api_id for c in existing):
            return
        await creds_uc.create(
            label="default", api_id=cfg.api_id, api_hash=cfg.api_hash,
            is_default=not any(c.is_default for c in existing),
        )
        logger.info("seeded default api credential from config")
    except Exception:
        logger.debug("credential seeding skipped", exc_info=True)


async def sync_pool_credentials(container: dict) -> None:
    """Load every vault credential into the in-memory ClientPool registry."""
    try:
        creds = await container["credentials"].list_all()
    except Exception:
        return
    pool = container["pool"]
    for c in creds:
        try:
            pool.register_credentials(c.id, c.api_id, c.api_hash)
            if c.is_default:
                pool.register_credentials("default", c.api_id, c.api_hash)
        except Exception:
            logger.warning("bad credential %s skipped", c.id[:8])


async def main() -> None:
    cfg = Config.load()
    try:
        cfg.validate()
    except ValueError as exc:
        logger.critical("Configuration validation error: %s", exc)
        sys.exit(1)

    if cfg.disable_bot:
        logger.info("ATF_DISABLE_BOT set: gRPC-only mode (no bot, no Telegram).")
        # Await the PostgreSQL pool coroutine (created inside build_container
        # when a running loop is present) before constructing repositories.
        container = build_container(cfg)
        if asyncio.iscoroutine(container):
            container = await container
        await _resolve_pg_pool(container)
        grpc_server = await serve_grpc(container, cfg)
        await seed_default_credential(container, cfg)
        await sync_pool_credentials(container)
        from core.infrastructure.telegram.session_boot import restore_all_sessions  # noqa: E402
        await restore_all_sessions(container)
        try:
            await asyncio.Event().wait()
        finally:
            mgr = container.get("boot_manager")
            if mgr:
                await mgr.stop_reconciler()
            await grpc_server.stop(grace=2)
            container["db"].close()
            logger.info("Core (gRPC-only) stopped cleanly.")
        return

    if not cfg.api_id or not cfg.api_hash or not cfg.bot_token:
        logger.error(
            "Missing api_id / api_hash / bot_token. "
            "Set them in config.yaml or via ATF_* environment variables."
        )
        sys.exit(1)

    container = build_container(cfg)
    if asyncio.iscoroutine(container):
        container = await container
    await _resolve_pg_pool(container)
    grpc_server = await serve_grpc(container, cfg)
    await seed_default_credential(container, cfg)
    await sync_pool_credentials(container)
    await container["prompt_service"].init_defaults()

    # Non-blocking debug-log feed to the Logger microservice.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.observability.log_client import LogClient, CentralLogHandler  # noqa: E402
    from core.application.log_policy import LogPolicy  # noqa: E402
    # The client is the door every event leaves through (audit choke point, the
    # info/warn/error helpers and the stdlib bridge), so the collection policy is
    # attached there once instead of at each call site.
    log_client = LogClient(cfg.logger_addr, policy=LogPolicy.from_env())
    try:
        await log_client.start()
    except Exception as exc:
        logger.warning("Logger service unreachable at %s: %s", cfg.logger_addr, exc)
    container["log_client"] = log_client
    log_client.info("core", "system", "core started", {"version": cfg.version})
    # Mirror every WARNING/ERROR from any python module (pv_responder,
    # dispatcher, pipeline, client_pool, …) into the central logger so the
    # Web-UI Log Explorer shows them without ssh/journalctl. Pyrogram TCP
    # noise is filtered inside the handler.
    logging.getLogger().addHandler(CentralLogHandler(log_client))
    # Late-bind the same client into the queue manager (built in
    # build_container before the LogClient existed) so delivery successes
    # reach the central log too.
    container["queue_manager"]._log_client = log_client
    # Same for the forwarding pipeline's audit emitter (Phase Six telemetry).
    if getattr(container["pipeline"], "_log_client", None) is None:
        container["pipeline"]._log_client = log_client

    # TASK 03 / audit D1: persisted auth state machine, so a login in
    # progress survives a restart instead of vanishing without explanation.
    from core.infrastructure.telegram.auth_state import AuthStateMachine  # noqa: E402
    auth_machine = AuthStateMachine(container["auth_flow_repo"], ttl_seconds=LOGIN_TTL)
    container["auth_machine"] = auth_machine
    await auth_machine.sweep_expired()

    bot = BotManager(
        bot_token=cfg.bot_token, admin_ids=cfg.admin_ids,
        pool=container["pool"], sessions=container["sessions"],
        rules=container["rules"], filter_rules=container["filters"],
        ai_configs=container["ai"], pipeline=container["pipeline"],
        i18n=container["i18n"], crypto=container["crypto"],
        credentials=container.get("credentials"),
        bot_tokens=container.get("bot_tokens"),
        dispatcher=container.get("dispatcher"),
        ui_state_repo=container.get("ui_state_repo"),
        auth_machine=auth_machine,
        users=container.get("users"),
    )
    await bot.start()
    if "admin_servicer" in container:
        container["admin_servicer"]._bot = bot.bot

    queue_mgr = container.get("queue_manager")
    if queue_mgr:
        await queue_mgr.start()
    logger.info("Bot started and durable queue workers active. Core is running.")

    # Restore saved sessions + bind real-time handlers (the actual forwarder).
    from core.infrastructure.telegram.session_boot import restore_all_sessions  # noqa: E402
    try:
        statuses = await restore_all_sessions(container)
        live = sum(1 for s in statuses.values() if s == "live")
        log_client.info("core", "boot", f"sessions restored: {live}/{len(statuses)} live")
    except Exception as exc:
        logger.warning("session restore failed: %s", exc)

    # v15: Telegram stops pushing channel posts the moment a session's channel
    # read-state is caught up, which happens constantly (health checks, status
    # fetches, the dashboard). Poll the active source channels on a short
    # interval so forwarding does not depend on push arriving. This is what
    # makes an active rule actually forward.
    try:
        from core.infrastructure.telegram.channel_polling import ChannelPollingService  # noqa: E402
        from core.infrastructure.persistence.sqlite_repositories import SqliteChannelCursorRepository  # noqa: E402
        cursor_repo = SqliteChannelCursorRepository(container["db"])
        poller = ChannelPollingService(
            pool=container["pool"],
            rules=container["rule_repo"],
            dispatcher=container["dispatcher"],
            state_store=cursor_repo,
            interval=getattr(cfg, "channel_poll_interval", 15.0),
        )
        await poller.start()
        container["channel_poller"] = poller
        log_client.info("core", "boot", "channel polling active")
    except Exception as exc:
        logger.warning("channel polling failed to start: %s", exc)

    # A restart kills the in-memory login clients; move any flow that still
    # claims to hold an open socket to AUTH_EXPIRED so the user is told,
    # instead of the bot silently ignoring their code.
    try:
        expired = await bot.login_manager.recover_after_restart()
        if expired:
            log_client.info("core", "boot", f"auth flows expired on restart: {expired}")
    except Exception as exc:
        logger.warning("auth flow recovery failed: %s", exc)

    # Mount the Pro button UI directly onto the live bot client.
    try:
        ui = ProBotUI(
            bot=bot.bot, i18n=container["i18n"],
            sessions=container["sessions"], rules=container["rules"],
            filter_rules=container["filters"], ai_configs=container["ai"],
            pipeline=container["pipeline"], pool=container["pool"],
            # Share the SAME login manager as BotManager: two instances would
            # mean two disjoint pending-login states and codes would never match.
            login=bot.login_manager,
            log_client=log_client, admin_ids=cfg.admin_ids,
            admin_service=container.get("admin_service"),
            db=container.get("db"),
            credentials=container.get("credentials"),
            bot_tokens=container.get("bot_tokens"),
            dispatcher=container.get("dispatcher"),
            boot_manager=container.get("boot_manager"),
            ui_state_repo=container.get("ui_state_repo"),
            error_log=container.get("error_log"),
            metrics=container.get("metrics"),
            rule_stats=container.get("rule_stats"),
            users=container.get("users"),
            prompt_service=container.get("prompt_service"),
            ai_transformer=container.get("ai_transformer"),
            pv_responder=container.get("pv_responder"),
            web_url=cfg.web_url,
            account_servicer=container.get("account_servicer"),
            backup_service=container.get("backup"),
            settings=container.get("settings"),
        )
        # An operator's saved dashboard URL wins over the config/env default —
        # they set it from the bot and expect it to survive a restart.
        try:
            saved_url = await container["settings"].get("web_url", "")
            if saved_url:
                ui._web_url = saved_url.rstrip("/")
        except Exception as exc:
            logger.warning("Could not load saved web_url: %s", exc)
        # Route ownership: while the button UI has an active step for a user
        # it answers; otherwise BotManager (commands) answers. No doubles.
        bot.bot_ui = ui
        bot.ui_step_checker = lambda uid: bool(ui._state(uid).step)
        log_client.info("core", "system", "pro button UI mounted")
    except Exception as exc:
        logger.warning("ProBotUI mount failed: %s", exc)

    try:
        await asyncio.Event().wait()
    finally:
        queue_mgr = container.get("queue_manager")
        if queue_mgr:
            await queue_mgr.stop()
        mgr = container.get("boot_manager")
        if mgr:
            await mgr.stop_reconciler()
        await bot.stop()
        await container["pool"].stop_all()
        await grpc_server.stop(grace=2)
        try:
            await log_client.close()
        except Exception:
            pass
        logger.info("Core stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
