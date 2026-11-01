"""Telegram Runtime entrypoint.

Runs the runtime as a narrow, independently understandable process:

    python -m telegram_runtime.main --addr 0.0.0.0:50053

In production the Core starts it inside its own process (see
``core/main.py``'s runtime wiring), so a deployment does not gain a new
service to operate during the migration. Running it standalone is for
development and integration tests.

Security: a missing/empty RPC token is a hard refusal. Unauthenticated
access to these RPCs would let any caller on the network drive Telegram
sessions — the runtime must never come up with an open internal surface.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logger = logging.getLogger("atf.telegram_runtime.main")


def _resolve_pool():
    """Locate the Core's ClientPool when it is available.

    The runtime is a *specialized adapter over* the Core's session
    infrastructure, not a second owner of it: reusing the pool keeps one
    authoritative home for session state.
    """
    # The in-process starter (the Core) wires the real pool in directly; the
    # standalone path bootstraps one itself (see pool_bootstrap).
    return None


async def main_async(args: argparse.Namespace) -> int:
    token = os.environ.get("ATF_CORE_RPC_TOKEN", "").strip()
    if not token:
        # Fail closed: never serve an unauthenticated internal RPC surface.
        print("ATF_CORE_RPC_TOKEN is empty — refusing to start the Telegram Runtime", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # The session pool: the Core's ClientPool when the runtime runs inside
    # the Core process, otherwise a minimal bootstrap that starts the
    # configured sessions from the session store.
    pool = _resolve_pool()
    if pool is None:
        from telegram_runtime.app.pool_bootstrap import bootstrap_pool
        pool = await bootstrap_pool(os.environ.get("ATF_DB_PATH", "data/atf.db"))

    from telegram_runtime.app.session_manager import SessionManager
    from telegram_runtime.grpc.server import serve

    media_dir = os.environ.get("ATF_MEDIA_DIR", "/tmp/atf-media")
    os.makedirs(media_dir, exist_ok=True)

    sessions = SessionManager(pool)
    # The Bot Api adapter resolves stored bot tokens through the Core's
    # owner-scoped repository. Tokens (and their decryption) stay inside this
    # process: the Go Worker only ever names a token by its stored id.
    bots = _resolve_bot_adapter(os.environ.get("ATF_DB_PATH", "data/atf.db"))
    server = await serve(sessions, args.addr, token, media_dir, bots=bots)
    logger.info("telegram runtime ready on %s", args.addr)
    await server.wait_for_termination()
    if bots is not None:
        await bots.aclose()
    return 0


def _resolve_bot_adapter(db_path: str):
    """Build the Bot Api adapter over the Core's owner-scoped token store.

    The standalone runtime reads the same shared atf.db the Core owns, so a
    token resolved here is the token the owner attached — with the owner in
    the SELECT's WHERE clause, exactly as the Core's own path does it. When
    the store cannot be opened the runtime still serves sessions and answers
    bot RPCs with a typed refusal: a missing capability is reported, never
    silently downgraded.
    """
    if not db_path or not os.path.exists(db_path):
        logger.info("no shared store at %s — bot transport unavailable (typed refusal)", db_path)
        return None
    try:
        from telegram_runtime.app.bot_store import standalone_bot_repo
        repo = standalone_bot_repo(db_path)
    except Exception as exc:  # a failed wiring must not kill the runtime
        logger.warning("bot token store unavailable (%s) — bot transport answers a typed refusal", exc)
        return None
    from telegram_runtime.adapters.bot_api import BotApiAdapter
    return BotApiAdapter(repo)


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoTelegramForward Telegram Runtime")
    parser.add_argument("--addr", default=os.environ.get("ATF_RUNTIME_ADDR", "0.0.0.0:50053"))
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
