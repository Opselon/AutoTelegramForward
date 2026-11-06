"""The prompt library's enable/disable switch.

A disabled template stays in the library (preview and the tester keep
working) but the delivery path must behave exactly as if no template were
set: the task is rewritten with its AI config's own prompt. These tests pin
the service contract and the fallback decision the pipeline makes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from core.application.prompt_service import PromptService
from core.domain.entities import PromptTemplate


class FakePromptRepo:
    """The repository surface PromptService touches, in memory."""

    def __init__(self) -> None:
        self.templates: Dict[str, PromptTemplate] = {}
        self.versions: List[Any] = []

    async def add_template(self, template: PromptTemplate) -> PromptTemplate:
        self.templates[template.id] = template
        return template

    async def update_template(self, template: PromptTemplate) -> PromptTemplate:
        self.templates[template.id] = template
        return template

    async def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        return self.templates.get(template_id)

    async def list_templates(self) -> List[PromptTemplate]:
        return list(self.templates.values())

    async def list_templates_by_owner(self, owner_user_id: int) -> List[PromptTemplate]:
        return [
            t for t in self.templates.values()
            if t.is_system or t.owner_user_id == owner_user_id
        ]

    async def delete_template(self, template_id: str) -> bool:
        return self.templates.pop(template_id, None) is not None

    async def add_version(self, version: Any) -> Any:
        self.versions.append(version)
        return version

    async def get_version(self, prompt_id: str, version: int) -> Optional[Any]:
        for v in self.versions:
            if v.prompt_id == prompt_id and v.version == version:
                return v
        return None

    async def list_versions(self, prompt_id: str) -> List[Any]:
        return [v for v in self.versions if v.prompt_id == prompt_id]

    async def activate_version(self, prompt_id: str, version: int) -> bool:
        return True


@pytest.fixture
def service() -> PromptService:
    return PromptService(FakePromptRepo())


async def test_new_templates_start_enabled(service: PromptService):
    tpl = await service.create_template(
        name="T", system_prompt="Rewrite {text}".replace("{text}", "the text"),
        user_prompt_template="{text}", owner_user_id=7)
    assert tpl.enabled is True


async def test_disable_then_reenable_keeps_the_row(service: PromptService):
    tpl = await service.create_template(
        name="T", system_prompt="s", user_prompt_template="{text}", owner_user_id=7)
    off = await service.set_enabled(tpl.id, False)
    assert off.enabled is False
    # still in the library, versions untouched
    assert await service.get_template(tpl.id) is not None
    fresh = await service.set_enabled(tpl.id, True)
    assert fresh.enabled is True


async def test_preview_still_works_on_a_disabled_template(service: PromptService):
    """The tester is how an operator checks a prompt before re-enabling it."""
    tpl = await service.create_template(
        name="T", system_prompt="s", user_prompt_template="{text}", owner_user_id=7)
    await service.set_enabled(tpl.id, False)
    preview = await service.preview_prompt(tpl.id, "hello")
    assert preview["rendered_user_prompt"] == "hello"


async def test_set_enabled_refuses_an_unknown_template(service: PromptService):
    with pytest.raises(ValueError):
        await service.set_enabled("nope", False)


async def test_update_template_preserves_the_enabled_state(service: PromptService):
    """A content edit must never silently re-enable a parked prompt."""
    tpl = await service.create_template(
        name="T", system_prompt="s", user_prompt_template="{text}", owner_user_id=7)
    await service.set_enabled(tpl.id, False)
    await service.update_template(
        tpl.id, name="T2", system_prompt="s2", user_prompt_template="{text}",
        description="", expected_version=tpl.current_version)
    fresh = await service.get_template(tpl.id)
    assert fresh is not None and fresh.enabled is False


def test_the_entity_default_is_enabled():
    assert PromptTemplate().enabled is True
