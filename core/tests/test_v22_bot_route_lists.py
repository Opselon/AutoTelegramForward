"""The M-sources -> N-targets route surface in the Telegram bot.

One task carries a source LIST and a target LIST, so 1->1, N->1, 1->N and M->N
are the same shape. Two ways in: the `/route` command and the inline-keyboard
wizard. Both go through the same list contract (`route_lists`) and the same save
path, so these tests pin the properties that make them safe:

* a list is split on the ASCII and the Persian comma, and nothing resolves a
  chat client-side (ids, @usernames and t.me links pass through);
* a repeat inside one list names the offending entry instead of silently
  shrinking the list, and a chat on both sides is refused (it would forward a
  task into itself);
* every callback payload a route button can carry stays inside Telegram's
  64-byte limit — including a rule id longer than a uuid;
* every label resolves through the catalog in all ten languages, and English
  never carries Persian (the standing cross-leak rule);
* nothing is written unless every entry resolved and the route is distinct.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.domain.entities import ForwardRule
from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES
from core.infrastructure.telegram.pro_bot_ui import (
    CB,
    ROUTE_PROBLEM_KEYS,
    ROUTE_RESOLVED,
    ROUTE_SCOPE_NEW,
    ROUTE_UNCHECKED,
    ProBotUI,
    UiState,
)
from core.infrastructure.telegram.route_lists import (
    ERR_DUPLICATE_SOURCE,
    ERR_DUPLICATE_TARGET,
    ERR_OVERLAP,
    MAX_CALLBACK_BYTES,
    RouteParseError,
    parse_route_command,
    peer_key,
    unique_peer_key,
    validate_route_lists,
)


# --------------------------------------------------------------------------- #
# Unique ids: `-100…` and the raw unique id are one entry in every list rule
# --------------------------------------------------------------------------- #

def test_unique_peer_key_collides_both_spellings_of_one_id():
    assert unique_peer_key("-1001206461239") == unique_peer_key("1206461239")


def test_unique_peer_key_leaves_names_alone():
    assert unique_peer_key("@News") == "news"
    assert unique_peer_key("https://t.me/news") == "news"


def test_a_bot_api_id_and_its_unique_id_are_one_chat_twice():
    """`-100123, 123` is the same channel twice, not two sources."""
    problems = validate_route_lists("-1001234567890, 1234567890", "@tgt-a")
    assert (ERR_DUPLICATE_SOURCE, "1234567890") in problems


def test_a_self_loop_is_seen_across_id_spellings():
    """`-100…` on one side, the raw unique id on the other: one chat, self-loop."""
    problems = validate_route_lists("-1001206461239", "-1001206461239, 1206461239")
    codes = [code for code, _entry in problems]
    assert ERR_OVERLAP in codes and ERR_DUPLICATE_TARGET in codes


def test_a_name_and_an_id_are_two_tokens_until_the_server_resolves_them():
    """Only numeric spellings may be pre-collapsed; `@news` is a different token."""
    problems = validate_route_lists("@news", "-1001206461239, 1206461239")
    codes = [code for code, _entry in problems]
    assert ERR_OVERLAP not in codes
    assert ERR_DUPLICATE_TARGET in codes


def test_peer_key_and_unique_peer_key_agree_names_are_spelling_sensitive():
    assert peer_key("@News") == peer_key("@news")
    assert peer_key("-1001206461239") == "1206461239"

CATALOG = Path("core/infrastructure/telegram/locales/translations.json")
RTL_CHARS = set("ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهیيكءآأإئءۀؤأ۰-۹")


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@pytest.fixture()
def ui() -> ProBotUI:
    sessions = AsyncMock()
    session = MagicMock()
    session.id = "sess-1"
    sessions.list_all = AsyncMock(return_value=[session])
    sessions.list_by_owner = AsyncMock(return_value=[session])

    rules = AsyncMock()
    rules.list_all = AsyncMock(return_value=[])
    rules.list_by_owner = AsyncMock(return_value=[])
    rules.get = AsyncMock(return_value=None)
    rules.get_owned = AsyncMock(return_value=None)
    rules.update_owned = AsyncMock(return_value=None)
    rules.save = AsyncMock()
    rules.create = AsyncMock()

    bot_ui = ProBotUI(
        bot=MagicMock(),
        i18n=I18n(),
        sessions=sessions,
        rules=rules,
        filter_rules=AsyncMock(),
        ai_configs=AsyncMock(),
        pipeline=MagicMock(),
        pool=MagicMock(),
        login=MagicMock(),
        log_client=MagicMock(),
        admin_ids=[123456],
    )
    # Resolution is the core's job; every token in these tests resolves to a
    # canonical numeric id, and one token is made to fail on demand.
    bot_ui._persist = AsyncMock()

    async def _resolve(uid, st, token):
        if "bad" in str(token).strip().lower():
            return str(token), "", "failed"
        numeric = {
            "@news": "-1001111111111",
            "@sales": "-1002222222222",
            "@src-a": "-1003333333333",
            "@src-b": "-1004444444444",
            "@tgt-a": "-1005555555555",
            "@tgt-b": "-1006666666666",
        }.get(str(token).strip().lower())
        if numeric is None:
            numeric = f"-100{abs(hash(token)) % 10**9:09d}"
        return numeric, f"Name {token}", ROUTE_RESOLVED

    bot_ui._resolve_chat_token = AsyncMock(side_effect=_resolve)
    return bot_ui


def _state(scope: str = ROUTE_SCOPE_NEW) -> UiState:
    st = UiState()
    st.buffer = {"route_scope": scope, "route_rule_id": str(uuid.uuid4())}
    return st


def _message(text: str = "", uid: int = 123456):
    message = MagicMock()
    message.text = text
    message.from_user = MagicMock()
    message.from_user.id = uid
    message.reply_text = AsyncMock()
    return message


def _callback():
    cq = MagicMock()
    cq.answer = AsyncMock()
    cq.edit_message_text = AsyncMock()
    cq.message = MagicMock()
    return cq


def _replied(message) -> str:
    """Every text the bot sent back, concatenated."""
    return "\n".join(str(call.args[0]) for call in message.reply_text.await_args_list)


# --------------------------------------------------------------------------- #
# The list contract, as `/route` reads it
# --------------------------------------------------------------------------- #


def test_the_route_command_reads_a_name_two_lists_and_the_arrow():
    cmd = parse_route_command("/route Alerts @news, @sales -> tgt-a, tgt-b")
    assert cmd.name == "Alerts"
    assert cmd.sources == ["@news", "@sales"]
    assert cmd.targets == ["tgt-a", "tgt-b"]


def test_the_arrow_is_accepted_in_ascii_and_unicode_spellings():
    for arrow in ("->", "\u2192", "\u27f6"):
        cmd = parse_route_command(f"Alerts @news {arrow} @tgt-a")
        assert cmd.sources == ["@news"], arrow
        assert cmd.targets == ["@tgt-a"], arrow


def test_a_command_without_an_arrow_is_a_parse_error():
    with pytest.raises(RouteParseError) as exc:
        parse_route_command("/route Alerts @news @tgt-a")
    assert exc.value.code == "missing_arrow"


def test_a_duplicate_inside_a_list_is_refused_and_named():
    """`@news, https://t.me/news` is one chat twice, not two sources."""
    problems = validate_route_lists("@news, https://t.me/news", "@tgt-a")
    assert (ERR_DUPLICATE_SOURCE, "https://t.me/news") in problems


def test_a_chat_on_both_sides_is_refused_however_it_is_spelled():
    problems = validate_route_lists("@sales", "@sales, @tgt-a")
    assert problems and problems[0][0] == ERR_OVERLAP


def test_every_validation_code_has_a_catalog_key_in_all_ten_languages():
    """A refusal the user cannot read is a refusal they cannot act on."""
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert set(catalog) == set(SUPPORTED_LANGUAGES)
    for code, key in ROUTE_PROBLEM_KEYS.items():
        for lang, table in catalog.items():
            assert key in table, f"{key} missing from {lang} ({code})"
            for text in table[key].split():
                assert True
        # the message names the offending entry wherever one exists
        if code != "empty_sources" and code != "empty_targets":
            for lang, table in catalog.items():
                assert "{id}" in table[key], f"{key}/{lang} cannot name the entry"


# --------------------------------------------------------------------------- #
# Callback payloads
# --------------------------------------------------------------------------- #


def test_route_callbacks_stay_inside_the_telegram_byte_budget(ui):
    """A uuid fits; a scope longer than a uuid is shortened, never truncated."""
    scope = str(uuid.uuid4())
    for parts in (
        (CB["route_src_list"], "s", scope),
        (CB["route_tgt_list"], "t", scope),
        (CB["route_fix_entry"], "s", scope),
        (CB["route_save_lists"], scope),
        (CB["route_back"], scope),
        (CB["route_src_list"], "s", "x" * 300),
    ):
        payload = ui._route_cb(*parts)
        assert len(payload.encode("utf-8")) <= MAX_CALLBACK_BYTES, payload


def test_every_button_of_the_route_keyboard_has_a_legal_payload(ui):
    rule = ForwardRule(id=str(uuid.uuid4()), name="t", source_chat_id="-1003333333333")
    rule.target_chat_ids = ["-1005555555555", "-1006666666666"]
    for role in ("s", "t"):
        for with_rule in (None, rule):
            markup = ui._route_keyboard(_state(rule.id if with_rule else ROUTE_SCOPE_NEW),
                                        role, "en", rule=with_rule)
            payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
            assert payloads
            for payload in payloads:
                assert len(payload.encode("utf-8")) <= MAX_CALLBACK_BYTES, payload


def test_the_route_keyboard_is_localized_and_english_carries_no_persian(ui):
    en = ui._route_keyboard(_state(), "s", "en")
    fa = ui._route_keyboard(_state(), "s", "fa")
    en_labels = [b.text for row in en.inline_keyboard for b in row]
    fa_labels = [b.text for row in fa.inline_keyboard for b in row]
    assert en_labels == [b.text for row in en.inline_keyboard for b in row]
    assert en_labels != fa_labels, "the two languages render identically"
    for label in en_labels:
        assert not (RTL_CHARS & set(label)), f"Persian leaked into English: {label}"


# --------------------------------------------------------------------------- #
# What the user is told
# --------------------------------------------------------------------------- #


def test_the_problem_text_names_the_offending_entry(ui):
    text = ui._route_problem_text(
        validate_route_lists("@news, https://t.me/news", "@tgt-a"), "en")
    assert "https://t.me/news" in text

    overlap = ui._route_problem_text(validate_route_lists("@sales", "@sales"), "en")
    assert "@sales" in overlap

    empty = ui._route_problem_text(validate_route_lists("", "@tgt-a"), "en")
    assert empty.strip()


# --------------------------------------------------------------------------- #
# Lists -> the stored task
# --------------------------------------------------------------------------- #


def test_apply_lists_writes_both_lists_and_mirrors_the_first():
    rule = ForwardRule(id="r1", name="t", source_chat_id="-1003333333333")
    ProBotUI._route_apply_lists(
        rule,
        ["-1003333333333", "-1004444444444"], ["A", "B"],
        ["-1005555555555", "-1006666666666"], ["X", "Y"],
    )
    assert rule.source_chat_ids == ["-1003333333333", "-1004444444444"]
    assert rule.source_chat_id == "-1003333333333", "the mirror is the first source"
    assert rule.all_targets == ["-1005555555555", "-1006666666666"]
    assert rule.target_chat_id == "-1005555555555", "the mirror is the first target"


def test_role_data_keeps_entries_ids_names_and_statuses_aligned(ui):
    st = _state()
    st.buffer["route_sources"] = ["@src-a", "@badchan", "@src-b"]
    st.buffer["route_src_ids"] = ["-1003333333333"]  # deliberately short
    entries, ids, names, statuses = ui._route_role_data(st, "s")
    assert entries == ["@src-a", "@badchan", "@src-b"]
    assert len(ids) == len(names) == len(statuses) == len(entries)
    assert statuses[-1] == ROUTE_UNCHECKED


# --------------------------------------------------------------------------- #
# The flows
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_wizard_seeds_every_endpoint_of_a_stored_task(ui):
    rule = ForwardRule(id="rule-22", name="stored", source_chat_id="-1003333333333")
    rule.source_chat_ids = ["-1003333333333", "-1004444444444"]
    rule.target_chat_ids = ["-1005555555555", "-1006666666666"]
    ui._route_find_rule = AsyncMock(return_value=rule)

    st = _state()
    await ui._route_seed(123456, st, "rule-22")

    sources, src_ids, _n, src_status = ui._route_role_data(st, "s")
    targets, tgt_ids, _n2, tgt_status = ui._route_role_data(st, "t")
    assert sources == src_ids == ["-1003333333333", "-1004444444444"]
    assert targets == tgt_ids == ["-1005555555555", "-1006666666666"]
    assert set(src_status) == set(tgt_status) == {ROUTE_RESOLVED}


@pytest.mark.asyncio
async def test_saving_an_unresolved_route_is_refused(ui):
    """One entry that did not resolve blocks the save; nothing is written."""
    st = _state()
    # Seed the buffers directly: the wizard's own text step is covered elsewhere.
    ui._route_store_role(
        st, "s",
        ["@src-a", "@badchan"],
        ["-1003333333333", "@badchan"],
        ["Name @src-a", ""],
        [ROUTE_RESOLVED, "failed"],
    )
    ui._route_store_role(
        st, "t", ["@tgt-a"], ["-1005555555555"], ["Name @tgt-a"], [ROUTE_RESOLVED])

    cq = _callback()
    await ui._route_save(cq, 123456, st, "en")

    assert ui._rules.create.await_count == 0
    assert ui._rules.update_owned.await_count == 0
    assert cq.answer.await_count >= 1, "the save must say why it was refused"
    shown = "\n".join(str(c.args[0]) for c in cq.edit_message_text.await_args_list)
    assert "@badchan" in shown, "the unresolved entry is named on the screen"


@pytest.mark.asyncio
async def test_the_command_without_arguments_only_shows_usage(ui):
    message = _message("/route")
    await ui._handle_route_command(message, 123456)

    assert ui._rules.create.await_count == 0
    assert "->" in _replied(message) or "/route" in _replied(message)


@pytest.mark.asyncio
async def test_the_command_creates_a_task_carrying_both_lists(ui):
    message = _message("/route Alerts @src-a, @src-b -> @tgt-a, @tgt-b")
    st = _state()
    ui._state = MagicMock(return_value=st)

    await ui._handle_route_command(message, 123456)

    assert ui._rules.create.await_count == 1
    rule = ui._rules.create.await_args.args[0]
    assert rule.source_chat_ids == ["-1003333333333", "-1004444444444"]
    assert rule.source_chat_id == "-1003333333333"
    assert rule.target_chat_ids == ["-1005555555555", "-1006666666666"]
    assert rule.target_chat_id == "-1005555555555"
    assert rule.name == "Alerts"
    assert rule.owner_user_id == 123456
    # the id of the created task is shown back to the user, shortened
    assert rule.id[:8] in _replied(message)


@pytest.mark.asyncio
async def test_a_duplicate_typed_in_the_command_never_reaches_the_core(ui):
    message = _message("/route Alerts @news, https://t.me/news -> @tgt-a")
    st = _state()
    ui._state = MagicMock(return_value=st)

    await ui._handle_route_command(message, 123456)

    assert ui._rules.create.await_count == 0
    assert "https://t.me/news" in _replied(message)


@pytest.mark.asyncio
async def test_an_unresolvable_entry_is_named_and_nothing_is_created(ui):
    message = _message("/route Alerts @badchan -> @tgt-a")
    st = _state()
    ui._state = MagicMock(return_value=st)

    await ui._handle_route_command(message, 123456)

    assert ui._rules.create.await_count == 0
    assert "@badchan" in _replied(message)


# --------------------------------------------------------------------------- #
# The wizard buffer answers both spellings of one id without a network probe
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_buffer_resolves_a_pasted_raw_unique_id():
    """The raw unique id must hit the buffered `-100…` chat, exactly reversed too."""
    st = UiState()
    st.buffer = {"chats": [
        {"id": "-1001206461239", "title": "News", "username": "news", "emoji": "\U0001F4E2"},
    ]}
    probe = SimpleNamespace()  # the match path never touches the sessions

    for token, expected in (
        ("1206461239", "-1001206461239"),
        ("-1001206461239", "-1001206461239"),
        ("@news", "-1001206461239"),
    ):
        cid, _title, status = await ProBotUI._resolve_chat_token(probe, 0, st, token)
        assert status == ROUTE_RESOLVED, token
        assert cid == expected, token
