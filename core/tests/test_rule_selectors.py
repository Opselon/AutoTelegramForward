"""The stable-selector contract: ids first, names second, ordinals never.

These are the rules every surface depends on — `/forward edit`, `/replace
delete`, the list search box and the bulk screens. They are pure functions, so
the matrix is cheap and exhaustive, and the precedence is pinned here rather
than re-derived in three handlers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application.rule_selectors import (
    AMBIGUOUS,
    EMPTY,
    ID,
    NOT_FOUND,
    PREFIX,
    TEXT,
    SelectorError,
    match_rule,
    parse_selector,
    resolve_rule,
    rule_label,
)

#: A fixed uuid: parametrised test ids are the test ids pytest reports, and a
#: random one at import time makes xdist workers collect different ids and
#: abort the whole run ("Different tests were collected between gw0 and gw3").
UUID4 = "b31fc230-956b-43d6-9138-3201442df04f"
HEX16 = "0123456789abcdef"


def rule(rid: str, name: str = "", source: str = "", chat: str = "", username: str = ""):
    return SimpleNamespace(id=rid, name=name, source=source, source_chat_id=chat,
                           source_username=username, source_chat_name=name)


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,kind,value", [
    (UUID4, ID, UUID4),
    (UUID4.upper(), ID, UUID4),
    (f"#{UUID4}", ID, UUID4),
    (HEX16, ID, HEX16),
    ("  " + HEX16 + "  ", ID, HEX16),
    ("#0123", PREFIX, "0123"),
    ("#ABCDEF01", PREFIX, "abcdef01"),
    ("nightly", TEXT, "nightly"),
    ("0123", TEXT, "0123"),          # too short to be an id, and not marked
    ("task one", TEXT, "task one"),
])
def test_parse_selector_classifies_without_guessing(raw, kind, value):
    parsed = parse_selector(raw)
    assert (parsed.kind, parsed.value) == (kind, value)


def test_parse_selector_rejects_empty():
    with pytest.raises(SelectorError) as caught:
        parse_selector("   ")
    assert caught.value.code == EMPTY


def test_parse_selector_does_not_treat_a_bare_number_as_an_ordinal():
    parsed = parse_selector("2")
    assert parsed.kind == TEXT and parsed.value == "2"


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #
def test_full_id_wins_over_a_name_that_looks_like_one():
    lookalike = rule("task-copy", name=HEX16)
    real = rule(HEX16, name="real")
    assert resolve_rule([lookalike, real], HEX16) is real


def test_explicit_prefix_resolves_when_unique():
    first = rule("abcdef0123456789", name="one")
    second = rule("ffffffffffffffff", name="two")
    assert resolve_rule([first, second], "#abcdef01") is first


def test_explicit_prefix_refuses_ambiguity_and_names_the_candidates():
    a = rule("abcd111111111111", name="alpha")
    b = rule("abcd222222222222", name="beta")
    with pytest.raises(SelectorError) as caught:
        resolve_rule([a, b], "#abcd")
    assert caught.value.code == AMBIGUOUS
    assert any("alpha" in label for label in caught.value.candidates)


def test_prefix_without_the_marker_is_text_not_an_id():
    row = rule("abcd111111111111", name="abcd")
    assert resolve_rule([row], "abcd").id == row.id


def test_names_resolve_case_insensitively_and_exactly():
    row = rule(HEX16, name="Nightly Promo")
    assert resolve_rule([row], "nightly promo").id == row.id


def test_two_rules_with_one_name_are_ambiguous():
    with pytest.raises(SelectorError) as caught:
        resolve_rule([rule("a" * 16, name="dup"), rule("b" * 16, name="dup")], "dup")
    assert caught.value.code == AMBIGUOUS


def test_unique_substring_matches_but_ambiguous_substring_refuses():
    promo = rule("a" * 16, name="nightly promo")
    other = rule("b" * 16, name="daily digest")
    assert resolve_rule([promo, other], "promo").id == promo.id
    with pytest.raises(SelectorError) as caught:
        resolve_rule([promo, rule("c" * 16, name="promo two")], "promo")
    assert caught.value.code == AMBIGUOUS


def test_chat_id_and_username_resolve():
    by_chat = rule("a" * 16, name="x", chat="-1001234567890")
    by_user = rule("b" * 16, name="y", username="newsdesk")
    assert resolve_rule([by_chat, by_user], "-1001234567890") is by_chat
    assert resolve_rule([by_chat, by_user], "newsdesk") is by_user


def test_foreign_and_missing_ids_are_the_same_answer():
    mine = [rule("a" * 16, name="mine")]
    with pytest.raises(SelectorError) as caught:
        resolve_rule(mine, "b" * 16)
    assert caught.value.code == NOT_FOUND
    # The refusal never carries the other tenant's label.
    assert caught.value.candidates == ()


def test_a_single_character_query_is_refused_instead_of_matching_everything():
    with pytest.raises(SelectorError) as caught:
        resolve_rule([rule("a" * 16, name="alpha")], "a")
    assert caught.value.code == NOT_FOUND


# --------------------------------------------------------------------------- #
# list search + labels
# --------------------------------------------------------------------------- #
def test_match_rule_accepts_id_prefix_name_and_chat():
    row = rule(HEX16, name="Nightly", chat="-100999", source="src")
    assert match_rule(row, HEX16)
    assert match_rule(row, "#01234567")
    assert match_rule(row, "night")
    assert match_rule(row, "-100999")
    assert not match_rule(row, "absent")


def test_rule_label_carries_the_short_id_beside_the_name():
    assert rule_label(rule(HEX16, name="Nightly")) == "Nightly · #01234567"
    assert rule_label(rule(HEX16)) == "#01234567"
