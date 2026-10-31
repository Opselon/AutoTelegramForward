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
from core.application.log_policy import LogPolicy  # noqa: E402
from core.application.delivery_reconciler import DeliveryReconciler  # noqa: E402
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
from core.infrastructure.persistence.account_limits import AccountLimitStore  # noqa: E402
from core.infrastructure.persistence.log_policy_store import LogPolicyStore  # noqa: E402
from core.infrastructure.persistence.postgres_repositories import (  # noqa: E402
    PostgresDeliveryQueueRepository,
    PostgresForwardRuleRepository,
    PostgresMessageMapRepository,
    PostgresPromptRepository,
    PostgresUserTaskRepository,
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
    SqliteRuleCounterRepository,
    SqliteRuleStatsRepository,
    SqliteSessionRepository,
    SqliteUserTaskRepository,
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
from core.infrastructure.telegram.button_style import (  # noqa: E402
    DANGER as BTN_DANGER,
    PRIMARY as BTN_PRIMARY,
    SUCCESS as BTN_SUCCESS,
    disable_styles,
)
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
    # "Forward every Nth message" cadence, beside the dedupe store it is
    # written with (and for the same reason: it is this worker's own
    # bookkeeping, and it must outlive a restart).
    counter_repo = SqliteRuleCounterRepository(db)
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
    # Audit findings #1/#5 (forwarding reliability): the send-side idempotency
    # fence and the ingestion-gap recovery store. Both are state-only (no
    # message payloads) and live beside the queue they protect, on whichever
    # engine the deployment uses.
    from core.infrastructure.persistence.sqlite_repositories import (  # noqa: E402
        SqliteDeliveryAttemptRepository,
        SqliteDiagnosticEventRepository,
        SqliteIngestionGapRepository,
    )
    # Audit findings #1/#5 (forwarding reliability): the send-side idempotency
    # fence and the ingestion-gap recovery store. Both are state-only (no
    # message payloads) and live beside the queue they protect, on whichever
    # engine the deployment uses.
    from core.infrastructure.persistence.postgres_repositories import (  # noqa: E402
        PostgresDeliveryAttemptRepository,
        PostgresIngestionGapRepository,
    )
    if pg_pool is not None:
        gap_repo = PostgresIngestionGapRepository(pg_pool)
        attempts_repo = PostgresDeliveryAttemptRepository(pg_pool)
    else:
        gap_repo = SqliteIngestionGapRepository(db)
        attempts_repo = SqliteDeliveryAttemptRepository(db)
    # Failure intelligence (schema v32): the structured diagnostic-event store.
    # Appended once per attempt, so the retried and fallback history of every
    # delivery is queryable. SQLite here; Postgres mirrors it in its own module.
    diagnostics_repo = SqliteDiagnosticEventRepository(db)
    # Same deferred-pool rebinding rule as the other pg repos above.
    if asyncio.iscoroutine(gap_repo._db):
        gap_repo._db = pg_pool
    if asyncio.iscoroutine(attempts_repo._db):
        attempts_repo._db = pg_pool
    if asyncio.iscoroutine(diagnostics_repo._db):
        diagnostics_repo._db = pg_pool
    # Per-user task lifecycle (API-first phase 2): the authoritative state
    # machine lives beside every other tenant resource, whichever engine is in
    # use. Same deferred-pool rebinding rules as the other pg repos.
    from core.application.user_tasks import UserTaskUseCases  # noqa: E402
    if pg_pool is not None:
        task_repo = PostgresUserTaskRepository(pg_pool)
    else:
        task_repo = SqliteUserTaskRepository(db)
    tasks = UserTaskUseCases(task_repo)
    if asyncio.iscoroutine(task_repo._db):
        task_repo._db = pg_pool
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
        # Same inheritance rule as the durable pipeline below; this path is the
        # one the Telegram bot and the reply/PV flows run through.
        bot_tokens=token_repo,
        counters=counter_repo,
    )
    pool = ClientPool(cfg.api_id, cfg.api_hash, limit_store=AccountLimitStore(db))
    # A restriction that outlives the process must outlive the process: load
    # live suspensions before any worker can send.
    try:
        pool.hydrate_limits()
    except Exception as exc:
        logger.warning("Could not restore account limits: %s", exc)
    sync_engine = SyncEngine(
        msg_map_repo, rule_repo, pool, filter_repo=filter_repo,
        # E2E-3: an edit arriving while the delivery job has not run is folded
        # into that job so the first post carries the new text.
        jobs_repo=queue_repo,
    )

    # ---- AI private-message auto-reply (v27) --------------------------------
    # Queue + engine + service, all tenant-scoped in SQLite. Construction is
    # synchronous like the rest of this graph; the worker is started by the
    # async entry point once the log client exists.
    from core.application.autoreply.engine import AutoReplyEngine  # noqa: E402
    from core.application.autoreply.service import AutoReplyService  # noqa: E402
    from core.application.autoreply.worker import AutoReplyWorker  # noqa: E402
    from core.infrastructure.persistence.autoreply_repositories import AutoReplyRepository  # noqa: E402

    autoreply_repo = AutoReplyRepository(db)
    autoreply_worker = AutoReplyWorker(
        repo=autoreply_repo, ai_repo=ai_repo, provider_factory=factory,
    )
    autoreply_engine = AutoReplyEngine(autoreply_repo)
    # The dispatcher registers a live send transport per session on the worker;
    # keeping the reference here avoids a second queue instance that would
    # never be drained.
    autoreply_engine.attach_worker(autoreply_worker)

    def _owner_for_session(session_id: str) -> int:
        try:
            row = db.query_one(
                "SELECT owner_user_id FROM sessions WHERE id=?", (str(session_id),)
            )
            return int(row["owner_user_id"] or 0) if row else 0
        except Exception:
            return 0

    async def _resolve_private_chat(session_id: str, query: str):
        """Resolve a chat through the tenant's own live session."""
        from core.infrastructure.telegram.autoreply_bridge import resolve_private_chat  # noqa: E402

        client = pool.get(str(session_id)) if session_id else None
        if client is None:
            for candidate in pool.list_sessions():
                client = pool.get(candidate)
                if client is not None:
                    break
        if client is None:
            return None
        return await resolve_private_chat(client, query)

    autoreply_service = AutoReplyService(
        autoreply_repo, ai_repo=ai_repo, resolver=_resolve_private_chat,
        engine=autoreply_engine, session_owner=_owner_for_session,
    )
    # LogClient is created later (line ~291); queue_manager receives it via a
    # late attribute set right after the client exists (see below).
    from core.infrastructure.telegram.bot_transport import BotTransportCache  # noqa: E402

    # Bot route: tasks can deliver through a bot the owner attached instead of
    # the owner's account. The cache holds one pooled transport per token, so
    # neither the database nor a TLS handshake is repeated per message.
    bot_route = BotTransportCache(token_repo)

    # Worker coordination (optional): one replica at a time for the singleton
    # maintenance loops. The queues' own idempotency stays in the database; this
    # only stops replicas from running the same maintenance pass concurrently.
    from core.infrastructure.redis import LeaseCoordinator  # noqa: E402
    # Built here, connected by the async entry points (main()); this function
    # must stay synchronous — the whole container graph is constructed on the
    # hot bootstrap path where there is no running loop yet.
    lease_coordinator = LeaseCoordinator.from_config(cfg)

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
        bot_route=bot_route,
        lease_coordinator=lease_coordinator,
        # Audit finding #5: the send-side idempotency fence — stops a lease
        # expiring mid-dispatch, a crash before the batch commit, or the legacy
        # direct path from double-dispatching one logical delivery.
        attempts_repo=attempts_repo,
        # Failure intelligence: the structured diagnostic-event store. Every
        # Telegram send failure appends the raw RPC error and the normalized
        # classification, so the outcome is queryable per message and per facet.
        diagnostics_repo=diagnostics_repo,
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
        # Tasks inherit the defaults of the bot they have attached; resolving it
        # here means the transform and the delivery job it enqueues both use the
        # effective settings.
        bot_tokens=token_repo,
        counters=counter_repo,
        # Audit finding #1: resolves an ingestion gap row the moment the
        # pipeline actually sees the source id — the only proof the post was
        # not lost.
        gap_repo=gap_repo,
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
        autoreply_engine=autoreply_engine,
        autoreply_worker=autoreply_worker,
        owner_lookup=_owner_for_session,
        # Audit finding #5: the legacy direct send path shares the worker's
        # idempotency fence — same key derivation, same store.
        attempts_repo=attempts_repo,
    )
    # The branding hop (A -> C -> B) needs a live client and the transform-aware
    # posting routine, both of which live on the dispatcher; the pipeline decides
    # *whether* to hop (shared decision) and hands the resulting plan to the
    # durable queue as job state. Wiring them here keeps the application layer
    # free of infrastructure imports.
    durable_pipeline.set_branding_hop(dispatcher)
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

    # The replace-rule manager: one instance for the whole process, over the
    # same rule use-cases every other surface writes through. Built here rather
    # than inside serve_grpc so the bot panel and the gRPC servicer cannot end
    # up with two managers over one store.
    from core.application.replace_rules import ReplaceRuleService
    replace_rules = ReplaceRuleService(rules)

    return {
        "config": cfg, "db": db, "crypto": crypto,
        "replace_rules": replace_rules,
        "admin_service": admin_service, "account_servicer": account_servicer,
        "admin_servicer": admin_servicer,
        "sessions": sessions, "rules": rules, "filters": filter_rules,
        "ai": ai_configs, "credentials": credentials, "bot_tokens": bot_tokens,
        "bot_route": bot_route,
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
        # v32: the idempotency fence repository, exposed so the reconciler can
        # inspect in-flight claims without a second lookup path.
        "attempts_repo": attempts_repo,
        # v32: the same gap repository the polling service owns, so the
        # reconciler reports on the same recovery work the poller performs.
        "gap_repo": gap_repo,
        # v32: the diagnostic-event store, for the monitoring API.
        "diagnostics_repo": diagnostics_repo,
        "task_repo": task_repo,
        "tasks": tasks,
        "queue_manager": queue_manager,
        "lease_coordinator": lease_coordinator,
        "sync_engine": sync_engine,
        "durable_pipeline": durable_pipeline,
        "prompt_repo": prompt_repo,
        "prompt_service": prompt_service,
        "backup": backup,
        "ai_transformer": ai_transformer,
        "ai_circuit_registry": ai_circuit_registry,
        "pv_responder": pv_responder,
        "autoreply_repo": autoreply_repo,
        "autoreply_engine": autoreply_engine,
        "autoreply_worker": autoreply_worker,
        "autoreply_service": autoreply_service,
        "settings": settings_repo,
        # The raw pool (or its deferred coroutine) — awaited by _resolve_pg_pool.
        "pg_pool_holder": pg_pool,
        # The producer-side collection policy is process state, not a connection:
        # build it here so BOTH the log client that enforces it and the control
        # plane that reports it (over GetLogPolicy) share one instance. Two
        # instances would let the API read a policy the process is not applying.
        # An operator change is restored from the store on top of the environment
        # defaults, so "turn on INFO for an hour" survives a restart of this
        # process instead of silently reverting at the next deploy.
        "log_policy": _restored_log_policy(db),
        "log_policy_store": LogPolicyStore(db),
    }


def _restored_log_policy(db) -> LogPolicy:
    """The policy Core must enforce: environment defaults, then the saved override.

    The environment stays the baseline and the store only overrides it, so an
    install that has never been changed behaves exactly as its configuration
    says — and a saved state that no longer validates is ignored, not fatal.
    """
    policy = LogPolicy.from_env()
    try:
        LogPolicyStore(db).restore_into(policy)
    except Exception as exc:  # a bad settings row must not stop Core starting
        logger.warning("[PIPELINE:log_policy] could not restore the saved policy: %s", exc)
    return policy


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
        # Sync bootstrap: pool is already resolved; still need to ensure the
        # audit store's tables exist (idempotent on every boot).
        audit_repo = container.get("audit_repo")
        if audit_repo is not None and hasattr(audit_repo, "initialize"):
            try:
                await audit_repo.initialize()
            except Exception as exc:
                logger.warning("Audit store schema initialization failed: %s", exc)
        return
    resolved = await pool
    # Rebind every repo that was constructed against the coroutine.
    for key in ("rule_repo", "prompt_repo", "msg_map_repo", "queue_repo",
                "task_repo"):
        obj = container.get(key)
        if obj is not None and asyncio.iscoroutine(getattr(obj, "_db", None)):
            obj._db = resolved
    audit_repo = container.get("audit_repo")
    if audit_repo is not None and asyncio.iscoroutine(getattr(audit_repo, "_db", None)):
        audit_repo._db = resolved
    # The audit store's tables are created here, on the resolved pool, rather
    # than in build_container(): before this rebind the repository still holds
    # the un-awaited pool coroutine, so an initialize() there would either
    # no-op or fail, and the first audit write would then raise
    # UndefinedTableError — swallowed by the worker's "audit must not kill a
    # task" handler, silently losing every audit record on a PostgreSQL
    # deployment. initialize() is idempotent (CREATE TABLE IF NOT EXISTS), so
    # running it on the already-resolved pool is a no-op on later boots.
    if audit_repo is not None and hasattr(audit_repo, "initialize"):
        try:
            await audit_repo.initialize()
        except Exception as exc:
            logger.warning("Audit store schema initialization failed: %s", exc)
    container["pg_pool_holder"] = resolved
    logger.info("PostgreSQL pool resolved and bound to repositories")


async def serve_grpc(container: dict, cfg: Config):
    import grpc
    from core.proto import pb_grpc
    from core.infrastructure.grpc_server.internal_auth import build_interceptors

    addr = f"{cfg.grpc_host}:{cfg.grpc_port}"
    interceptors = build_interceptors(getattr(cfg, "core_rpc_token", ""))
    server = grpc.aio.server(interceptors=interceptors)
    pb_grpc.add_SessionControlServiceServicer_to_server(
        servicers.SessionControlServicer(container["sessions"], container["pool"]), server
    )
    pb_grpc.add_ForwardRuleControlServiceServicer_to_server(
        servicers.ForwardRuleControlServicer(
            container["rules"], queue=container.get("queue_repo"),
            bot_tokens=container.get("bot_tokens"),
        ), server
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
    # AI private-message auto-reply (v28). Constructed from the same
    # AutoReplyService the bot UI uses, so a change made on either surface is
    # the behaviour the worker applies to the next inbound message.
    from core.infrastructure.grpc_server.autoreply_servicer import AutoReplyControlServicer  # noqa: E402

    pb_grpc.add_AutoReplyControlServiceServicer_to_server(
        AutoReplyControlServicer(
            container["autoreply_service"], engine=container.get("autoreply_engine"),
        ),
        server,
    )
    system_status = servicers.SystemStatusControlServicer(
        container["sessions"], container["rules"],
        container["pipeline"], "", cfg.version,
        # Same instance the log client enforces, so the control plane
        # reports the policy this process is really applying.
        log_policy=container.get("log_policy"),
        # ... and the same store, so a change an operator makes here is the one
        # that survives a restart.
        log_policy_store=container.get("log_policy_store"),
    )
    # Kept so the Logger client, built further down, can be attached to it: the
    # audit record of a policy change has to travel through the same policy and
    # the same client as every other event, and the servicer is constructed
    # before the client exists.
    container["system_status_servicer"] = system_status
    pb_grpc.add_SystemStatusControlServiceServicer_to_server(system_status, server)
    pb_grpc.add_BotTokenControlServiceServicer_to_server(
        servicers.BotTokenControlServicer(
            container["bot_tokens"], rules=container["rules"],
            bot_route=container.get("bot_route"),
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
            # v32 (reconciliation): the stats screen also reports the findings
            # and health aggregates, owner-scoped by the same rule ownership
            # the servicer already enforces. Built from the repos the queue
            # already owns; nothing new is persisted.
            reconciler=DeliveryReconciler(
                container.get("queue_repo"),
                container.get("msg_map"),
                container["rules"],
                attempts_repo=container.get("attempts_repo"),
                gap_repo=container.get("gap_repo"),
                lease_seconds=float(container.get("queue_lease_seconds", 30.0) or 30.0),
            ),
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
    # Replace rules: the manager behind /replace on every surface. The instance
    # was built with the container, so the bot panel and this servicer share it
    # and a rule created from the dashboard is the one the next forwarded
    # message is checked against.
    from core.application.replace_rules import ReplaceRuleService  # noqa: E402
    from core.infrastructure.grpc_server.replace_servicer import (  # noqa: E402
        ReplaceRuleControlServicer,
    )

    replace_rules = container.get("replace_rules") or ReplaceRuleService(container["rules"])
    container["replace_rules"] = replace_rules
    pb_grpc.add_ReplaceRuleControlServiceServicer_to_server(
        ReplaceRuleControlServicer(replace_rules), server,
    )
    # Per-user task lifecycle (API-first phase 2) — creation/control surface.
    from core.infrastructure.grpc_server.task_servicer import TaskControlServicer  # noqa: E402
    pb_grpc.add_TaskControlServiceServicer_to_server(
        TaskControlServicer(container["tasks"]), server,
    )
    server.add_insecure_port(addr)
    await server.start()
    if interceptors:
        # Say *that* it is enforced, never the credential itself.
        logger.info("gRPC server listening on %s (internal rpc credential required)", addr)
    else:
        logger.warning(
            "gRPC server listening on %s WITHOUT an internal rpc credential: every servicer "
            "trusts the owner_user_id in its request, so anything that can reach this port can "
            "act as any tenant. Set ATF_CORE_RPC_TOKEN (same value on the API) to enforce one.",
            addr,
        )
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


async def _start_task_worker(container: dict) -> None:
    """Start the per-user task worker. It is NOT a startup gate: if the build
    fails the process continues without task execution and logs the reason."""
    from core.application.user_task_worker import UserTaskWorker, build_executors  # noqa: E402
    try:
        executors = build_executors(container)
        worker = UserTaskWorker(container["tasks"], executors,
                                container=container,
                                max_concurrent=getattr(container["config"],
                                                       "task_worker_concurrency", 4),
                                retention_seconds=getattr(container["config"],
                                                          "task_retention_seconds", 0))
        container["task_worker"] = worker
        container["task_worker_handle"] = asyncio.create_task(worker.run_forever())
        logger.info("User task worker started (%d executor kinds)", len(executors))
    except Exception:
        logger.exception("user task worker failed to start; task creation "
                         "remains enabled and tasks stay PENDING")


async def _stop_task_worker(container: dict) -> None:
    worker = container.get("task_worker")
    if worker is None:
        return
    worker.stop()
    handle = container.get("task_worker_handle")
    if handle is not None and not handle.done():
            handle.cancel()
            try:
                await handle
            except (asyncio.CancelledError, Exception):
                pass


async def _connect_lease_coordinator(container: dict) -> None:
    """Connect the optional lease store before any coordinated loop starts.

    A store that cannot be reached is not an error: the coordinator records the
    outage and the loops simply run un-coordinated (single-replica semantics)
    until it recovers. See core/infrastructure/redis/lease.py.
    """
    coord = container.get("lease_coordinator")
    if coord is not None:
        await coord.connect()


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
        await _connect_lease_coordinator(container)
        await _start_task_worker(container)
        grpc_server = await serve_grpc(container, cfg)
        await seed_default_credential(container, cfg)
        await sync_pool_credentials(container)
        from core.infrastructure.telegram.session_boot import restore_all_sessions  # noqa: E402
        await restore_all_sessions(container)
        try:
            await asyncio.Event().wait()
        finally:
            await _stop_task_worker(container)
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
    await _connect_lease_coordinator(container)
    grpc_server = await serve_grpc(container, cfg)
    await seed_default_credential(container, cfg)
    await sync_pool_credentials(container)
    await container["prompt_service"].init_defaults()

    # Non-blocking debug-log feed to the Logger microservice.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.observability.log_client import LogClient, CentralLogHandler  # noqa: E402
    # The client is the door every event leaves through (audit choke point, the
    # info/warn/error helpers and the stdlib bridge), so the collection policy is
    # attached there once instead of at each call site. Reuse the instance built
    # with the container: the control plane reports that same object, and two
    # instances would let it describe a policy this process is not applying.
    log_client = LogClient(
        cfg.logger_addr,
        policy=container["log_policy"],
        token=os.environ.get("ATF_LOGGER_INGEST_TOKEN", ""),
    )
    try:
        await log_client.start()
    except Exception as exc:
        logger.warning("Logger service unreachable at %s: %s", cfg.logger_addr, exc)
    container["log_client"] = log_client
    # Attach the client to the control-plane servicer now that it exists, so a
    # policy change writes its audit record through the policy it changed rather
    # than escaping to a private sink.
    container["system_status_servicer"].attach_log_client(log_client)
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
    # Auto-reply telemetry: the same client, so an inbound message's
    # correlation id ties the decision, the generation and the delivery
    # together in the Log Explorer.
    container["autoreply_engine"].attach_log(log_client)
    container["autoreply_worker"].attach_log(log_client)

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
    await _start_task_worker(container)

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
            lease_coordinator=container["lease_coordinator"],
            # Audit finding #1: unreturned ids become durable recovery work
            # that this poller re-fetches with bounded backoff.
            gap_repo=container["gap_repo"],
            # Structured emission of every gap/recovery event (with the raw
            # Telegram error attached) into the central log.
            log_client=container.get("log_client"),
            # Persisted gap diagnostics: the cause of every gap is queryable
            # per source message, with the raw Telegram error attached.
            diagnostics_repo=container.get("diagnostics_repo"),
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
            bot_route=container.get("bot_route"),
            dispatcher=container.get("dispatcher"),
            boot_manager=container.get("boot_manager"),
            ui_state_repo=container.get("ui_state_repo"),
            error_log=container.get("error_log"),
            metrics=container.get("metrics"),
            rule_stats=container.get("rule_stats"),
            delivery_queue=container.get("queue_repo"),
            users=container.get("users"),
            prompt_service=container.get("prompt_service"),
            ai_transformer=container.get("ai_transformer"),
            pv_responder=container.get("pv_responder"),
            web_url=cfg.web_url,
            account_servicer=container.get("account_servicer"),
            backup_service=container.get("backup"),
            settings=container.get("settings"),
            replace_rules=container.get("replace_rules"),
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

        # Engine → owner notices ride the same bot: the delivery worker pauses
        # a task when its source/target is gone, and the owner has to hear
        # about it (with the reason and a recovery button), not discover it.
        try:
            container["queue_manager"]._owner_notifier = ui.notify_task_event
        except Exception as exc:
            logger.warning("Could not wire owner notifications: %s", exc)

        # Opt-in diagnostic: ATF_BUTTON_STYLE_PROBE=<chat id> sends ONE message
        # with a styled keyboard through the live client, which is the only way
        # to prove the server accepts keyboardButtonStyle on this session's
        # layer. Off unless set; a rejection disables styles for the process so
        # a panel can never be left unrenderable.
        probe_chat = os.environ.get("ATF_BUTTON_STYLE_PROBE", "").strip()
        if probe_chat:
            async def _style_probe(chat_id: str) -> None:
                try:
                    target = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
                    await ui.bot.send_message(
                        target,
                        "Button style probe: primary / success / danger",
                        reply_markup=ui._kbd([[
                            ("Primary", "probe:p", BTN_PRIMARY),
                            ("Success", "probe:s", BTN_SUCCESS),
                            ("Danger", "probe:d", BTN_DANGER),
                        ]]),
                    )
                    logger.info("[UI:STYLE] probe delivered to %s — styled keyboard accepted", target)
                except Exception as exc:
                    disable_styles(str(exc)[:120])
                    logger.error("[UI:STYLE] probe rejected by the server: %s", exc)

            asyncio.create_task(_style_probe(probe_chat))

        log_client.info("core", "system", "pro button UI mounted")
    except Exception as exc:
        logger.warning("ProBotUI mount failed: %s", exc)

    try:
        # Drain the auto-reply queue: a pending reply is a promise the user
        # made to their contact, so it ships rather than being dropped on
        # restart. The worker's own maintenance loop also recovers any lease
        # a previous process left dangling.
        await container["autoreply_worker"].start()
        from core.infrastructure.telegram.autoreply_bridge import register_pool_senders

        wired = register_pool_senders(container["pool"], container["autoreply_worker"])
        log_client.info("core", "autoreply", "auto-reply queue worker started",
                        {"sessions_wired": wired})
    except Exception as exc:
        logger.warning("Auto-reply worker failed to start: %s", exc)

    try:
        await asyncio.Event().wait()
    finally:
        ar_worker = container.get("autoreply_worker")
        if ar_worker is not None:
            try:
                await ar_worker.stop()
            except Exception:
                pass
        queue_mgr = container.get("queue_manager")
        if queue_mgr:
            await queue_mgr.stop()
        mgr = container.get("boot_manager")
        if mgr:
            await mgr.stop_reconciler()
        await bot.stop()
        await container["pool"].stop_all()
        # Release any lease still held (the polls/watchdog are stopped by now)
        # before dropping the connection: an unreleased lease would keep every
        # other replica waiting for its TTL.
        coord = container.get("lease_coordinator")
        if coord is not None:
            await coord.close()
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
