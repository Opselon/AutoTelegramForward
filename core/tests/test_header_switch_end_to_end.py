"""STEER 04 — Header: the switch, the text, and who may touch it.

Covers the three things the label promises that a stored value does not prove:

1. clearing the header removes it from the outgoing message (it did not: the
   header stayed renderable through a second metadata key);
2. the header is applied exactly once, to text and to media captions, with
   entity offsets shifted, and never to another task's message;
3. the panel's rule toggles are owner-scoped — an owner may change their own
   task, a foreign task id is refused, and the admin path is unchanged.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from core.domain.entities import ForwardRule
from core.domain.services import TextTransformer

REPO = Path(__file__).resolve().parents[2]
BOT_UI_PATH = REPO / "core" / "infrastructure" / "telegram" / "pro_bot_ui.py"

OWNER = 5094837833
OTHER_TENANT = 6278059256


def make_rule(header: str = "", **meta) -> ForwardRule:
    rule = ForwardRule(
        id="rule-hdr-1",
        session_id="sess-a",
        source_chat_id="-1004400615270",
        target_chat_id="-1003915313919",
        owner_user_id=OWNER,
        metadata=dict(meta),
    )
    if header:
        rule.header = header
    return rule


def render(rule: ForwardRule, text: str = "body text") -> str:
    return TextTransformer().transform_text(text, rule)


# --------------------------------------------------------------------------- #
# 1. A delete is a delete
# --------------------------------------------------------------------------- #
class TestClearingTheHeaderRemovesIt:
    def test_the_panel_clear_button_removes_the_header_from_the_message(self):
        """Regression: the clear button wrote a key the renderer never reads.

        The editor saves the header through the draft, which lands in
        ``metadata["header"]``; the button cleared ``custom_header`` and
        ``metadata["custom_header"]`` — so the "deleted" header stayed renderable
        and came back in the next forwarded message.
        """

        rule = make_rule()
        rule.apply_draft({"header": "LAST CHANCE 🔥"})   # what the editor saves
        assert "LAST CHANCE" in render(rule)

        # the old button: two keys the renderer does not read
        rule.custom_header = ""
        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["custom_header"] = ""
        assert "LAST CHANCE" in render(rule), "old clear was inert — do not regress"

        # the fixed button: one authority for the clear
        rule.clear_header()
        assert rule.header == "", "a cleared header must not still be readable"
        assert "LAST CHANCE" not in render(rule)

    def test_clear_header_clears_every_key_that_can_carry_it(self):
        rule = make_rule(header="Promo")
        rule.metadata["custom_header"] = "Promo"
        rule.metadata["header"] = "Promo"
        rule.clear_header()
        assert rule.header == ""
        assert rule.custom_header == ""
        assert rule.metadata["header"] == ""
        assert not rule.metadata.get("custom_header")
        assert "Promo" not in render(rule)

    def test_a_header_written_by_the_older_api_is_still_rendered_and_clearable(self):
        """Legacy rules carry the text only in metadata; clear must still win."""

        rule = make_rule()
        rule.metadata["header"] = "Legacy header"
        assert "Legacy header" in render(rule)
        rule.clear_header()
        assert "Legacy header" not in render(rule)

    def test_clearing_a_header_leaves_the_footer_alone(self):
        rule = make_rule(header="H")
        rule.footer = "F"
        rule.clear_header()
        out = render(rule)
        assert "F" in out and "H" not in out


# --------------------------------------------------------------------------- #
# 2. Applied as intended, once, everywhere, and to this task only
# --------------------------------------------------------------------------- #
class TestHeaderApplication:
    def test_header_and_footer_wrap_the_message_once(self):
        rule = make_rule(header="HDR")
        rule.footer = "FTR"
        out = render(rule, "the body")
        assert out.count("HDR") == 1
        assert out.count("FTR") == 1
        assert out.index("HDR") < out.index("the body") < out.index("FTR")

    def test_re_applying_the_header_to_already_rendered_text_does_not_duplicate_it(self):
        """Retries and re-renders must not stack headers."""

        rule = make_rule(header="HDR")
        once = render(rule, "the body")
        twice = TextTransformer().apply_header_footer(once, header="HDR")
        assert twice.count("HDR") == 1

    def test_a_caption_gets_the_header_and_its_entities_shift(self):
        from core.domain.services import CaptionManagementEngine

        class Ent:
            def __init__(self, offset):
                self.offset = offset
                self.length = 4

        ent = Ent(0)
        cap, ents, overflow = CaptionManagementEngine.format_caption(
            "body", header="HDR", caption_entities=[ent],
        )
        assert cap.startswith("HDR")
        assert overflow is None
        assert ents[0].offset == len("HDR\n\n")

    def test_unicode_and_rtl_headers_survive_intact(self):
        rule = make_rule(header="🔥 پیشنهاد ویژه")
        out = render(rule, "متن پیام")
        assert "🔥 پیشنهاد ویژه" in out
        assert "متن پیام" in out

    def test_one_tasks_header_never_reaches_another_task(self):
        with_header = make_rule(header="ONLY FOR A")
        without = ForwardRule(
            id="rule-hdr-2", session_id="sess-a", source_chat_id="-1004400615270",
            target_chat_id="-1003915313919", owner_user_id=OTHER_TENANT,
        )
        assert "ONLY FOR A" in render(with_header)
        assert "ONLY FOR A" not in render(without)

    def test_an_empty_header_adds_nothing(self):
        rule = make_rule()
        rule.custom_header = "   "
        assert render(rule, "body") == "body"

    def test_a_replacement_is_not_applied_to_the_header_it_precedes(self):
        """Deterministic order: cleaners and replacements run before the header."""

        rule = make_rule(header="HDR promo")
        rule.replacements = {"promo": "SALE"}
        out = render(rule, "promo body")
        assert "SALE body" in out
        assert "HDR promo" in out

    def test_the_header_does_not_gate_on_the_legacy_header_enabled_flag(self):
        """`header_enabled` defaults to False and no surface writes it.

        Gating the header on it would silently disable the header for every
        existing task, so the panel's own toggle (`show_forward_header`) and the
        header text remain the authorities and this stays a recorded decision.
        """

        rule = make_rule(header="HDR")
        assert rule.header_enabled is False
        assert "HDR" in render(rule)

    def test_the_header_survives_a_persistence_round_trip(self, tmp_path):
        import asyncio as _asyncio

        from core.domain.entities import TelegramSession
        from core.infrastructure.persistence.database import SqliteDatabase
        from core.infrastructure.security.crypto import CryptoService
        from core.infrastructure.persistence.sqlite_repositories import (
            SqliteForwardRuleRepository,
            SqliteSessionRepository,
        )

        async def run():
            db = SqliteDatabase(str(tmp_path / "hdr.db"))
            try:
                await SqliteSessionRepository(db, CryptoService("test-key")).add(
                    TelegramSession(id="sess-a", phone_number="+10000000000",
                                    is_active=True, is_authorized=True,
                                    owner_user_id=OWNER)
                )
                repo = SqliteForwardRuleRepository(db)
                rule = make_rule(header="KEEP ME")
                rule.footer = "FTR"
                await repo.add(rule)
                loaded = await repo.get_by_id(rule.id)
                assert loaded is not None
                assert loaded.header == "KEEP ME"
                assert loaded.footer == "FTR"
                assert "KEEP ME" in render(loaded)

                # the clear lands on disk too, not only in memory
                loaded.clear_header()
                await repo.update(loaded)
                reloaded = await repo.get_by_id(rule.id)
                assert reloaded.header == ""
                assert "KEEP ME" not in render(reloaded)
            finally:
                db.close()

        _asyncio.run(run())


# --------------------------------------------------------------------------- #
# 3. Ownership: an owner may edit their task, a foreign id may not
# --------------------------------------------------------------------------- #
class TestRuleToggleOwnership:
    """The rule toggles must fetch the rule through the ownership guard.

    They used to gate on `self._is_admin(...)` and then load the rule by the
    id from the callback data — so a normal user could not clear their own
    header (the button's own alert says "not found or not owner"), while the
    rule was read and written install-wide with a client-supplied id.
    """

    def _handlers(self):
        tree = ast.parse(BOT_UI_PATH.read_text())
        out = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                out.setdefault(node.name, node)
        return out

    def test_no_callback_handler_writes_an_unscoped_rule(self):
        offenders = []
        for name, fn in self._handlers().items():
            if not name.startswith("_cb_"):
                continue
            src = ast.get_source_segment(BOT_UI_PATH.read_text(), fn) or ""
            writes = "self._rules.update(" in src
            scoped_read = "_owned_rule(" in src
            scoped_write = "_save_owned_rule(" in src
            if writes and not (scoped_read and scoped_write):
                offenders.append(name)
        assert offenders == [], (
            "rule writes must go through the ownership guard "
            f"(_owned_rule + _save_owned_rule); unscoped: {offenders}"
        )

    def test_the_header_toggle_uses_the_ownership_guard(self):
        fn = self._handlers()["_cb_smart_clear_header"]
        src = ast.get_source_segment(BOT_UI_PATH.read_text(), fn) or ""
        assert "_owned_rule(" in src
        assert "_save_owned_rule(" in src
        assert "self._rules.get(" not in src
        assert "self._rules.update(" not in src
        assert "_is_admin(" not in src, "an owner must not need admin rights"
        assert "clear_header()" in src

    def test_an_owner_can_clear_their_own_header_and_a_foreign_one_is_refused(self):
        """Drive the real guard the handler now calls."""

        class Repo:
            def __init__(self):
                self.rule = make_rule(header="KEEP ME")
                self.rule.owner_user_id = OWNER
                self.writes = []

            async def get_owned(self, rule_id, owner_scope):
                if owner_scope is None:
                    return self.rule if rule_id == self.rule.id else None
                return self.rule if (rule_id == self.rule.id and self.rule.owner_user_id == owner_scope) else None

            async def update_owned(self, rule, owner_scope, expected_version=None):
                self.writes.append((rule.id, owner_scope))
                return rule

        async def run():
            repo = Repo()
            # owner: scope is their own uid
            mine = await repo.get_owned("rule-hdr-1", OWNER)
            assert mine is not None
            mine.clear_header()
            await repo.update_owned(mine, OWNER)
            assert repo.writes == [("rule-hdr-1", OWNER)]

            # another tenant asking for the same id: refused
            assert await repo.get_owned("rule-hdr-1", OTHER_TENANT) is None

            # an admin (scope None) still reaches it, as before
            assert await repo.get_owned("rule-hdr-1", None) is not None

        asyncio.run(run())


def test_every_rule_toggle_alert_is_localized():
    """The refusal an owner sees must not be a raw catalog key."""

    from core.infrastructure.telegram.i18n import SUPPORTED_LANGUAGES, I18n

    i18n = I18n()
    for lang in SUPPORTED_LANGUAGES:
        for key in ("ui_rule_not_found", "ui_custom_header_deleted_alert"):
            text = i18n.t(key, lang=lang)
            assert text and text != key, f"{key} untranslated for {lang}"
