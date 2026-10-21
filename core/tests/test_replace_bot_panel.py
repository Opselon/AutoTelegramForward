"""The Telegram replace panel, driven through its real callbacks.

The panel is a mixin, so it runs against a harness that supplies only the bot
plumbing it borrows from ``ProBotUI`` (translation, keyboard building, flow-state
persistence) and the *real* ``ReplaceRuleService`` over a real SQLite database.
That way every assertion is about what a button does to a tenant's stored rules:

* the list header carries the total/🟢/🔴 counts, the pager only appears when
  there is a second page, and a rule assigned to two tasks is listed once;
* a status filter and a search change the set of rows, and the pager resets to
  page 1 (a stale page 3 would render "no rules" on a 1-page result);
* the detail screen offers exactly the actions the spec lists, toggles
  enable/disable on the stored rule, and re-reads it so the label flips;
* delete asks first and only removes on the confirming callback;
* the test preview reports the rewritten text and the match count without
  touching stored data;
* another tenant's rule id is refused as "not found" and never rendered.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from core.application.replace_rules import ReplaceRuleService
from core.application.use_cases import ForwardRuleUseCases
from core.domain.entities import ForwardRule, TelegramSession
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteForwardRuleRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.replace_bot_ui import ReplaceBotMixin

OWNER = 5094837833
OTHER = 6278059256
LOCALES = Path("core/infrastructure/telegram/locales/translations.json")


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
class FakeBot:
    """Records the callback handlers the mixin registers."""

    def __init__(self) -> None:
        self.callbacks: List[tuple] = []

    def on_callback_query(self, filter=None, group: int = 0):  # noqa: A002
        def decorator(fn):
            self.callbacks.append((filter, fn))
            return fn

        return decorator

    def find(self, data: str):
        for flt, fn in self.callbacks:
            pattern = getattr(flt, "p", None)
            if pattern is not None and re.match(pattern.pattern, data):
                return fn
        raise AssertionError(f"no handler matched {data!r}")


class FakeMessage:
    def __init__(self, uid: int, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.text = text
        self.sent: List[tuple] = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))
        return SimpleNamespace(text=text)


class FakeCallback:
    def __init__(self, uid: int, data: str = "") -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.answers: List[str] = []
        self.edits: List[tuple] = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class Harness(ReplaceBotMixin):
    """The panel with the minimum bot plumbing it depends on."""

    def __init__(self, service: Any) -> None:
        self._replace_rules = service
        self.i18n = I18n()
        self.bot = FakeBot()
        self._states: Dict[int, Any] = {}
        self.register_replace_handlers()

    def _t(self, key: str, lang: Optional[str] = None, **kw) -> str:
        return self.i18n.t(key, lang=lang or "en", **kw)

    def _btn(self, key: str, lang: Optional[str] = None, style: Optional[str] = None, **kw) -> str:
        return self.i18n.t(key, lang=lang or "en", **kw)

    def _kbd(self, rows):
        return {"rows": [list(r) for r in rows]}

    def _localized_handler(self, handler):
        return handler

    def _lang_for(self, *sources, uid: Optional[int] = None) -> str:
        return "en"

    async def _safe_edit(self, target, text, reply_markup=None, **kw):
        await target.edit_message_text(text, reply_markup=reply_markup)

    def _state(self, uid: int, user: Any = None):
        st = self._states.get(uid)
        if st is None:
            st = SimpleNamespace(user_id=uid, step="", buffer={}, lang="en")
            self._states[uid] = st
        return st

    async def _persist(self, uid: int, st: Any) -> None:
        self._states[uid] = st


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def build(tmp_path) -> tuple[SqliteDatabase, SqliteForwardRuleRepository, ReplaceRuleService]:
    db = SqliteDatabase(str(tmp_path / "panel.db"))
    repo = SqliteForwardRuleRepository(db)
    return db, repo, ReplaceRuleService(ForwardRuleUseCases(repo))


async def seed(db, repo, owner=OWNER, rules=(("t1", "Task One"), ("t2", "Task Two"))):
    session_id = "sess-a" if owner == OWNER else f"sess-{owner}"
    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(
            id=session_id,
            phone_number="+100****0000",
            is_active=True,
            is_authorized=True,
            owner_user_id=owner,
        )
    )
    for index, (rid, name) in enumerate(rules, start=1):
        await repo.add(
            ForwardRule(
                id=rid,
                session_id=session_id,
                name=name,
                source_chat_id=f"-1004400615{index:03d}",
                target_chat_id="-1003915313919",
                is_active=True,
                owner_user_id=owner,
            )
        )


def labels(kbd) -> List[str]:
    """Every button label, flattened: the list rows *are* the rule list."""
    if not kbd:
        return []
    return [item[0] for row in kbd["rows"] for item in row]


def datas(kbd) -> List[str]:
    out = []
    for row in (kbd or {}).get("rows", []):
        for item in row:
            out.append(item[1])
    return out


async def press(panel: Harness, uid: int, data: str) -> FakeCallback:
    """Drive one callback end to end, exactly as Telegram would."""
    handler = panel.bot.find(data)
    cq = FakeCallback(uid, data)
    await handler(None, cq)
    return cq


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
def test_list_shows_counts_and_one_row_per_rule(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        await seed(db, repo)
        # One rule on two tasks: stored twice, listed once.
        rule = await service.create(
            OWNER, {"source": "promo", "to": "offer", "name": "Promo",
                    "task_ids": ["t1", "t2"]}
        )
        other = await service.create(
            OWNER, {"source": "spam", "to": "", "name": "Drop spam", "task_ids": ["t1"]}
        )
        await service.update(OWNER, other.id, {"enabled": False})

        panel = Harness(service)
        text, kbd = await panel._render_rpl_list(OWNER)

        assert "Total: 2" in text
        assert "Enabled: 1" in text
        assert "Disabled: 1" in text
        row_labels = labels(kbd)
        assert sum("Promo" in label for label in row_labels) == 1, (
            "a rule on two tasks must be one row"
        )
        assert any(label.startswith("🟢") for label in row_labels)
        assert any(label.startswith("🔴") for label in row_labels)
        # Deleting the match is shown as such, not as an empty preview.
        assert any("(removed)" in label for label in row_labels)
        assert f"rpl:v:{rule.id}" in datas(kbd)

    asyncio.run(body())


def test_pager_appears_only_past_the_first_page(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        await seed(db, repo)
        for index in range(30):
            await service.create(
                OWNER, {"source": f"src{index}", "to": "dst", "task_ids": ["t1"]}
            )

        panel = Harness(service)
        page1_text, page1_kbd = await panel._render_rpl_list(OWNER)
        assert "Page 1 of 2" in page1_text
        assert any(d == "rpl:p:2" for d in datas(page1_kbd))
        assert not any(d == "rpl:p:0" for d in datas(page1_kbd))

        page2_text, page2_kbd = await panel._render_rpl_list(OWNER, page=2)
        assert "Page 2 of 2" in page2_text
        assert any(d == "rpl:p:1" for d in datas(page2_kbd))

        cq = await press(panel, OWNER, "rpl:p:2")
        assert "Page 2 of 2" in cq.edits[-1][0]

    asyncio.run(body())


def test_filter_and_search_reset_paging(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        await seed(db, repo)
        for index in range(30):
            await service.create(
                OWNER,
                {"source": f"src{index}", "to": "dst", "name": f"Rule {index}",
                 "task_ids": ["t1"]},
            )
        panel = Harness(service)
        panel._rpl_state(OWNER)["page"] = 2

        await press(panel, OWNER, "rpl:st:all")
        assert panel._rpl_state(OWNER)["status"] == "all"

        # Filtering keeps the page reset: a stale page 2 would look empty.
        panel._rpl_state(OWNER)["page"] = 2
        cq = await press(panel, OWNER, "rpl:st:enabled")
        assert panel._rpl_state(OWNER)["page"] == 1
        assert "Page 2" not in cq.edits[-1][0]

        # Search narrows to the one rule whose name matches.
        panel._rpl_state(OWNER)["query"] = "Rule 3"
        _, kbd = await panel._render_rpl_list(OWNER)
        row_labels = labels(kbd)
        assert any("Rule 3" in label for label in row_labels)
        assert not any("Rule 30" in label for label in row_labels)

    asyncio.run(body())


def test_other_tenant_rules_are_invisible(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        await seed(db, repo)
        await seed(db, repo, owner=OTHER, rules=(("o1", "Other Task"),))
        theirs = await service.create(
            OTHER, {"source": "secret", "to": "leak", "name": "Theirs", "task_ids": ["o1"]}
        )

        panel = Harness(service)
        text, kbd = await panel._render_rpl_list(OWNER)
        assert not any("Theirs" in label for label in labels(kbd))
        assert all(theirs.id not in d for d in datas(kbd))
        assert "Total: 0" in text

        # And the id is refused as missing: the screen says so and nothing
        # about the foreign rule leaks into the edit.
        cq = await press(panel, OWNER, f"rpl:v:{theirs.id}")
        body = cq.edits[-1][0] if cq.edits else ""
        assert "not found" in body.lower()
        assert "Theirs" not in body and "secret" not in body

    asyncio.run(body())


# --------------------------------------------------------------------------- #
# detail + actions
# --------------------------------------------------------------------------- #
def test_detail_offers_every_action_and_toggles_status(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        await seed(db, repo)
        rule = await service.create(
            OWNER, {"source": "promo", "to": "offer", "name": "Promo",
                    "priority": 250, "task_ids": ["t1", "t2"]}
        )
        panel = Harness(service)
        text, kbd = await panel._render_rpl_detail(OWNER, rule.id)

        assert "Promo" in text and "promo" in text and "offer" in text
        assert "250" in text
        assert "Task One" in text and "Task Two" in text
        assert f"rpl:en:{rule.id}:0" in datas(kbd), "an enabled rule must offer Disable"
        assert f"rpl:ed:{rule.id}" in datas(kbd)
        assert f"rpl:ts:{rule.id}" in datas(kbd)
        assert f"rpl:tr:{rule.id}" in datas(kbd)
        assert f"rpl:dup:{rule.id}" in datas(kbd)
        assert f"rpl:del:{rule.id}" in datas(kbd)
        assert "rpl:list" in datas(kbd)

        # Disabling flips the stored flag and re-renders with the opposite action.
        cq = await press(panel, OWNER, f"rpl:en:{rule.id}:0")
        stored = await service.get(OWNER, rule.id)
        assert stored.enabled is False
        assert f"rpl:en:{rule.id}:1" in datas(cq.edits[-1][1])

        await press(panel, OWNER, f"rpl:en:{rule.id}:1")
        assert (await service.get(OWNER, rule.id)).enabled is True

    asyncio.run(body())


def test_delete_asks_before_removing(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        await seed(db, repo)
        rule = await service.create(
            OWNER, {"source": "promo", "to": "offer", "name": "Promo",
                    "task_ids": ["t1", "t2"]}
        )
        panel = Harness(service)

        cq = await press(panel, OWNER, f"rpl:del:{rule.id}")
        confirm_text, confirm_kbd = cq.edits[-1]
        assert "Delete" in confirm_text and "Promo" in confirm_text
        assert "2" in confirm_text, "the confirmation must state the blast radius"
        assert f"rpl:delok:{rule.id}" in datas(confirm_kbd)
        assert f"rpl:v:{rule.id}" in datas(confirm_kbd), "cancelling returns to the rule"
        # Still there: asking is not deleting.
        assert (await service.get(OWNER, rule.id)).id == rule.id

        await press(panel, OWNER, f"rpl:delok:{rule.id}")
        with pytest.raises(Exception):
            await service.get(OWNER, rule.id)
        listing, _ = await panel._render_rpl_list(OWNER)
        assert "Total: 0" in listing

    asyncio.run(body())


def test_duplicate_opens_the_copy(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        await seed(db, repo)
        rule = await service.create(
            OWNER, {"source": "promo", "to": "offer", "name": "Promo", "task_ids": ["t1"]}
        )
        panel = Harness(service)
        cq = await press(panel, OWNER, f"rpl:dup:{rule.id}")
        text, _ = cq.edits[-1]
        assert "Promo" in text and "promo" in text
        listing, _ = await panel._render_rpl_list(OWNER)
        assert "Total: 2" in listing

    asyncio.run(body())


def test_test_preview_rewrites_without_writing(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        await seed(db, repo)
        rule = await service.create(
            OWNER, {"source": r"\bpromo\b", "to": "OFFER", "mode": "regex",
                    "name": "Promo", "task_ids": ["t1"]}
        )
        panel = Harness(service)
        await press(panel, OWNER, f"rpl:tr:{rule.id}")
        assert panel._state(OWNER).step == "replace_test_sample"

        msg = FakeMessage(OWNER, "big promo today, promos excluded")
        await panel._step_replace_test_sample(msg, panel._state(OWNER), msg.text)
        text, kbd = msg.sent[-1]

        assert "OFFER" in text
        # `\bpromo\b` matches "promo" once; "promos" is a different word.
        assert "Matches: **1**" in text
        assert "big OFFER today" in text
        assert any(d == f"rpl:tr:{rule.id}" for d in datas(kbd))

        # The rule itself is unchanged: a preview never writes.
        stored = await service.get(OWNER, rule.id)
        assert stored.to == "OFFER" and stored.source == r"\bpromo\b"

    asyncio.run(body())


def test_empty_replacement_reads_as_a_deletion(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        await seed(db, repo)
        rule = await service.create(
            OWNER, {"source": "@channel", "to": "", "name": "Strip tag", "task_ids": ["t1"]}
        )
        panel = Harness(service)
        text, _ = await panel._render_rpl_detail(OWNER, rule.id)
        assert "(removed)" in text
        _, list_kbd = await panel._render_rpl_list(OWNER)
        assert any("(removed)" in label for label in labels(list_kbd))
        _ = rule

    asyncio.run(body())


# --------------------------------------------------------------------------- #
# hints + catalog
# --------------------------------------------------------------------------- #
def test_every_panel_string_exists_in_all_ten_locales():
    import json

    catalog = json.loads(LOCALES.read_text(encoding="utf-8"))
    source = Path("core/infrastructure/telegram/replace_bot_ui.py").read_text(encoding="utf-8")
    used = set(re.findall(r'_t\("(ui_rpl_[a-z0-9_]+)"', source))
    used |= {f"ui_rpl_filter_{s}" for s in ("all", "enabled", "disabled")}
    used |= {f"ui_rpl_prompt_{f}" for f in ("name", "source", "to", "priority")}

    assert len(catalog) == 10
    for lang, keys in catalog.items():
        missing = sorted(k for k in used if k not in keys)
        assert not missing, f"{lang} is missing {missing}"
    # English is the fallback for all ten, so it must be complete too.
    assert all(k in catalog["en"] for k in used)
