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
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService  # noqa: E402
from core.infrastructure.telegram.bot_manager import BotManager  # noqa: E402
from core.infrastructure.telegram.client_pool import ClientPool  # noqa: E402
from core.infrastructure.telegram.dispatcher import MessageDispatcher  # noqa: E402
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
    factory = AIProviderFactory()

    sessions = SessionUseCases(session_repo)
    rules = ForwardRuleUseCases(rule_repo)
    filter_rules = FilterRuleUseCases(filter_repo)
    ai_configs = AIConfigUseCases(ai_repo, factory)

    pipeline = MessageForwardingUseCase(
        rule_repo, filter_repo, ai_repo, factory,
        filter_engine=FilterEngine(), routing_policy=RoutingPolicy(),
    )
    pool = ClientPool(cfg.api_id, cfg.api_hash)
    dispatcher = MessageDispatcher(pool, pipeline)
    i18n = I18n()
    i18n.set_language(cfg.language)

    return {
        "config": cfg, "db": db, "crypto": crypto,
        "sessions": sessions, "rules": rules, "filters": filter_rules,
        "ai": ai_configs, "pipeline": pipeline, "pool": pool,
        "dispatcher": dispatcher, "i18n": i18n, "factory": factory,
        "logger_addr": cfg.logger_addr,
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
    addr = f"{cfg.grpc_host}:{cfg.grpc_port}"
    server.add_insecure_port(addr)
    await server.start()
    logger.info("gRPC server listening on %s", addr)
    return server


async def main() -> None:
    cfg = Config.load()
    if cfg.disable_bot:
        logger.info("ATF_DISABLE_BOT set: gRPC-only mode (no bot, no Telegram).")
        container = build_container(cfg)
        grpc_server = await serve_grpc(container, cfg)
        try:
            await asyncio.Event().wait()
        finally:
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

    bot = BotManager(
        bot_token=cfg.bot_token, admin_ids=cfg.admin_ids,
        pool=container["pool"], sessions=container["sessions"],
        rules=container["rules"], filter_rules=container["filters"],
        ai_configs=container["ai"], pipeline=container["pipeline"],
        i18n=container["i18n"], crypto=container["crypto"],
    )
    await bot.start()
    logger.info("Bot started. Core is running. Press Ctrl+C to stop.")

    # Mount the Pro button UI directly onto the live bot client.
    try:
        from core.infrastructure.telegram.login_flow import LoginFlowManager  # noqa: E402
        ProBotUI(
            bot=bot.bot, i18n=container["i18n"],
            sessions=container["sessions"], rules=container["rules"],
            filter_rules=container["filters"], ai_configs=container["ai"],
            pipeline=container["pipeline"], pool=container["pool"],
            login=LoginFlowManager(container["pool"], container["sessions"]),
            log_client=log_client, admin_ids=cfg.admin_ids,
        )
        log_client.info("core", "system", "pro button UI mounted")
    except Exception as exc:
        logger.warning("ProBotUI mount failed: %s", exc)

    try:
        await asyncio.Event().wait()
    finally:
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
