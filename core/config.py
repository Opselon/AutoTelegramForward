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
    version: str = "1.0.0"
    disable_bot: bool = False

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
                    "grpc_host", "grpc_port", "language", "version"):
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
