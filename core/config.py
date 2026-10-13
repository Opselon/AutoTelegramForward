"""Configuration loading (env > config.yaml defaults)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
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
                    "database_url", "channel_poll_interval"):
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
            # v15 channel polling
            "ATF_CHANNEL_POLL_INTERVAL": ("channel_poll_interval", float),
        }
        for env_key, (attr, cast) in env_map.items():
            val = os.environ.get(env_key)
            if val:
                setattr(cfg, attr, cast(val))
        admins = os.environ.get("ATF_ADMIN_IDS")
        if admins:
            cfg.admin_ids = [int(x) for x in admins.split(",") if x.strip().isdigit()]
        cfg.disable_bot = os.environ.get("ATF_DISABLE_BOT", "").lower() in ("1", "true", "yes")
        return cfg
