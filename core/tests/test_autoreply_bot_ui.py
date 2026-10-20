"""End-to-end tests for the Telegram auto-reply panel (v28).

The panel is a mixin, so it can be exercised against a harness that supplies
only the bot-plumbing it borrows from ``ProBotUI`` (translation, keyboard
building, state persistence) and the *real* ``AutoReplyService`` over a real
SQLite database. That way the assertions are about what the buttons actually do
to a tenant's data, not about a mock's return value.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from core.application.autoreply.service import AutoReplyService
from core.infrastructure.persistence.autoreply_repositories import AutoReplyRepository
from core.infrastructure.telegram.autoreply_bot_ui import AutoReplyBotMixin
from core.infrastructure.telegram.i18n import I18n

OWNER = 5094837833
OTHER = 111222333

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
            if flt is None:
                continue
            pattern = getattr(flt, "p", None)
            if pattern is None:
                continue
            if re.match(pattern.pattern, data):
                return fn
        raise AssertionError(f"no handler matched {data!r}")


class FakeMessage:
    def __init__(self, uid: int, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.sent: List[tuple] = []
        self._text = text

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


class Harness(AutoReplyBotMixin):
    """The panel with the minimum bot plumbing it depends on."""

    def __init__(self, service: Any) -> None:
        self._autoreply = service
        self.i18n = I18n()
        self.bot = FakeBot()
        self._states: Dict[int, Any] = {}
        self._sessions = None
        self.register_autoreply_handlers()

    # translation / rendering
    def _t(self, key: str, lang: Optional[str] = None, **kw) -> str:
        return self.i18n.t(key, lang=lang or "en", **kw)

    def _btn(self, key: str, lang: Optional[str] = None, style: Optional[str] = None, **kw) -> str:
        return self.i18n.t(key, lang=lang or "en", **kw)

    def _kbd(self, rows):
        # ProBotUI turns these into InlineKeyboardMarkup; the panel logic only
        # ever cares about (label, callback_data) pairs.
        return {"rows": [list(r) for r in rows]}

    def _localized_handler(self, handler):
        return handler

    def _lang_for(self, *sources, uid: Optional[int] = None) -> str:
        return "en"

    async def _get_user_lang(self, uid: int, user: Any = None) -> str:
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
@pytest.fixture()
def service(tmp_path: Path) -> AutoReplyService:
    from core.infrastructure.persistence.database import SqliteDatabase

    db = SqliteDatabase(str(tmp_path / "panel.db"))
    repo = AutoReplyRepository(db)
    return AutoReplyService(repo)


@pytest.fixture()
def harness(service: AutoReplyService) -> Harness:
    return Harness(service)


def data_of(kbd) -> List[str]:
    """Every callback_data in a keyboard, across all of its rows."""
    return [item[1] for row in kbd["rows"] for item in row]


async def press(harness: Harness, uid: int, data: str) -> FakeCallback:
    cq = FakeCallback(uid, data)
    await harness.bot.find(data)(None, cq)
    return cq


# --------------------------------------------------------------------------- #
# menu
# --------------------------------------------------------------------------- #
def test_menu_shows_off_without_provider_and_offers_the_switch(harness):
    text, kbd = harness._render_ar_menu(OWNER)
    assert "off" in text
    assert harness._t("ui_ar_need_provider") in text
    assert "ar:toggle" in data_of(kbd)
    assert "ar:prompts" in data_of(kbd) and "ar:contacts" in data_of(kbd)
    # Every row is a real (label, callback) pair — no placeholder buttons.
    for row in kbd["rows"]:
        for label, data in row:
            assert label.strip() and data.startswith(("ar:", "pv:"))


async def test_toggle_turns_the_account_on_and_persists_it(harness, service):
    cq = await press(harness, OWNER, "ar:toggle")
    assert service.get_settings(OWNER).enabled is True
    assert cq.answers == [harness._t("ui_ar_enabled_alert")]
    assert "on" in cq.edits[-1][0]

    cq = await press(harness, OWNER, "ar:toggle")
    assert service.get_settings(OWNER).enabled is False
    assert cq.answers == [harness._t("ui_ar_disabled_alert")]


def test_menu_is_tenant_scoped(harness, service):
    service.update_settings(OWNER, {"enabled": True})
    owner_text, _ = harness._render_ar_menu(OWNER)
    other_text, _ = harness._render_ar_menu(OTHER)
    assert "on" in owner_text
    assert "off" in other_text


async def test_mode_buttons_change_targeting(harness, service):
    cq = await press(harness, OWNER, "ar:mode:SELECTED_ONLY")
    assert service.get_settings(OWNER).target_mode.value == "SELECTED_ONLY"
    assert "SELECTED_ONLY" not in cq.answers[0]  # a label, not the enum
    assert harness._t("ui_ar_mode_selected_only") in cq.answers[0]

    await press(harness, OWNER, "ar:mode:ALL_EXCEPT")
    assert service.get_settings(OWNER).target_mode.value == "ALL_EXCEPT"


async def test_unknown_mode_is_refused_without_writing(harness, service):
    before = service.get_settings(OWNER).target_mode.value
    cq = await press(harness, OWNER, "ar:mode:NONSENSE")
    assert service.get_settings(OWNER).target_mode.value == before
    assert cq.answers and cq.answers[0]  # an error toast, not silence


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #
async def test_prompt_creation_two_step_flow_sets_the_default(harness, service):
    await press(harness, OWNER, "ar:pnew")
    assert harness._state(OWNER).step == "ar_prompt_name"

    msg = FakeMessage(OWNER, "Sales — friendly")
    handled = await harness.ar_route_text(msg, harness._state(OWNER), "Sales — friendly")
    assert handled is True
    assert harness._state(OWNER).step == "ar_prompt_text"

    body = "You are a helpful assistant for a small shop. Reply briefly and warmly."
    msg2 = FakeMessage(OWNER, body)
    handled = await harness.ar_route_text(msg2, harness._state(OWNER), body)
    assert handled is True
    assert harness._state(OWNER).step == ""

    prompts = service.list_prompts(OWNER)
    assert [p.name for p in prompts] == ["Sales — friendly"]
    assert prompts[0].system_prompt == body
    assert service.get_settings(OWNER).prompt_id == prompts[0].id
    # The confirmation carries the panel so the user is not dropped on a text screen.
    assert msg2.sent[-1][1] is not None


async def test_short_prompt_body_is_rejected_and_the_step_stays(harness, service):
    harness._state(OWNER).step = "ar_prompt_name"
    await harness.ar_route_text(FakeMessage(OWNER), harness._state(OWNER), "Mine")
    msg = FakeMessage(OWNER, "hi")
    await harness.ar_route_text(msg, harness._state(OWNER), "hi")
    assert harness._state(OWNER).step == "ar_prompt_text"
    assert service.list_prompts(OWNER) == []
    assert harness._t("ui_ar_prompt_too_short") in msg.sent[-1][0]


async def test_empty_prompt_name_is_refused(harness, service):
    harness._state(OWNER).step = "ar_prompt_name"
    msg = FakeMessage(OWNER, "   ")
    await harness.ar_route_text(msg, harness._state(OWNER), "   ")
    assert harness._state(OWNER).step == "ar_prompt_name"
    assert service.list_prompts(OWNER) == []


def test_prompt_list_marks_the_default_and_offers_duplicate(harness, service):
    first = service.save_prompt(OWNER, {"name": "Alpha", "system_prompt": "Be brief and kind."})
    service.save_prompt(OWNER, {"name": "Beta", "system_prompt": "Be formal and precise."})
    service.update_settings(OWNER, {"prompt_id": first.id})

    text, kbd = harness._render_ar_prompts(OWNER)
    assert "Alpha" in text or "Alpha" in json.dumps(kbd["rows"], ensure_ascii=False)
    labels = [row[0][0] for row in kbd["rows"]]
    assert any(lbl.startswith("✅") and "Alpha" in lbl for lbl in labels)
    assert "ar:pdup" in data_of(kbd)
    assert "ar:pnew" in data_of(kbd)


async def test_pick_prompt_and_duplicate_and_clear(harness, service):
    first = service.save_prompt(OWNER, {"name": "Alpha", "system_prompt": "Be brief and kind."})
    second = service.save_prompt(OWNER, {"name": "Beta", "system_prompt": "Be formal and precise."})

    await press(harness, OWNER, f"ar:p:{second.id}")
    assert service.get_settings(OWNER).prompt_id == second.id

    await press(harness, OWNER, "ar:pdup")
    names = sorted(p.name for p in service.list_prompts(OWNER))
    assert len(names) == 3, names

    await press(harness, OWNER, "ar:pnone")
    assert not service.get_settings(OWNER).prompt_id  # NULL or "" both mean "none"


async def test_duplicate_without_a_default_explains_itself(harness, service):
    cq = await press(harness, OWNER, "ar:pdup")
    assert cq.answers == [harness._t("ui_ar_prompt_pick_first")]
    assert service.list_prompts(OWNER) == []


def test_prompt_list_is_tenant_scoped(harness, service):
    service.save_prompt(OTHER, {"name": "Theirs", "system_prompt": "Nothing of yours."})
    text, kbd = harness._render_ar_prompts(OWNER)
    assert "Theirs" not in json.dumps(kbd["rows"], ensure_ascii=False)
    assert text == harness._t("ui_ar_prompts_empty")


# --------------------------------------------------------------------------- #
# contacts
# --------------------------------------------------------------------------- #
async def test_adding_a_contact_switches_the_mode_so_it_takes_effect(harness, service):
    harness._state(OWNER).step = "ar_contact_add"
    msg = FakeMessage(OWNER, "777001")
    await harness.ar_route_text(msg, harness._state(OWNER), "777001")

    targets = service.list_targets(OWNER)
    assert [t.chat_id for t in targets] == ["777001"]
    assert targets[0].is_enabled is True
    # EVERYONE would ignore a list, so adding a contact flips to a list mode.
    assert service.get_settings(OWNER).target_mode.value == "ALL_EXCEPT"
    assert harness._state(OWNER).step == ""


async def test_contact_toggle_flips_a_single_chat(harness, service):
    service.add_targets(OWNER, [{"chat_id": "555", "title": "Sam"}])
    await press(harness, OWNER, "ar:t:555")
    assert service.list_targets(OWNER)[0].is_enabled is False
    await press(harness, OWNER, "ar:t:555")
    assert service.list_targets(OWNER)[0].is_enabled is True


async def test_toggling_a_foreign_contact_is_refused(harness, service):
    service.add_targets(OTHER, [{"chat_id": "999", "title": "Not yours"}])
    cq = await press(harness, OWNER, "ar:t:999")
    assert cq.answers == [harness._t("ui_ar_contact_missing")]
    assert service.list_targets(OTHER)[0].is_enabled is True


def test_contact_list_renders_enabled_state(harness, service):
    service.add_targets(OWNER, [{"chat_id": "1", "title": "On"}, {"chat_id": "2", "title": "Off"}])
    service.set_target_enabled(OWNER, "2", False)
    text, kbd = harness._render_ar_contacts(OWNER)
    labels = [row[0][0] for row in kbd["rows"]]
    assert any(lbl.startswith("✅") and "On" in lbl for lbl in labels)
    assert any(lbl.startswith("⬜️") and "Off" in lbl for lbl in labels)
    assert "ar:cadd" in data_of(kbd)


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
def _rule(service, owner, name="VIP only", enabled=True, priority=50):
    return service.save_rule(owner, {
        "name": name, "is_enabled": enabled, "priority": priority,
        "action": "REPLY", "conditions": {"chat_ids": ["777"]},
        "exclusions": {}, "prompt_id": "", "ai_config_id": "",
        "cooldown_seconds": 0, "language": "", "tone": "", "response_length": "",
    })


async def test_rule_toggle_preserves_every_other_field(harness, service):
    rule = _rule(service, OWNER, priority=70)
    await press(harness, OWNER, f"ar:rt:{rule.id}")
    after = service.get_rule(OWNER, rule.id)
    assert after.is_enabled is False
    assert after.priority == 70
    assert after.conditions == {"chat_ids": ["777"]}
    await press(harness, OWNER, f"ar:rt:{rule.id}")
    assert service.get_rule(OWNER, rule.id).is_enabled is True


async def test_deleting_a_rule_removes_only_that_rule(harness, service):
    keep = _rule(service, OWNER, name="Keep")
    drop = _rule(service, OWNER, name="Drop")
    await press(harness, OWNER, f"ar:rdel:{drop.id}")
    assert [r.id for r in service.list_rules(OWNER)] == [keep.id]


async def test_rule_panel_is_tenant_scoped(harness, service):
    _rule(service, OTHER, name="Theirs")
    text, kbd = harness._render_ar_rules(OWNER)
    assert text == harness._t("ui_ar_rules_empty")
    assert "ar:rt:" not in " ".join(data_of(kbd))


# --------------------------------------------------------------------------- #
# activity
# --------------------------------------------------------------------------- #
def _enqueue(service, owner, chat_id="777", text="hello"):
    from core.domain.autoreply import AutoReplyJob, IncomingMessage, JobStatus

    msg = IncomingMessage(owner_user_id=owner, chat_id=chat_id, message_id=1,
                          text=text, chat_type="private", session_id="s1")
    job = AutoReplyJob(
        owner_user_id=owner, session_id=msg.session_id, chat_id=chat_id,
        chat_title="Sam", source_message_id=msg.message_id, status=JobStatus.PENDING,
        max_attempts=3, next_attempt_at=0, prompt_id="", rule_id=None,
        ai_config_id="", inbound_text=msg.text[:4000], correlation_id="",
    )
    created, stored = service._repo.enqueue(job)
    assert created, "the harness must enqueue a real job"
    return stored


def test_activity_panel_lists_jobs_and_offers_retry_for_failures(harness, service):
    job = _enqueue(service, OWNER)
    claimed = service._repo.claim_next(worker_id="w1")
    assert claimed is not None
    service._repo.mark_failed(claimed.id, claimed.lease_token, "boom",
                              error_code="provider_error", dead_letter=True)

    text, kbd = harness._render_ar_activity(OWNER)
    assert "DEAD_LETTER" in text
    assert f"ar:jr:{job.id}" in data_of(kbd)
    assert "retry" in json.dumps(kbd["rows"], ensure_ascii=False).lower()


async def test_retry_puts_a_dead_letter_back_in_the_queue(harness, service):
    _enqueue(service, OWNER)
    claimed = service._repo.claim_next(worker_id="w1")
    service._repo.mark_failed(claimed.id, claimed.lease_token, "boom",
                              error_code="provider_error", dead_letter=True)
    cq = await press(harness, OWNER, f"ar:jr:{claimed.id}")
    assert cq.answers == [harness._t("ui_ar_job_retried")]
    job = service._repo.get_job(claimed.id, OWNER)
    assert job.status.value in ("PENDING", "RETRY_WAIT")


async def test_retrying_another_tenants_job_is_refused(harness, service):
    _enqueue(service, OTHER)
    claimed = service._repo.claim_next(worker_id="w1")
    service._repo.mark_failed(claimed.id, claimed.lease_token, "boom",
                              error_code="provider_error", dead_letter=True)
    cq = await press(harness, OWNER, f"ar:jr:{claimed.id}")
    assert cq.answers and cq.answers[0] != harness._t("ui_ar_job_retried")
    assert service._repo.get_job(claimed.id, OTHER).status.value == "DEAD_LETTER"


def test_activity_panel_is_empty_for_a_new_account(harness):
    text, _ = harness._render_ar_activity(OTHER)
    assert text == harness._t("ui_ar_activity_empty")


def test_menu_without_the_service_says_so_instead_of_crashing():
    harness = Harness(None)
    text, kbd = harness._render_ar_menu(OWNER)
    assert text == harness._t("ui_ar_not_loaded")
    assert "pv:menu" in data_of(kbd)


# --------------------------------------------------------------------------- #
# catalog parity
# --------------------------------------------------------------------------- #
def test_every_panel_string_exists_in_all_ten_languages():
    catalog = json.loads(LOCALES.read_text(encoding="utf-8"))
    expected = {"en", "es", "fa", "fr", "id", "it", "pt", "ru", "uz", "zh"}
    assert expected <= set(catalog), sorted(set(catalog))
    ar_keys = {k for k in catalog["en"] if k.startswith("ui_ar_")}
    assert len(ar_keys) >= 55
    for lang in expected:
        missing = ar_keys - set(catalog[lang])
        assert not missing, (lang, sorted(missing))
        blanks = [k for k in ar_keys if not catalog[lang][k].strip()]
        assert not blanks, (lang, blanks)


def test_panel_renders_in_every_language_without_raw_keys(harness, service):
    service.save_prompt(OWNER, {"name": "Alpha", "system_prompt": "Be brief and kind."})
    service.add_targets(OWNER, [{"chat_id": "777001", "title": "Sam"}])
    _rule(service, OWNER)
    for lang in ("en", "es", "fa", "fr", "id", "it", "pt", "ru", "uz", "zh"):
        renderers = [
            lambda h=harness, lg=lang: h._render_ar_menu(OWNER, lang=lg),
            lambda h=harness, lg=lang: h._render_ar_modes(lang=lg, uid=OWNER),
            lambda h=harness, lg=lang: h._render_ar_prompts(OWNER, lang=lg),
            lambda h=harness, lg=lang: h._render_ar_contacts(OWNER, lang=lg),
            lambda h=harness, lg=lang: h._render_ar_rules(OWNER, lang=lg),
            lambda h=harness, lg=lang: h._render_ar_activity(OWNER, lang=lg),
        ]
        for idx, render in enumerate(renderers):
            body, kbd = render()
            assert "ui_ar_" not in body, (lang, idx, body)
            for row in kbd["rows"]:
                label, data = row[0]
                assert "ui_ar_" not in label, (lang, idx, label)
                assert "{" not in label and "}" not in label, (lang, idx, label)


def test_every_renderer_has_the_same_call_shape(harness, service):
    """Callbacks share one dispatcher: ``renderer(uid, lang=...)``.

    A renderer whose first positional argument is not the owner raises
    ``TypeError: got multiple values for argument 'lang'`` on every button that
    uses it — the panel section simply never opens.
    """
    service.save_prompt(OWNER, {"name": "Alpha", "system_prompt": "Be brief and kind."})
    service.add_targets(OWNER, [{"chat_id": "777001", "title": "Sam"}])
    for render in (harness._render_ar_menu, harness._render_ar_modes,
                   harness._render_ar_prompts, harness._render_ar_contacts,
                   harness._render_ar_rules, harness._render_ar_activity):
        body, kbd = render(OWNER, lang="fa")
        assert body and kbd["rows"], render.__name__


def test_localization_actually_differs_between_languages(harness):
    en, _ = harness._render_ar_modes(lang="en")
    fa, _ = harness._render_ar_modes(lang="fa")
    zh, _ = harness._render_ar_modes(lang="zh")
    assert fa != en and zh != en
    assert re.search(r"[\u0600-\u06FF]", fa), fa
    assert re.search(r"[\u4e00-\u9fff]", zh), zh


def test_bot_regexes_accept_uuid_ids_and_chat_ids(harness):
    # uuid4 strings are what the service returns for prompt/rule/job ids.
    samples = {
        "ar:p:3f2b1c9e-1a2b-4c3d-8e9f-0a1b2c3d4e5f": True,
        "ar:rt:3f2b1c9e-1a2b-4c3d-8e9f-0a1b2c3d4e5f": True,
        "ar:rdel:3f2b1c9e-1a2b-4c3d-8e9f-0a1b2c3d4e5f": True,
        "ar:jr:3f2b1c9e-1a2b-4c3d-8e9f-0a1b2c3d4e5f": True,
        "ar:t:-1001234567890": True,
        "ar:t:777001": True,
        "ar:mode:SELECTED_ONLY": True,
        "ar:mode:ALL_EXCEPT": True,
        "ar:menu": True,
        "ar:bogus": False,
    }
    for data, ok in samples.items():
        try:
            harness.bot.find(data)
            matched = True
        except AssertionError:
            matched = False
        assert matched is ok, data
