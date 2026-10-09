"""AutoTelegramForward Core — composition root.

Wires config -> persistence -> use cases -> adapters (bot, gRPC, dispatcher)
and runs the asyncio event loop.
"""

import asyncio
import logging
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
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("atf.core")


def build_container(cfg: Config) -> dict:
    crypto = CryptoService(cfg.master_key)
    db = SqliteDatabase(cfg.db_path)
    session_repo = SqliteSessionRepository(db, crypto)
    rule_repo = SqliteForwardRuleRepository(db)
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
    msg_map_repo = SqliteMessageMapRepository(db)
    queue_repo = SqliteDeliveryQueueRepository(db)
    prompt_repo = SqlitePromptRepository(db)
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
    rules = ForwardRuleUseCases(rule_repo)
    filter_rules = FilterRuleUseCases(filter_repo)
    ai_configs = AIConfigUseCases(ai_repo, factory)
    from core.application.use_cases import (  # noqa: E402
        ApiCredentialUseCases,
        BotTokenUseCases,
    )
    credentials = ApiCredentialUseCases(cred_repo)
    bot_tokens = BotTokenUseCases(token_repo)

    pipeline = MessageForwardingUseCase(
        rule_repo, filter_repo, ai_repo, factory,
        filter_engine=FilterEngine(), routing_policy=RoutingPolicy(),
        processed_repo=processed_repo,
    )
    pool = ClientPool(cfg.api_id, cfg.api_hash)
    sync_engine = SyncEngine(msg_map_repo, rule_repo, pool)
    queue_manager = DurableQueueManager(
        queue_repo=queue_repo,
        message_map_repo=msg_map_repo,
        client_pool=pool,
        num_workers=3,
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

    return {
        "config": cfg, "db": db, "crypto": crypto,
        "sessions": sessions, "rules": rules, "filters": filter_rules,
        "ai": ai_configs, "credentials": credentials, "bot_tokens": bot_tokens,
        "processed": processed_repo, "pipeline": pipeline, "pool": pool,
        "dispatcher": dispatcher, "i18n": i18n, "factory": factory,
        "logger_addr": cfg.logger_addr,
        "ui_state_repo": ui_state_repo, "auth_flow_repo": auth_flow_repo,
        "error_log": error_log_repo,
        "metrics": metrics_repo, "rule_stats": rule_stats_repo,
        "users": user_repo,
        "msg_map": msg_map_repo,
        "queue_repo": queue_repo,
        "queue_manager": queue_manager,
        "sync_engine": sync_engine,
        "durable_pipeline": durable_pipeline,
        "prompt_repo": prompt_repo,
        "prompt_service": prompt_service,
        "ai_transformer": ai_transformer,
        "ai_circuit_registry": ai_circuit_registry,
        "pv_responder": pv_responder,
    }


async def serve_grpc(container: dict, cfg: Config):
    import grpc
    from core.proto import pb_grpc

    server = grpc.aio.server()
    pb_grpc.add_SessionControlServiceServicer_to_server(
        servicers.SessionControlServicer(container["sessions"], container["pool"]), server
    )
    pb_grpc.add_ForwardRuleControlServiceServicer_to_server(
        servicers.ForwardRuleControlServicer(container["rules"]), server
    )
    pb_grpc.add_FilterControlServiceServicer_to_server(
        servicers.FilterControlServicer(container["filters"]), server
    )
    pb_grpc.add_AIControlServiceServicer_to_server(
        servicers.AIControlServicer(container["ai"]), server
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
            container["db"], container["rules"], container.get("pipeline"),
        ),
        server,
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
    if cfg.disable_bot:
        logger.info("ATF_DISABLE_BOT set: gRPC-only mode (no bot, no Telegram).")
        container = build_container(cfg)
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
    grpc_server = await serve_grpc(container, cfg)
    await seed_default_credential(container, cfg)
    await sync_pool_credentials(container)
    await container["prompt_service"].init_defaults()

    # Non-blocking debug-log feed to the Logger microservice.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from logger.log_client import LogClient  # noqa: E402
    log_client = LogClient(cfg.logger_addr)
    try:
        await log_client.start()
    except Exception as exc:
        logger.warning("Logger service unreachable at %s: %s", cfg.logger_addr, exc)
    container["log_client"] = log_client
    log_client.info("core", "system", "core started", {"version": cfg.version})

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
    )
    await bot.start()

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
            credentials=container.get("credentials"),
            bot_tokens=container.get("bot_tokens"),
            dispatcher=container.get("dispatcher"),
            boot_manager=container.get("boot_manager"),
            ui_state_repo=container.get("ui_state_repo"),
            error_log=container.get("error_log"),
            metrics=container.get("metrics"),
            rule_stats=container.get("rule_stats"),
            users=container.get("users"),
            pv_responder=container.get("pv_responder"),
        )
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
