"""Configuration loading (env > config.yaml defaults)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _redis_url_from_addr(value: str) -> str:
    """Accept the compose form (``redis:6379``) as well as a full URL.

    The API and the Core must read the same variable — an operator sets one
    address for the deployment, not one per service — so the Core accepts the
    bare ``host:port`` the compose file passes and turns it into the URL its
    client wants. A value that already carries a scheme is left alone.
    """
    raw = (value or "").strip()
    if not raw or "://" in raw:
        return raw
    return "redis://" + raw


@dataclass
class Config:
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    admin_user_id: int = 0          # immutable numeric ID of the primary owner administrator
    admin_ids: list = field(default_factory=list)
    master_key: str = "change-me"
    db_path: str = "data/atf.db"
    grpc_host: str = "0.0.0.0"
    grpc_port: int = 50051
    language: str = "en"
    version: str = "1.2.2"
    disable_bot: bool = False
    logger_addr: str = "localhost:50052"
    web_url: str = ""               # public dashboard base URL, e.g. https://sub.legoten.com:8444
    jwt_secret: str = ""            # optional shared JWT secret (defaults to derived key)
    # v14 queue scaling knobs (env > yaml > defaults).
    queue_num_workers: int = 3
    queue_batch_size: int = 8
    queue_max_size: int = 10000
    queue_lease_seconds: float = 30.0
    queue_max_per_minute_per_target: int = 25
    # v15 channel polling: how often to sweep source channels for posts that
    # Telegram did not push (it stops pushing once a channel's read-state
    # catches up, so polling is the reliable delivery path).
    channel_poll_interval: float = 15.0
    # v14 PostgreSQL (optional). When database_url is set the queue uses the
    # Postgres repositories (FOR UPDATE SKIP LOCKED); otherwise SQLite.
    database_url: str = ""
    postgres_all: bool = False
    # v16 Redis (optional). The API uses the same address for its rate limiter;
    # the Core uses it to coordinate its singleton loops across replicas (see
    # core/infrastructure/redis). Empty = single-replica mode, which is what
    # every deployment was before this existed.
    redis_url: str = ""
    redis_password: str = ""
    redis_prefix: str = "atf:rl:"
    redis_coordination: bool = True
    redis_timeout_seconds: float = 1.0
    # Shared credential required on the core's internal gRPC surface (see
    # core/infrastructure/grpc_server/internal_auth.py). Empty = the surface is
    # unauthenticated and the core says so at startup: every servicer takes the
    # tenant from owner_user_id, so anything that can reach this port can act as
    # any tenant. Set the same value on the API (ATF_CORE_RPC_TOKEN) to enforce
    # it. Never logged.
    core_rpc_token: str = ""
    # Terminal per-user task retention (seconds). The worker deletes
    # SUCCEEDED/FAILED/CANCELLED task rows older than this in bounded batches;
    # PENDING/RETRY_WAIT/RUNNING rows are live work and are never swept. 0 (or
    # negative) keeps terminal tasks forever.
    task_retention_seconds: int = 60 * 60 * 24 * 30

    @classmethod
    def load(cls, config_path: str = None) -> "Config":
        cfg = cls()
        # 1. YAML file
        candidates = [
            config_path,
            os.environ.get("ATF_CONFIG"),
            "config.yaml",
            str(Path(__file__).resolve().parent.parent.parent / "config.yaml"),
        ]
        yaml_data = {}
        for path in candidates:
            if path and Path(path).exists():
                with open(path, encoding="utf-8") as fh:
                    yaml_data = yaml.safe_load(fh) or {}
                break
        for key in ("api_id", "api_hash", "bot_token", "master_key", "db_path",
                    "grpc_host", "grpc_port", "language", "version", "disable_bot",
                    "logger_addr", "web_url", "jwt_secret",
                    "queue_num_workers", "queue_batch_size", "queue_max_size",
                    "queue_lease_seconds", "queue_max_per_minute_per_target",
                    "database_url", "channel_poll_interval",
                    "redis_url", "redis_password", "redis_prefix",
                    "redis_coordination", "redis_timeout_seconds",
                    "core_rpc_token", "task_retention_seconds"):
            if key in yaml_data:
                setattr(cfg, key, yaml_data[key])
        if "admin_ids" in yaml_data:
            cfg.admin_ids = list(yaml_data["admin_ids"])
        # 2. Environment overrides
        env_map = {
            "ATF_API_ID": ("api_id", int),
            "ATF_API_HASH": ("api_hash", str),
            "ATF_BOT_TOKEN": ("bot_token", str),
            "ATF_MASTER_KEY": ("master_key", str),
            "ATF_DB_PATH": ("db_path", str),
            "ATF_GRPC_HOST": ("grpc_host", str),
            "ATF_GRPC_PORT": ("grpc_port", int),
            "ATF_LANGUAGE": ("language", str),
            "ATF_LOGGER_ADDR": ("logger_addr", str),
            "ATF_WEB_URL": ("web_url", str),
            "ATF_JWT_SECRET": ("jwt_secret", str),
            # v14 queue scaling
            "ATF_QUEUE_NUM_WORKERS": ("queue_num_workers", int),
            "ATF_QUEUE_BATCH_SIZE": ("queue_batch_size", int),
            "ATF_QUEUE_MAX_SIZE": ("queue_max_size", int),
            "ATF_QUEUE_LEASE_SECONDS": ("queue_lease_seconds", float),
            "ATF_QUEUE_MAX_PER_MINUTE": ("queue_max_per_minute_per_target", int),
            "ATF_DATABASE_URL": ("database_url", str),
            "ATF_POSTGRES_ALL": ("postgres_all", lambda v: str(v).lower() in ("1", "true", "yes")),
            # v15 channel polling
            "ATF_CHANNEL_POLL_INTERVAL": ("channel_poll_interval", float),
            # v16 Redis coordination (the API's limiter reads the same names).
            # The plain address first, the explicit URL last: a deployment that
            # sets both gets the full URL it wrote literally.
            "ATF_CORE_RPC_TOKEN": ("core_rpc_token", str),
            "ATF_TASK_RETENTION_SECONDS": ("task_retention_seconds", int),
            "ATF_REDIS_ADDR": ("redis_url", _redis_url_from_addr),
            "ATF_REDIS_URL": ("redis_url", str),
            "ATF_RATELIMIT_PREFIX": ("redis_prefix", str),
            "ATF_REDIS_COORDINATION": (
                "redis_coordination",
                lambda v: str(v).lower() in ("1", "true", "yes"),
            ),
            "ATF_REDIS_TIMEOUT_MS": ("redis_timeout_seconds", lambda v: float(v) / 1000.0),
        }
        for env_key, (attr, cast) in env_map.items():
            val = os.environ.get(env_key)
            if val:
                setattr(cfg, attr, cast(val))
        # The credential never rides inside the address string this Core logs
        # about: it lives in its own field and the client receives it as a
        # client option, exactly the way the Go layer does. An operator URL
        # that carries credentials is decomposed here — nothing keeps a
        # password-bearing URL in memory at all.
        if "@" in cfg.redis_url and "://" in cfg.redis_url:
            scheme, rest = cfg.redis_url.split("://", 1)
            userinfo, _, hostport = rest.partition("@")
            cfg.redis_url = f"{scheme}://{hostport}"
            cfg.redis_password = userinfo.partition(":")[2] or cfg.redis_password
        else:
            env_password = os.environ.get("ATF_REDIS_PASSWORD", "").strip()
            if env_password:
                cfg.redis_password = env_password
        admins = os.environ.get("ATF_ADMIN_IDS") or os.environ.get("ADMIN_IDS")
        if admins:
            parsed_admins = []
            for part in admins.split(","):
                part = part.strip()
                if part.isdigit():
                    parsed_admins.append(int(part))
            if parsed_admins:
                cfg.admin_ids = parsed_admins

        owner_admin_env = os.environ.get("ADMIN_USER_ID") or os.environ.get("ATF_ADMIN_USER_ID")
        if owner_admin_env:
            cleaned = owner_admin_env.strip()
            if cleaned.isdigit() and int(cleaned) > 0:
                cfg.admin_user_id = int(cleaned)
                if cfg.admin_user_id not in cfg.admin_ids:
                    cfg.admin_ids.insert(0, cfg.admin_user_id)
            else:
                raise ValueError(
                    f"ADMIN_USER_ID must be a positive integer Telegram user ID, got {owner_admin_env!r}"
                )

        # Standard Telegram Bot Token aliases
        bot_token_env = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
        if bot_token_env and not cfg.bot_token:
            cfg.bot_token = bot_token_env.strip()

        cfg.disable_bot = os.environ.get("ATF_DISABLE_BOT", "").lower() in ("1", "true", "yes")
        return cfg

    def validate(self) -> None:
        """Validate startup configuration; fail fast on essential missing values."""
        if not self.disable_bot:
            if not self.bot_token:
                raise ValueError(
                    "TELEGRAM_BOT_TOKEN (or ATF_BOT_TOKEN) is required when bot is enabled. "
                    "Configure it in your .env or environment before starting."
                )
            token = self.bot_token.strip()
            if ":" not in token or not token.split(":", 1)[0].isdigit():
                raise ValueError(
                    "Invalid TELEGRAM_BOT_TOKEN format. Expected '<bot_id>:<secret>' issued by @BotFather."
                )
        if self.admin_user_id < 0:
            raise ValueError("ADMIN_USER_ID must be a positive integer Telegram numeric ID.")
