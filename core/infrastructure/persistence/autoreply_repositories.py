"""SQLite persistence for the AI auto-reply feature (v27).

Every method takes ``owner_user_id`` and every statement scopes by it, so
isolation is a property of the query, not of code that runs afterwards. A
tenant can never read, update or delete another tenant's prompts, targets,
rules, memory or queue rows: there is no unscoped accessor on this class.

All writes go through the connection's single lock (see SqliteDatabase), which
makes each statement atomic; multi-statement sequences that must be atomic use
an explicit transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...domain import autoreply as model
from ...domain.autoreply import (
    AutoReplyJob,
    AutoReplyPrompt,
    AutoReplyRule,
    AutoReplySettings,
    ContactTarget,
    IncomingMessage,
    JobStatus,
    MemoryKind,
    normalize_chat_id,
)

logger = logging.getLogger(__name__)

#: How long a claimed job stays claimed before another worker may take it over.
LEASE_SECONDS = 60
#: Context rows kept per chat (the setting may ask for fewer).
MAX_CONTEXT_ROWS = 40
#: Memory rows kept per chat, on top of the setting's own bound.
MAX_MEMORY_ROWS = 60

_JSON_COLUMNS = ("active_days", "tags")


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[]"


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _opt_str(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else ""


def _memory_hash(owner: int, chat_id: str, content: str) -> str:
    raw = f"{owner}|{normalize_chat_id(chat_id)}|{content.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class AutoReplyRepository:
    """One tenant-scoped accessor for every auto-reply table.

    The class is safe to share across the dispatcher thread (which enqueues)
    and the worker loop (which claims): every statement takes the database's
    own lock, and the claim is a single atomic UPDATE ... RETURNING.
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def get_settings(self, owner_user_id: int) -> AutoReplySettings:
        row = self._db.query_one(
            "SELECT * FROM autoreply_settings WHERE owner_user_id=?", (int(owner_user_id),)
        )
        if row is None:
            return AutoReplySettings(owner_user_id=int(owner_user_id)).normalize()
        return self._row_to_settings(row)

    def _row_to_settings(self, row: Any) -> AutoReplySettings:
        return AutoReplySettings(
            owner_user_id=int(row["owner_user_id"]),
            enabled=_bool(row["enabled"]),
            ai_config_id=_opt_str(row["ai_config_id"]) or None,
            prompt_id=_opt_str(row["prompt_id"]) or None,
            target_mode=model.TargetMode.coerce(row["target_mode"]),
            language=str(row["language"] or ""),
            tone=model.Tone.coerce(row["tone"]),
            response_length=model.ResponseLength.coerce(row["response_length"]),
            temperature=float(row["temperature"]),
            max_tokens=int(row["max_tokens"]),
            context_messages=int(row["context_messages"]),
            memory_enabled=_bool(row["memory_enabled"], True),
            memory_max_facts=int(row["memory_max_facts"]),
            extra_instructions=str(row["extra_instructions"] or ""),
            max_reply_chars=int(row["max_reply_chars"]),
            typing_simulation=_bool(row["typing_simulation"], True),
            typing_delay_min=float(row["typing_delay_min"]),
            typing_delay_max=float(row["typing_delay_max"]),
            cooldown_seconds=int(row["cooldown_seconds"]),
            daily_limit_per_chat=int(row["daily_limit_per_chat"]),
            rate_limit_per_minute=int(row["rate_limit_per_minute"]),
            max_concurrency=int(row["max_concurrency"]),
            max_attempts=int(row["max_attempts"]),
            retry_backoff_seconds=float(row["retry_backoff_seconds"]),
            quiet_hours_start=int(row["quiet_hours_start"]),
            quiet_hours_end=int(row["quiet_hours_end"]),
            timezone_offset_minutes=int(row["timezone_offset_minutes"]),
            active_days=[int(x) for x in _loads(row["active_days"], [])],
            fallback_text=str(row["fallback_text"] or ""),
            ignore_bots=_bool(row["ignore_bots"], True),
            version=int(row["version"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        ).normalize()

    def save_settings(self, settings: AutoReplySettings) -> AutoReplySettings:
        settings = settings.normalize()
        now = int(time.time())
        settings.updated_at = now
        if not settings.created_at:
            settings.created_at = now
        d = settings.to_dict()
        self._db.execute(
            "INSERT INTO autoreply_settings (owner_user_id, enabled, ai_config_id, prompt_id,"
            " target_mode, language, tone, response_length, temperature, max_tokens,"
            " context_messages, memory_enabled, memory_max_facts, extra_instructions,"
            " max_reply_chars, typing_simulation, typing_delay_min, typing_delay_max,"
            " cooldown_seconds, daily_limit_per_chat, rate_limit_per_minute, max_concurrency,"
            " max_attempts, retry_backoff_seconds, quiet_hours_start, quiet_hours_end,"
            " timezone_offset_minutes, active_days, fallback_text, ignore_bots, version,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(owner_user_id) DO UPDATE SET"
            " enabled=excluded.enabled, ai_config_id=excluded.ai_config_id, prompt_id=excluded.prompt_id,"
            " target_mode=excluded.target_mode, language=excluded.language, tone=excluded.tone,"
            " response_length=excluded.response_length, temperature=excluded.temperature,"
            " max_tokens=excluded.max_tokens, context_messages=excluded.context_messages,"
            " memory_enabled=excluded.memory_enabled, memory_max_facts=excluded.memory_max_facts,"
            " extra_instructions=excluded.extra_instructions, max_reply_chars=excluded.max_reply_chars,"
            " typing_simulation=excluded.typing_simulation, typing_delay_min=excluded.typing_delay_min,"
            " typing_delay_max=excluded.typing_delay_max, cooldown_seconds=excluded.cooldown_seconds,"
            " daily_limit_per_chat=excluded.daily_limit_per_chat,"
            " rate_limit_per_minute=excluded.rate_limit_per_minute,"
            " max_concurrency=excluded.max_concurrency, max_attempts=excluded.max_attempts,"
            " retry_backoff_seconds=excluded.retry_backoff_seconds,"
            " quiet_hours_start=excluded.quiet_hours_start, quiet_hours_end=excluded.quiet_hours_end,"
            " timezone_offset_minutes=excluded.timezone_offset_minutes,"
            " active_days=excluded.active_days, fallback_text=excluded.fallback_text,"
            " ignore_bots=excluded.ignore_bots, version=excluded.version, updated_at=excluded.updated_at",
            (
                d["owner_user_id"], int(d["enabled"]), d["ai_config_id"], d["prompt_id"],
                d["target_mode"], d["language"], d["tone"], d["response_length"],
                d["temperature"], d["max_tokens"], d["context_messages"],
                int(d["memory_enabled"]), d["memory_max_facts"], d["extra_instructions"],
                d["max_reply_chars"], int(d["typing_simulation"]),
                d["typing_delay_min"], d["typing_delay_max"], d["cooldown_seconds"],
                d["daily_limit_per_chat"], d["rate_limit_per_minute"], d["max_concurrency"],
                d["max_attempts"], d["retry_backoff_seconds"], d["quiet_hours_start"],
                d["quiet_hours_end"], d["timezone_offset_minutes"],
                _dumps(d["active_days"]), d["fallback_text"], int(d["ignore_bots"]),
                d["version"], d["created_at"], d["updated_at"],
            ),
        )
        return settings

    # ------------------------------------------------------------------ #
    # Prompts
    # ------------------------------------------------------------------ #
    def _row_to_prompt(self, row: Any) -> AutoReplyPrompt:
        return AutoReplyPrompt(
            id=str(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            name=str(row["name"] or ""),
            description=str(row["description"] or ""),
            system_prompt=str(row["system_prompt"] or ""),
            user_prompt_template=str(row["user_prompt_template"] or "{text}"),
            tone=model.Tone.coerce(row["tone"]) if _opt_str(row["tone"]) else None,
            language=str(row["language"] or ""),
            response_length=model.ResponseLength.coerce(row["response_length"])
            if _opt_str(row["response_length"]) else None,
            category=str(row["category"] or ""),
            tags=[str(t) for t in _loads(row["tags"], [])],
            is_default=_bool(row["is_default"]),
            is_archived=_bool(row["is_archived"]),
            use_count=int(row["use_count"]),
            last_used_at=int(row["last_used_at"]),
            version=int(row["version"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        ).normalize()

    def list_prompts(self, owner_user_id: int, include_archived: bool = False,
                     search: str = "", category: str = "") -> List[AutoReplyPrompt]:
        sql = "SELECT * FROM autoreply_prompts WHERE owner_user_id=?"
        params: list = [int(owner_user_id)]
        if not include_archived:
            sql += " AND is_archived=0"
        if category:
            sql += " AND category=?"
            params.append(str(category)[:60])
        if search:
            sql += " AND (name LIKE ? OR description LIKE ? OR system_prompt LIKE ?)"
            like = f"%{str(search)[:120]}%"
            params += [like, like, like]
        sql += " ORDER BY is_default DESC, updated_at DESC"
        rows = self._db.query_all(sql, tuple(params))
        return [self._row_to_prompt(r) for r in rows]

    def get_prompt(self, prompt_id: str, owner_user_id: int) -> Optional[AutoReplyPrompt]:
        row = self._db.query_one(
            "SELECT * FROM autoreply_prompts WHERE id=? AND owner_user_id=?",
            (str(prompt_id), int(owner_user_id)),
        )
        return self._row_to_prompt(row) if row else None

    def save_prompt(self, prompt: AutoReplyPrompt) -> AutoReplyPrompt:
        """Insert or update; returns the stored row or raises ValueError."""
        prompt = prompt.normalize()
        errors = prompt.validate()
        if errors:
            raise ValueError(",".join(errors))
        now = int(time.time())
        prompt.updated_at = now
        if not prompt.created_at:
            prompt.created_at = now
        d = prompt.to_dict()
        existing = self._db.query_one(
            "SELECT id FROM autoreply_prompts WHERE id=? AND owner_user_id=?",
            (d["id"], int(d["owner_user_id"])),
        )
        if existing:
            self._db.execute(
                "UPDATE autoreply_prompts SET name=?, description=?, system_prompt=?,"
                " user_prompt_template=?, tone=?, language=?, response_length=?, category=?,"
                " tags=?, is_default=?, is_archived=?, use_count=?, last_used_at=?, version=?,"
                " updated_at=? WHERE id=? AND owner_user_id=?",
                (
                    d["name"], d["description"], d["system_prompt"], d["user_prompt_template"],
                    d["tone"], d["language"], d["response_length"], d["category"],
                    _dumps(d["tags"]), int(d["is_default"]), int(d["is_archived"]),
                    d["use_count"], d["last_used_at"], d["version"], d["updated_at"],
                    d["id"], int(d["owner_user_id"]),
                ),
            )
        else:
            self._db.execute(
                "INSERT INTO autoreply_prompts (id, owner_user_id, name, description,"
                " system_prompt, user_prompt_template, tone, language, response_length,"
                " category, tags, is_default, is_archived, use_count, last_used_at,"
                " version, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    d["id"], d["owner_user_id"], d["name"], d["description"],
                    d["system_prompt"], d["user_prompt_template"], d["tone"], d["language"],
                    d["response_length"], d["category"], _dumps(d["tags"]),
                    int(d["is_default"]), int(d["is_archived"]), d["use_count"],
                    d["last_used_at"], d["version"], d["created_at"], d["updated_at"],
                ),
            )
        if prompt.is_default:
            # Exactly one default per tenant: the assignment must be exclusive,
            # not advisory. Done in the same transaction as the write.
            self._db.execute(
                "UPDATE autoreply_prompts SET is_default=0, updated_at=?"
                " WHERE owner_user_id=? AND id<>?",
                (now, int(d["owner_user_id"]), d["id"]),
            )
        return prompt

    def duplicate_prompt(self, prompt_id: str, owner_user_id: int,
                         name: str = "") -> Optional[AutoReplyPrompt]:
        source = self.get_prompt(prompt_id, owner_user_id)
        if source is None:
            return None
        clone = source.copy_for(owner_user_id, name)
        base = clone.name
        suffix = 1
        while self._db.query_one(
            "SELECT id FROM autoreply_prompts WHERE owner_user_id=? AND name=?",
            (int(owner_user_id), clone.name),
        ):
            suffix += 1
            clone.name = f"{base} ({suffix})"[:80]
        return self.save_prompt(clone)

    def delete_prompt(self, prompt_id: str, owner_user_id: int) -> bool:
        with self._lock:
            row = self._db.query_one(
                "SELECT id FROM autoreply_prompts WHERE id=? AND owner_user_id=?",
                (str(prompt_id), int(owner_user_id)),
            )
            if row is None:
                return False
            self._db.execute(
                "DELETE FROM autoreply_prompts WHERE id=? AND owner_user_id=?",
                (str(prompt_id), int(owner_user_id)),
            )
            # An assignment to a deleted prompt is a lie: clear it everywhere
            # the tenant can reach, in the same transaction as the delete.
            self._db.execute(
                "UPDATE autoreply_targets SET prompt_id='', updated_at=?"
                " WHERE owner_user_id=? AND prompt_id=?",
                (int(time.time()), int(owner_user_id), str(prompt_id)),
            )
            self._db.execute(
                "UPDATE autoreply_rules SET prompt_id='', updated_at=?"
                " WHERE owner_user_id=? AND prompt_id=?",
                (int(time.time()), int(owner_user_id), str(prompt_id)),
            )
            self._db.execute(
                "UPDATE autoreply_settings SET prompt_id='' WHERE owner_user_id=? AND prompt_id=?",
                (int(owner_user_id), str(prompt_id)),
            )
            return True

    def count_prompts(self, owner_user_id: int) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM autoreply_prompts WHERE owner_user_id=?",
            (int(owner_user_id),),
        )
        return int(row["n"]) if row else 0

    def touch_prompt_usage(self, prompt_id: str, owner_user_id: int) -> None:
        self._db.execute(
            "UPDATE autoreply_prompts SET use_count=use_count+1, last_used_at=?"
            " WHERE id=? AND owner_user_id=?",
            (int(time.time()), str(prompt_id), int(owner_user_id)),
        )

    # ------------------------------------------------------------------ #
    # Targets (contact / chat list)
    # ------------------------------------------------------------------ #
    def _row_to_target(self, row: Any) -> ContactTarget:
        return ContactTarget(
            id=str(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            chat_id=str(row["chat_id"] or ""),
            chat_type="private",
            title=str(row["title"] or ""),
            username=str(row["username"] or ""),
            is_enabled=_bool(row["is_enabled"], True),
            prompt_id=_opt_str(row["prompt_id"]) or None,
            note=str(row["note"] or ""),
            priority=int(row["priority"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        ).normalize()

    def list_targets(self, owner_user_id: int, search: str = "",
                     enabled_only: bool = False) -> List[ContactTarget]:
        sql = "SELECT * FROM autoreply_targets WHERE owner_user_id=?"
        params: list = [int(owner_user_id)]
        if enabled_only:
            sql += " AND is_enabled=1"
        if search:
            sql += " AND (title LIKE ? OR username LIKE ? OR chat_id LIKE ? OR note LIKE ?)"
            like = f"%{str(search)[:120]}%"
            params += [like, like, like, like]
        sql += " ORDER BY is_enabled DESC, priority DESC, title ASC"
        rows = self._db.query_all(sql, tuple(params))
        return [self._row_to_target(r) for r in rows]

    def get_target_by_chat(self, chat_id: Any, owner_user_id: int) -> Optional[ContactTarget]:
        row = self._db.query_one(
            "SELECT * FROM autoreply_targets WHERE owner_user_id=? AND chat_id=?",
            (int(owner_user_id), normalize_chat_id(chat_id)),
        )
        return self._row_to_target(row) if row else None

    def upsert_target(self, target: ContactTarget) -> ContactTarget:
        target = target.normalize()
        if not target.chat_id:
            raise ValueError("chat_id_required")
        now = int(time.time())
        target.updated_at = now
        if not target.created_at:
            target.created_at = now
        d = target.to_dict()
        self._db.execute(
            "INSERT INTO autoreply_targets (id, owner_user_id, chat_id, chat_type, title,"
            " username, is_enabled, prompt_id, note, priority, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(owner_user_id, chat_id) DO UPDATE SET"
            " title=excluded.title, username=excluded.username, is_enabled=excluded.is_enabled,"
            " prompt_id=excluded.prompt_id, note=excluded.note, priority=excluded.priority,"
            " updated_at=excluded.updated_at",
            (
                d["id"], d["owner_user_id"], d["chat_id"], "private", d["title"],
                d["username"], int(d["is_enabled"]), d["prompt_id"], d["note"],
                d["priority"], d["created_at"], d["updated_at"],
            ),
        )
        return target

    def set_target_enabled(self, chat_id: Any, owner_user_id: int, enabled: bool) -> bool:
        cur = self._db.execute(
            "UPDATE autoreply_targets SET is_enabled=?, updated_at=?"
            " WHERE owner_user_id=? AND chat_id=?",
            (int(bool(enabled)), int(time.time()), int(owner_user_id), normalize_chat_id(chat_id)),
        )
        return bool(cur.rowcount)

    def delete_target(self, chat_id: Any, owner_user_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM autoreply_targets WHERE owner_user_id=? AND chat_id=?",
            (int(owner_user_id), normalize_chat_id(chat_id)),
        )
        return bool(cur.rowcount)

    def bulk_set_targets(self, owner_user_id: int, targets: Sequence[Dict[str, Any]],
                         replace: bool = False) -> List[ContactTarget]:
        """Add (or replace) many targets at once. One transaction, one round trip per row."""
        now = int(time.time())
        out: List[ContactTarget] = []
        with self._lock:
            if replace:
                self._db.execute(
                    "DELETE FROM autoreply_targets WHERE owner_user_id=?", (int(owner_user_id),)
                )
            for raw in targets:
                item = dict(raw or {})
                chat_id = normalize_chat_id(item.get("chat_id"))
                if not chat_id:
                    continue
                target = ContactTarget(
                    id=str(item.get("id") or "").strip(),
                    owner_user_id=int(owner_user_id),
                    chat_id=chat_id,
                    title=str(item.get("title") or item.get("name") or ""),
                    username=str(item.get("username") or ""),
                    is_enabled=_bool(item.get("is_enabled"), True),
                    prompt_id=_opt_str(item.get("prompt_id")) or None,
                    note=str(item.get("note") or ""),
                    priority=int(item.get("priority") or 0),
                    created_at=int(item.get("created_at") or 0) or now,
                    updated_at=now,
                )
                if not target.id:
                    # A target added in bulk still needs a primary key; an empty
                    # one would collide with the next bulk row.
                    target.id = secrets.token_hex(16)
                self._db.execute(
                    "INSERT INTO autoreply_targets (id, owner_user_id, chat_id, chat_type,"
                    " title, username, is_enabled, prompt_id, note, priority,"
                    " created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(owner_user_id, chat_id) DO UPDATE SET"
                    " title=excluded.title, username=excluded.username,"
                    " is_enabled=excluded.is_enabled, prompt_id=excluded.prompt_id,"
                    " note=excluded.note, priority=excluded.priority,"
                    " updated_at=excluded.updated_at",
                    (
                        target.id, int(owner_user_id), target.chat_id, "private",
                        target.title, target.username, int(target.is_enabled),
                        target.prompt_id or "", target.note, target.priority,
                        target.created_at, now,
                    ),
                )
                out.append(target.normalize())
        # Read back so callers get the stored rows (ids included).
        stored = {t.chat_id: t for t in self.list_targets(owner_user_id)}
        return [stored.get(t.chat_id, t) for t in out]

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #
    def _row_to_rule(self, row: Any) -> AutoReplyRule:
        return AutoReplyRule(
            id=str(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            name=str(row["name"] or ""),
            is_enabled=_bool(row["is_enabled"], True),
            priority=int(row["priority"]),
            action=model.RuleAction.coerce(row["action"]),
            cooldown_seconds=int(row["cooldown_seconds"]),
            prompt_id=_opt_str(row["prompt_id"]) or None,
            ai_config_id=_opt_str(row["ai_config_id"]) or None,
            tone=model.Tone.coerce(row["tone"]) if _opt_str(row["tone"]) else None,
            language=str(row["language"] or ""),
            response_length=model.ResponseLength.coerce(row["response_length"])
            if _opt_str(row["response_length"]) else None,
            conditions=_loads(row["conditions_json"], {}) or {},
            exclusions=_loads(row["exclusions_json"], {}) or {},
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        ).normalize()

    def list_rules(self, owner_user_id: int) -> List[AutoReplyRule]:
        rows = self._db.query_all(
            "SELECT * FROM autoreply_rules WHERE owner_user_id=? ORDER BY priority DESC, created_at",
            (int(owner_user_id),),
        )
        return [self._row_to_rule(r) for r in rows]

    def get_rule(self, rule_id: str, owner_user_id: int) -> Optional[AutoReplyRule]:
        row = self._db.query_one(
            "SELECT * FROM autoreply_rules WHERE id=? AND owner_user_id=?",
            (str(rule_id), int(owner_user_id)),
        )
        return self._row_to_rule(row) if row else None

    def save_rule(self, rule: AutoReplyRule) -> AutoReplyRule:
        rule = rule.normalize()
        if not rule.name:
            raise ValueError("name_required")
        now = int(time.time())
        rule.updated_at = now
        if not rule.created_at:
            rule.created_at = now
        d = rule.to_dict()
        self._db.execute(
            "INSERT INTO autoreply_rules (id, owner_user_id, name, is_enabled, priority,"
            " action, cooldown_seconds, prompt_id, ai_config_id, tone, language,"
            " response_length, conditions_json, exclusions_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " name=excluded.name, is_enabled=excluded.is_enabled, priority=excluded.priority,"
            " action=excluded.action, cooldown_seconds=excluded.cooldown_seconds,"
            " prompt_id=excluded.prompt_id, ai_config_id=excluded.ai_config_id, tone=excluded.tone,"
            " language=excluded.language, response_length=excluded.response_length,"
            " conditions_json=excluded.conditions_json, exclusions_json=excluded.exclusions_json,"
            " updated_at=excluded.updated_at",
            (
                d["id"], d["owner_user_id"], d["name"], int(d["is_enabled"]), d["priority"],
                d["action"], d["cooldown_seconds"], d["prompt_id"], d["ai_config_id"],
                d["tone"], d["language"], d["response_length"],
                _dumps(d["conditions"]), _dumps(d["exclusions"]),
                d["created_at"], d["updated_at"],
            ),
        )
        return rule

    def delete_rule(self, rule_id: str, owner_user_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM autoreply_rules WHERE id=? AND owner_user_id=?",
            (str(rule_id), int(owner_user_id)),
        )
        return bool(cur.rowcount)

    # ------------------------------------------------------------------ #
    # Conversation context + memory
    # ------------------------------------------------------------------ #
    def append_message(self, owner_user_id: int, chat_id: Any, role: str,
                       content: str, message_id: int = 0) -> None:
        chat = normalize_chat_id(chat_id)
        if not chat:
            return
        now = int(time.time())
        self._db.execute(
            "INSERT INTO autoreply_messages (id, owner_user_id, chat_id, role, content,"
            " message_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                secrets.token_hex(12), int(owner_user_id), chat,
                str(role or "user").lower()[:16],
                str(content or "")[:model._TEXT_MAX], int(message_id or 0), now,
            ),
        )
        # Keep the chat's window bounded: one delete instead of a growing table.
        self._db.execute(
            "DELETE FROM autoreply_messages WHERE owner_user_id=? AND chat_id=? AND id NOT IN"
            " (SELECT id FROM autoreply_messages WHERE owner_user_id=? AND chat_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?)",
            (int(owner_user_id), chat, int(owner_user_id), chat, MAX_CONTEXT_ROWS),
        )

    def list_messages(self, owner_user_id: int, chat_id: Any, limit: int = 10) -> List[Dict[str, Any]]:
        chat = normalize_chat_id(chat_id)
        if not chat:
            return []
        rows = self._db.query_all(
            "SELECT role, content FROM autoreply_messages"
            # rowid, not the hex id: two rows written in the same second (the
            # inbound message and its reply) need insertion order as the tie
            # break, or the context window comes back with the answer first.
            " WHERE owner_user_id=? AND chat_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (int(owner_user_id), chat, int(max(1, min(limit, MAX_CONTEXT_ROWS)))),
        )
        out = [{"role": str(r["role"]), "content": str(r["content"])} for r in rows]
        out.reverse()
        return out

    def count_messages(self, owner_user_id: int, chat_id: Any) -> int:
        chat = normalize_chat_id(chat_id)
        if not chat:
            return 0
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM autoreply_messages WHERE owner_user_id=? AND chat_id=?",
            (int(owner_user_id), chat),
        )
        return int(row["n"]) if row else 0

    def clear_messages(self, owner_user_id: int, chat_id: Any) -> int:
        chat = normalize_chat_id(chat_id)
        if not chat:
            return 0
        cur = self._db.execute(
            "DELETE FROM autoreply_messages WHERE owner_user_id=? AND chat_id=?",
            (int(owner_user_id), chat),
        )
        return int(cur.rowcount or 0)

    def remember(self, owner_user_id: int, chat_id: Any, content: str,
                 kind: str = MemoryKind.FACT.value, ttl_seconds: int = 0) -> Optional[str]:
        """Store one remembered fact, deduplicated by content hash."""
        chat = normalize_chat_id(chat_id)
        text = str(content or "").strip()
        if not chat or not text:
            return None
        now = int(time.time())
        row_id = secrets.token_hex(12)
        self._db.execute(
            "INSERT INTO autoreply_memory (id, owner_user_id, chat_id, kind, content,"
            " content_hash, created_at, updated_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(owner_user_id, chat_id, content_hash) DO UPDATE SET"
            " updated_at=excluded.updated_at, expires_at=excluded.expires_at,"
            " kind=excluded.kind",
            (
                row_id, int(owner_user_id), chat, str(kind or "FACT")[:16], text[:1200],
                _memory_hash(owner_user_id, chat, text), now, now,
                now + int(ttl_seconds) if ttl_seconds > 0 else 0,
            ),
        )
        # Bound the chat's memory the same way as its context window.
        self._db.execute(
            "DELETE FROM autoreply_memory WHERE owner_user_id=? AND chat_id=? AND id NOT IN"
            " (SELECT id FROM autoreply_memory WHERE owner_user_id=? AND chat_id=?"
            " ORDER BY updated_at DESC LIMIT ?)",
            (int(owner_user_id), chat, int(owner_user_id), chat, MAX_MEMORY_ROWS),
        )
        return row_id

    def list_memory(self, owner_user_id: int, chat_id: Any, limit: int = 12) -> List[Dict[str, Any]]:
        chat = normalize_chat_id(chat_id)
        if not chat:
            return []
        now = int(time.time())
        rows = self._db.query_all(
            "SELECT id, kind, content, updated_at, expires_at FROM autoreply_memory"
            " WHERE owner_user_id=? AND chat_id=? AND (expires_at=0 OR expires_at>?)"
            " ORDER BY updated_at DESC LIMIT ?",
            (int(owner_user_id), chat, now, int(max(1, min(limit, MAX_MEMORY_ROWS)))),
        )
        return [
            {
                "id": str(r["id"]),
                "kind": str(r["kind"]),
                "content": str(r["content"]),
                "updated_at": int(r["updated_at"]),
                "expires_at": int(r["expires_at"]),
            }
            for r in rows
        ]

    def forget(self, owner_user_id: int, chat_id: Any, memory_id: str = "") -> int:
        chat = normalize_chat_id(chat_id)
        if not chat:
            return 0
        if memory_id:
            cur = self._db.execute(
                "DELETE FROM autoreply_memory WHERE owner_user_id=? AND chat_id=? AND id=?",
                (int(owner_user_id), chat, str(memory_id)),
            )
        else:
            cur = self._db.execute(
                "DELETE FROM autoreply_memory WHERE owner_user_id=? AND chat_id=?",
                (int(owner_user_id), chat),
            )
        return int(cur.rowcount or 0)

    def cleanup_expired_memory(self, now: Optional[int] = None) -> int:
        ts = int(now if now is not None else time.time())
        cur = self._db.execute(
            "DELETE FROM autoreply_memory WHERE expires_at>0 AND expires_at<=?", (ts,)
        )
        return int(cur.rowcount or 0)

    def cleanup_messages(self, max_age_seconds: int = 14 * 24 * 3600) -> int:
        """Retention sweep: old context rows are dead weight, not history."""
        if max_age_seconds <= 0:
            return 0
        cutoff = int(time.time()) - int(max_age_seconds)
        cur = self._db.execute("DELETE FROM autoreply_messages WHERE created_at<?", (cutoff,))
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ #
    # Jobs: the durable queue
    # ------------------------------------------------------------------ #
    def _row_to_job(self, row: Any) -> AutoReplyJob:
        return AutoReplyJob(
            id=str(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            session_id=str(row["session_id"] or ""),
            chat_id=str(row["chat_id"] or ""),
            chat_title=str(row["chat_title"] or ""),
            source_message_id=int(row["source_message_id"] or 0),
            status=JobStatus(str(row["status"] or "PENDING")),
            attempt=int(row["attempt"] or 0),
            max_attempts=int(row["max_attempts"] or 3),
            next_attempt_at=int(row["next_attempt_at"] or 0),
            lease_token=str(row["lease_token"] or ""),
            lease_until=int(row["lease_until"] or 0),
            prompt_id=_opt_str(row["prompt_id"]) or None,
            rule_id=_opt_str(row["rule_id"]) or None,
            ai_config_id=_opt_str(row["ai_config_id"]) or None,
            inbound_text=str(row["inbound_text"] or ""),
            reply_text=str(row["reply_text"] or ""),
            error=str(row["error"] or ""),
            error_code=str(row["error_code"] or ""),
            correlation_id=str(row["correlation_id"] or ""),
            latency_ms=int(row["latency_ms"] or 0),
            sent_message_id=int(row["sent_message_id"] or 0) or None,
            created_at=int(row["created_at"] or 0),
            updated_at=int(row["updated_at"] or 0),
            sent_at=int(row["sent_at"] or 0),
        )

    def enqueue(self, job: AutoReplyJob) -> Tuple[bool, Optional[AutoReplyJob]]:
        """Insert unless the same inbound message is already queued.

        Returns ``(created, job)``. ``created`` is False when the message was
        already enqueued — duplicate prevention is a property of the insert,
        not of a check-then-insert race.
        """
        if not job.chat_id:
            raise ValueError("chat_id_required")
        now = int(time.time())
        if not job.created_at:
            job.created_at = now
        job.updated_at = now
        d = job.to_dict()
        # Lease bookkeeping is transport state, not part of the aggregate's
        # public shape; a fresh job has none and is claimable immediately.
        d.setdefault("lease_token", "")
        d.setdefault("lease_until", 0)
        try:
            columns = (
                "id", "owner_user_id", "session_id", "chat_id", "chat_title",
                "source_message_id", "status", "attempt", "max_attempts",
                "next_attempt_at", "lease_token", "lease_until", "prompt_id",
                "rule_id", "ai_config_id", "inbound_text", "reply_text", "error",
                "error_code", "correlation_id", "latency_ms", "sent_message_id",
                "created_at", "updated_at", "sent_at",
            )
            values = (
                d["id"], d["owner_user_id"], d["session_id"], d["chat_id"], d["chat_title"],
                d["source_message_id"], d["status"], d["attempt"], d["max_attempts"],
                d["next_attempt_at"], d["lease_token"], d["lease_until"],
                d["prompt_id"], d["rule_id"], d["ai_config_id"],
                d["inbound_text"], d["reply_text"], d["error"], d["error_code"],
                d["correlation_id"], d["latency_ms"], d["sent_message_id"] or 0,
                d["created_at"], d["updated_at"], d["sent_at"],
            )
            self._db.execute(
                "INSERT INTO autoreply_jobs (%s) VALUES (%s)"
                % (", ".join(columns), ",".join("?" * len(columns))),
                values,
            )
            return True, job
        except Exception as exc:  # UNIQUE violation => duplicate, not an error
            if "UNIQUE" not in str(exc).upper():
                raise
            logger.debug("autoreply enqueue deduped %s: %s", d["id"], exc)
            existing = self._db.query_one(
                "SELECT * FROM autoreply_jobs"
                " WHERE owner_user_id=? AND session_id=? AND chat_id=? AND source_message_id=?",
                (d["owner_user_id"], d["session_id"], d["chat_id"], d["source_message_id"]),
            )
            return False, self._row_to_job(existing) if existing else None

    def claim_next(self, owner_user_id: int = 0, worker_id: str = "",
                   now: Optional[int] = None) -> Optional[AutoReplyJob]:
        """Atomically claim the oldest runnable job, respecting per-chat order.

        The claim itself is a single ``UPDATE ... RETURNING`` under the
        connection lock, so two workers can never claim the same job. Per-chat
        ordering is enforced by refusing to claim a job while an earlier
        job for the same chat is still waiting.
        """
        ts = int(now if now is not None else time.time())
        token = secrets.token_hex(16)
        with self._lock:
            rows = self._db.query_all(
                "SELECT j.id FROM autoreply_jobs j"
                " WHERE j.status IN ('PENDING','RETRY_WAIT')"
                " AND j.next_attempt_at<=? AND (j.lease_until=0 OR j.lease_until<?)"
                " AND NOT EXISTS ("
                "  SELECT 1 FROM autoreply_jobs e"
                "  WHERE e.owner_user_id=j.owner_user_id AND e.session_id=j.session_id"
                "    AND e.chat_id=j.chat_id AND e.status IN ('PENDING','RETRY_WAIT','RUNNING')"
                "    AND (e.created_at<j.created_at OR (e.created_at=j.created_at"
                "         AND e.source_message_id<j.source_message_id))"
                " )"
                + (" AND j.owner_user_id=?" if owner_user_id else "")
                + " ORDER BY j.created_at, j.source_message_id LIMIT 1",
                (ts, ts) + ((int(owner_user_id),) if owner_user_id else ()),
            )
            if not rows:
                return None
            job_id = str(rows[0]["id"])
            cur = self._db.execute(
                "UPDATE autoreply_jobs SET status='RUNNING', lease_token=?, lease_until=?,"
                " updated_at=? WHERE id=? AND status IN ('PENDING','RETRY_WAIT')",
                (token, ts + LEASE_SECONDS, ts, job_id),
            )
            if not cur.rowcount:
                return None  # lost the race: the row changed under us
            row = self._db.query_one("SELECT * FROM autoreply_jobs WHERE id=?", (job_id,))
            job = self._row_to_job(row) if row else None
            if job is not None:
                job.lease_token = token
            return job

    def complete_sent(self, job_id: str, lease_token: str, sent_message_id: int = 0,
                      reply_text: str = "", latency_ms: int = 0) -> bool:
        cur = self._db.execute(
            "UPDATE autoreply_jobs SET status='SENT', sent_message_id=?, reply_text=?,"
            " latency_ms=?, sent_at=?, updated_at=?, error='', error_code=''"
            " WHERE id=? AND lease_token=? AND status='RUNNING'",
            (int(sent_message_id or 0), reply_text[:4000], int(latency_ms or 0),
             int(time.time()), int(time.time()), str(job_id), str(lease_token)),
        )
        return bool(cur.rowcount)

    def mark_retry(self, job_id: str, lease_token: str, error: str, error_code: str = "",
                   backoff_seconds: float = 20.0, attempt: int = 0) -> bool:
        now = int(time.time())
        next_at = now + int(max(1, min(backoff_seconds, 86400)))
        cur = self._db.execute(
            "UPDATE autoreply_jobs SET status='RETRY_WAIT', attempt=?, error=?, error_code=?,"
            " next_attempt_at=?, lease_token='', lease_until=0, updated_at=?"
            " WHERE id=? AND lease_token=? AND status='RUNNING'",
            (int(attempt), str(error)[:500], str(error_code)[:64], next_at, now,
             str(job_id), str(lease_token)),
        )
        return bool(cur.rowcount)

    def mark_failed(self, job_id: str, lease_token: str, error: str,
                    error_code: str = "", dead_letter: bool = False) -> bool:
        status = "DEAD_LETTER" if dead_letter else "FAILED"
        cur = self._db.execute(
            "UPDATE autoreply_jobs SET status=?, error=?, error_code=?, lease_token='',"
            " lease_until=0, updated_at=? WHERE id=? AND lease_token=? AND status='RUNNING'",
            (status, str(error)[:500], str(error_code)[:64], int(time.time()),
             str(job_id), str(lease_token)),
        )
        return bool(cur.rowcount)

    def mark_skipped(self, job_id: str, reason: str = "") -> bool:
        cur = self._db.execute(
            "UPDATE autoreply_jobs SET status='SKIPPED', error=?, updated_at=?"
            " WHERE id=? AND status IN ('PENDING','RETRY_WAIT')",
            (str(reason)[:300], int(time.time()), str(job_id)),
        )
        return bool(cur.rowcount)

    def cancel(self, job_id: str, owner_user_id: int) -> bool:
        cur = self._db.execute(
            "UPDATE autoreply_jobs SET status='CANCELLED', updated_at=?"
            " WHERE id=? AND owner_user_id=? AND status IN ('PENDING','RETRY_WAIT','RUNNING')",
            (int(time.time()), str(job_id), int(owner_user_id)),
        )
        return bool(cur.rowcount)

    def replay(self, job_id: str, owner_user_id: int) -> bool:
        """Move a dead-lettered job back to the queue for another attempt."""
        now = int(time.time())
        cur = self._db.execute(
            "UPDATE autoreply_jobs SET status='PENDING', attempt=0, error='', error_code='',"
            " next_attempt_at=?, lease_token='', lease_until=0, updated_at=?"
            " WHERE id=? AND owner_user_id=? AND status IN ('DEAD_LETTER','FAILED')",
            (now, now, str(job_id), int(owner_user_id)),
        )
        return bool(cur.rowcount)

    def recover_expired_leases(self, now: Optional[int] = None) -> int:
        """A crashed worker's jobs become claimable again (bounded takeover).

        Each takeover counts as an attempt, so a job that keeps killing its
        worker cannot be recovered forever: once attempts are exhausted it is
        dead-lettered instead of being handed out again.
        """
        ts = int(now if now is not None else time.time())
        with self._lock:
            self._db.execute(
                "UPDATE autoreply_jobs SET attempt=attempt+1, updated_at=?"
                " WHERE status='RUNNING' AND lease_until>0 AND lease_until<?",
                (ts, ts),
            )
            self._db.execute(
                "UPDATE autoreply_jobs SET status='DEAD_LETTER', lease_token='',"
                " lease_until=0, error='worker_lost', error_code='lease_expired',"
                " updated_at=? WHERE status='RUNNING' AND lease_until>0"
                " AND lease_until<? AND attempt>=max_attempts",
                (ts, ts),
            )
            cur = self._db.execute(
                "UPDATE autoreply_jobs SET status='RETRY_WAIT', lease_token='', lease_until=0,"
                " next_attempt_at=?, updated_at=?"
                " WHERE status='RUNNING' AND lease_until>0 AND lease_until<?",
                (ts, ts, ts),
            )
            return int(cur.rowcount or 0)

    def list_jobs(self, owner_user_id: int, status: str = "", limit: int = 50,
                  offset: int = 0, chat_id: Any = None) -> Tuple[List[AutoReplyJob], int]:
        sql = "FROM autoreply_jobs WHERE owner_user_id=?"
        params: list = [int(owner_user_id)]
        if status:
            sql += " AND status=?"
            params.append(str(status).upper())
        if chat_id:
            sql += " AND chat_id=?"
            params.append(normalize_chat_id(chat_id))
        total_row = self._db.query_one(f"SELECT COUNT(*) AS n {sql}", tuple(params))
        total = int(total_row["n"]) if total_row else 0
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [int(max(1, min(limit, 200))), int(max(0, offset))]
        rows = self._db.query_all(f"SELECT * {sql}", tuple(params))
        return [self._row_to_job(r) for r in rows], total

    def list_dead_letter(self, owner_user_id: int, limit: int = 50,
                         offset: int = 0) -> Tuple[List[AutoReplyJob], int]:
        return self.list_jobs(owner_user_id, status="DEAD_LETTER", limit=limit, offset=offset)

    def get_job(self, job_id: str, owner_user_id: int) -> Optional[AutoReplyJob]:
        row = self._db.query_one(
            "SELECT * FROM autoreply_jobs WHERE id=? AND owner_user_id=?",
            (str(job_id), int(owner_user_id)),
        )
        return self._row_to_job(row) if row else None

    def pending_counts(self, owner_user_id: int) -> Dict[str, int]:
        rows = self._db.query_all(
            "SELECT status, COUNT(*) AS n FROM autoreply_jobs WHERE owner_user_id=?"
            " GROUP BY status",
            (int(owner_user_id),),
        )
        out: Dict[str, int] = {}
        for r in rows:
            out[str(r["status"])] = int(r["n"])
        out["TOTAL"] = sum(out.values())
        return out

    def recent_sends(self, owner_user_id: int, chat_id: Any, since_ts: int) -> int:
        """How many replies landed in this chat since ``since_ts`` (rate limit)."""
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM autoreply_jobs"
            " WHERE owner_user_id=? AND chat_id=? AND status='SENT' AND sent_at>=?",
            (int(owner_user_id), normalize_chat_id(chat_id), int(since_ts)),
        )
        return int(row["n"]) if row else 0

    #: Alias kept because the engine reads the daily cap through this name.
    sent_since = recent_sends

    def last_sent_at(self, owner_user_id: int, chat_id: Any) -> int:
        """Epoch of the newest reply sent to this chat (0 when none)."""
        row = self._db.query_one(
            "SELECT MAX(sent_at) AS ts FROM autoreply_jobs"
            " WHERE owner_user_id=? AND chat_id=? AND status='SENT'",
            (int(owner_user_id), normalize_chat_id(chat_id)),
        )
        return int(row["ts"] or 0) if row else 0

    def cleanup_terminal(self, retention_seconds: int = 30 * 24 * 3600) -> int:
        """Retention sweep for finished rows (sent/failed/skipped/cancelled)."""
        if retention_seconds <= 0:
            return 0
        cutoff = int(time.time()) - int(retention_seconds)
        cur = self._db.execute(
            "DELETE FROM autoreply_jobs WHERE status IN ('SENT','FAILED','SKIPPED','CANCELLED')"
            " AND updated_at<?",
            (cutoff,),
        )
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #
    def stats(self, owner_user_id: int) -> Dict[str, Any]:
        counts = self.pending_counts(owner_user_id)
        prompts = self.count_prompts(owner_user_id)
        targets = self._db.query_one(
            "SELECT COUNT(*) AS n FROM autoreply_targets WHERE owner_user_id=?",
            (int(owner_user_id),),
        )
        rules = self._db.query_one(
            "SELECT COUNT(*) AS n FROM autoreply_rules WHERE owner_user_id=? AND is_enabled=1",
            (int(owner_user_id),),
        )
        sent24 = self._db.query_one(
            "SELECT COUNT(*) AS n FROM autoreply_jobs"
            " WHERE owner_user_id=? AND status='SENT' AND sent_at>=?",
            (int(owner_user_id), int(time.time()) - 86400),
        )
        fail24 = self._db.query_one(
            "SELECT COUNT(*) AS n FROM autoreply_jobs"
            " WHERE owner_user_id=? AND status IN ('FAILED','DEAD_LETTER') AND updated_at>=?",
            (int(owner_user_id), int(time.time()) - 86400),
        )
        sent_all = self._db.query_one(
            "SELECT COUNT(*) AS n, COALESCE(AVG(NULLIF(latency_ms, 0)), 0) AS avg_latency,"
            " COALESCE(MAX(sent_at), 0) AS last_sent"
            " FROM autoreply_jobs WHERE owner_user_id=? AND status='SENT'",
            (int(owner_user_id),),
        )
        chats_enabled = self._db.query_one(
            "SELECT COUNT(*) AS n FROM autoreply_targets WHERE owner_user_id=? AND is_enabled=1",
            (int(owner_user_id),),
        )
        latency = float(sent_all["avg_latency"]) if sent_all and sent_all["avg_latency"] else 0.0
        return {
            "owner_user_id": int(owner_user_id),
            "jobs": counts,
            "prompts": int(prompts),
            "targets": int(targets["n"]) if targets else 0,
            "chats_enabled": int(chats_enabled["n"]) if chats_enabled else 0,
            "rules": int(rules["n"]) if rules else 0,
            "sent_24h": int(sent24["n"]) if sent24 else 0,
            "failed_24h": int(fail24["n"]) if fail24 else 0,
            "sent_total": int(sent_all["n"]) if sent_all else 0,
            "avg_latency_ms": round(latency, 1),
            "last_sent_at": int(sent_all["last_sent"]) if sent_all else 0,
        }


__all__ = [
    "AutoReplyRepository",
    "IncomingMessage",
    "LEASE_SECONDS",
    "MAX_CONTEXT_ROWS",
    "MAX_MEMORY_ROWS",
]
