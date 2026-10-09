"""Prompt Management Service.

Provides complete lifecycle management for Prompt Templates and historic versions:
1. System vs custom prompt classification.
2. Optimistic Concurrency Control (OCC) during edits.
3. Linear, immutable version history and safe rollback.
4. Prompt validation, preview, and test execution.
5. Auto-seeding of production-grade default system prompts.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ..domain.entities import PromptTemplate, PromptVersion
from .repositories import ConcurrencyError, IPromptRepository

logger = logging.getLogger(__name__)

# Built-in production system prompts
DEFAULT_SYSTEM_PROMPTS = [
    {
        "id": "sys_pro_clean",
        "name": "Professional Clean",
        "description": "Removes channel promotional text, ads, clarifies grammar and tone.",
        "system_prompt": (
            "You are an expert editor. Clean and polish the following Telegram message:\n"
            "- Remove promotional spam, channel mentions (@channel), and ad links.\n"
            "- Maintain professional, engaging tone.\n"
            "- Preserve core factual meaning, metrics, and genuine links."
        ),
        "user_prompt_template": "{text}",
        "target_language": "en",
        "is_system": True,
    },
    {
        "id": "sys_translate_fa_en",
        "name": "Persian <-> English Translator",
        "description": "Translates Persian messages to English or vice versa with contextual fluency.",
        "system_prompt": (
            "You are a professional translator specializing in Telegram channel content.\n"
            "If the message is primarily Persian, translate it into natural, fluent English.\n"
            "If the message is primarily English, translate it into natural, fluent Persian.\n"
            "Keep technical terms and financial tickers intact."
        ),
        "user_prompt_template": "{text}",
        "target_language": "auto",
        "is_system": True,
    },
    {
        "id": "sys_summarizer",
        "name": "Executive Summarizer",
        "description": "Summarizes long messages into concise bullet points with key takeaways.",
        "system_prompt": (
            "You are an executive intelligence briefer.\n"
            "Summarize the provided message concisely:\n"
            "- 1 headline line with relevant emoji.\n"
            "- 2-3 key bullet points summarizing essential facts.\n"
            "- 1 concluding takeaway line."
        ),
        "user_prompt_template": "{text}",
        "target_language": "en",
        "is_system": True,
    },
    {
        "id": "sys_format_enhancer",
        "name": "Telegram Format Enhancer",
        "description": "Optimizes message visual structure using bold headers and clean spacing.",
        "system_prompt": (
            "Enhance the readability and visual layout of the Telegram message:\n"
            "- Add clean bold section headers.\n"
            "- Format lists with clean bullet points.\n"
            "- Ensure comfortable spacing and paragraph breaks."
        ),
        "user_prompt_template": "{text}",
        "target_language": "en",
        "is_system": True,
    },
]


class PromptService:
    """Manages prompt templates, audit history, OCC, validation, preview, and testing."""

    def __init__(self, prompt_repo: IPromptRepository) -> None:
        self.prompt_repo = prompt_repo

    async def init_defaults(self) -> None:
        """Seeds built-in system prompt templates if not already present."""
        for p in DEFAULT_SYSTEM_PROMPTS:
            existing = await self.prompt_repo.get_template(p["id"])
            if not existing:
                now = int(time.time())
                template = PromptTemplate(
                    id=p["id"],
                    name=p["name"],
                    description=p["description"],
                    system_prompt=p["system_prompt"],
                    user_prompt_template=p["user_prompt_template"],
                    target_language=p["target_language"],
                    current_version=1,
                    is_system=p["is_system"],
                    created_at=now,
                    updated_at=now,
                )
                await self.prompt_repo.add_template(template)
                # Seed version 1
                version = PromptVersion(
                    prompt_id=p["id"],
                    version=1,
                    system_prompt=p["system_prompt"],
                    user_prompt_template=p["user_prompt_template"],
                    change_summary="Initial system default version",
                    is_active=True,
                    created_at=now,
                )
                await self.prompt_repo.add_version(version)
                logger.info("Seeded default prompt template: %s", p["name"])

    # -------------------------------------------------------------------------
    # Validation & Preview
    # -------------------------------------------------------------------------

    @staticmethod
    def validate_prompt(system_prompt: str, user_template: str) -> Tuple[bool, str]:
        """Validates prompt text, placeholders, and basic constraints."""
        if not system_prompt.strip():
            return False, "System prompt cannot be empty."
        if len(system_prompt) > 8000:
            return False, f"System prompt exceeds maximum length (8000 chars, got {len(system_prompt)})."

        if not user_template.strip():
            return False, "User prompt template cannot be empty."
        if "{text}" not in user_template:
            return False, "User prompt template must contain '{text}' placeholder."
        if len(user_template) > 4000:
            return False, f"User prompt template exceeds maximum length (4000 chars, got {len(user_template)})."

        # Validate that template doesn't have unclosed curly braces other than {text}
        braces = re.findall(r"\{([^}]+)\}", user_template)
        for b in braces:
            if b != "text":
                return False, f"Unrecognized template placeholder '{{{b}}}'. Only '{{text}}' is permitted."

        return True, ""

    async def preview_prompt(
        self,
        template_id: str,
        sample_text: str,
        version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Renders prompt with sample text for admin inspection without executing LLM."""
        template = await self.prompt_repo.get_template(template_id)
        if not template:
            raise ValueError(f"Prompt template '{template_id}' not found.")

        sys_prompt = template.system_prompt
        user_tpl = template.user_prompt_template
        ver = template.current_version

        if version and version != template.current_version:
            v_obj = await self.prompt_repo.get_version(template_id, version)
            if v_obj:
                sys_prompt = v_obj.system_prompt
                user_tpl = v_obj.user_prompt_template
                ver = v_obj.version

        if "{text}" in user_tpl:
            rendered_user = user_tpl.replace("{text}", sample_text)
        else:
            rendered_user = f"{user_tpl}\n\n{sample_text}"

        return {
            "template_id": template_id,
            "version": ver,
            "name": template.name,
            "system_prompt": sys_prompt,
            "rendered_user_prompt": rendered_user,
            "target_language": template.target_language,
        }

    async def test_prompt(
        self,
        template_id: str,
        sample_text: str,
        config_id: str,
        transformer: Any,
        version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Executes a test transformation against a real/mock AI config and returns full diagnostics."""
        from ..infrastructure.ai.transformer import AIRequest

        preview = await self.preview_prompt(template_id, sample_text, version=version)
        req = AIRequest(
            text=sample_text,
            system_prompt=preview["system_prompt"],
            user_template=preview["rendered_user_prompt"],
            prompt_id=template_id,
            prompt_version=preview["version"],
            target_language=preview["target_language"],
            timeout_seconds=15.0,
        )

        resp = await transformer.transform(req, primary_config_id=config_id)
        return {
            "success": resp.success,
            "transformed_text": resp.transformed_text,
            "error_message": resp.error_message,
            "error_code": resp.error_code,
            "execution_duration_ms": round(resp.execution_duration_ms, 2),
            "provider_used": resp.provider_used,
            "model_used": resp.model_used,
            "prompt_version_used": resp.prompt_version_used,
            "circuit_state": resp.circuit_state,
        }

    # -------------------------------------------------------------------------
    # Template CRUD & Optimistic Concurrency Control (OCC)
    # -------------------------------------------------------------------------

    async def create_template(
        self,
        name: str,
        system_prompt: str,
        user_prompt_template: str = "{text}",
        description: str = "",
        target_language: str = "en",
        template_id: Optional[str] = None,
    ) -> PromptTemplate:
        valid, err = self.validate_prompt(system_prompt, user_prompt_template)
        if not valid:
            raise ValueError(f"Validation failed: {err}")

        now = int(time.time())
        template = PromptTemplate(
            id=template_id or "",
            name=name.strip(),
            description=description.strip(),
            system_prompt=system_prompt.strip(),
            user_prompt_template=user_prompt_template.strip(),
            target_language=target_language.strip() or "en",
            current_version=1,
            is_system=False,
            created_at=now,
            updated_at=now,
        )
        await self.prompt_repo.add_template(template)

        # Create version 1
        version = PromptVersion(
            prompt_id=template.id,
            version=1,
            system_prompt=template.system_prompt,
            user_prompt_template=template.user_prompt_template,
            change_summary="Initial creation",
            is_active=True,
            created_at=now,
        )
        await self.prompt_repo.add_version(version)
        return template

    async def update_template(
        self,
        template_id: str,
        name: str,
        system_prompt: str,
        user_prompt_template: str = "{text}",
        description: str = "",
        target_language: str = "en",
        change_summary: str = "",
        expected_version: Optional[int] = None,
    ) -> PromptTemplate:
        valid, err = self.validate_prompt(system_prompt, user_prompt_template)
        if not valid:
            raise ValueError(f"Validation failed: {err}")

        template = await self.prompt_repo.get_template(template_id)
        if not template:
            raise ValueError(f"Prompt template '{template_id}' not found.")

        # OCC check
        if expected_version is not None and template.current_version != expected_version:
            raise ConcurrencyError(
                f"Conflict: Prompt was updated to v{template.current_version} by another operation. "
                f"Expected v{expected_version}."
            )

        new_version = template.current_version + 1
        now = int(time.time())

        # Create new historical version
        version = PromptVersion(
            prompt_id=template.id,
            version=new_version,
            system_prompt=system_prompt.strip(),
            user_prompt_template=user_prompt_template.strip(),
            change_summary=change_summary.strip() or f"Updated to version {new_version}",
            is_active=True,
            created_at=now,
        )
        await self.prompt_repo.add_version(version)

        # Update template aggregate
        template.name = name.strip()
        template.description = description.strip()
        template.system_prompt = system_prompt.strip()
        template.user_prompt_template = user_prompt_template.strip()
        template.target_language = target_language.strip() or "en"
        template.current_version = new_version
        template.updated_at = now

        await self.prompt_repo.update_template(template)
        await self.prompt_repo.activate_version(template.id, new_version)
        return template

    async def rollback(
        self,
        template_id: str,
        target_version: int,
    ) -> PromptTemplate:
        """Rolls back to a target version by creating a new version to preserve linear audit history."""
        target_ver_obj = await self.prompt_repo.get_version(template_id, target_version)
        if not target_ver_obj:
            raise ValueError(f"Version {target_version} for template '{template_id}' not found.")

        template = await self.prompt_repo.get_template(template_id)
        if not template:
            raise ValueError(f"Template '{template_id}' not found.")

        return await self.update_template(
            template_id=template_id,
            name=template.name,
            system_prompt=target_ver_obj.system_prompt,
            user_prompt_template=target_ver_obj.user_prompt_template,
            description=template.description,
            target_language=template.target_language,
            change_summary=f"Rolled back to v{target_version}",
            expected_version=template.current_version,
        )

    async def activate_version(self, template_id: str, version: int) -> bool:
        """Activates a specific version."""
        return await self.prompt_repo.activate_version(template_id, version)

    async def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        return await self.prompt_repo.get_template(template_id)

    async def list_templates(self) -> List[PromptTemplate]:
        return await self.prompt_repo.list_templates()

    async def list_versions(self, template_id: str) -> List[PromptVersion]:
        return await self.prompt_repo.list_versions(template_id)

    async def get_version(self, template_id: str, version: int) -> Optional[PromptVersion]:
        return await self.prompt_repo.get_version(template_id, version)

    async def delete_template(self, template_id: str) -> bool:
        template = await self.prompt_repo.get_template(template_id)
        if not template:
            return False
        if template.is_system:
            raise ValueError("System default prompt templates cannot be deleted.")
        return await self.prompt_repo.delete_template(template_id)
