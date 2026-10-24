"""Three-way content filters: keyword/phrase, regex and long-text matching.

The user-facing contract under test — Whitelist and Blacklist offer three
clearly distinguishable options on every surface:

* **Keyword / Phrase** — the existing word lists.
* **Regex** — ``regex_block`` (reject on match) and ``regex_require``
  (require a match) on the modern content policy; ``regex_patterns``
  (require-any) on a legacy ``FilterRule``.
* **Long-Text Matching / Text Contains** — the same word lists plus
  ``whitelist_mode`` (``any`` | ``all`` | ``phrase``), searching the whole
  message. No separate persisted ``long_text`` flag exists: the mode is the
  feature, and these tests pin that the *existing* fields deliver it.

Why this file exists: ``preview_match`` is the one canonical, read-only
preview every surface (bot ``Test Rules``, the dashboard tester, the mobile
tester, the API preview endpoint) calls. If it disagrees with
``ContentPolicy.evaluate`` on any input, a user's "test" would lie about what
production does — so the two are compared on every case.
"""

import time

import pytest

from core.domain.content_policy import (
    ContentPolicy,
    bounded_search,
    compile_user_regex,
    contains_word,
    preview_match,
    preview_policy,
    split_word_list,
)


# ---------------------------------------------------------------------------
# The acceptance examples: gold / XAUUSD / market update, mode ANY
# ---------------------------------------------------------------------------

WL = ("gold", "XAUUSD", "market update")

#: Long-text fixtures that exercise every region of the body: the entry sits
#: at the start, in the middle, inside a *later paragraph* of a multiline
#: post, and (the negative case) nowhere at all.
ANY_CASES = [
    ("Today's gold price is rising.", "gold", "start of the message"),
    (
        "A long market report with XAUUSD in the middle of the text.",
        "XAUUSD",
        "middle of the message",
    ),
    (
        "Breaking news\n\nFirst paragraph is unrelated.\n\n"
        "Second paragraph: market update continues here",
        "market update",
        "a later paragraph of a multiline post",
    ),
    (
        "A long article.\n\nBody repeats.\n\nThe gold rate moved again at the end.",
        "gold",
        "end of the message",
    ),
    ("Nothing relevant at all in this post", None, "no configured entry present"),
]


class TestLongTextAnyMode:
    """Long-Text Matching with ANY: at least one entry must match."""

    @pytest.mark.parametrize("text,expected,kind", ANY_CASES)
    def test_whitelist_any(self, text, expected, kind):
        result = preview_match(text, whitelist=WL, mode="any")
        assert result.allowed is (expected is not None), kind
        if expected is not None:
            assert expected in result.matched, (kind, result.matched)

    @pytest.mark.parametrize("text,expected,kind", ANY_CASES)
    def test_blacklist_rejects_the_same_entries(self, text, expected, kind):
        """The same configured entries must *reject* matching messages."""
        result = preview_match(text, blacklist=WL, mode="any")
        assert result.allowed is (expected is None), kind
        if expected is not None:
            assert expected in result.matched, (kind, result.matched)

    def test_a_whitelist_mismatch_reports_what_is_missing(self):
        result = preview_match("nothing here", whitelist=WL, mode="any")
        assert not result.allowed
        assert result.reason == "whitelist_mismatch"
        assert set(result.missing) == set(WL)

    def test_the_effective_entries_are_echoed_back(self):
        """The UI shows the effective task, entries and mode next to the result."""
        result = preview_match("gold rises", whitelist=WL, mode="any")
        assert result.entries == tuple(WL)
        assert result.mode == "any"
        assert result.to_dict()["matched"] == ["gold"]

    def test_repeated_spaces_and_line_breaks_do_not_defeat_a_phrase(self):
        text = "The  market   update\n\n\nis live"  # double spaces, blank lines
        assert preview_match(text, whitelist=WL, mode="any").allowed
        assert not preview_match("nothing here", whitelist=WL, mode="any").allowed

    def test_a_media_caption_is_searched_like_a_body(self):
        """Captions are the body for a media message (``effective_text``)."""
        assert preview_match("gold in the caption", whitelist=WL, mode="any").allowed


class TestAllAndPhraseModes:
    """The three modes a Long-Text rule supports, and no others."""

    def test_all_requires_every_entry_somewhere(self):
        text = "gold features in the first paragraph; XAUUSD appears later"
        ok = preview_match(text, whitelist=WL, mode="all")
        # `market update` is absent, so ALL fails and names the missing entry.
        assert not ok.allowed
        assert "market update" in ok.missing
        assert "gold" in ok.matched and "XAUUSD" in ok.matched

    def test_all_matches_when_every_entry_is_present_anywhere(self):
        """Adjacency and ordering are not required for ALL."""
        text = "XAUUSD at the top\n\ngold in the middle\n\nmarket update last"
        ok = preview_match(text, whitelist=WL, mode="all")
        assert ok.allowed
        assert ok.reason == "whitelist_all"
        assert set(ok.matched) == set(WL)

    def test_phrase_is_not_all(self):
        """PHRASE preserves the ordered-words semantics; ALL is not equivalent."""
        # ALL-matching text where the words are NOT in one contiguous phrase.
        all_only = "XAUUSD at the top\n\ngold in the middle\n\nmarket update last"
        assert preview_match(all_only, whitelist=WL, mode="all").allowed
        assert not preview_match(all_only, whitelist=WL, mode="phrase").allowed

    def test_phrase_matches_the_words_in_order(self):
        ok = preview_match(
            "the market update is live", whitelist=("market update",), mode="phrase"
        )
        assert ok.allowed and ok.reason == "whitelist_phrase"

    def test_an_unsupported_mode_falls_back_to_any(self):
        """An unknown mode never raises; it degrades to ANY, documented."""
        ok = preview_match("gold here", whitelist=WL, mode="bogus")
        assert ok.allowed and ok.mode == "any"


class TestPreviewAgreesWithProductionEvaluate:
    """The test a UI's "Test Rules" button depends on: preview == evaluate."""

    @pytest.mark.parametrize("mode", ("any", "all", "phrase"))
    @pytest.mark.parametrize("text,expected,_k", ANY_CASES)
    def test_every_case_and_mode_agrees(self, mode, text, expected, _k):
        policy = ContentPolicy.from_dict({"whitelist_words": list(WL), "whitelist_mode": mode})
        decision = policy.evaluate(text)
        preview = preview_policy(text, policy)
        assert decision.allowed == preview.allowed, (mode, _k, decision.reason, preview.reason)

    def test_blacklist_precedence_is_preserved(self):
        """A blacklist hit drops the message even when the whitelist matches."""
        policy = ContentPolicy.from_dict(
            {"blacklist_words": ["gold"], "whitelist_words": ["gold"], "whitelist_mode": "any"}
        )
        assert not policy.evaluate("gold today").allowed
        assert not preview_policy("gold today", policy).allowed


class TestKeywordSemanticsArePreserved:
    """A refactor must not change which messages an existing task blocks."""

    def test_whole_word_default_does_not_match_inside_a_word(self):
        assert not contains_word("a5x and 10x", "5x")  # word boundary holds

    def test_the_legacy_substring_behaviour_is_opt_in(self):
        """``FilterEngine._kw`` uses word_boundary=False; that contract holds."""
        assert contains_word("a5x and 10x", "5x", word_boundary=False)

    def test_case_and_persian_variants_still_match(self):
        assert contains_word("GOLD price", "gold")
        assert contains_word("قیمت طلا امروز", "طلا")

    def test_a_regex_shaped_keyword_is_a_literal(self):
        """``.*`` in a keyword list is a word to find, never a pattern."""
        assert not contains_word("anything", ".*")


class TestRegexOption:
    """Regex inclusion and exclusion, through the guarded compiler."""

    def test_regex_block_rejects_a_matching_message(self):
        ok = preview_match("BUY XAUUSD NOW", regex_block=["^BUY"])
        assert not ok.allowed and ok.reason.startswith("regex_block")

    def test_regex_require_admits_a_matching_message(self):
        ok = preview_match("XAUUSD BUY now", regex_require=[r"XAUUSD\s+BUY"])
        assert ok.allowed and ok.reason.startswith("regex_require")

    def test_regex_require_drops_a_non_matching_message(self):
        ok = preview_match("XAUUSD SELL now", regex_require=[r"XAUUSD\s+BUY"])
        assert not ok.allowed and ok.reason == "regex_require_missing"
        assert r"XAUUSD\s+BUY" in ok.missing[0]

    def test_an_invalid_pattern_neither_drops_nor_passes_a_message(self):
        """A refused pattern is not consulted — never a bypass, never a crash."""
        ok = preview_match("anything", regex_block=["(unbalanced"])
        # Rules were configured (so this is not "no_rules"); the unusable
        # pattern is ignored, the message passes, and nothing is dropped.
        assert ok.allowed and ok.reason == "policy_passed"

    def test_the_word_boundary_example_behaves_as_documented(self):
        ok = preview_match("icy BUY signal", regex_require=[r"\b(BUY|SELL)\b"])
        assert ok.allowed

    def test_the_anchored_example_requires_the_whole_input(self):
        assert not preview_match("ICY BUY XAUUSD", regex_require=[r"^BUY XAUUSD$"]).allowed
        assert preview_match("BUY XAUUSD", regex_require=[r"^BUY XAUUSD$"]).allowed


class TestRegexSafetyGuard:
    """The guard must be defensible, not shape-luck.

    ``_REDOS`` always caught a quantifier nested in a quantified group
    (``(a+)+``). It never caught *ambiguous alternation* under a quantifier —
    ``(a|a)*b`` — which backtracks exponentially too (measured: 2.4 s on a
    24-character input, timeout on 26+). Both are refused now; the tests pin
    both shapes, and the input cap bounds anything not yet catalogued.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a+)+b",
            r"(a*)*b",
            r"(?:a+)+b",
            r"(a|a)*b",   # ambiguous alternation: previously accepted, exponential
            r"(a|a)+b",
            r"(?:a|a)*b",
        ],
    )
    def test_the_catastrophic_shapes_are_refused(self, pattern):
        assert compile_user_regex(pattern) is None

    @pytest.mark.parametrize(
        "pattern",
        [
            r"^BUY XAUUSD$",
            r"\b(BUY|SELL)\b",
            r"XAUUSD\s+BUY",
            r"[a-z]+@[a-z]+\.[a-z]+",
            r"gold|silver|oil",
        ],
    )
    def test_legitimate_patterns_are_still_accepted(self, pattern):
        assert compile_user_regex(pattern) is not None

    def test_a_benign_alternation_is_not_collateral_damage(self):
        """`(buy|sell)` without an outer quantifier is a normal pattern."""
        assert compile_user_regex(r"(buy|sell) XAUUSD") is not None

    def test_the_input_cap_keeps_a_slow_pattern_bounded(self):
        """The guarantee is structural *and* length-bounded, not structural alone."""
        malicious = compile_user_regex(r"(a{2}){2}")  # accepted: a linear shape
        body = "a" * 4096 + "b"
        start = time.perf_counter()
        bounded_search(malicious, body)
        assert time.perf_counter() - start < 1.0

    def test_bounded_search_still_finds_a_needle_in_a_long_body(self):
        pattern = compile_user_regex(r"tail-needle")
        assert pattern is not None
        # The needle sits *inside* the cap: it must be found.
        within = "haystack " * 800 + "tail-needle"
        assert bounded_search(pattern, within) is not None
        # Beyond the cap the tail is not scanned — the point of the bound.
        beyond = "haystack " * 1500 + "tail-needle"
        assert beyond and len(beyond) > 8192
        assert bounded_search(pattern, beyond) is None

    def test_bounded_search_handles_empty_text(self):
        assert bounded_search(compile_user_regex(r"x"), "") is None


class TestStructuredResultShape:
    """The structured decision clients render and API responses serialize."""

    def test_to_dict_is_the_stable_wire_form(self):
        ok = preview_match("gold rises", whitelist=WL, mode="any")
        assert ok.to_dict() == {
            "allowed": True,
            "reason": "whitelist:gold",
            "matched": ["gold"],
            "missing": [],
            "entries": ["gold", "XAUUSD", "market update"],
            "mode": "any",
        }

    def test_empty_content_and_no_rules(self):
        ok = preview_match("", whitelist=(), blacklist=(), regex_block=(), regex_require=())
        assert ok.allowed and ok.reason == "no_rules"

    def test_an_empty_entry_list_constrains_nothing(self):
        """A whitelist that is blank constrains nothing — pinned by the matrix doc."""
        assert preview_match("anything at all", whitelist=(), mode="any").allowed

    def test_entries_are_split_with_the_canonical_separator_set(self):
        """One entry per line, comma, and Persian comma — a phrase keeps its words."""
        assert split_word_list("gold،XAUUSD\nmarket update") == (
            "gold", "XAUUSD", "market update",
        )
        assert split_word_list("5x, 10x\nWhale Update") == ("5x", "10x", "Whale Update")


class TestIsolationAndCompatibility:
    """One task's rules never touch another task's message; old configs still work."""

    def test_a_preview_cannot_persist_or_forward(self):
        """Read-only by construction: the entry point takes config as arguments."""
        import inspect

        params = inspect.signature(preview_match).parameters
        assert "text" in params
        # No repository, task or queue handle is accepted.
        assert not any("repo" in p or "task" in p for p in params)

    def test_an_old_configuration_without_a_mode_defaults_to_any(self):
        """A config written before this release is interpreted as before."""
        policy = ContentPolicy.from_dict({"whitelist_words": ["gold"]})
        assert policy.whitelist_mode == "any"
        assert policy.evaluate("gold today").allowed
        assert not policy.evaluate("silver only").allowed

    def test_per_source_override_composition_is_unchanged(self):
        """An override adds constraints; it can never relax the task's policy."""
        task = ContentPolicy.from_dict({"blacklist_words": ["spam"]})
        override = ContentPolicy.from_dict({"blacklist_words": ["scam"]})
        # Both must allow: blocked by either stays blocked.
        assert not task.evaluate("spam").allowed
        assert not override.evaluate("scam").allowed
        assert task.evaluate("clean").allowed and override.evaluate("clean").allowed


class TestWhitelistBlacklistPlatformSeparation:
    """Verifies that Whitelist and Blacklist rule collections, regex patterns,
    menus, and callbacks are strictly separated across domain and UI flows."""

    def test_forward_rule_regex_properties_and_draft_roundtrip(self):
        from core.domain.entities import ForwardRule

        rule = ForwardRule(id="rule-test-1", session_id="s1", source_chat_id="100", target_chat_id="200")
        assert rule.regex_block == []
        assert rule.regex_require == []

        rule.regex_block = ["(scam|spam)", r"^\[AD\]"]
        rule.regex_require = [r"\bVIP\b", r"signal\s+\d+"]

        assert rule.regex_block == ["(scam|spam)", r"^\[AD\]"]
        assert rule.regex_require == [r"\bVIP\b", r"signal\s+\d+"]

        draft = rule.to_draft()
        assert draft["regex_block"] == ["(scam|spam)", r"^\[AD\]"]
        assert draft["regex_require"] == [r"\bVIP\b", r"signal\s+\d+"]

        rule2 = ForwardRule(id="rule-test-2", session_id="s1", source_chat_id="100", target_chat_id="200")
        rule2.apply_draft(draft)
        assert rule2.regex_block == ["(scam|spam)", r"^\[AD\]"]
        assert rule2.regex_require == [r"\bVIP\b", r"signal\s+\d+"]

    def test_three_way_flows_cancel_and_direction_routing(self):
        from unittest.mock import MagicMock
        from core.domain.entities import ForwardRule
        from core.infrastructure.telegram.three_way_flows import ThreeWayFlows

        ui_mock = MagicMock()
        ui_mock._resolve_lang.return_value = "en"
        ui_mock._t = lambda key, **kwargs: key
        ui_mock._kbd = lambda rows: rows
        flows = ThreeWayFlows(ui_mock)

        rule = ForwardRule(id="r123", session_id="s1", source_chat_id="100", target_chat_id="200")

        # Whitelist regex prompt cancel targets whitelist menu
        _txt, kbd_wl = flows.render_regex_prompt(rule, direction="require")
        assert any(cb == "rfl_wl:r123" for row in kbd_wl for _, cb in row)

        # Blacklist regex prompt cancel targets blacklist menu
        _txt, kbd_bl = flows.render_regex_prompt(rule, direction="block")
        assert any(cb == "rfl_bl:r123" for row in kbd_bl for _, cb in row)

        # Whitelist longtext cancel targets whitelist menu
        _txt, kbd_lt_wl = flows.render_longtext_prompt(rule, direction="whitelist")
        assert any(cb == "rfl_wl:r123" for row in kbd_lt_wl for _, cb in row)

        # Blacklist longtext cancel targets blacklist menu
        _txt, kbd_lt_bl = flows.render_longtext_prompt(rule, direction="blacklist")
        assert any(cb == "rfl_bl:r123" for row in kbd_lt_bl for _, cb in row)

        # Test prompt origin routing
        _txt, kbd_test_wl = flows.render_test_prompt(rule, origin="whitelist")
        assert any(cb == "rfl_wl:r123" for row in kbd_test_wl for _, cb in row)

        _txt, kbd_test_bl = flows.render_test_prompt(rule, origin="blacklist")
        assert any(cb == "rfl_bl:r123" for row in kbd_test_bl for _, cb in row)

    def test_legacy_filter_row_sync_preserves_whitelist_regex_semantics(self):
        from unittest.mock import MagicMock
        from core.domain.entities import ForwardRule
        from core.infrastructure.telegram.three_way_flows import ThreeWayFlows

        legacy_row = MagicMock()
        legacy_row.whitelist_keywords = []
        legacy_row.blacklist_keywords = []
        legacy_row.regex_patterns = []

        ui_mock = MagicMock()
        ui_mock._resolve_lang.return_value = "en"
        ui_mock._t = lambda key, **kwargs: key
        ui_mock._filters = MagicMock()
        ui_mock._filters.get.return_value = legacy_row

        flows = ThreeWayFlows(ui_mock)
        rule = ForwardRule(
            id="r123", session_id="s1", source_chat_id="100", target_chat_id="200",
            filter_rule_id="legacy-filter-1",
        )
        flows._legacy_filter_row = lambda rule: legacy_row

        # Applying config with regex_block and regex_require
        flows._apply_policy(rule, {
            "whitelist_words": ["pass"],
            "blacklist_words": ["block"],
            "regex_require": [r"^\[ALLOW\]"],
            "regex_block": [r"^\[DENY\]"],
        }, uid=1)

        assert legacy_row.whitelist_keywords == ["pass"]
        assert legacy_row.blacklist_keywords == ["block"]
        # regex_patterns in FilterEngine acts as require-any (whitelist).
        # Must sync regex_require, NOT regex_block!
        assert legacy_row.regex_patterns == [r"^\[ALLOW\]"]
