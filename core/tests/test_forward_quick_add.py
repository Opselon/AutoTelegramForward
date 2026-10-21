"""`/forward add` — the one-line task command, end to end.

Three layers are pinned here, each at its own boundary:

* the **parser** reads the documented grammar (label, two lists, optional
  filters) and refuses ambiguity with a code the surface can localize;
* the **application service** is the only place that decides whether a task may
  exist — ownership, duplicate labels, resolved ids, per-task filters;
* the **handler** resolves peers through the same resolver the wizard uses,
  writes through a real SQLite store, and answers with the created task.

The last test is the tenant boundary: two users may use the same label, and
neither one's task or filters are visible to the other.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.application.quick_add import (
    QuickAddError,
    QuickAddRequest,
    QuickAddService,
    TaskFilters,
)
from core.application.use_cases import ForwardRuleUseCases, SessionUseCases
from core.domain.entities import TelegramSession
from core.domain.value_objects import ChannelReference
from core.infrastructure.telegram.quick_add import QuickAddParseError, parse_quick_add
from core.tests.bot_dispatch import BotHarness

OWNER = 5094837833
OTHER = 7711223344
SRC = "-1003333333333"
SRC2 = "-1004444444444"
TGT = "-1005555555555"
TGT2 = "-1006666666666"


# --------------------------------------------------------------------------- #
# Tiny in-memory ports: the service is exercised without SQL
# --------------------------------------------------------------------------- #


class _RuleRepo:
    def __init__(self) -> None:
        self.rows: dict = {}

    async def add(self, rule):
        self.rows[rule.id] = rule
        return rule

    async def list_by_owner(self, owner_user_id, limit=None, offset=0):
        return [r for r in self.rows.values() if int(getattr(r, "owner_user_id", 0)) == int(owner_user_id)]

    async def get_by_id(self, rule_id):
        return self.rows.get(rule_id)


class _SessionRepo:
    def __init__(self, sessions) -> None:
        self.rows = {s.id: s for s in sessions}

    async def list_by_owner(self, owner_user_id):
        return [s for s in self.rows.values() if int(getattr(s, "owner_user_id", 0)) == int(owner_user_id)]

    async def get_by_id(self, session_id):
        return self.rows.get(session_id)

    async def get_by_phone(self, phone):
        for s in self.rows.values():
            if s.phone_number == phone:
                return s
        return None

    async def add(self, session):
        self.rows[session.id] = session
        return session


def _service(*, sessions=None):
    sessions = sessions if sessions is not None else [
        TelegramSession(id="sess-a", phone_number="+100", is_active=True,
                        is_authorized=True, owner_user_id=OWNER),
        TelegramSession(id="sess-b", phone_number="+200", is_active=True,
                        is_authorized=True, owner_user_id=OTHER),
    ]
    rules = _RuleRepo()
    repo = _SessionRepo(sessions)
    service = QuickAddService(
        ForwardRuleUseCases(rules), SessionUseCases(repo))
    return service, rules


def _request(label="news_feed", sources=(SRC,), targets=(TGT,), filters=None):
    return QuickAddRequest(
        label=label,
        sources=[ChannelReference(chat_id=c, title=f"title {c}") for c in sources],
        targets=[ChannelReference(chat_id=c, title=f"title {c}") for c in targets],
        filters=filters or TaskFilters(),
    )


# --------------------------------------------------------------------------- #
# Grammar
# --------------------------------------------------------------------------- #


def test_the_documented_line_reads_label_lists_and_no_filters():
    cmd = parse_quick_add(f"/forward add news_feed {SRC},{SRC2} -> {TGT}, {TGT2}")

    assert cmd.label == "news_feed"
    assert cmd.sources == [SRC, SRC2]
    assert cmd.targets == [TGT, TGT2]
    assert cmd.filters.is_empty


def test_the_unicode_arrow_and_persian_comma_are_accepted():
    cmd = parse_quick_add(f"/forward add work-alerts @news\u060c@src -> @dst")

    assert cmd.label == "work-alerts"
    assert cmd.sources == ["@news", "@src"]
    assert cmd.targets == ["@dst"]


def test_per_task_filters_are_typed_and_kept_together():
    cmd = parse_quick_add(
        f"/forward add news {SRC} -> {TGT} "
        "--only=media --block=links,text --keywords=btc,eth --deny=spam --delay=5 --every=3"
    )

    assert cmd.filters.only == "media"
    assert cmd.filters.block == ("links", "text")
    assert cmd.filters.keywords == ("btc", "eth")
    assert cmd.filters.deny == ("spam",)
    assert cmd.filters.delay == 5
    assert cmd.filters.every == 3


def test_a_quoted_label_is_unwrapped_before_validation():
    cmd = parse_quick_add(f'/forward add "news_feed" {SRC} -> {TGT}')
    assert cmd.label == "news_feed"


@pytest.mark.parametrize("text,code", [
    ("/forward add news_feed -1001", "missing_arrow"),
    ("/forward add -> -1002", "missing_label"),
    ("/forward add news feed -1001 -> -1002", "bad_label"),
    ("/forward add news@feed -1001 -> -1002", "bad_label"),
    ("/forward add " + "x" * 51 + " -1001 -> -1002", "bad_label"),
    (f"/forward add news {SRC} -> {TGT} --unknown=1", "unknown_filter"),
    (f"/forward add news {SRC} -> {TGT} --only=video", "bad_filter_value"),
    (f"/forward add news {SRC} -> {TGT} --block=text --block=links", "duplicate_filter"),
    (f"/forward add news {SRC} -> {TGT} --delay=0", "bad_filter_value"),
    ("/forward add news nope -> -1002", "bad_label"),
    (f"/forward add news badword,second -> {TGT}", "bad_label"),
    (f"/forward add news {SRC} -> {TGT} --only", "bad_filter_value"),
])
def test_bad_input_is_refused_with_a_code(text, code):
    with pytest.raises(QuickAddParseError) as caught:
        parse_quick_add(text)

    assert caught.value.code == code


# --------------------------------------------------------------------------- #
# Application rules
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_creating_a_task_writes_both_lists_owner_and_filters():
    service, rules = _service()
    filters = TaskFilters(
        block=("links", "voice"), only="text", keywords=("btc",),
        deny=("spam",), delay=7, every=4,
    )

    rule = await service.create(OWNER, _request(filters=filters))

    stored = rules.rows[rule.id]
    assert stored.owner_user_id == OWNER
    assert stored.session_id == "sess-a"
    assert stored.source_chat_ids == [SRC]
    assert stored.target_chat_ids == [TGT]
    assert stored.block_links is True and stored.block_voice is True
    assert stored.block_text is False
    assert stored.media_handling == "TEXT_ONLY"
    assert stored.text_contains == ["btc"]
    assert stored.blacklist_words == ["spam"]
    assert stored.forward_delay_seconds == 7.0
    assert stored.forward_every_n == 4


@pytest.mark.asyncio
async def test_filters_belong_to_one_task_only():
    service, rules = _service()
    filtered = await service.create(OWNER, _request(label="filtered", filters=TaskFilters(block=("links",))))
    plain = await service.create(OWNER, _request(label="plain"))

    assert rules.rows[filtered.id].block_links is True
    assert rules.rows[plain.id].block_links is False
    assert rules.rows[plain.id].media_handling == "AUTO"


@pytest.mark.asyncio
async def test_a_duplicate_label_on_the_same_account_is_refused():
    service, rules = _service()
    await service.create(OWNER, _request(label="News_Feed"))

    with pytest.raises(QuickAddError) as caught:
        await service.create(OWNER, _request(label="news_feed"))

    assert caught.value.code == "duplicate_label"
    assert len(rules.rows) == 1


@pytest.mark.asyncio
async def test_two_users_may_each_own_the_same_label_without_seeing_each_other():
    service, rules = _service()
    mine = await service.create(OWNER, _request(label="alerts", filters=TaskFilters(block=("links",))))
    theirs = await service.create(OTHER, _request(label="alerts"))

    assert mine.owner_user_id == OWNER and theirs.owner_user_id == OTHER
    assert mine.id != theirs.id
    assert rules.rows[theirs.id].block_links is False

    duplicate = await service.find_by_label(OWNER, "alerts")
    assert duplicate.id == mine.id
    assert await service.find_by_label(OTHER, "alerts") is not None


@pytest.mark.asyncio
async def test_no_session_means_no_task():
    service, rules = _service(sessions=[])
    with pytest.raises(QuickAddError) as caught:
        await service.create(OWNER, _request())
    assert caught.value.code == "no_session"
    assert rules.rows == {}


@pytest.mark.asyncio
async def test_an_anonymous_owner_fails_closed():
    service, _rules = _service()
    with pytest.raises(QuickAddError) as caught:
        await service.create(0, _request())
    assert caught.value.code == "no_owner"


@pytest.mark.asyncio
async def test_a_chat_on_both_sides_is_refused():
    service, rules = _service()
    with pytest.raises(QuickAddError) as caught:
        await service.create(OWNER, _request(sources=(SRC,), targets=(SRC,)))
    assert caught.value.code == "overlap"
    assert rules.rows == {}


# --------------------------------------------------------------------------- #
# The handler, a real database and the tenant boundary
# --------------------------------------------------------------------------- #


class _Message:
    def __init__(self, uid: int, text: str) -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.text = text
        self.sent: list[str] = []

    async def reply_text(self, text=None, **kwargs):
        self.sent.append(str(text or ""))


@pytest.fixture()
def harness(tmp_path):
    # Pyrogram's import-time `async_to_sync` needs a loop on this thread; the
    # harness imports Pyrogram through ProBotUI, so create one before it does.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    h = BotHarness(str(tmp_path / "quick-add.db"))
    try:
        yield h
    finally:
        h.close()


@pytest.mark.asyncio
async def test_the_command_persists_a_task_and_names_it_back(harness):
    await harness.seed_session("sess-a", OWNER)
    harness.ui._resolve_chat_token = _resolver()
    message = _Message(OWNER, f"/forward add news_feed {SRC},{SRC2} -> {TGT} --block=links --keywords=btc")

    await harness.ui._handle_forward_command(message, OWNER)

    rows = await harness.rules_repo.list_by_owner(OWNER)
    assert len(rows) == 1
    rule = rows[0]
    assert rule.name == "news_feed"
    assert rule.source_chat_ids == [SRC, SRC2]
    assert rule.target_chat_ids == [TGT]
    assert rule.block_links is True
    assert rule.text_contains == ["btc"]
    assert rule.id[:8] in message.sent[-1]
    assert SRC in message.sent[-1] and TGT in message.sent[-1]


@pytest.mark.asyncio
async def test_the_same_label_twice_creates_one_task_and_explains_the_refusal(harness):
    await harness.seed_session("sess-a", OWNER)
    harness.ui._resolve_chat_token = _resolver()

    await harness.ui._handle_forward_command(
        _Message(OWNER, f"/forward add news {SRC} -> {TGT}"), OWNER)
    second = _Message(OWNER, f"/forward add news {SRC2} -> {TGT2}")
    await harness.ui._handle_forward_command(second, OWNER)

    assert len(await harness.rules_repo.list_by_owner(OWNER)) == 1
    assert "news" in second.sent[-1]


@pytest.mark.asyncio
async def test_a_second_tenant_cannot_see_or_collide_with_the_first_task(harness):
    await harness.seed_session("sess-a", OWNER)
    await harness.seed_session("sess-b", OTHER)
    harness.ui._resolve_chat_token = _resolver()

    await harness.ui._handle_forward_command(
        _Message(OWNER, f"/forward add alerts {SRC} -> {TGT}"), OWNER)
    await harness.ui._handle_forward_command(
        _Message(OTHER, f"/forward add alerts {SRC2} -> {TGT2}"), OTHER)

    mine = await harness.rules_repo.list_by_owner(OWNER)
    theirs = await harness.rules_repo.list_by_owner(OTHER)
    assert [r.id for r in mine] != [r.id for r in theirs]
    assert mine[0].source_chat_ids == [SRC]
    assert theirs[0].source_chat_ids == [SRC2]
    assert harness.rule_count(OWNER) == 1 and harness.rule_count(OTHER) == 1


@pytest.mark.asyncio
async def test_an_unresolvable_chat_creates_nothing_and_is_named(harness):
    await harness.seed_session("sess-a", OWNER)

    async def _resolve(uid, st, token):
        if "bad" in str(token):
            return str(token), "", "failed"
        return SRC if "src" in str(token) else TGT, "title", "resolved"

    harness.ui._resolve_chat_token = _resolve
    message = _Message(OWNER, f"/forward add news @badchan -> {TGT}")

    await harness.ui._handle_forward_command(message, OWNER)

    assert await harness.rules_repo.list_by_owner(OWNER) == []
    assert "@badchan" in message.sent[-1]


def _resolver():
    table = {
        SRC: "-1003333333333", SRC2: "-1004444444444",
        TGT: "-1005555555555", TGT2: "-1006666666666",
    }

    async def _resolve(uid, st, token):
        key = str(token).strip()
        if key in table:
            return table[key], f"Title {table[key]}", "resolved"
        return key, "", "failed"

    return _resolve
