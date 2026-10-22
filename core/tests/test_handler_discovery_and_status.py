"""The discovery catalog and the forwarding status screen, through the registry.

These are behaviour tests for the surfaces the handler modules own: the
`/getchannel` catalog (name *and* id per row), the chat action sheet that enters
the staged task builder, and the status screen that folds a task's state, its
counters and its queue into one view.

They drive the registered callbacks (`BotHarness.press`) and the registered
command (`_cmd_getchannel`) — never a helper in isolation — so a dead button or
an unclaimed command fails here. Ownership is asserted as a *visible* refusal.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from core.domain.entities import ForwardRule
from core.tests.bot_dispatch import BotHarness, _FakeMessage

OWNER = 5094837833
OTHER = 4242424242

SESSION_ID = "sess-discover-1"
RULE_ID = "rule-discover-1"
CHANNEL_ID = "-1001234567890"
CHANNEL_TITLE = "News Channel"
TARGET_ID = "-1005550001112"
TARGET_TITLE = "Target Channel"

DIALOGS = [
    {"id": CHANNEL_ID, "title": CHANNEL_TITLE, "username": "news",
     "type": "channel", "is_self": False},
    {"id": TARGET_ID, "title": TARGET_TITLE, "username": "",
     "type": "channel", "is_self": False},
]


def buttons(kbd) -> list[tuple[str, str]]:
    """Every button as `(label, payload)` — the shape a press delivers."""
    return [(b.text, b.callback_data) for row in kbd.inline_keyboard for b in row]


def payloads(kbd) -> list[str]:
    return [data for _label, data in buttons(kbd)]


def message(user_id: int, text: str = "") -> _FakeMessage:
    msg = _FakeMessage(chat_id=user_id)
    msg.from_user = SimpleNamespace(id=user_id, first_name="U", username="u",
                                    language_code="en", is_bot=False)
    msg.text = text
    return msg


@pytest.fixture()
async def harness(tmp_path, monkeypatch):
    h = BotHarness(str(tmp_path / "atf.db"))
    await h.seed_session(SESSION_ID, OWNER)
    await h.seed_rule(RULE_ID, OWNER, session_id=SESSION_ID,
                      source_chat_id=CHANNEL_ID, target_chat_id=TARGET_ID, name="news")
    # The scan is infrastructure; the catalog policy above it is what these
    # tests are about. Patching the gateway's port keeps every layer in play.
    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    async def fake_fetch(self, owner_user_id: int):
        return list(DIALOGS)

    monkeypatch.setattr(SessionDialogGateway, "fetch", fake_fetch)
    try:
        yield h
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# `/getchannel` — the catalog page
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_getchannel_lists_name_and_id_per_row(harness):
    msg = message(OWNER, "/getchannel")
    await harness.ui._cmd_getchannel(msg, "")

    assert msg.sent, "the command must answer"
    text, kwargs = msg.sent[-1][1], msg.sent[-1][2]
    assert CHANNEL_TITLE in text
    assert CHANNEL_ID in text
    assert any(CHANNEL_TITLE in label for label, _p in buttons(kwargs["reply_markup"]))


@pytest.mark.asyncio
async def test_getchannel_without_a_connected_session_answers_the_empty_state(harness, monkeypatch):
    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    async def no_rows(self, owner_user_id: int):
        return []

    monkeypatch.setattr(SessionDialogGateway, "fetch", no_rows)
    msg = message(OWNER, "/getchannel")
    await harness.ui._cmd_getchannel(msg, "")
    text = msg.sent[-1][1]
    assert "¯\\_(ツ)_/¯" in text or "no" in text.lower() or "هیچ" in text


@pytest.mark.asyncio
async def test_a_page_button_rerenders_the_page(harness):
    cq = await harness.press("gch_pg:channel:0", OWNER)
    assert cq.message.sent
    text = cq.message.sent[-1][1]
    assert CHANNEL_TITLE in text


@pytest.mark.asyncio
async def test_a_stale_row_index_is_answered_not_swallowed(harness):
    cq = await harness.press("gch_v:channel:999", OWNER)
    assert cq.answers.texts, "a dead button must say something"
    assert not cq.message.sent, "and must not edit the catalog into nonsense"


# --------------------------------------------------------------------------- #
# The action sheet and the staged builder
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_action_sheet_offers_source_target_and_tasks(harness):
    cq = await harness.press("gch_v:channel:0", OWNER)
    data = payloads(cq.message.sent[-1][2]["reply_markup"])
    assert f"gch_src:{CHANNEL_ID}" in data
    assert f"gch_tgt:{CHANNEL_ID}" in data
    assert f"gch_tsk:{CHANNEL_ID}" in data
    assert CHANNEL_ID in cq.message.sent[-1][1]


@pytest.mark.asyncio
async def test_using_a_chat_as_source_arms_the_target_step(harness):
    await harness.press(f"gch_src:{CHANNEL_ID}", OWNER)

    st = harness.ui._state(OWNER)
    assert st.step == "rule_target"
    assert st.rule_source == CHANNEL_ID
    assert st.buffer.get("source_name") == CHANNEL_TITLE
    assert not st.rule_target


@pytest.mark.asyncio
async def test_using_a_chat_as_target_without_a_source_arms_the_source_step(harness):
    await harness.press(f"gch_tgt:{CHANNEL_ID}", OWNER)

    st = harness.ui._state(OWNER)
    assert st.step == "rule_source"
    assert st.rule_target == CHANNEL_ID
    assert st.buffer["pending_target"]["id"] == CHANNEL_ID
    assert st.buffer["pending_target"]["title"] == CHANNEL_TITLE


@pytest.mark.asyncio
async def test_source_then_target_creates_the_task(harness):
    before = harness.rule_count(OWNER)

    await harness.press(f"gch_src:{CHANNEL_ID}", OWNER)
    await harness.press(f"gch_tgt:{TARGET_ID}", OWNER)

    assert harness.rule_count(OWNER) == before + 1
    rows = harness.db.connection.execute(
        "SELECT source_chat_id, target_chat_id FROM forward_rules WHERE owner_user_id = ?",
        (OWNER,),
    ).fetchall()
    assert (CHANNEL_ID, TARGET_ID) in [tuple(r) for r in rows]


@pytest.mark.asyncio
async def test_a_typed_source_after_a_catalog_target_completes_the_task(harness):
    """The target-first flow must not strand the user in a second question."""
    before = harness.rule_count(OWNER)
    await harness.press(f"gch_tgt:{TARGET_ID}", OWNER)

    st = harness.ui._state(OWNER)
    assert st.step == "rule_source"

    msg = message(OWNER, CHANNEL_ID)
    await harness.ui._step_rule_source(msg, st, CHANNEL_ID)

    assert harness.rule_count(OWNER) == before + 1


@pytest.mark.asyncio
async def test_tasks_related_to_a_chat_are_listed_with_a_status_button(harness):
    cq = await harness.press(f"gch_tsk:{CHANNEL_ID}", OWNER)
    text = cq.message.sent[-1][1]
    data = payloads(cq.message.sent[-1][2]["reply_markup"])
    assert RULE_ID[:8] in text
    assert f"fwd_st:{RULE_ID}" in data


# --------------------------------------------------------------------------- #
# Forwarding status
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_status_screen_shows_state_and_counters(harness):
    cq = await harness.press(f"fwd_st:{RULE_ID}", OWNER)

    assert cq.message.sent, "the status screen must render"
    text = cq.message.sent[-1][1]
    assert "Forwarding status" in text
    assert "Forwarded:" in text
    data = payloads(cq.message.sent[-1][2]["reply_markup"])
    assert f"fwd_off:{RULE_ID}" in data


@pytest.mark.asyncio
async def test_status_refuses_a_task_the_user_does_not_own(harness):
    cq = await harness.press(f"fwd_st:{RULE_ID}", OTHER)
    assert any("not found" in t.lower() for t in cq.answers.texts)
    assert not cq.message.sent


@pytest.mark.asyncio
async def test_status_folds_rule_counters_and_queue_into_one_view(harness):
    """The use case, with a queue that really reports counts."""
    from core.application.forward_status import ForwardingStatusUseCase

    class _Queue:
        async def counts_for_rule(self, rule_id):
            return {"PENDING": 2, "RETRY_WAIT": 1, "SENT": 7, "FAILED": 1}

    status = await ForwardingStatusUseCase(
        rules=harness.rules,
        rule_stats=harness.stats_repo,
        delivery_queue=_Queue(),
    ).for_rule(RULE_ID, OWNER)

    assert status is not None
    assert status.state == "running"
    assert status.pending == 3
    assert status.retrying == 1
    assert status.sent == 7
    assert status.failed == 1

    text, kbd = harness.ui._render_forwarding_status(status, lang="en")
    assert "Queue:" in text
    assert f"fwd_off:{RULE_ID}" in payloads(kbd)


@pytest.mark.asyncio
async def test_status_for_a_missing_task_returns_none(harness):
    from core.application.forward_status import ForwardingStatusUseCase

    status = await ForwardingStatusUseCase(
        rules=harness.rules, rule_stats=None, delivery_queue=None,
    ).for_rule("nope", OWNER)
    assert status is None


# --------------------------------------------------------------------------- #
# Enable / disable
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enable_switch_turns_the_task_on(harness):
    await harness.rules_repo.set_task_status(
        RULE_ID, target_active=False, expected_state="running", owner_user_id=OWNER)

    await harness.press(f"fwd_on:{RULE_ID}", OWNER)

    rule = await harness.rules_repo.get_by_id(RULE_ID)
    assert rule.is_active is True


@pytest.mark.asyncio
async def test_disable_switch_turns_the_task_off(harness):
    await harness.press(f"fwd_off:{RULE_ID}", OWNER)

    rule = await harness.rules_repo.get_by_id(RULE_ID)
    assert rule.is_active is False


@pytest.mark.asyncio
async def test_a_second_tenants_toggle_is_refused_and_changes_nothing(harness):
    cq = await harness.press(f"fwd_off:{RULE_ID}", OTHER)

    rule = await harness.rules_repo.get_by_id(RULE_ID)
    assert rule.is_active is True
    assert any("not found" in t.lower() for t in cq.answers.texts)


# --------------------------------------------------------------------------- #
# The module's own contract
# --------------------------------------------------------------------------- #
def test_the_discovery_module_owns_every_command_it_declares(harness):
    for name in harness.ui.discovery_commands:
        assert callable(getattr(harness.ui, f"_cmd_{name}", None)), name


def test_every_registered_payload_is_within_the_callback_limit(harness):
    """Telegram truncates callback data at 64 bytes; a longer id is a dead button."""
    for provider in (
        harness.ui._render_discovery_page(
            __import__("core.application.discovery", fromlist=["DialogCatalog"])
            .DialogCatalog.from_dicts(DIALOGS),
            kind="channel", page=0, lang="en",
        )[1],
        harness.ui._render_chat_actions(
            __import__("core.application.discovery", fromlist=["DialogCatalog"])
            .DialogCatalog.from_dicts(DIALOGS).at(0),
            kind="channel", page=0, lang="en",
        )[1],
    ):
        for data in payloads(provider):
            assert len(data.encode()) <= 64, data


        # --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Delete: a task's owner can delete it; nobody else can even see the confirm
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_confirmation_is_offered_to_the_owner(harness):
    cq = await harness.press(f"rdel:{RULE_ID}", OWNER)

    assert cq.message.sent, "the owner sees the confirmation screen"
    assert any(d.startswith("rdelyes:") for d in payloads(cq.message.sent[-1][2]["reply_markup"]))


@pytest.mark.asyncio
async def test_delete_is_refused_for_a_task_the_user_does_not_own(harness):
    """The confirmation screen must not name another tenant's task."""
    cq = await harness.press(f"rdel:{RULE_ID}", OTHER)

    assert any("not found" in t.lower() or "owner" in t.lower() for t in cq.answers.texts)
    assert not cq.message.sent
    assert (await harness.rules_repo.get_by_id(RULE_ID)) is not None


@pytest.mark.asyncio
async def test_confirming_the_delete_removes_the_task(harness):
    await harness.press(f"rdel:{RULE_ID}", OWNER)
    cq = await harness.press(f"rdelyes:{RULE_ID}", OWNER)

    assert (await harness.rules_repo.get_by_id(RULE_ID)) is None
    assert harness.rule_count(OWNER) == 0
    assert cq.answers.texts


@pytest.mark.asyncio
async def test_a_second_tenant_cannot_confirm_someone_elses_delete(harness):
    await harness.press(f"rdel:{RULE_ID}", OWNER)
    cq = await harness.press(f"rdelyes:{RULE_ID}", OTHER)

    assert (await harness.rules_repo.get_by_id(RULE_ID)) is not None
    assert any("not found" in t.lower() or "owner" in t.lower() for t in cq.answers.texts)


# --------------------------------------------------------------------------- #
# `/getchannel -id` — the copy-ready one-line list
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_getchannel_id_answers_the_plain_copy_ready_list(harness):
    msg = message(OWNER, "/getchannel -id")
    await harness.ui._cmd_getchannel(msg, "-id")

    assert msg.sent, "the flag must be answered"
    text, kwargs = msg.sent[-1][1], msg.sent[-1][2]
    assert f"**{CHANNEL_TITLE}** \u2192 `{CHANNEL_ID}`" in text
    # The answer exists to be copied from: one row, no buttons.
    assert not buttons(kwargs["reply_markup"]) if kwargs.get("reply_markup") else True
    assert not payloads(kwargs["reply_markup"]) if kwargs.get("reply_markup") else True
    assert "/forward add" in text, "the next command is written under the list"


@pytest.mark.asyncio
async def test_getchannel_without_the_flag_keeps_the_interactive_sheet(harness):
    msg = message(OWNER, "/getchannel")
    await harness.ui._cmd_getchannel(msg, "")

    text, kwargs = msg.sent[-1][1], msg.sent[-1][2]
    assert CHANNEL_TITLE in text
    assert any(CHANNEL_TITLE in label for label, _p in buttons(kwargs["reply_markup"]))


@pytest.mark.asyncio
async def test_getchannel_id_pages_and_names_the_next_command(harness, monkeypatch):
    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    rows = [
        {"id": f"-1001000000{i:04d}", "title": f"Chat {i}", "username": "",
         "type": "channel", "is_self": False}
        for i in range(25)
    ]

    async def many_rows(self, owner_user_id: int):
        return rows

    monkeypatch.setattr(SessionDialogGateway, "fetch", many_rows)
    msg = message(OWNER, "/getchannel -id 2")
    await harness.ui._cmd_getchannel(msg, "-id 2")

    text = msg.sent[-1][1]
    assert "`21.`" in text and "`25.`" in text, "page two shows chats 21..25"
    assert "`20.`" not in text
    assert "Page 2 of 2" in text
    assert "/getchannel -id 2" in text  # last page: the hint names page 2 again


@pytest.mark.asyncio
async def test_an_unknown_command_token_is_refused_with_usage(harness):
    msg = message(OWNER, "/getchannel -summery")
    await harness.ui._cmd_getchannel(msg, "-summery")

    text, kwargs = msg.sent[-1][1], msg.sent[-1][2]
    assert "-summery" in text, "the refusal names the offending token"
    assert "/getchannel -id" in text, "and shows the working spellings"
    assert not payloads(kwargs["reply_markup"]) if kwargs.get("reply_markup") else True


@pytest.mark.asyncio
async def test_getchannel_all_lists_every_kind_in_order(harness, monkeypatch):
    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    rows = [
        *DIALOGS,
        {"id": "5094837833", "title": "Just Me", "username": "me",
         "type": "user", "is_self": False},
        {"id": "777000111", "title": "Helper Bot", "username": "helper",
         "type": "bot", "is_self": False},
    ]

    async def many_rows(self, owner_user_id: int):
        return rows

    monkeypatch.setattr(SessionDialogGateway, "fetch", many_rows)
    msg = message(OWNER, "/getchannel -all -id")
    await harness.ui._cmd_getchannel(msg, "-all -id")

    text = msg.sent[-1][1]
    positions = {
        CHANNEL_ID: text.find(CHANNEL_ID),
        TARGET_ID: text.find(TARGET_ID),
        "5094837833": text.find("5094837833"),
        "777000111": text.find("777000111"),
    }
    assert all(p >= 0 for p in positions.values()), text
    sources = sorted(positions, key=positions.__getitem__)
    # Catalog priority: channels, then bots, then users.
    assert sources == [CHANNEL_ID, TARGET_ID, "777000111", "5094837833"], sources
    assert text.count("→") == len(rows), "one line per chat"


@pytest.mark.asyncio
async def test_getchannel_id_on_an_empty_catalog_stays_a_plain_refusal(harness, monkeypatch):
    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    async def no_rows(self, owner_user_id: int):
        return []

    monkeypatch.setattr(SessionDialogGateway, "fetch", no_rows)
    msg = message(OWNER, "/getchannel -id")
    await harness.ui._cmd_getchannel(msg, "-id")
    text, kwargs = msg.sent[-1][1], msg.sent[-1][2]
    assert text, "the empty state must still answer"
    assert not payloads(kwargs["reply_markup"]) if kwargs.get("reply_markup") else True
