import pytest

from core.domain.entities import AIConfig, FilterRule, ForwardRule, MessagePayload
from core.domain.value_objects import AIProviderType, ForwardMode, RoutingType
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAIConfigRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.ai.providers import AIProviderFactory
from core.application.use_cases import (
    AIConfigUseCases,
    FilterRuleUseCases,
    ForwardRuleUseCases,
    MessageForwardingUseCase,
)


class FakeProvider:
    def __init__(self, out="REWRITTEN"):
        self.out = out

    async def rewrite(self, config, text):
        return f"{self.out}:{text[:10]}"

    async def health_check(self, config):
        return True


class FakeFactory:
    def get(self, config):
        return FakeProvider()


@pytest.fixture()
def pipeline_env():
    db = SqliteDatabase(":memory:")
    crypto = CryptoService("k")
    session_repo = SqliteSessionRepository(db, crypto)

    import anyio  # noqa: F401  (pytest-asyncio present)

    # Create a session synchronously via the repo's sqlite layer
    from core.domain.entities import TelegramSession
    s = TelegramSession(phone_number="+1000", session_string_encrypted="")
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id, username,"
        " first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES (?, ?, '', '', '', '', 1, 1, 0, 0)",
        (s.id, s.phone_number),
    )
    rule_repo = SqliteForwardRuleRepository(db)
    filter_repo = SqliteFilterRuleRepository(db)
    ai_repo = SqliteAIConfigRepository(db, crypto)
    factory = FakeFactory()
    uc = MessageForwardingUseCase(rule_repo, filter_repo, ai_repo, factory)
    return rule_repo, filter_repo, ai_repo, uc, s.id


@pytest.mark.asyncio
async def test_forward_happy_path_copy(pipeline_env):
    rule_repo, _, _, uc, session_id = pipeline_env
    rule = ForwardRule(session_id=session_id, source_chat_id="-1001", target_chat_id="-1002",
                       routing_type=RoutingType.CHANNEL_TO_CHANNEL,
                       forward_mode=ForwardMode.COPY_MESSAGE)
    await rule_repo.add(rule)

    sent = {}

    async def sender(payload, rule, text, evaluation):
        sent["target"] = rule.target_chat_id
        sent["text"] = text
        return True

    uc.sender = sender
    p = MessagePayload(message_id=1, chat_id="-1001", text="hello world")
    out = await uc.process_message(p)
    assert out.forwarded and sent["target"] == "-1002" and sent["text"] == "hello world"


@pytest.mark.asyncio
async def test_no_matching_rules(pipeline_env):
    _, _, _, uc, _ = pipeline_env
    p = MessagePayload(message_id=1, chat_id="-9999", text="x")
    out = await uc.process_message(p)
    assert not out.forwarded and out.reason == "no_matching_rules"


@pytest.mark.asyncio
async def test_filter_drops_before_send(pipeline_env):
    rule_repo, filter_repo, _, uc, session_id = pipeline_env
    fr = await filter_repo.add(FilterRule(name="blk", blacklist_keywords=["stop"]))
    await rule_repo.add(ForwardRule(session_id=session_id, source_chat_id="-1001",
                                    target_chat_id="-1002", filter_rule_id=fr.id))
    called = []

    async def sender(*args):
        called.append(1)
        return True

    uc.sender = sender
    out = await uc.process_message(MessagePayload(message_id=1, chat_id="-1001", text="STOP word"))
    assert not out.forwarded and not called


@pytest.mark.asyncio
async def test_ai_rewrite_applied(pipeline_env):
    rule_repo, _, ai_repo, uc, session_id = pipeline_env
    cfg = await ai_repo.add(AIConfig(name="c1", provider=AIProviderType.OPENAI, api_key="sk"))
    await rule_repo.add(ForwardRule(session_id=session_id, source_chat_id="-1001",
                                    target_chat_id="-1002", ai_config_id=cfg.id))
    sent = {}

    async def sender(payload, rule, text, evaluation):
        sent["text"] = text
        return True

    uc.sender = sender
    await uc.process_message(MessagePayload(message_id=1, chat_id="-1001", text="original text here"))
    assert sent["text"].startswith("REWRITTEN:")
    assert uc.stats.rewritten == 1


@pytest.mark.asyncio
async def test_remove_links_option(pipeline_env):
    rule_repo, _, _, uc, session_id = pipeline_env
    await rule_repo.add(ForwardRule(session_id=session_id, source_chat_id="-1001",
                                    target_chat_id="-1002", remove_links=True))
    sent = {}

    async def sender(payload, rule, text, evaluation):
        sent["text"] = text
        return True

    uc.sender = sender
    await uc.process_message(MessagePayload(message_id=1, chat_id="-1001",
                                            text="see https://spam.example.com now"))
    assert "spam.example.com" not in sent["text"]


@pytest.mark.asyncio
async def test_ai_failure_falls_back_to_original(pipeline_env):
    rule_repo, _, ai_repo, uc, session_id = pipeline_env

    class BrokenFactory:
        def get(self, config):
            class Broken:
                async def rewrite(self, c, t):
                    raise RuntimeError("provider down")

                async def health_check(self, c):
                    return False
            return Broken()

    from core.infrastructure.persistence.database import SqliteDatabase as _Db
    filter_repo = SqliteFilterRuleRepository(_Db(":memory:"))
    uc = MessageForwardingUseCase(rule_repo, filter_repo, ai_repo, BrokenFactory())
    cfg = await ai_repo.add(AIConfig(name="c", provider=AIProviderType.OPENAI, api_key="k"))
    await rule_repo.add(ForwardRule(session_id=session_id, source_chat_id="-1001",
                                    target_chat_id="-1002", ai_config_id=cfg.id))
    sent = {}

    async def sender(payload, rule, text, evaluation):
        sent["text"] = text
        return True

    uc.sender = sender
    await uc.process_message(MessagePayload(message_id=1, chat_id="-1001", text="keep me"))
    assert sent["text"] == "keep me"
