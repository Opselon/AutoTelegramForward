r"""Regression tests for the Blacklist/Whitelist → Add Regex workflow.

The production defect these tests pin (evidence: ``atf-logger`` error rows
``route_text failed for <uid> step=tw_regex_<rule-id>: ButtonDataInvalid``)
was a *post-save* crash inside ``_step_tw_regex``:

  1. The user submits a valid pattern; the pattern is validated and persisted.
  2. The step handler re-renders the whitelist/blacklist menu to show the
     saved state.
  3. ``_render_whitelist_menu`` builds one **Edit / Delete button per stored
     entry**, embedding the raw pattern in the callback data
     (``rfl_wl_edit:<rule-id>:<pattern>``).
  4. Telegram rejects the whole ``send_message`` call with
     ``BUTTON_DATA_INVALID`` whenever any single button exceeds its 64-byte
     callback-data ceiling — which any ordinary regex attached to a UUID rule
     id easily does (``\bWhale\s+Update\b`` on a 36-char id is 70 bytes).

So the rule *was* saved, but the confirmation screen never appeared: the save
message itself raised, the exception was surfaced as an internal-error toast,
and the panel looked frozen right at the moment the user expected feedback.

The fix keeps per-entry management but addresses entries by a short, stable
index (with the rule's ``version`` as the staleness guard — the same pattern
``_resolve_replace_click`` already uses) instead of embedding the pattern.

These tests must pass both before and after the fix for everything *except*
the render contract they pin.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.domain.entities import ForwardRule, _heal_regex
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.pro_bot_ui import ProBotUI

OWNER = 5094837833
OTHER = 6753459624

RULE_ID = "16e817ea-e6bd-4499-8b2b-42f8b734e4c7"
# The exact production pattern: a 22-byte regex that, glued onto the 36-byte
# rule id, pushes the per-entry button payload to 70 bytes — past Telegram's
# 64-byte callback-data ceiling.
PRODUCTION_PATTERN = r"\bWhale\s+Update\b"
# The corrupted form found in the live DB (a *literal* doubled backslash).
CORRUPTED_PATTERN = r"\\bWhale\\s+Update\\b"


# --------------------------------------------------------------------------- #
# Fakes — the same seams test_bot_route_panel drives
# --------------------------------------------------------------------------- #

class FakeBot:
    def __init__(self) -> None:
        self.callbacks = []

    def __getattr__(self, name):
        if not name.startswith("on_"):
            raise AttributeError(name)

        def register(*args, **kwargs):
            def decorator(fn):
                if name == "on_callback_query":
                    self.callbacks.append(fn)
                return fn

            return decorator

        return register


class FakeRules:
    def __init__(self, rules=()):
        self.rules = list(rules)
        self.updated = []

    async def get_owned(self, rule_id, owner_user_id):
        for r in self.rules:
            if r.id != rule_id:
                continue
            if owner_user_id is not None and r.owner_user_id != owner_user_id:
                return None
            return r
        return None

    async def list_by_owner(self, owner_user_id):
        return list(self.rules)

    async def update_owned(self, rule, owner_user_id, expected_version=None):
        self.updated.append(rule)
        return rule


class FakeCQ:
    def __init__(self, data, uid=OWNER):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


class FakeMessage:
    def __init__(self, text, uid=OWNER):
        self.text = text
        self.from_user = SimpleNamespace(id=uid)
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def build_ui(rules=()):
    bot = FakeBot()
    ui = ProBotUI(
        bot=bot,
        i18n=I18n(),
        sessions=SimpleNamespace(),
        rules=FakeRules(rules),
        filter_rules=SimpleNamespace(),
        ai_configs=SimpleNamespace(),
        pipeline=SimpleNamespace(),
        pool=SimpleNamespace(),
        login=SimpleNamespace(),
        log_client=None,
        admin_ids=[],
    )
    return ui, bot


def handler(bot, name):
    for fn in bot.callbacks:
        seen = fn
        while seen is not None:
            if getattr(seen, "__name__", "") == name:
                return fn
            seen = getattr(seen, "__wrapped__", None)
    raise AssertionError(f"handler {name} was never registered")


def callbacks_of(markup):
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            if getattr(btn, "callback_data", None):
                out.append(
                    btn.callback_data.decode("utf-8")
                    if isinstance(btn.callback_data, bytes)
                    else btn.callback_data
                )
    return out


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def a_rule(**kwargs):
    rule = ForwardRule(id=RULE_ID, session_id="s1", source_chat_id="-1001",
                       target_chat_id="-1002", owner_user_id=OWNER)
    for key, value in kwargs.items():
        setattr(rule, key, value)
    return rule


async def add_regex_flow(ui, bot, direction="require", pattern=PRODUCTION_PATTERN,
                         uid=OWNER, rule_id=RULE_ID):
    """Drive the full Add Regex conversation: open the menu, tap Add Regex,
    submit the pattern — and return the message the bot answered with."""
    menu_cb = "rfl_wl:rule" if False else f"rfl_{'wl' if direction == 'require' else 'bl'}:{rule_id}"
    regex_cb = f"rfl_{'wl' if direction == 'require' else 'bl'}_regex:{rule_id}"

    cq = FakeCQ(menu_cb, uid=uid)
    await handler(bot, f"_cb_rule_{'whitelist' if direction == 'require' else 'blacklist'}_menu")(None, cq)

    cq = FakeCQ(regex_cb, uid=uid)
    await handler(bot, f"_cb_rule_{'w' if direction == 'require' else 'b'}l_regex")(None, cq)

    message = FakeMessage(pattern, uid=uid)
    st = ui._state(uid)
    st.buffer["tw_direction"] = direction
    st.step = f"tw_regex_{rule_id}"
    await ui._route_text(message)
    return message


def every_callback_in(markup):
    """All callback payloads on a markup, as bytes for the Telegram limit."""
    return [d.encode("utf-8") for d in callbacks_of(markup)]


# --------------------------------------------------------------------------- #
# The original defect: the post-save confirmation render blows past the
# 64-byte callback-data ceiling and the whole save message is rejected.
# --------------------------------------------------------------------------- #

class TestAddRegexDoesNotBreakThePanel:
    def test_a_whitelist_regex_save_renders_a_usable_menu(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        message = run(add_regex_flow(ui, bot, direction="require"))

        # The save itself happened.
        assert rule.regex_require == [PRODUCTION_PATTERN]
        assert ui._rules.updated, "the rule must be persisted"
        # And the bot answered — the panel did not freeze.
        assert message.replies, "a saved regex must produce a confirmation message"

    def test_a_blacklist_regex_save_renders_a_usable_menu(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        message = run(add_regex_flow(ui, bot, direction="block"))

        assert rule.regex_block == [PRODUCTION_PATTERN]
        assert ui._rules.updated
        assert message.replies, "a saved regex must produce a confirmation message"

    def test_every_button_on_the_saved_menu_fits_telegrams_64_byte_limit(self):
        """The exact production failure: the *whole* message is rejected when
        one button's callback data is too large, so the user sees the save
        toast fail while the rule is already stored."""
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        message = run(add_regex_flow(ui, bot, direction="require"))

        assert message.replies, "the confirmation must actually be sendable"
        _text, markup = message.replies[-1]
        for data in every_callback_in(markup):
            assert len(data) <= 64, (
                "a button exceeds Telegram's 64-byte callback-data ceiling and "
                f"Telegram rejects the entire message: {data!r}"
            )

    def test_the_saved_entry_is_visible_on_the_menu(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        message = run(add_regex_flow(ui, bot, direction="require"))
        _toast, markup = message.replies[-1]

        # The toast confirms the save; the menu rendered underneath carries the
        # actual pattern, both as a listed row and on its per-entry buttons.
        menu_text, menu_markup = ui._render_whitelist_menu(rule)
        assert PRODUCTION_PATTERN in menu_text, "the menu must list the saved pattern"
        labels = [btn.text for row in menu_markup.inline_keyboard for btn in row]
        assert any(PRODUCTION_PATTERN[:12] in lbl for lbl in labels), (
            "the per-entry row must be labelled with the saved pattern"
        )

    def test_a_regex_only_whitelist_is_not_advertised_as_open(self):
        """A whitelist with no keywords but an active regex is still a
        constraint. The menu must not claim "all messages allowed"."""
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, direction="require"))

        text, _markup = ui._render_whitelist_menu(rule)

        assert "all messages allowed" not in text, (
            "an active regex contradicts the empty-keywords line"
        )

    def test_a_regex_only_blacklist_is_not_advertised_as_empty(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, direction="block"))

        text, _markup = ui._render_blacklist_menu(rule)

        assert "No keywords in blacklist." not in text, (
            "an active regex contradicts the empty-keywords line"
        )

    def test_the_regex_appears_only_on_the_list_it_was_saved_to(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        run(add_regex_flow(ui, bot, direction="require"))
        assert rule.regex_require == [PRODUCTION_PATTERN]
        assert rule.regex_block == [], "a whitelist regex must never land on the blacklist"

        rule2 = a_rule()
        ui2, bot2 = build_ui(rules=[rule2])
        run(add_regex_flow(ui2, bot2, direction="block"))
        assert rule2.regex_block == [PRODUCTION_PATTERN]
        assert rule2.regex_require == [], "a blacklist regex must never land on the whitelist"


# --------------------------------------------------------------------------- #
# Recovery paths: invalid input must not freeze the conversation
# --------------------------------------------------------------------------- #

class TestAddRegexRecovery:
    def test_an_invalid_pattern_keeps_the_rule_and_explains_itself(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        message = run(add_regex_flow(ui, bot, pattern="([", direction="require"))

        assert rule.regex_require == [], "an invalid pattern must not be persisted"
        assert ui._rules.updated == []
        assert message.replies, "the user must be told why, not left waiting"
        assert "regular expression" in message.replies[-1][0].lower(), (
            "the refusal must say the pattern is not a valid regex"
        )

    def test_an_empty_submission_reprompts_instead_of_freezing(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        message = run(add_regex_flow(ui, bot, pattern="   ", direction="require"))

        assert rule.regex_require == []
        assert message.replies

    def test_a_pathological_pattern_is_refused_before_it_is_stored(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        message = run(add_regex_flow(ui, bot, pattern="(a+)+$", direction="block"))

        assert rule.regex_block == [], "a catastrophic-backtracking pattern must be refused"
        assert message.replies

    def test_backslashes_survive_the_round_trip_verbatim(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        run(add_regex_flow(ui, bot, pattern=PRODUCTION_PATTERN, direction="require"))

        assert rule.regex_require == [PRODUCTION_PATTERN]
        # And it survives a re-read through the stored policy dict.
        assert rule.content_policy_config["regex_require"] == [PRODUCTION_PATTERN]


# --------------------------------------------------------------------------- #
# Per-entry management: entries addressed by identity, not by raw value
# --------------------------------------------------------------------------- #

class TestPerEntryManagementButtons:
    def _saved(self, direction="require"):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, direction=direction))
        return rule, ui, bot

    def test_every_per_entry_button_fits_the_64_byte_limit(self):
        for direction in ("require", "block"):
            rule, ui, bot = self._saved(direction)
            render = ui._render_whitelist_menu if direction == "require" else ui._render_blacklist_menu
            _text, markup = render(rule)
            for data in every_callback_in(markup):
                assert len(data) <= 64, f"{direction}: {data!r}"

    def test_edit_and_delete_buttons_exist_for_a_saved_regex(self):
        rule, ui, bot = self._saved("require")
        _text, markup = ui._render_whitelist_menu(rule)
        cbs = callbacks_of(markup)
        assert any(c.startswith("rfl_wl_edit:") for c in cbs), "an Edit button must exist"
        assert any(c.startswith("rfl_wl_del:") for c in cbs), "a Delete button must exist"
        assert any(c.startswith("rfl_wl_tgl:") for c in cbs), "the list switch must exist"

    def test_the_blacklist_menu_has_its_own_edit_and_delete(self):
        rule, ui, bot = self._saved("block")
        _text, markup = ui._render_blacklist_menu(rule)
        cbs = callbacks_of(markup)
        assert any(c.startswith("rfl_bl_edit:") for c in cbs)
        assert any(c.startswith("rfl_bl_del:") for c in cbs)
        # Category separation: no whitelist callback may appear here.
        assert not any(c.startswith("rfl_wl_") for c in cbs)

    def test_the_whitelist_menu_carries_no_blacklist_callbacks(self):
        rule, ui, bot = self._saved("require")
        _text, markup = ui._render_whitelist_menu(rule)
        cbs = callbacks_of(markup)
        assert not any(c.startswith("rfl_bl_") for c in cbs)

    def test_deleting_one_regex_leaves_the_others_intact(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, pattern=r"\b5x\b", direction="require"))
        run(add_regex_flow(ui, bot, pattern=r"\b10x\b", direction="require"))
        assert rule.regex_require == [r"\b5x\b", r"\b10x\b"]

        _text, markup = ui._render_whitelist_menu(rule)
        cbs = [c for c in callbacks_of(markup) if c.startswith("rfl_wl_del:")]
        assert cbs, "there must be a per-entry delete button"
        cq = FakeCQ(cbs[0])
        run(handler(bot, "_cb_rule_whitelist_delete_entry")(None, cq))

        assert len(rule.regex_require) == 1, "only the addressed entry moved"
        assert ui._rules.updated, "the deletion persisted"
        assert cq.answers, "the user gets feedback"

    def test_editing_one_regex_does_not_touch_the_others(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, pattern=r"\b5x\b", direction="require"))
        run(add_regex_flow(ui, bot, pattern=r"\b10x\b", direction="require"))

        _text, markup = ui._render_whitelist_menu(rule)
        edit_cbs = [c for c in callbacks_of(markup) if c.startswith("rfl_wl_edit:")]
        cq = FakeCQ(edit_cbs[0])
        run(handler(bot, "_cb_rule_whitelist_edit_entry")(None, cq))

        st = ui._state(OWNER)
        # The edit step is armed with the real entry value, whatever it is.
        assert st.step.startswith("edit_wl_")
        assert st.buffer.get("edit_entry")

        message = FakeMessage(r"\b25x\b", uid=OWNER)
        run(ui._route_text(message))

        assert len(rule.regex_require) == 2, "the edit replaced one entry, not clobbered the list"
        assert r"\b25x\b" in rule.regex_require

    def test_the_list_switch_parks_a_list_without_clearing_it(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, direction="require"))
        pattern_count = len(rule.regex_require)

        cq = FakeCQ(f"rfl_wl_tgl:{rule.id}")
        run(handler(bot, "_cb_rule_whitelist_toggle")(None, cq))

        assert rule.whitelist_off is True
        assert len(rule.regex_require) == pattern_count, "parking must not delete the words"

        cq = FakeCQ(f"rfl_wl_tgl:{rule.id}")
        run(handler(bot, "_cb_rule_whitelist_toggle")(None, cq))
        assert rule.whitelist_off is False

    def test_a_stale_click_is_refused_and_re_renders(self):
        """A click carrying a version the rule has moved on from must not act
        on whatever entry slid into that slot."""
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, pattern=r"\b5x\b", direction="require"))

        _text, markup = ui._render_whitelist_menu(rule)
        stale_cb = [c for c in callbacks_of(markup) if c.startswith("rfl_wl_del:")][0]
        # The rule moves on after this render, so the rendered click is stale.
        rule.version = int(rule.version) + 1
        before = list(rule.regex_require)

        cq = FakeCQ(stale_cb)
        run(handler(bot, "_cb_rule_whitelist_delete_entry")(None, cq))

        assert rule.regex_require == before, "a stale click deleted nothing"
        assert cq.answers and cq.answers[-1][1] is True, "the user is alerted"
        assert cq.edits, "the menu re-rendered with the current state"


# --------------------------------------------------------------------------- #
# Authorization: a stranger's tap must never touch another tenant's task
# --------------------------------------------------------------------------- #

class TestAuthorization:
    def test_a_stranger_cannot_add_a_regex_to_another_users_task(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])

        message = run(add_regex_flow(ui, bot, direction="require", uid=OTHER))

        assert rule.regex_require == [], "no write may land on a task the caller does not own"
        assert ui._rules.updated == []

    def test_a_stranger_cannot_toggle_or_delete_another_users_entries(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, direction="require"))
        assert rule.regex_require

        for data, name in (
            (f"rfl_wl_tgl:{rule.id}", "_cb_rule_whitelist_toggle"),
            (f"rfl_wl_clr:{rule.id}", "_cb_rule_whitelist_clear"),
        ):
            cq = FakeCQ(data, uid=OTHER)
            run(handler(bot, name)(None, cq))
            assert cq.answers and cq.answers[-1][1] is True, f"{name} must refuse with an alert"
        assert rule.regex_require, "nothing was cleared or toggled by the stranger"
        assert rule.whitelist_off is False

    def test_a_forged_index_callback_still_cannot_act(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, direction="require"))
        saves_before = len(ui._rules.updated)

        # Forged with the real rule id and a plausible index/version, but a
        # stranger identity: ownership is re-checked on every callback.
        forged = f"rfl_wl_del:{rule.id}:0:{rule.version}"
        cq = FakeCQ(forged, uid=OTHER)
        run(handler(bot, "_cb_rule_whitelist_delete_entry")(None, cq))
        assert rule.regex_require == [PRODUCTION_PATTERN], "the entry survived"
        assert len(ui._rules.updated) == saves_before, "the stranger wrote nothing"


# --------------------------------------------------------------------------- #
# The saved state is the authoritative state the engine enforces
# --------------------------------------------------------------------------- #

class TestSavedStateIsAuthoritative:
    def test_a_saved_whitelist_regex_is_enforced_by_the_engine(self):
        from core.domain.content_policy import preview_match

        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, pattern=PRODUCTION_PATTERN, direction="require"))

        policy = rule.policy_chain()[0]
        preview = preview_match("Whale Update: big move", regex_require=policy.regex_require)
        assert preview.allowed, "a message matching the saved whitelist regex must pass"

        preview = preview_match("Nothing relevant here", regex_require=policy.regex_require)
        assert not preview.allowed, "a non-matching message must be dropped"

    def test_the_saved_pattern_matches_the_users_intent_not_an_escaped_copy(self):
        from core.domain.content_policy import compile_user_regex

        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, pattern=PRODUCTION_PATTERN, direction="require"))

        compiled = compile_user_regex(rule.regex_require[0])
        assert compiled is not None, "the stored pattern must still compile"
        assert compiled.search("Whale Update") is not None
        # The production corruption matched the literal text instead.
        assert compile_user_regex(CORRUPTED_PATTERN).search("Whale Update") is None

    def test_backslash_corruption_is_healed_on_read(self):
        """The corruption seen in production: a stored *literal* doubled
        backslash instead of the escape the user typed. The engine must see
        the user's pattern when the rule is read back."""
        from core.domain.content_policy import compile_user_regex

        rule = a_rule()
        rule.content_policy_config = {"regex_require": [CORRUPTED_PATTERN]}
        assert rule.regex_require == [PRODUCTION_PATTERN], "healed on read"
        assert compile_user_regex(rule.regex_require[0]).search("Whale Update") is not None


# --------------------------------------------------------------------------- #
# _heal_regex: the storage corruption seen in production
# --------------------------------------------------------------------------- #

class TestHealRegex:
    def test_a_doubled_backslash_pattern_is_unfolded(self):
        assert _heal_regex(CORRUPTED_PATTERN) == PRODUCTION_PATTERN

    def test_an_already_correct_pattern_is_untouched(self):
        assert _heal_regex(PRODUCTION_PATTERN) == PRODUCTION_PATTERN

    def test_a_pattern_without_backslashes_is_untouched(self):
        assert _heal_regex("[a-z]+") == "[a-z]+"

    def test_a_doubled_numeric_escape_is_unfolded(self):
        assert _heal_regex(r"\\d{3}") == r"\d{3}"

    def test_a_pattern_with_a_single_backslash_is_untouched(self):
        # A genuine regex, or a literal the user typed on purpose.
        assert _heal_regex(r"\d{3}") == r"\d{3}"

    def test_a_pattern_with_mixed_backslashes_is_untouched(self):
        # One real doubled escape plus a single one: ambiguous, so never guess.
        assert _heal_regex(r"\b($|:)(\\d{3})\b") == r"\b($|:)(\\d{3})\b"

    def test_an_invalid_unfolded_form_is_refused(self):
        # Unfolding would produce something the engine rejects: keep the stored text.
        assert _heal_regex(r"\\") == r"\\"

    def test_non_string_input_is_coerced(self):
        assert _heal_regex(42) == "42"

    def test_healing_applies_on_the_rule_property(self):
        rule = a_rule()
        rule.content_policy_config = {"regex_require": [CORRUPTED_PATTERN]}
        assert rule.regex_require == [PRODUCTION_PATTERN], (
            "a rule read back from the database must see the pattern the user typed"
        )


# --------------------------------------------------------------------------- #
# Conversation state: every step can be cancelled and nothing leaks
# --------------------------------------------------------------------------- #

class TestConversationState:
    def test_the_state_is_cleared_after_a_successful_save(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, direction="require"))
        st = ui._state(OWNER)
        assert st.step == ""
        assert "tw_direction" not in st.buffer

    def test_opening_the_blacklist_menu_clears_a_pending_regex_step(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        st = ui._state(OWNER)
        st.step = f"tw_regex_{rule.id}"
        st.buffer["tw_direction"] = "require"
        ui._states[OWNER] = st

        cq = FakeCQ(f"rfl_bl:{rule.id}")
        run(handler(bot, "_cb_rule_blacklist_menu")(None, cq))

        st = ui._state(OWNER)
        assert st.step == "", "navigating away must settle the pending prompt"
        assert "tw_direction" not in st.buffer

    def test_two_users_do_not_share_conversation_state(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        st_a = ui._state(OWNER)
        st_a.step = f"tw_regex_{rule.id}"
        st_a.buffer["tw_direction"] = "require"
        st_b = ui._state(OTHER)
        st_b.step = ""
        st_b.buffer["tw_direction"] = "block"

        assert st_a is not st_b
        assert st_a.buffer.get("tw_direction") == "require"
        assert st_b.buffer.get("tw_direction") == "block"

    def test_a_duplicate_submission_does_not_duplicate_the_rule(self):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        run(add_regex_flow(ui, bot, pattern=PRODUCTION_PATTERN, direction="require"))
        run(add_regex_flow(ui, bot, pattern=PRODUCTION_PATTERN, direction="require"))

        assert rule.regex_require == [PRODUCTION_PATTERN], "the same pattern is stored once"
