"""Replace-entry options: literal/regex, case, whole word, names, and safety.

The engine is the single interpreter of ``metadata["replacements"]`` for the
pipeline, the bot panel and the text commands, so its contract is pinned here:

* legacy rows (``"old": "new"``) keep the original exact-then-ignore-case
  behaviour, bit for bit;
* an option dict changes matching only when the user asked for it;
* a pattern the guarded compiler refuses (length, nested quantifiers) leaves
  the message untouched instead of raising, because the text being edited is
  the user's message, not our debugging surface.
"""

from __future__ import annotations

import pytest

from core.domain.text_replace import (
    MAX_ENTRIES,
    MODE_LITERAL,
    MODE_REGEX,
    ReplaceError,
    apply_entry,
    apply_replacements,
    clean_flags,
    describe_entry,
    entry_from_value,
    entry_with,
    find_key,
    merge_entry,
    parse_add_arguments,
    parse_freeform,
    tokenize,
)


# --------------------------------------------------------------------------- #
# Backward compatibility: the legacy shape
# --------------------------------------------------------------------------- #


def test_legacy_entry_replaces_the_exact_form_only():
    assert apply_replacements("Gold GOLD gold", {"Gold": "X"}) == "X GOLD gold"


def test_legacy_entry_falls_back_to_case_insensitive_when_exact_absent():
    assert apply_replacements("gold and GOLD", {"Gold": "X"}) == "X and X"


def test_legacy_entry_can_delete_a_word():
    assert apply_replacements("buy cheap now", {"cheap ": ""}) == "buy now"


def test_legacy_replacement_text_is_literal_not_a_regex_template():
    # A stray back-reference must render as typed, not raise or eat a group.
    assert apply_replacements("price", {"price": r"\1"}) == r"\1"


def test_legacy_value_round_trips_to_the_same_storage_shape():
    entry = entry_from_value("old", "new")
    assert entry.is_plain
    assert entry.to_value() == "new"


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


def test_regex_mode_replaces_by_pattern():
    entry = entry_from_value(r"promo (\d+)%", {"to": r"offer \1%", "mode": "regex"})
    assert entry.is_regex
    assert apply_entry("save promo 25% today", entry) == "save offer 25% today"


def test_regex_mode_supports_multiline_and_dotall_flags():
    entry = entry_from_value("^a$", {"to": "b", "mode": "regex", "flags": "m"})
    assert apply_entry("a\nx\na", entry) == "b\nx\nb"


def test_ignore_case_flag_matches_any_case():
    entry = entry_from_value("gold", {"to": "X", "flags": "i"})
    assert apply_entry("Gold GOLD gold", entry) == "X X X"


def test_case_sensitive_flag_opts_out_of_the_legacy_fallback():
    # Without options this would also replace the lowercase form; `c` says no.
    entry = entry_from_value("Gold", {"to": "X", "flags": "c"})
    assert apply_entry("Gold gold", entry) == "X gold"


def test_whole_word_does_not_match_inside_a_longer_word():
    entry = entry_from_value("promo", {"to": "X", "whole_word": True})
    assert apply_entry("promo promotion promos promo", entry) == "X promotion promos X"


def test_whole_word_from_the_w_flag_matches_the_same_way():
    entry = entry_from_value("promo", {"to": "X", "flags": "w"})
    assert entry.whole_word is True
    assert apply_entry("promotion promo", entry) == "promotion X"


def test_whole_word_keeps_anchored_patterns_matchable():
    entry = entry_from_value(r"^promo\d+$", {"to": "X", "mode": "regex", "whole_word": True})
    assert apply_entry("promo12", entry) == "X"


def test_clean_flags_canonicalises_and_drops_regex_only_flags_for_literals():
    assert clean_flags("I W") == "iw"
    assert clean_flags("iw", MODE_LITERAL) == "iw"
    assert clean_flags("imx", MODE_LITERAL) == "i"
    assert clean_flags("imx", MODE_REGEX) == "imx"
    assert clean_flags("ic") == "c"


def test_mode_and_flags_survive_a_storage_round_trip():
    original = entry_from_value("a+", {"to": "b", "mode": "regex", "flags": "im",
                                       "name": "demo", "whole_word": True})
    restored = entry_from_value("a+", original.to_value())
    assert restored == original


# --------------------------------------------------------------------------- #
# Safety: a refused pattern must never damage or drop the message
# --------------------------------------------------------------------------- #


def test_catastrophic_backtracking_pattern_is_refused_without_touching_the_text():
    entry = entry_from_value("(a+)+$", {"to": "BOOM", "mode": "regex"})
    with pytest.raises(ReplaceError) as exc:
        from core.domain.text_replace import validate_entry

        validate_entry(entry)
    assert exc.value.code == "invalid_regex"
    # Even if a bad row already sits in storage, delivery stays intact.
    assert apply_entry("aaaa", entry) == "aaaa"


def test_overlong_pattern_is_refused():
    from core.domain.text_replace import validate_entry

    with pytest.raises(ReplaceError) as exc:
        validate_entry(entry_from_value("a" * 400, {"to": "b", "mode": "regex"}))
    assert exc.value.code == "pattern_too_long"


def test_invalid_regex_is_refused_at_entry_time():
    from core.domain.text_replace import validate_entry

    with pytest.raises(ReplaceError) as exc:
        validate_entry(entry_from_value("a(b", {"to": "c", "mode": "regex"}))
    assert exc.value.code == "invalid_regex"


def test_empty_source_is_refused():
    with pytest.raises(ReplaceError) as exc:
        merge_entry({}, entry_from_value("   ", {"to": "c"}))
    assert exc.value.code == "empty_source"


# --------------------------------------------------------------------------- #
# Lookup, merge and bounds
# --------------------------------------------------------------------------- #


def test_find_key_prefers_a_name_then_the_source():
    mapping = {"https://x.test": {"to": "LINK", "name": "site"}}
    assert find_key(mapping, "site") == "https://x.test"
    assert find_key(mapping, "https://x.test") == "https://x.test"
    assert find_key(mapping, "missing") is None


def test_merge_by_name_replaces_instead_of_duplicating():
    mapping = merge_entry({}, entry_from_value("old1", {"to": "a", "name": "promo"}))
    mapping = merge_entry(mapping, entry_from_value("old2", {"to": "b", "name": "promo"}))
    assert list(mapping) == ["old2"]
    assert entry_from_value("old2", mapping["old2"]).to == "b"


def test_entry_cap_is_enforced():
    mapping = {}
    for i in range(MAX_ENTRIES):
        mapping = merge_entry(mapping, entry_from_value(f"w{i}", "x"))
    with pytest.raises(ReplaceError) as exc:
        merge_entry(mapping, entry_from_value("one-too-many", "x"))
    assert exc.value.code == "too_many_entries"


def test_entry_with_keeps_whole_word_and_flags_in_step():
    mapping = {"w": "x"}
    entry = entry_with(mapping, "w", flags="w")
    assert entry.whole_word is True and entry.to_value()["whole_word"] is True
    entry = entry_with({"w": entry.to_value()}, "w", flags="")
    assert entry.whole_word is False and "whole_word" not in entry.to_value()


def test_describe_entry_shows_the_mode_badge_and_name():
    line = describe_entry(entry_from_value("a", {"to": "b", "mode": "regex", "name": "n"}))
    assert line.startswith("[re]") and "`a`" in line and "— n" in line


# --------------------------------------------------------------------------- #
# Panel free text
# --------------------------------------------------------------------------- #


def test_freeform_parses_several_lines_and_deletions():
    mapping, problems = parse_freeform(
        "discount 10% -> discount 50%\n"
        "cheap ->\n"
        "re:^promo\\s*(\\d+)% -> OFFER \\1% | i\n"
    )
    assert problems == []
    assert mapping["discount 10%"] == "discount 50%"
    assert mapping["cheap"] == ""
    assert mapping["^promo\\s*(\\d+)%"] == {"to": "OFFER \\1%", "mode": "regex", "flags": "i"}


def test_freeform_accepts_colon_and_smart_arrow():
    mapping, _ = parse_freeform("a: b\nc → d")
    assert mapping == {"a": "b", "c": "d"}


def test_freeform_reports_the_lines_it_refused():
    mapping, problems = parse_freeform("ok -> fine\nre:(a+)+$ -> x\n")
    assert mapping == {"ok": "fine"}
    assert problems and problems[0][1] == "invalid_regex"


def test_freeform_ignores_blank_lines():
    assert parse_freeform("\n \n") == ({}, [])


# --------------------------------------------------------------------------- #
# Command arguments
# --------------------------------------------------------------------------- #


def test_tokenize_keeps_quoted_spaces_and_drops_the_leading_dash():
    assert tokenize('-"old text" -"new text" --regex') == ["old text", "new text", "--regex"]


def test_tokenize_reports_an_unterminated_quote():
    with pytest.raises(ReplaceError) as exc:
        tokenize('-"open')
    assert exc.value.code == "unterminated_quote"


def test_add_arguments_with_name_and_options():
    entry, named = parse_add_arguments('promo -"discount 10%" -"discount 50%" -w')
    assert named is True
    assert entry.name == "promo"
    assert entry.source == "discount 10%" and entry.to == "discount 50%"
    assert entry.whole_word is True


def test_add_arguments_without_name_treats_two_values_as_source_and_target():
    entry, named = parse_add_arguments('-"a" -"b"')
    assert named is False
    assert (entry.source, entry.to) == ("a", "b")


def test_add_arguments_single_value_deletes_the_word():
    entry, _ = parse_add_arguments('-"gone"')
    assert entry.source == "gone" and entry.to == ""


def test_add_arguments_regex_flags_and_ignore_case():
    entry, _ = parse_add_arguments('-"a(\\d+)" -"b\\1" --regex -i')
    assert entry.mode == MODE_REGEX and entry.flags == "i"


def test_add_arguments_refuses_unknown_option():
    with pytest.raises(ReplaceError) as exc:
        parse_add_arguments('-"a" -"b" --nope')
    assert exc.value.code == "unknown_option"


def test_add_arguments_refuses_missing_source():
    with pytest.raises(ReplaceError) as exc:
        parse_add_arguments("--regex")
    assert exc.value.code == "missing_source"


def test_literal_mode_is_the_default_for_the_command():
    entry, _ = parse_add_arguments('-"a" -"b"')
    assert entry.mode == MODE_LITERAL


def test_add_arguments_name_option_takes_a_value():
    entry, named = parse_add_arguments('-"a" -"b" -n promo')
    assert named is True and entry.name == "promo"
    assert (entry.source, entry.to) == ("a", "b")


def test_add_arguments_delete_option_empties_the_replacement():
    entry, _ = parse_add_arguments('-"a" -"b" --delete')
    assert entry.to == ""


def test_add_arguments_name_option_without_a_value_is_refused():
    with pytest.raises(ReplaceError) as exc:
        parse_add_arguments('-"a" -"b" -n')
    assert exc.value.code == "missing_source"
