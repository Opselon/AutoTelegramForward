"""Portable backup & restore for server migration.

A snapshot carries everything needed to move an installation (or a single
tenant of it) from one box to another:

  * Telegram sessions   — session strings travel in PLAINTEXT, because the
                          file must be readable on a box with a DIFFERENT
                          master_key; the repository re-encrypts on import
  * Forward rules       — the routing table
  * Filter rules        — content gates the rules reference
  * AI configs          — provider settings, with the API key in plaintext
                          for the same reason
  * Prompt templates    — rewrite prompts (system prompts are skipped: they
                          are identical everywhere and read-only)
  * API credentials     — api_id/api_hash the session pool needs to connect

Everything is scoped to one owner_user_id; a multi-tenant box exports one
tenant at a time and never sees another tenant's secrets.

Secrets are read out PLAINTEXT (the sqlite repositories decrypt on read) and
written back PLAINTEXT (the repositories encrypt on write), so the JSON bundle
contains live credentials. Callers must protect the bytes: the API and the
bot both offer an optional passphrase-encrypted bundle.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from ..domain.entities import (
    AIFallbackPolicy,
    AIConfig,
    FilterRule,
    ForwardRule,
    PromptTemplate,
    TelegramSession,
)
from ..domain.value_objects import AIProviderType, ForwardMode, RoutingType
from .repositories import (
    ApiCredential,
    IAIConfigRepository,
    IApiCredentialRepository,
    IFilterRuleRepository,
    IForwardRuleRepository,
    IPromptRepository,
    ISessionRepository,
)

logger = logging.getLogger(__name__)

# Bump when the document shape changes in a way an older restore would refuse
# or misread. Restore validates >= minimum and warns on newer-than-known.
SNAPSHOT_FORMAT = 2


def _provider_value(p: Any) -> str:
    """AIProviderType is a str-enum; snapshots from older builds may be plain str."""
    return p.value if hasattr(p, "value") else str(p)


class BackupService:
    """Exports and imports a tenant-scoped application snapshot."""

    def __init__(
        self,
        sessions: ISessionRepository,
        rules: IForwardRuleRepository,
        filters: IFilterRuleRepository,
        ai_configs: IAIConfigRepository,
        credentials: IApiCredentialRepository,
        prompts: IPromptRepository,
        crypto: Any = None,
    ) -> None:
        self._sessions = sessions
        self._rules = rules
        self._filters = filters
        self._ai = ai_configs
        self._creds = credentials
        self._prompts = prompts
        # Secrets are handled by the repository layer (encrypt on write,
        # decrypt on read), so no cipher is needed here. Kept optional for
        # backward compatibility with older wiring sites.
        self._crypto = crypto

    # ------------------------------------------------------------------ export
    async def create_snapshot(self, owner_user_id: Optional[int] = None) -> Dict[str, Any]:
        """Collect everything owned by `owner_user_id` (None = everything).

        Secret columns are decrypted here and shipped as plaintext inside the
        document, so the destination can re-encrypt them under its own
        master_key. Callers must treat the returned dict as confidential.
        """
        doc: Dict[str, Any] = {
            "format": SNAPSHOT_FORMAT,
            "exported_at": int(time.time()),
            "source_owner_user_id": owner_user_id,
            "entities": {},
        }
        ent = doc["entities"]

        scoped = owner_user_id is not None
        ent["sessions"] = [
            self._session_dict(s) for s in (
                await self._sessions.list_by_owner(owner_user_id)
                if scoped else await self._sessions.list_all()
            )
        ]
        ent["forward_rules"] = [
            self._rule_dict(r) for r in (
                await self._rules.list_by_owner(owner_user_id)
                if scoped else await self._rules.list_all()
            )
        ]
        ent["filter_rules"] = [
            self._filter_dict(f) for f in (
                await self._filters.list_by_owner(owner_user_id)
                if scoped else await self._filters.list_all()
            )
        ]
        ent["ai_configs"] = [
            self._ai_dict(a) for a in (
                await self._ai.list_by_owner(owner_user_id)
                if scoped else await self._ai.list_all()
            )
        ]
        # v12: credentials are per-tenant. A non-admin export must only carry
        # the caller's own api_id/api_hash pairs; the unscoped list_all() here
        # leaked every tenant's credentials to any authenticated user.
        ent["api_credentials"] = [
            self._cred_dict(c) for c in (
                await self._creds.list_by_owner(owner_user_id)
                if scoped else await self._creds.list_all()
            )
        ]
        ent["prompt_templates"] = [
            self._prompt_dict(t) for t in (
                await self._prompts.list_templates_by_owner(owner_user_id)
                if scoped else await self._prompts.list_templates()
            )
        ]

        doc["counts"] = {k: len(v) for k, v in ent.items() if isinstance(v, list)}
        logger.info("snapshot built for owner=%s: %s", owner_user_id, doc["counts"])
        return doc

    # ------------------------------------------------------- entity -> dict
    def _session_dict(self, s: TelegramSession) -> Dict[str, Any]:
        # The repository decrypted on read, so session_string_encrypted here is
        # plaintext; it is renamed in the bundle for clarity.
        return {
            "id": s.id,
            "phone_number": s.phone_number,
            "session_string": s.session_string_encrypted,
            "user_id": s.user_id,
            "username": s.username,
            "first_name": s.first_name,
            "is_active": s.is_active,
            "is_authorized": s.is_authorized,
            "api_credential_id": s.api_credential_id,
            "proxy": s.proxy,
            "owner_user_id": s.owner_user_id,
        }

    def _rule_dict(self, r: ForwardRule) -> Dict[str, Any]:
        return {
            "id": r.id,
            "session_id": r.session_id,
            "name": r.name,
            "description": r.description,
            "source_chat_id": r.source_chat_id,
            "source_chat_name": r.source_chat_name,
            "target_chat_id": r.target_chat_id,
            "target_chat_name": r.target_chat_name,
            "target_chat_ids": r.target_chat_ids,
            "routing_type": r.routing_type.value,
            "forward_mode": r.forward_mode.value,
            "is_active": r.is_active,
            "is_paused": r.is_paused,
            "paused_until": r.paused_until,
            "priority": r.priority,
            "message_category": r.message_category,
            "filter_rule_id": r.filter_rule_id,
            "ai_config_id": r.ai_config_id,
            "ai_fallback_policy": (
                r.ai_fallback_policy.value
                if isinstance(r.ai_fallback_policy, AIFallbackPolicy)
                else str(r.ai_fallback_policy or "")
            ),
            "ai_prompt_version": r.ai_prompt_version,
            "ai_timeout_seconds": r.ai_timeout_seconds,
            "ai_secondary_config_id": r.ai_secondary_config_id,
            "custom_header": r.custom_header,
            "custom_footer": r.custom_footer,
            "header_enabled": r.header_enabled,
            "rate_limit_per_minute": r.rate_limit_per_minute,
            "rate_limit_burst": r.rate_limit_burst,
            "media_handling": r.media_handling,
            "dedupe_policy": r.dedupe_policy,
            "max_retries": r.max_retries,
            "retry_backoff_base": r.retry_backoff_base,
            "template_text": r.template_text,
            "template_media": r.template_media,
            "template_album": r.template_album,
            "caption_max_length": r.caption_max_length,
            "preserve_signature": r.preserve_signature,
            "delay_seconds": r.delay_seconds,
            "skip_history": r.skip_history,
            "since_ts": r.since_ts,
            "ignore_edits": r.ignore_edits,
            "trigger_events": r.trigger_events,
            "remove_links": r.remove_links,
            "link_policy": r.link_policy,
            "domain_allowlist": r.domain_allowlist,
            "domain_blocklist": r.domain_blocklist,
            "link_rewrite_map": r.link_rewrite_map,
            "allowed_media_types": r.allowed_media_types,
            "split_long_caption": r.split_long_caption,
            "owner_user_id": r.owner_user_id,
            "album_aggregation_window_seconds": r.album_aggregation_window_seconds,
            "custom_caption_template": r.custom_caption_template,
            "intermediate_channel_id": r.intermediate_channel_id,
            "intermediate_channel_name": r.intermediate_channel_name,
            "intermediate_target_chat_id": r.intermediate_target_chat_id,
            "use_intermediate": r.use_intermediate,
            "intermediate_mode": r.intermediate_mode,
            "fallback_mode": (
                r.fallback_mode.value if hasattr(r.fallback_mode, "value") else str(r.fallback_mode)
            ),
            "fallback_enabled": r.fallback_enabled,
            "multi_route": r.multi_route,
            "execution_order": r.execution_order,
            "detection_criteria": r.detection_criteria,
            "metadata": r.metadata,
        }

    def _filter_dict(self, f: FilterRule) -> Dict[str, Any]:
        return {
            "id": f.id,
            "name": f.name,
            "whitelist_keywords": f.whitelist_keywords,
            "blacklist_keywords": f.blacklist_keywords,
            "regex_patterns": f.regex_patterns,
            "allowed_media_types": f.allowed_media_types,
            "blocked_media_types": f.blocked_media_types,
            "drop_service_messages": f.drop_service_messages,
            "min_message_length": f.min_message_length,
            "max_message_length": f.max_message_length,
            "owner_user_id": f.owner_user_id,
        }

    def _ai_dict(self, a: AIConfig) -> Dict[str, Any]:
        # The sqlite repo stores api_key encrypted and decrypts on read, so the
        # entity handed to us already carries plaintext.
        return {
            "id": a.id,
            "name": a.name,
            "provider": _provider_value(a.provider),
            "model": a.model,
            "api_key": a.api_key,
            "base_url": a.base_url,
            "system_prompt": a.system_prompt,
            "user_prompt_template": a.user_prompt_template,
            "temperature": a.temperature,
            "is_enabled": a.is_enabled,
            "target_language": a.target_language,
            "owner_user_id": a.owner_user_id,
        }

    def _cred_dict(self, c: ApiCredential) -> Dict[str, Any]:
        return {
            "id": c.id,
            "label": c.label,
            "api_id": c.api_id,
            "api_hash": c.api_hash,
            "proxy": c.proxy,
            "is_default": c.is_default,
        }

    def _prompt_dict(self, t: PromptTemplate) -> Dict[str, Any]:
        return {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "system_prompt": t.system_prompt,
            "user_prompt_template": t.user_prompt_template,
            "target_language": t.target_language,
            "current_version": t.current_version,
            "is_system": t.is_system,
            "owner_user_id": int(getattr(t, "owner_user_id", 0) or 0),
        }

    # ------------------------------------------------------------------ import
    async def restore_snapshot(
        self,
        doc: Dict[str, Any],
        target_owner_user_id: Optional[int] = None,
        *,
        overwrite_existing: bool = False,
    ) -> Dict[str, Any]:
        """Apply a snapshot document.

        `target_owner_user_id` re-stamps every imported entity with a new owner
        (used when a tenant's Telegram id differs between boxes). Leave None to
        keep the ids from the source.

        `overwrite_existing=False` skips rows whose id already exists, so
        re-importing a snapshot is idempotent instead of creating duplicates.
        """
        fmt = doc.get("format", 1)
        if not isinstance(fmt, int) or fmt < 1:
            raise ValueError("snapshot has no recognizable format version")
        if fmt > SNAPSHOT_FORMAT:
            logger.warning(
                "snapshot format %d is newer than known %d — best-effort restore",
                fmt, SNAPSHOT_FORMAT,
            )

        ent = doc.get("entities") or {}
        stats: Dict[str, Dict[str, int]] = {}

        # ---- API credentials first: sessions reference them --------------------
        c_stats = {"imported": 0, "skipped": 0, "failed": 0}
        for raw in ent.get("api_credentials", []):
            try:
                existing = await self._creds.get_by_id(raw["id"])
                if existing and not overwrite_existing:
                    c_stats["skipped"] += 1
                    continue
                cred = ApiCredential(
                    id=raw["id"],
                    label=raw.get("label", ""),
                    api_id=int(raw.get("api_id") or 0),
                    api_hash=raw.get("api_hash", ""),
                    proxy=str(raw.get("proxy") or ""),
                    is_default=bool(raw.get("is_default", False)),
                    created_at=int(raw.get("created_at") or time.time()),
                    updated_at=int(time.time()),
                    # v12: re-stamp to the import target so an imported pair does
                    # not stay invisible to its new owner (or default to 0).
                    owner_user_id=int(
                        target_owner_user_id
                        if target_owner_user_id is not None
                        else raw.get("owner_user_id") or 0
                    ),
                )
                if existing:
                    await self._creds.update(cred)
                else:
                    await self._creds.add(cred)
                c_stats["imported"] += 1
            except Exception as exc:
                logger.warning("credential import failed: %s", exc)
                c_stats["failed"] += 1
        stats["api_credentials"] = c_stats

        # ---- sessions ----------------------------------------------------------
        s_stats = {"imported": 0, "skipped": 0, "failed": 0}
        for raw in ent.get("sessions", []):
            try:
                sid = raw["id"]
                existing = await self._sessions.get_by_id(sid)
                if existing and not overwrite_existing:
                    s_stats["skipped"] += 1
                    continue
                session_string = raw.get("session_string", "")
                # NOTE: the repository encrypts on write, so plaintext goes in
                # here and lands encrypted at rest. A session string already
                # encrypted would be double-encrypted, so a snapshot generated
                # on THIS box (whose export step decrypted it) is always plain.
                new_owner = (
                    target_owner_user_id
                    if target_owner_user_id is not None
                    else raw.get("owner_user_id")
                )
                s = TelegramSession(
                    id=sid,
                    phone_number=raw.get("phone_number", ""),
                    session_string_encrypted=session_string,
                    user_id=raw.get("user_id", ""),
                    username=raw.get("username", ""),
                    first_name=raw.get("first_name", ""),
                    is_active=bool(raw.get("is_active", False)),
                    is_authorized=bool(raw.get("is_authorized", False)),
                    api_credential_id=raw.get("api_credential_id", "default"),
                    proxy=raw.get("proxy"),
                    owner_user_id=new_owner or 0,
                )
                if existing:
                    await self._sessions.update(s)
                else:
                    await self._sessions.add(s)
                s_stats["imported"] += 1
            except Exception as exc:
                logger.warning("session import failed: %s", exc)
                s_stats["failed"] += 1
        stats["sessions"] = s_stats

        # ---- filter rules (rules reference them) -------------------------------
        f_stats = {"imported": 0, "skipped": 0, "failed": 0}
        for raw in ent.get("filter_rules", []):
            try:
                fid = raw["id"]
                existing = await self._filters.get_by_id(fid)
                if existing and not overwrite_existing:
                    f_stats["skipped"] += 1
                    continue
                new_owner = (
                    target_owner_user_id
                    if target_owner_user_id is not None
                    else raw.get("owner_user_id")
                )
                f = FilterRule(
                    id=fid,
                    name=raw.get("name", ""),
                    whitelist_keywords=raw.get("whitelist_keywords") or [],
                    blacklist_keywords=raw.get("blacklist_keywords") or [],
                    regex_patterns=raw.get("regex_patterns") or [],
                    allowed_media_types=raw.get("allowed_media_types") or [],
                    blocked_media_types=raw.get("blocked_media_types") or [],
                    drop_service_messages=bool(raw.get("drop_service_messages", True)),
                    min_message_length=int(raw.get("min_message_length") or 0),
                    max_message_length=int(raw.get("max_message_length") or 0),
                    owner_user_id=new_owner or 0,
                )
                if existing:
                    await self._filters.update(f)
                else:
                    await self._filters.add(f)
                f_stats["imported"] += 1
            except Exception as exc:
                logger.warning("filter import failed: %s", exc)
                f_stats["failed"] += 1
        stats["filter_rules"] = f_stats

        # ---- AI configs ---------------------------------------------------------
        a_stats = {"imported": 0, "skipped": 0, "failed": 0}
        for raw in ent.get("ai_configs", []):
            try:
                aid = raw["id"]
                existing = await self._ai.get_by_id(aid)
                if existing and not overwrite_existing:
                    a_stats["skipped"] += 1
                    continue
                new_owner = (
                    target_owner_user_id
                    if target_owner_user_id is not None
                    else raw.get("owner_user_id")
                )
                # Snapshots store the provider name as a plain string; the
                # sqlite repo expects the str-enum.
                provider_raw = raw.get("provider", "openai")
                try:
                    provider = AIProviderType(str(provider_raw))
                except Exception:
                    provider = AIProviderType.OPENAI
                a = AIConfig(
                    id=aid,
                    name=raw.get("name", ""),
                    provider=provider,
                    model=raw.get("model", ""),
                    api_key=raw.get("api_key", ""),
                    base_url=raw.get("base_url", ""),
                    system_prompt=raw.get("system_prompt", ""),
                    user_prompt_template=raw.get("user_prompt_template") or "{text}",
                    temperature=float(raw.get("temperature") or 0.7),
                    is_enabled=bool(raw.get("is_enabled", True)),
                    target_language=raw.get("target_language") or "en",
                    owner_user_id=new_owner or 0,
                )
                if existing:
                    await self._ai.update(a)
                else:
                    await self._ai.add(a)
                a_stats["imported"] += 1
            except Exception as exc:
                logger.warning("ai config import failed: %s", exc)
                a_stats["failed"] += 1
        stats["ai_configs"] = a_stats

        # ---- prompt templates ----------------------------------------------------
        p_stats = {"imported": 0, "skipped": 0, "failed": 0}
        for raw in ent.get("prompt_templates", []):
            try:
                pid = raw["id"]
                existing = await self._prompts.get_template(pid)
                # System templates are identical everywhere and read-only.
                if existing or raw.get("is_system"):
                    p_stats["skipped"] += 1
                    continue
                t = PromptTemplate(
                    id=pid,
                    name=raw.get("name", ""),
                    description=raw.get("description", ""),
                    system_prompt=raw.get("system_prompt", ""),
                    user_prompt_template=raw.get("user_prompt_template") or "{text}",
                    target_language=raw.get("target_language") or "auto",
                    current_version=int(raw.get("current_version") or 1),
                    is_system=False,
                    # v12: re-stamp to the import target; a user prompt imported
                    # from another box must not land in the shared owner-0 scope.
                    owner_user_id=int(
                        target_owner_user_id
                        if target_owner_user_id is not None
                        else raw.get("owner_user_id") or 0
                    ),
                )
                await self._prompts.add_template(t)
                p_stats["imported"] += 1
            except Exception as exc:
                logger.warning("prompt import failed: %s", exc)
                p_stats["failed"] += 1
        stats["prompt_templates"] = p_stats

        # ---- forward rules -------------------------------------------------------
        r_stats = {"imported": 0, "skipped": 0, "failed": 0}
        for raw in ent.get("forward_rules", []):
            try:
                rid = raw["id"]
                existing = await self._rules.get_by_id(rid)
                if existing and not overwrite_existing:
                    r_stats["skipped"] += 1
                    continue
                new_owner = (
                    target_owner_user_id
                    if target_owner_user_id is not None
                    else raw.get("owner_user_id")
                )
                r = self._rule_from_dict(raw, new_owner or 0)
                if existing:
                    await self._rules.update(r)
                else:
                    await self._rules.add(r)
                r_stats["imported"] += 1
            except Exception as exc:
                logger.warning("rule import failed: %s", exc)
                r_stats["failed"] += 1
        stats["forward_rules"] = r_stats

        logger.info("snapshot restored into owner=%s: %s", target_owner_user_id, stats)
        return stats

    def _rule_from_dict(self, raw: Dict[str, Any], owner_user_id: int) -> ForwardRule:
        def _enum(cls, value, default):
            try:
                return cls(str(value))
            except Exception:
                return default

        try:
            policy = AIFallbackPolicy(str(raw.get("ai_fallback_policy") or "DROP").upper())
        except Exception:
            policy = AIFallbackPolicy.DROP

        return ForwardRule(
            id=raw["id"],
            session_id=raw.get("session_id", ""),
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            source_chat_id=str(raw.get("source_chat_id", "")),
            source_chat_name=raw.get("source_chat_name", ""),
            target_chat_id=str(raw.get("target_chat_id", "")),
            target_chat_name=raw.get("target_chat_name", ""),
            target_chat_ids=raw.get("target_chat_ids") or [],
            routing_type=_enum(RoutingType, raw.get("routing_type"), RoutingType.CHANNEL_TO_CHANNEL),
            forward_mode=_enum(ForwardMode, raw.get("forward_mode"), ForwardMode.COPY_MESSAGE),
            is_active=bool(raw.get("is_active", True)),
            is_paused=bool(raw.get("is_paused", False)),
            paused_until=int(raw.get("paused_until") or 0),
            priority=int(raw.get("priority") or 10),
            message_category=raw.get("message_category", "ALL"),
            filter_rule_id=raw.get("filter_rule_id"),
            ai_config_id=raw.get("ai_config_id"),
            ai_fallback_policy=policy,
            ai_prompt_version=int(raw.get("ai_prompt_version") or 0),
            ai_timeout_seconds=float(raw.get("ai_timeout_seconds") or 15.0),
            ai_secondary_config_id=raw.get("ai_secondary_config_id"),
            custom_header=raw.get("custom_header", ""),
            custom_footer=raw.get("custom_footer", ""),
            header_enabled=bool(raw.get("header_enabled", False)),
            rate_limit_per_minute=int(raw.get("rate_limit_per_minute") or 25),
            rate_limit_burst=int(raw.get("rate_limit_burst") or 0),
            media_handling=raw.get("media_handling", "AUTO"),
            dedupe_policy=raw.get("dedupe_policy", "STRICT"),
            max_retries=int(raw.get("max_retries") or 3),
            retry_backoff_base=float(raw.get("retry_backoff_base") or 2.0),
            template_text=raw.get("template_text", ""),
            template_media=raw.get("template_media", ""),
            template_album=raw.get("template_album", ""),
            caption_max_length=int(raw.get("caption_max_length") or 0),
            preserve_signature=bool(raw.get("preserve_signature", False)),
            delay_seconds=float(raw.get("delay_seconds") or 0.0),
            skip_history=bool(raw.get("skip_history", True)),
            since_ts=int(raw.get("since_ts") or 0),
            ignore_edits=bool(raw.get("ignore_edits", False)),
            trigger_events=raw.get("trigger_events") or [],
            remove_links=bool(raw.get("remove_links", False)),
            link_policy=raw.get("link_policy", "PRESERVE_ALL"),
            domain_allowlist=raw.get("domain_allowlist") or [],
            domain_blocklist=raw.get("domain_blocklist") or [],
            link_rewrite_map=raw.get("link_rewrite_map") or {},
            allowed_media_types=raw.get("allowed_media_types") or [],
            split_long_caption=bool(raw.get("split_long_caption", True)),
            owner_user_id=owner_user_id,
            album_aggregation_window_seconds=float(raw.get("album_aggregation_window_seconds") or 0.8),
            custom_caption_template=raw.get("custom_caption_template", ""),
            intermediate_channel_id=raw.get("intermediate_channel_id", ""),
            intermediate_channel_name=raw.get("intermediate_channel_name", ""),
            intermediate_target_chat_id=raw.get("intermediate_target_chat_id", ""),
            use_intermediate=bool(raw.get("use_intermediate", False)),
            intermediate_mode=raw.get("intermediate_mode", "NONE"),
            fallback_mode=_enum(ForwardMode, raw.get("fallback_mode"), ForwardMode.COPY_MESSAGE),
            fallback_enabled=bool(raw.get("fallback_enabled", True)),
            multi_route=bool(raw.get("multi_route", False)),
            execution_order=int(raw.get("execution_order") or 0),
            detection_criteria=raw.get("detection_criteria") or {},
            metadata=raw.get("metadata") or {},
        )


# --------------------------------------------------------------------------- #
# Bundle serialization helpers — the wire format the API and the bot share.
# --------------------------------------------------------------------------- #

_BUNDLE_PREFIX = "enc:v1:"


def serialize_bundle(doc: Dict[str, Any]) -> str:
    """Snapshot document -> compact JSON text."""
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def deserialize_bundle(text: str) -> Dict[str, Any]:
    """JSON text -> snapshot document, with a shape sanity check."""
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("bundle is not a JSON object")
    if "entities" not in doc:
        raise ValueError("bundle has no 'entities' section")
    return doc



_SESSION_BACKUP_PREFIX = "atf-session:v1:"


def encrypt_session_backup(bundle: Dict[str, Any], crypto: Any) -> str:
    """Encrypt a per-session backup with the installation's AES-GCM key.

    This format is intentionally installation-bound. Cross-installation
    migrations must use the passphrase-protected full-bundle export instead.
    """
    payload = json.dumps(bundle, separators=(",", ":"), ensure_ascii=False)
    return _SESSION_BACKUP_PREFIX + crypto.encrypt(payload)


def decrypt_session_backup(blob: str, crypto: Any) -> Dict[str, Any]:
    """Decrypt current session backups and read legacy base64-only backups.

    Legacy support is import-only; all newly exported files use authenticated
    encryption. Callers must remove downloaded temporary files after import.
    """
    import base64

    if blob.startswith(_SESSION_BACKUP_PREFIX):
        payload = crypto.decrypt(blob[len(_SESSION_BACKUP_PREFIX):])
    else:
        # Compatibility with old .atf files that were merely base64-encoded.
        payload = base64.urlsafe_b64decode(blob.encode("ascii")).decode("utf-8")
    value = json.loads(payload)
    if not isinstance(value, dict) or not value.get("session_string"):
        raise ValueError("invalid session backup payload")
    return value



def encrypt_bundle(plaintext: str, passphrase: str) -> str:
    """Encrypt a serialized bundle with a user passphrase (AES-GCM).

    Returns a self-describing string so decrypt can tell an encrypted bundle
    from a plain one: 'enc:v1:<b64(nonce+ciphertext)>'.
    """
    import base64
    import hashlib
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not passphrase:
        raise ValueError("passphrase required")
    key = hashlib.sha256(passphrase.encode("utf-8")).digest()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _BUNDLE_PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_bundle(blob: str, passphrase: str) -> str:
    """Reverse encrypt_bundle. Raises ValueError on a wrong/missing passphrase."""
    import base64
    import hashlib

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(_BUNDLE_PREFIX):
        # plain bundle — a passphrase on an unencrypted bundle is a hard error
        # so nobody silently imports an unprotected bundle while expecting one
        if passphrase:
            raise ValueError("bundle is not encrypted but a passphrase was supplied")
        return blob
    if not passphrase:
        raise ValueError("this bundle is passphrase-protected")
    key = hashlib.sha256(passphrase.encode("utf-8")).digest()
    raw = base64.urlsafe_b64decode(blob[len(_BUNDLE_PREFIX):].encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    try:
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as exc:
        raise ValueError("wrong passphrase or corrupted bundle") from exc
