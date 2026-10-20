"""Tenant-facing auto-reply operations.

This is the only layer the transport surfaces (gRPC → Go API → web/mobile/bot)
call. It owns three invariants:

* **Ownership.** ``owner_user_id`` is always required and always non-zero. Any
  id a caller supplies (prompt, rule, target, job, AI config) is re-read and
  checked against that owner before it is used or changed. A foreign id is
  answered exactly like a missing one.
* **Referential honesty.** Assigning a prompt or an AI config that the caller
  does not own is refused, so a conversation can never be answered with
  another tenant's key or instructions.
* **Boundedness.** Every list is limited and every text field is clamped by the
  domain model, so no endpoint can be used to grow a row without limit.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from ...domain.autoreply import (
    AutoReplyJob,
    AutoReplyPrompt,
    AutoReplyRule,
    AutoReplySettings,
    ContactTarget,
    JobStatus,
    MemoryKind,
    normalize_chat_id,
)
from .context import (
    PromptContext,
    build_messages,
    build_system_prompt,
    resolve_language,
)
from .engine import AutoReplyDecision
from .worker import GenerationResult

#: Import/export document version — clients refuse a document they do not know.
EXPORT_SCHEMA_VERSION = 1
#: Ceiling on one import document (prompts), so a paste cannot exhaust memory.
MAX_IMPORT_PROMPTS = 200


class AutoReplyError(Exception):
    """A refusal with a machine-readable code the API maps to HTTP."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _require_owner(owner_user_id: int) -> int:
    owner = int(owner_user_id or 0)
    if owner <= 0:
        raise AutoReplyError("owner_required", "an authenticated account is required")
    return owner


class AutoReplyService:
    def __init__(self, repo: Any, ai_repo: Any = None,
                 resolver: Optional[Callable[[str, str], Awaitable[Optional[Dict[str, Any]]]]] = None,
                 engine: Any = None,
                 session_owner: Optional[Callable[[str], int]] = None) -> None:
        self._repo = repo
        self._ai = ai_repo
        self._resolver = resolver
        self._engine = engine
        self._session_owner = session_owner

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def get_settings(self, owner_user_id: int) -> AutoReplySettings:
        owner = _require_owner(owner_user_id)
        settings = self._repo.get_settings(owner)
        # Never hand out a dangling reference: if the prompt or config was
        # deleted elsewhere, the returned row says so by clearing the id.
        if settings.prompt_id and self._repo.get_prompt(settings.prompt_id, owner) is None:
            settings.prompt_id = None
        if settings.ai_config_id and not self._owns_ai(settings.ai_config_id, owner):
            settings.ai_config_id = None
        return settings

    def update_settings(self, owner_user_id: int, patch: Dict[str, Any]) -> AutoReplySettings:
        owner = _require_owner(owner_user_id)
        current = self._repo.get_settings(owner)
        data = current.to_dict()
        for key, value in (patch or {}).items():
            if key in ("owner_user_id", "created_at", "updated_at", "version"):
                continue
            if key in data:
                data[key] = value
        updated = AutoReplySettings.from_dict(data, owner_user_id=owner)
        if updated.prompt_id and self._repo.get_prompt(updated.prompt_id, owner) is None:
            raise AutoReplyError("prompt_not_found", "that prompt does not exist for this account")
        if updated.ai_config_id and not self._owns_ai(updated.ai_config_id, owner):
            raise AutoReplyError("ai_config_not_found", "that AI configuration does not exist for this account")
        updated.version = int(current.version) + 1
        saved = self._repo.save_settings(updated)
        if patch and "target_mode" in (patch or {}):
            # Switching modes while an empty list would silently answer nobody:
            # the caller gets the effective mode it will actually behave like.
            saved.target_mode = updated.target_mode
        return saved

    def _owns_ai(self, config_id: str, owner: int) -> bool:
        if not self._ai:
            return True
        try:
            get_owned = getattr(self._ai, "get_owned", None)
            if get_owned is not None:
                return get_owned(config_id, owner) is not None
            cfg = self._ai.get(config_id)
            return cfg is not None and int(getattr(cfg, "owner_user_id", 0) or 0) == owner
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Prompts
    # ------------------------------------------------------------------ #
    def list_prompts(self, owner_user_id: int, search: str = "",
                     category: str = "", include_archived: bool = False) -> List[AutoReplyPrompt]:
        return self._repo.list_prompts(_require_owner(owner_user_id), include_archived=include_archived,
                                       search=search, category=category)

    def get_prompt(self, owner_user_id: int, prompt_id: str) -> AutoReplyPrompt:
        owner = _require_owner(owner_user_id)
        prompt = self._repo.get_prompt(prompt_id, owner)
        if prompt is None:
            raise AutoReplyError("prompt_not_found")
        return prompt

    def save_prompt(self, owner_user_id: int, data: Dict[str, Any]) -> AutoReplyPrompt:
        owner = _require_owner(owner_user_id)
        prompt = AutoReplyPrompt.from_dict(data, owner_user_id=owner)
        prompt.owner_user_id = owner
        if data.get("id"):
            existing = self._repo.get_prompt(prompt.id, owner)
            if existing is None:
                # A client may send a new id for an existing name; treat an
                # unknown id as a create, never as an upsert into someone else.
                prompt.id = ""
        if not prompt.id:
            prompt.id = AutoReplyPrompt().id
        prompt.updated_at = int(time.time())
        try:
            return self._repo.save_prompt(prompt)
        except ValueError as exc:
            raise AutoReplyError("prompt_invalid", str(exc)) from exc
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise AutoReplyError("prompt_name_taken", "another prompt already uses that name") from exc
            raise

    def duplicate_prompt(self, owner_user_id: int, prompt_id: str, name: str = "") -> AutoReplyPrompt:
        owner = _require_owner(owner_user_id)
        clone = self._repo.duplicate_prompt(prompt_id, owner, name)
        if clone is None:
            raise AutoReplyError("prompt_not_found")
        return clone

    def delete_prompt(self, owner_user_id: int, prompt_id: str) -> bool:
        owner = _require_owner(owner_user_id)
        # Answering "nothing was deleted" for another tenant's id would leak
        # that the id exists; not-found is the only honest answer.
        if self._repo.get_prompt(str(prompt_id), owner) is None:
            raise AutoReplyError("prompt_not_found")
        return bool(self._repo.delete_prompt(str(prompt_id), owner))

    def set_default_prompt(self, owner_user_id: int, prompt_id: str) -> AutoReplyPrompt:
        owner = _require_owner(owner_user_id)
        prompt = self._repo.get_prompt(prompt_id, owner)
        if prompt is None:
            raise AutoReplyError("prompt_not_found")
        prompt.is_default = True
        prompt.is_archived = False
        return self._repo.save_prompt(prompt)

    def export_prompts(self, owner_user_id: int, prompt_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """A portable document: prompt content only, no ids, no owner, no keys."""
        owner = _require_owner(owner_user_id)
        prompts = self._repo.list_prompts(owner, include_archived=True)
        wanted = {str(p) for p in prompt_ids or []}
        selected = [p for p in prompts if not wanted or p.id in wanted]
        return {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "kind": "autoreply_prompts",
            "exported_at": int(time.time()),
            "prompts": [
                {
                    "name": p.name,
                    "description": p.description,
                    "system_prompt": p.system_prompt,
                    "user_prompt_template": p.user_prompt_template,
                    "tone": p.tone.value if p.tone else "",
                    "language": p.language,
                    "response_length": p.response_length.value if p.response_length else "",
                    "category": p.category,
                    "tags": list(p.tags),
                    "is_default": bool(p.is_default),
                }
                for p in selected
            ],
        }

    def import_prompts(self, owner_user_id: int, document: Any, *,
                       rename_conflicts: bool = True) -> Dict[str, Any]:
        """Import prompts, never overwriting an existing one.

        A conflict is renamed (``name (imported)``), and the same prompt
        imported twice is deduplicated by content hash — importing a file
        repeatedly must not fill a library with copies.
        """
        owner = _require_owner(owner_user_id)
        data = document
        if isinstance(document, str):
            try:
                data = json.loads(document)
            except ValueError as exc:
                raise AutoReplyError("import_invalid", "the document is not valid JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("prompts"), list):
            raise AutoReplyError("import_invalid", "expected an object with a 'prompts' array")
        if len(data["prompts"]) > MAX_IMPORT_PROMPTS:
            raise AutoReplyError("import_too_large", f"at most {MAX_IMPORT_PROMPTS} prompts per import")
        existing = self._repo.list_prompts(owner, include_archived=True)
        by_name = {p.name.casefold() for p in existing}
        by_content = {(p.system_prompt.strip(), p.user_prompt_template.strip()) for p in existing}
        imported: List[AutoReplyPrompt] = []
        skipped = 0
        for raw in data["prompts"]:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            candidate = AutoReplyPrompt.from_dict(raw, owner_user_id=owner)
            candidate.id = ""
            candidate.is_default = False
            key = (candidate.system_prompt.strip(), candidate.user_prompt_template.strip())
            if key in by_content:
                skipped += 1
                continue
            if not candidate.name:
                candidate.name = "Imported prompt"
            if candidate.name.casefold() in by_name:
                if not rename_conflicts:
                    skipped += 1
                    continue
                candidate.name = f"{candidate.name} (imported)"[:80]
                suffix = 1
                while candidate.name.casefold() in by_name:
                    suffix += 1
                    candidate.name = f"{candidate.name.split(' (imported')[0]} (imported {suffix})"[:80]
            try:
                stored = self._repo.save_prompt(candidate)
            except ValueError:
                skipped += 1
                continue
            imported.append(stored)
            by_name.add(stored.name.casefold())
            by_content.add(key)
        return {"imported": len(imported), "skipped": skipped,
                "prompts": [p.to_dict() for p in imported]}

    # ------------------------------------------------------------------ #
    # Targeting
    # ------------------------------------------------------------------ #
    def list_targets(self, owner_user_id: int, search: str = "",
                     enabled_only: bool = False) -> List[ContactTarget]:
        return self._repo.list_targets(_require_owner(owner_user_id), search=search,
                                       enabled_only=enabled_only)

    def add_targets(self, owner_user_id: int, entries: Sequence[Dict[str, Any]],
                    replace: bool = False) -> List[ContactTarget]:
        owner = _require_owner(owner_user_id)
        cleaned: List[Dict[str, Any]] = []
        seen: set = set()
        for raw in entries or []:
            item = dict(raw or {})
            chat_id = normalize_chat_id(item.get("chat_id"))
            if not chat_id or chat_id in seen:
                continue
            seen.add(chat_id)
            item["chat_id"] = chat_id
            prompt_id = str(item.get("prompt_id") or "").strip()
            if prompt_id and self._repo.get_prompt(prompt_id, owner) is None:
                raise AutoReplyError("prompt_not_found", f"prompt {prompt_id} is not yours")
            cleaned.append(item)
        return self._repo.bulk_set_targets(owner, cleaned, replace=replace)

    def remove_targets(self, owner_user_id: int, chat_ids: Sequence[str]) -> int:
        owner = _require_owner(owner_user_id)
        removed = 0
        for chat_id in chat_ids or []:
            if self._repo.delete_target(chat_id, owner):
                removed += 1
        return removed

    def set_target_enabled(self, owner_user_id: int, chat_id: str, enabled: bool) -> ContactTarget:
        owner = _require_owner(owner_user_id)
        chat = normalize_chat_id(chat_id)
        if not self._repo.set_target_enabled(chat, owner, enabled):
            raise AutoReplyError("target_not_found")
        target = self._repo.get_target_by_chat(chat, owner)
        if target is None:  # pragma: no cover - the update just succeeded
            raise AutoReplyError("target_not_found")
        return target

    async def resolve_chat(self, owner_user_id: int, query: str,
                           session_id: str = "") -> Optional[Dict[str, Any]]:
        """Look up a private chat through the tenant's own session.

        ``query`` is a numeric id, an @username, or a name fragment. The
        resolver is the live client pool, so the answer is a chat the account
        can actually message — not an invitation to type an arbitrary id.
        """
        owner = _require_owner(owner_user_id)
        text = str(query or "").strip()
        if not text:
            raise AutoReplyError("query_required")
        if session_id and self._session_owner is not None:
            # A session id is a credential-shaped argument: refuse one that
            # belongs to another tenant instead of searching with it.
            if int(self._session_owner(session_id) or 0) != owner:
                raise AutoReplyError("session_not_found")
        if self._resolver is None:
            # No live transport (tests, a replica without Pyrogram): accept the
            # identifier the caller typed, and say it was not resolved.
            chat_id = normalize_chat_id(text.lstrip("@"))
            if not chat_id:
                return None
            return {"chat_id": chat_id, "title": text, "username": text.lstrip("@"),
                    "chat_type": "private", "resolved": False}
        found = await self._resolver(session_id, text)
        if found is None:
            return None
        found = dict(found)
        found.setdefault("chat_type", "private")
        found["resolved"] = True
        return found

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #
    def list_rules(self, owner_user_id: int) -> List[AutoReplyRule]:
        return self._repo.list_rules(_require_owner(owner_user_id))

    def get_rule(self, owner_user_id: int, rule_id: str) -> AutoReplyRule:
        rule = self._repo.get_rule(rule_id, _require_owner(owner_user_id))
        if rule is None:
            raise AutoReplyError("rule_not_found")
        return rule

    def save_rule(self, owner_user_id: int, data: Dict[str, Any]) -> AutoReplyRule:
        owner = _require_owner(owner_user_id)
        try:
            rule = AutoReplyRule.from_dict(data, owner_user_id=owner)
        except ValueError as exc:
            # An unparsable body (bad regex, bad JSON) is the caller's bug, not
            # a server fault: the same mapping as the repo layer below.
            raise AutoReplyError("rule_invalid", str(exc)) from exc
        rule.owner_user_id = owner
        if data.get("id") and self._repo.get_rule(rule.id, owner) is None:
            rule.id = AutoReplyRule().id
        if rule.prompt_id and self._repo.get_prompt(rule.prompt_id, owner) is None:
            raise AutoReplyError("prompt_not_found")
        if rule.ai_config_id and not self._owns_ai(rule.ai_config_id, owner):
            raise AutoReplyError("ai_config_not_found")
        try:
            return self._repo.save_rule(rule)
        except ValueError as exc:
            raise AutoReplyError("rule_invalid", str(exc)) from exc

    def delete_rule(self, owner_user_id: int, rule_id: str) -> bool:
        owner = _require_owner(owner_user_id)
        if self._repo.get_rule(str(rule_id), owner) is None:
            raise AutoReplyError("rule_not_found")
        return bool(self._repo.delete_rule(str(rule_id), owner))

    def reorder_rules(self, owner_user_id: int, ordered_ids: Sequence[str]) -> List[AutoReplyRule]:
        """Persist an explicit priority order (highest first)."""
        owner = _require_owner(owner_user_id)
        rules = {r.id: r for r in self._repo.list_rules(owner)}
        total = len(ordered_ids)
        for index, rule_id in enumerate(ordered_ids):
            rule = rules.get(str(rule_id))
            if rule is None:
                continue
            rule.priority = (total - index) * 10
            self._repo.save_rule(rule)
        return self._repo.list_rules(owner)

    # ------------------------------------------------------------------ #
    # Queue / jobs
    # ------------------------------------------------------------------ #
    def list_jobs(self, owner_user_id: int, status: str = "", limit: int = 50,
                  offset: int = 0, chat_id: str = "") -> Tuple[List[AutoReplyJob], int]:
        return self._repo.list_jobs(_require_owner(owner_user_id), status=status,
                                    limit=limit, offset=offset, chat_id=chat_id or None)

    def retry_job(self, owner_user_id: int, job_id: str) -> AutoReplyJob:
        owner = _require_owner(owner_user_id)
        job = self._repo.get_job(job_id, owner)
        if job is None:
            raise AutoReplyError("job_not_found")
        if not self._repo.replay(job_id, owner):
            raise AutoReplyError("job_not_retryable", "only a failed or dead-lettered reply can be retried")
        refreshed = self._repo.get_job(job_id, owner)
        if refreshed is None:  # pragma: no cover
            raise AutoReplyError("job_not_found")
        return refreshed

    def cancel_job(self, owner_user_id: int, job_id: str) -> bool:
        owner = _require_owner(owner_user_id)
        if not self._repo.cancel(job_id, owner):
            raise AutoReplyError("job_not_cancellable")
        return True

    def stats(self, owner_user_id: int) -> Dict[str, Any]:
        return self._repo.stats(_require_owner(owner_user_id))

    # ------------------------------------------------------------------ #
    # Memory
    # ------------------------------------------------------------------ #
    def list_memory(self, owner_user_id: int, chat_id: str) -> List[Dict[str, Any]]:
        owner = _require_owner(owner_user_id)
        settings = self._repo.get_settings(owner)
        rows = self._repo.list_memory(owner, chat_id, max(settings.memory_max_facts, 20))
        history = self._repo.count_messages(owner, chat_id)
        return [{"chat_id": normalize_chat_id(chat_id), "context_messages": history, **row}
                for row in rows]

    def remember(self, owner_user_id: int, chat_id: str, content: str,
                 kind: str = MemoryKind.FACT.value, ttl_seconds: int = 0) -> Dict[str, Any]:
        owner = _require_owner(owner_user_id)
        chat = normalize_chat_id(chat_id)
        text = str(content or "").strip()
        if not chat:
            raise AutoReplyError("chat_required")
        if not text:
            raise AutoReplyError("content_required")
        memory_id = self._repo.remember(owner, chat, text, kind=kind, ttl_seconds=ttl_seconds)
        return {"id": memory_id or "", "chat_id": chat, "content": text}

    def forget(self, owner_user_id: int, chat_id: str, memory_id: str = "") -> int:
        return self._repo.forget(_require_owner(owner_user_id), chat_id, memory_id)

    # ------------------------------------------------------------------ #
    # Preview (generate a reply without sending it)
    # ------------------------------------------------------------------ #
    async def preview(self, owner_user_id: int, sample_text: str,
                      prompt_id: str = "", ai_config_id: str = "") -> GenerationResult:
        owner = _require_owner(owner_user_id)
        sample = str(sample_text or "").strip()
        if not sample:
            raise AutoReplyError("sample_required")
        settings = self._repo.get_settings(owner)
        prompt = None
        wanted_prompt = prompt_id or settings.prompt_id
        if wanted_prompt:
            prompt = self._repo.get_prompt(wanted_prompt, owner)
            if prompt is None and prompt_id:
                # An explicitly named prompt must exist; the account default may
                # simply have been deleted, in which case the built-in is used.
                raise AutoReplyError("prompt_not_found")
        language = resolve_language(settings, prompt, None, sample)
        memory = []
        if settings.memory_enabled:
            memory = [r["content"] for r in self._repo.list_memory(owner, "preview", 3)]
        system_prompt = build_system_prompt(settings, prompt, None, memory, language, sample)
        template = prompt.user_prompt_template if prompt else "{text}"
        ctx = PromptContext(
            system_prompt=system_prompt,
            tone=prompt.tone.value if (prompt and prompt.tone) else settings.tone.value,
            response_length=prompt.response_length.value if (prompt and prompt.response_length)
            else settings.response_length.value,
            language=language,
            prompt_id=prompt.id if prompt else None,
            ai_config_id=ai_config_id or settings.ai_config_id,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            max_reply_chars=settings.max_reply_chars,
            user_template=template,
            history=[],
            memory=memory,
            inbound=sample,
        )
        cfg = None
        wanted_config = ai_config_id or settings.ai_config_id
        if wanted_config:
            if not self._owns_ai(wanted_config, owner):
                raise AutoReplyError("ai_config_not_found")
            cfg = self._ai.get_owned(wanted_config, owner) if self._ai else None
        if cfg is None:
            return GenerationResult(False, error_code="no_ai_config",
                      error="link an AI provider before previewing a reply")
        from ...infrastructure.ai.providers import AIProviderFactory, OpenAICompatibleProvider
        factory = AIProviderFactory()
        key = cfg.provider.value if hasattr(cfg.provider, "value") else str(cfg.provider)
        provider_cls = getattr(factory, "_registry", {}).get(key, OpenAICompatibleProvider)
        provider = provider_cls()
        started = time.time()
        try:
            messages = build_messages(system_prompt, [], sample, template)
            text = await provider.chat_complete(cfg, messages, temperature=settings.temperature)
            text = (text or "").strip()[: settings.max_reply_chars]
            return GenerationResult(True, text=text, latency_ms=int((time.time() - started) * 1000),
                      provider=key, model=str(getattr(cfg, "model", "")),
                      prompt_id=ctx.prompt_id or "")
        except Exception as exc:
            return GenerationResult(False, error=str(exc)[:300], error_code="provider_error",
                      latency_ms=int((time.time() - started) * 1000))


__all__ = ["AutoReplyError", "AutoReplyService", "EXPORT_SCHEMA_VERSION", "MAX_IMPORT_PROMPTS"]
