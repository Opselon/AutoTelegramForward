"""`/replace` by stable id, plus the one-line create form.

The legacy per-task grammar (`/replace add promo -"OLD" -"NEW"`) must keep
working exactly as before; the id-addressed verbs are additive. Both write
through the same `ReplaceRuleService`, so these tests check stored rules, not
rendered strings alone — and the tenant boundary is checked at the command, not
only in the service.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.application.replace_rules import ReplaceRuleService
from core.tests.bot_dispatch import BotHarness

OWNER = 5094837833
OTHER = 7711223344

TASK_A = "task-a"
TASK_B = "task-b"


class Msg:
    def __init__(self, uid: int, text: str) -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.text = text
        self.sent: list = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))
        return SimpleNamespace(text=text)

    @property
    def last(self) -> str:
        return self.sent[-1][0] if self.sent else ""

    @property
    def all_text(self) -> str:
        return "\n".join(text for text, _kbd in self.sent)


async def replace(h: BotHarness, uid: int, args: str) -> Msg:
    msg = Msg(uid, f"/replace {args}")
    await h.ui._cmd_replace(msg, args)
    return msg


async def seed(h: BotHarness, uid: int = OWNER, tasks=(TASK_A, TASK_B)) -> None:
    try:
        await h.seed_session(f"sess-{uid}", uid)
    except Exception:
        pass  # already seeded by an earlier call in the same test
    for tid in tasks:
        if not h.rule_row(tid):
            await h.seed_rule(tid, uid, name=tid, session_id=f"sess-{uid}")


async def make_rule(h: BotHarness, uid: int, task: str, source: str, to: str, name: str):
    return await h.replace_rules.create(uid, {
        "source": source, "to": to, "name": name, "task_ids": [task],
    })


@pytest.fixture
def harness(tmp_path):
    h = BotHarness(str(tmp_path / "rpl.db"))
    h.replace_rules = ReplaceRuleService(h.rules)
    h.ui._replace_rules = h.replace_rules
    asyncio.run(seed(h))
    yield h
    h.close()


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
def test_list_with_a_query_filters_the_panel(harness):
    async def go():
        await make_rule(harness, OWNER, TASK_A, "BTC", "Bitcoin", "btc")
        await make_rule(harness, OWNER, TASK_A, "ETH", "Ethereum", "eth")
        return await replace(harness, OWNER, "list btc")

    msg = asyncio.run(go())
    # The header counts describe the account; the rows are buttons, so the
    # filter is asserted on the rendered keyboard.
    buttons = " ".join(
        str(getattr(btn, "text", ""))
        for row in msg.sent[-1][1].inline_keyboard for btn in row
    )
    assert "btc" in buttons and "eth" not in buttons


# --------------------------------------------------------------------------- #
# edit / delete by id
# --------------------------------------------------------------------------- #
def test_edit_by_id_renders_that_rule(harness):
    async def go():
        rule = await make_rule(harness, OWNER, TASK_A, "BTC", "Bitcoin", "btc")
        return rule, await replace(harness, OWNER, f"edit {rule.id}")

    rule, msg = asyncio.run(go())
    assert "Bitcoin" in msg.last and "BTC" in msg.last
    assert rule.id[:8] in msg.last or "btc" in msg.last


def test_edit_by_explicit_prefix_resolves(harness):
    async def go():
        rule = await make_rule(harness, OWNER, TASK_A, "BTC", "Bitcoin", "btc")
        return await replace(harness, OWNER, f"edit #{rule.id[:6]}")

    msg = asyncio.run(go())
    assert "Bitcoin" in msg.last


def test_delete_by_id_asks_first_and_only_then_removes(harness):
    async def go():
        rule = await make_rule(harness, OWNER, TASK_A, "BTC", "Bitcoin", "btc")
        return rule, await replace(harness, OWNER, f"delete {rule.id}")

    rule, msg = asyncio.run(go())
    assert "Cannot be undone" in msg.last or "cannot be undone" in msg.last.lower()
    assert asyncio.run(harness.replace_rules.get(OWNER, rule.id)).id == rule.id

    asyncio.run(harness.press(f"rpl:delok:{rule.id}", OWNER))
    with pytest.raises(Exception):
        asyncio.run(harness.replace_rules.get(OWNER, rule.id))


def test_delete_by_unknown_id_says_so_and_deletes_nothing(harness):
    async def go():
        rule = await make_rule(harness, OWNER, TASK_A, "BTC", "Bitcoin", "btc")
        return rule, await replace(harness, OWNER, "delete 0123456789abcdef")

    rule, msg = asyncio.run(go())
    assert "Nothing matches" in msg.all_text
    assert asyncio.run(harness.replace_rules.get(OWNER, rule.id)).id == rule.id


# --------------------------------------------------------------------------- #
# fast add
# --------------------------------------------------------------------------- #
def test_fast_add_creates_the_rule_on_the_named_task(harness):
    async def go():
        return await replace(harness, OWNER, 'add "USDT" -> "Tether" --task task-a --name usdt')

    msg = asyncio.run(go())
    data = asyncio.run(harness.replace_rules.list(OWNER))
    assert data["total"] == 1
    created = data["items"][0]
    assert created["source"] == "USDT" and created["to"] == "Tether"
    assert created["task_ids"] == [TASK_A]
    assert "Rule created" in msg.last and f"#{created['id'][:8]}" in msg.last


def test_fast_add_with_one_task_needs_no_flag(harness):
    async def go():
        await harness.rules.delete(TASK_B)          # exactly one task left
        return await replace(harness, OWNER, 'add "a" -> "b"')

    msg = asyncio.run(go())
    assert "Rule created" in msg.last
    assert asyncio.run(harness.replace_rules.list(OWNER))["total"] == 1


def test_fast_add_with_several_tasks_refuses_to_guess(harness):
    async def go():
        await seed(harness, tasks=("task-c",))
        return await replace(harness, OWNER, 'add "a" -> "b"')

    msg = asyncio.run(go())
    assert "--task" in msg.last
    assert "task-a" in msg.last and "task-c" in msg.last
    assert asyncio.run(harness.replace_rules.list(OWNER))["total"] == 0


def test_fast_add_with_an_unknown_task_names_the_real_ones(harness):
    msg = asyncio.run(replace(harness, OWNER, 'add "a" -> "b" --task nope'))
    assert "nope" in msg.last and "task-a" in msg.last
    assert asyncio.run(harness.replace_rules.list(OWNER))["total"] == 0


def test_fast_add_refuses_a_bad_priority(harness):
    msg = asyncio.run(replace(harness, OWNER, 'add "a" -> "b" --task task-a --priority soon'))
    assert "Priority" in msg.last
    assert asyncio.run(harness.replace_rules.list(OWNER))["total"] == 0


def test_fast_add_understands_regex_and_delete(harness):
    async def go():
        await replace(harness, OWNER, 'add "a(\\d+)" -> "b$1" --task task-a --regex')
        return await harness.replace_rules.list(OWNER)

    data = asyncio.run(go())
    created = data["items"][0]
    assert created["mode"] == "regex"
    assert created["to"] == "b$1"


# --------------------------------------------------------------------------- #
# tenant boundary
# --------------------------------------------------------------------------- #
def test_another_tenants_rule_id_is_not_found_and_never_rendered(harness):
    async def go():
        await seed(harness, uid=OTHER, tasks=("other-task",))
        foreign = await make_rule(harness, OTHER, "other-task", "SECRET", "Hidden", "secret")
        return foreign, await replace(harness, OWNER, f"edit {foreign.id}")

    foreign, msg = asyncio.run(go())
    assert "Nothing matches" in msg.all_text
    assert "SECRET" not in msg.all_text
    assert "Hidden" not in msg.all_text


# --------------------------------------------------------------------------- #
# grammar detection
# --------------------------------------------------------------------------- #
def test_the_arrow_alone_decides_the_grammar():
    from core.infrastructure.telegram.replace_fast_add import is_arrow_form

    # Flags are legal in the legacy form too, so they must not switch grammars.
    assert is_arrow_form('"a" -> "b" --regex')
    assert is_arrow_form('"a"->"b"')
    assert not is_arrow_form('-\"old\" -\"new\" --regex -i -w -n yearly')
    # A per-task entry whose *text* contains an arrow is still the legacy form.
    assert not is_arrow_form('name -"a -> b" -"c"')


# --------------------------------------------------------------------------- #
# back-compat
# --------------------------------------------------------------------------- #
def test_the_legacy_entry_grammar_still_writes_the_task(harness):
    msg = asyncio.run(replace(harness, OWNER, 'add task-a ORD -"ORD" -"NEW"'))

    async def stored():
        task = await harness.rules_repo.get_by_id(TASK_A)
        return dict(task.replacements or {})

    entries = asyncio.run(stored())
    assert entries, "legacy add wrote no replacement entry"
    assert any("ORD" in str(entry) for entry in entries.values())
    # The legacy write keeps its per-task entry *and* produces the unified rule
    # that the id-addressed surface reads, so both generations stay in sync.
    unified = asyncio.run(harness.replace_rules.list(OWNER))
    assert unified["total"] == 1
    assert unified["items"][0]["source"] == "ORD"
    assert msg.last
