import pytest

from core.domain.entities import (
    AIConfig,
    FilterRule,
    ForwardRule,
    MessagePayload,
    TelegramSession,
)
from core.domain.value_objects import (
    AIProviderType,
    ForwardMode,
    MediaType,
    RoutingType,
)
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAIConfigRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService


@pytest.fixture()
def db():
    d = SqliteDatabase(":memory:")
    yield d
    d.close()


@pytest.fixture()
def crypto():
    return CryptoService("test-master-key")


@pytest.mark.asyncio
async def test_session_roundtrip_encrypted(db, crypto):
    repo = SqliteSessionRepository(db, crypto)
    s = TelegramSession(phone_number="+989123456789", session_string_encrypted="SESSION_SECRET")
    await repo.add(s)
    got = await repo.get_by_phone("+989123456789")
    assert got is not None
    assert got.session_string_encrypted == "SESSION_SECRET"
    raw = db.query_one("SELECT session_string_encrypted FROM sessions WHERE id=?", (s.id,))
    assert raw["session_string_encrypted"] != "SESSION_SECRET"  # stored encrypted


@pytest.mark.asyncio
async def test_session_update_and_delete(db, crypto):
    repo = SqliteSessionRepository(db, crypto)
    s = await repo.add(TelegramSession(phone_number="+10001112223"))
    s.activate()
    await repo.update(s)
    got = await repo.get_by_id(s.id)
    assert got.is_authorized and got.is_active
    assert await repo.delete(s.id) is True
    assert await repo.get_by_id(s.id) is None


@pytest.mark.asyncio
async def test_forward_rule_active_by_source(db, crypto):
    s_repo = SqliteSessionRepository(db, crypto)
    await s_repo.add(TelegramSession(phone_number="+1000", session_string_encrypted=""))
    repo = SqliteForwardRuleRepository(db)
    r = ForwardRule(
        session_id=s_repo and (await s_repo.list_all())[0].id,
        source_chat_id="-100111",
        target_chat_id="-100222",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
    )
    await repo.add(r)
    active = await repo.list_active_by_source("-100111")
    assert len(active) == 1 and active[0].id == r.id
    r.is_active = False
    await repo.update(r)
    assert await repo.list_active_by_source("-100111") == []


@pytest.mark.asyncio
async def test_filter_rule_roundtrip(db):
    repo = SqliteFilterRuleRepository(db)
    fr = FilterRule(name="f1", whitelist_keywords=["a"], blacklist_keywords=["b"],
                    regex_patterns=["\\d+"], allowed_media_types=["photo"],
                    blocked_media_types=["video"], min_message_length=2, max_message_length=9)
    await repo.add(fr)
    got = await repo.get_by_id(fr.id)
    assert got.whitelist_keywords == ["a"] and got.max_message_length == 9
    got.blacklist_keywords = []
    await repo.update(got)
    assert (await repo.get_by_id(fr.id)).blacklist_keywords == []


@pytest.mark.asyncio
async def test_ai_config_api_key_encrypted(db, crypto):
    repo = SqliteAIConfigRepository(db, crypto)
    c = AIConfig(name="gpt", provider=AIProviderType.OPENAI,
                 model="gpt-4o-mini", api_key="sk-secret-abc")
    await repo.add(c)
    got = await repo.get_by_id(c.id)
    assert got.api_key == "sk-secret-abc"
    raw = db.query_one("SELECT api_key_encrypted FROM ai_configs WHERE id=?", (c.id,))
    assert raw["api_key_encrypted"] != "sk-secret-abc"


@pytest.mark.asyncio
async def test_ai_roundtrip_and_delete(db, crypto):
    repo = SqliteAIConfigRepository(db, crypto)
    c = await repo.add(AIConfig(name="groq1", provider=AIProviderType.GROQ))
    assert await repo.get_by_id(c.id) is not None
    assert await repo.delete(c.id) is True
    assert await repo.get_by_id(c.id) is None
