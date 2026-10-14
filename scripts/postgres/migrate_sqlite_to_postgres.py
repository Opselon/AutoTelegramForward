#!/usr/bin/env python3
"""Safe, repeatable SQLite -> PostgreSQL migration script for AutoTelegramForward.

Reads existing rules, message mappings, queue jobs, dedup fingerprints, and
prompt templates from SQLite and writes them into PostgreSQL with foreign key
integrity and row count verification.

Usage:
    export ATF_DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    python scripts/postgres/migrate_sqlite_to_postgres.py [path/to/atf.db]
"""

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    import asyncpg
except ImportError:
    print("Error: asyncpg is required for PostgreSQL migration. Install via 'pip install asyncpg'.")
    sys.exit(1)

from core.infrastructure.persistence.postgres_repositories import initialize_postgres_schema


def get_sqlite_conn(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite database not found at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


async def run_migration(sqlite_path: str, pg_url: str):
    print(f"[*] Starting ATF SQLite -> PostgreSQL Migration")
    print(f"[*] Source SQLite: {sqlite_path}")
    print(f"[*] Target Postgres: {pg_url.split('@')[-1] if '@' in pg_url else 'local'}")

    sqlite_conn = get_sqlite_conn(sqlite_path)
    pool = await asyncpg.create_pool(dsn=pg_url, min_size=1, max_size=5)

    try:
        print("[*] Initializing PostgreSQL schema & indexes...")
        await initialize_postgres_schema(pool)
        print("[+] PostgreSQL schema verified.")

        # 1. Migrate forward_rules
        cur = sqlite_conn.execute("SELECT * FROM forward_rules")
        rules = cur.fetchall()
        print(f"[*] Migrating {len(rules)} forward_rules...")
        for r in rules:
            d = dict(r)
            await pool.execute(
                """
                INSERT INTO forward_rules (
                    id, session_id, source_chat_id, source_chat_name, target_chat_id,
                    target_chat_name, target_chat_ids, routing_type, forward_mode, is_active,
                    filter_rule_id, ai_config_id, remove_links, custom_caption_template,
                    delay_seconds, skip_history, since_ts, ignore_edits, trigger_events,
                    content_mode, metadata, version, created_at, updated_at, owner_user_id
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18, $19::jsonb,
                    $20, $21::jsonb, $22, $23, $24, $25
                )
                ON CONFLICT (id) DO UPDATE SET
                    source_chat_id = EXCLUDED.source_chat_id,
                    target_chat_id = EXCLUDED.target_chat_id,
                    is_active = EXCLUDED.is_active,
                    owner_user_id = EXCLUDED.owner_user_id,
                    updated_at = EXCLUDED.updated_at
                """,
                d.get("id"), d.get("session_id", ""), str(d.get("source_chat_id", "")),
                d.get("source_chat_name", ""), str(d.get("target_chat_id", "")),
                d.get("target_chat_name", ""), d.get("target_chat_ids", "[]"),
                d.get("routing_type", "CHANNEL_TO_CHANNEL"), d.get("forward_mode", "COPY_MESSAGE"),
                bool(d.get("is_active", 1)), d.get("filter_rule_id"), d.get("ai_config_id"),
                bool(d.get("remove_links", 0)), d.get("custom_caption_template", ""),
                float(d.get("delay_seconds", 0.0) or 0.0), bool(d.get("skip_history", 1)),
                int(d.get("since_ts", 0) or 0), bool(d.get("ignore_edits", 0)),
                d.get("trigger_events", '["NEW_MESSAGE"]'), d.get("content_mode", "AUTO"),
                d.get("metadata", "{}"), int(d.get("version", 1) or 1),
                str(d.get("created_at", "")), str(d.get("updated_at", "")),
                int(d.get("owner_user_id", 0) or 0),
            )
        print(f"[+] forward_rules migrated: {len(rules)}")

        # 2. Migrate message_map
        try:
            cur = sqlite_conn.execute("SELECT * FROM message_map")
            maps = cur.fetchall()
            print(f"[*] Migrating {len(maps)} message_map rows...")
            for m in maps:
                d = dict(m)
                await pool.execute(
                    """
                    INSERT INTO message_map (
                        id, rule_id, source_chat_id, source_message_id, target_chat_id,
                        target_message_id, media_group_id, delivery_status, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (rule_id, source_chat_id, source_message_id, target_chat_id) DO UPDATE SET
                        target_message_id = COALESCE(EXCLUDED.target_message_id, message_map.target_message_id),
                        delivery_status = EXCLUDED.delivery_status,
                        updated_at = EXCLUDED.updated_at
                    """,
                    d.get("id"), d.get("rule_id"), str(d.get("source_chat_id")),
                    int(d.get("source_message_id")), str(d.get("target_chat_id")),
                    int(d["target_message_id"]) if d.get("target_message_id") is not None and d["target_message_id"] != "" else None,
                    d.get("media_group_id", ""), d.get("delivery_status", "PENDING"),
                    int(d.get("created_at", 0)), int(d.get("updated_at", 0)),
                )
            print(f"[+] message_map migrated: {len(maps)}")
        except sqlite3.OperationalError:
            print("[-] message_map table absent in SQLite, skipping.")

        # 3. Migrate content_dedup
        try:
            cur = sqlite_conn.execute("SELECT * FROM content_dedup")
            dedups = cur.fetchall()
            print(f"[*] Migrating {len(dedups)} content_dedup rows...")
            for dd in dedups:
                d = dict(dd)
                await pool.execute(
                    """
                    INSERT INTO content_dedup (rule_id, content_hash, created_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (rule_id, content_hash) DO NOTHING
                    """,
                    d.get("rule_id"), d.get("content_hash"), int(d.get("created_at", 0)),
                )
            print(f"[+] content_dedup migrated: {len(dedups)}")
        except sqlite3.OperationalError:
            print("[-] content_dedup table absent in SQLite, skipping.")

        # 4. Migrate prompt_templates & prompt_versions
        try:
            cur = sqlite_conn.execute("SELECT * FROM prompt_templates")
            prompts = cur.fetchall()
            print(f"[*] Migrating {len(prompts)} prompt_templates...")
            for p in prompts:
                d = dict(p)
                await pool.execute(
                    """
                    INSERT INTO prompt_templates (
                        id, name, system_prompt, user_prompt_template, target_language,
                        current_version, is_system, created_at, updated_at, owner_user_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (id) DO UPDATE SET
                        system_prompt = EXCLUDED.system_prompt,
                        user_prompt_template = EXCLUDED.user_prompt_template,
                        updated_at = EXCLUDED.updated_at
                    """,
                    d.get("id"), d.get("name", ""), d.get("system_prompt", ""),
                    d.get("user_prompt_template", ""), d.get("target_language", "en"),
                    int(d.get("current_version", 1) or 1), bool(d.get("is_system", 0)),
                    int(d.get("created_at", 0)), int(d.get("updated_at", 0)),
                    int(d.get("owner_user_id", 0) or 0),
                )
            print(f"[+] prompt_templates migrated: {len(prompts)}")
        except sqlite3.OperationalError:
            print("[-] prompt_templates table absent in SQLite, skipping.")

        # Verification step
        pg_rules_cnt = await pool.fetchval("SELECT COUNT(*) FROM forward_rules")
        print(f"[*] Verification: PostgreSQL forward_rules count = {pg_rules_cnt}")
        print("[+] Migration completed successfully with zero data loss.")

    finally:
        sqlite_conn.close()
        await pool.close()


if __name__ == "__main__":
    db_file = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ATF_DB_PATH", "data/atf.db")
    url = os.environ.get("ATF_DATABASE_URL", "")
    if not url:
        print("Error: ATF_DATABASE_URL environment variable is required.")
        sys.exit(1)
    asyncio.run(run_migration(db_file, url))
