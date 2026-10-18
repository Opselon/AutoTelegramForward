"""An endpoint is stored only if the core may be made to call it.

The probe checks a URL when a key is tested; a rewrite replays the stored URL on
every message, so the refusal has to sit at the write too — otherwise a URL that
was never probed (the bot's edit step, a direct API save, a restored backup)
would be dialled forever with no one to say no.

The Telegram wizard is covered too: it is the surface a tenant can reach from a
chat, and it must not be the one way around the guard.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.application.use_cases import AIConfigUseCases
from core.domain.entities import AIConfig
from core.domain.value_objects import AIProviderType
from core.infrastructure.ai.providers import endpoint_guard

# Imported at module scope on purpose: pyrogram resolves an event loop while
# being imported, and pytest-asyncio has already closed one once an async test
# in this file has run — a later import would fail with a RuntimeError instead of
# testing anything.
from core.infrastructure.telegram.pro_bot_ui import ProBotUI


class _FakeRepo:
    def __init__(self):
        self.saved: list[AIConfig] = []

    async def add(self, cfg: AIConfig) -> AIConfig:
        self.saved.append(cfg)
        return cfg

    async def update(self, cfg: AIConfig) -> AIConfig:
        self.saved.append(cfg)
        return cfg


def _cfg(base_url: str, provider: AIProviderType = AIProviderType.OPENAI) -> AIConfig:
    return AIConfig(
        name="tenant-config",
        provider=provider,
        model="gpt-4o-mini",
        base_url=base_url,
        api_key="sk-tenant",
        owner_user_id=5094837833,
    )


def _use_cases() -> tuple[AIConfigUseCases, _FakeRepo]:
    repo = _FakeRepo()
    return AIConfigUseCases(repo, provider_factory=None), repo


BLOCKED = [
    "http://localhost:11434/v1",
    "http://127.0.0.1:8080/v1",
    "http://169.254.169.254/latest/meta-data",
    "http://metadata.google.internal/computeMetadata/v1",
    "file:///etc/passwd",
    "ftp://example.com/models",
    "http://10.0.0.5:8000/v1",
    "http://192.168.1.50:1234/v1",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BLOCKED)
async def test_a_blocked_endpoint_is_never_stored(url):
    use_cases, repo = _use_cases()
    with pytest.raises(ValueError, match="endpoint refused"):
        await use_cases.create(_cfg(url))
    assert repo.saved == [], f"{url} reached the database"


@pytest.mark.asyncio
async def test_the_same_refusal_applies_to_an_update():
    use_cases, repo = _use_cases()
    with pytest.raises(ValueError, match="endpoint refused"):
        await use_cases.update(_cfg("http://169.254.169.254/"))
    assert repo.saved == []


@pytest.mark.asyncio
async def test_a_public_endpoint_is_stored():
    use_cases, repo = _use_cases()
    stored = await use_cases.create(_cfg("https://api.openai.com/v1"))
    assert stored.base_url == "https://api.openai.com/v1"
    assert len(repo.saved) == 1


@pytest.mark.asyncio
async def test_an_empty_endpoint_means_the_provider_default_and_is_kept():
    # "" is not a URL to validate: it tells the rewrite path to use the
    # provider's own host, which the operator configured, not the tenant.
    use_cases, repo = _use_cases()
    stored = await use_cases.create(_cfg(""))
    assert stored.base_url == ""
    assert len(repo.saved) == 1


@pytest.mark.asyncio
async def test_a_self_hosted_private_endpoint_is_refused_by_default():
    # The operator's escape hatch is a server-side env flag, deliberately not
    # something a tenant can set from a form.
    assert endpoint_guard("http://192.168.1.50:1234/v1") == "endpoint_blocked"


# --------------------------------------------------------------------------- #
# the Telegram wizard asks for the same URL — it must refuse the same way
# --------------------------------------------------------------------------- #

def _ui():
    ui = ProBotUI.__new__(ProBotUI)
    ui._state = MagicMock()
    ui._persist = AsyncMock()
    ui._t = lambda key, **kwargs: key
    ui._kbd = lambda rows: rows
    return ui


def test_the_guard_helper_accepts_a_public_url():
    assert _ui()._endpoint_problem("https://api.openai.com/v1") == ""
    assert _ui()._endpoint_problem("") == ""


def test_the_guard_helper_refuses_the_metadata_address():
    assert _ui()._endpoint_problem("http://169.254.169.254/latest/meta-data") == "endpoint_blocked"


@pytest.mark.asyncio
async def test_the_bot_url_step_stays_on_the_step_and_explains():
    ui = _ui()
    ui._lang_for = lambda st: "en"
    st = MagicMock()
    st.step = "ai_base_url"
    st.ai_provider = "openai"
    st.buffer = {}
    message = MagicMock()
    message.from_user.id = 5094837833
    message.reply_text = AsyncMock()

    await ui._step_ai_base_url(message, st, "http://127.0.0.1:8080/v1")

    assert st.step == "ai_base_url", "the wizard moved on with a refused URL"
    message.reply_text.assert_awaited()
    sent = message.reply_text.await_args.args[0]
    assert sent == "ui_ai_endpoint_blocked"


@pytest.mark.asyncio
async def test_the_bot_url_step_advances_on_an_acceptable_url():
    ui = _ui()
    ui._lang_for = lambda st: "en"
    ui._model_picker = lambda st, provider: None
    st = MagicMock()
    st.ai_provider = "openai"
    st.buffer = {}
    message = MagicMock()
    message.from_user.id = 5094837833
    message.reply_text = AsyncMock()

    await ui._step_ai_base_url(message, st, "https://api.openai.com/v1")

    assert st.step == "ai_model"
    assert st.ai_base_url == "https://api.openai.com/v1"
