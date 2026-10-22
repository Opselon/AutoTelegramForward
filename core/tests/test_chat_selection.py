"""The selection value object: what must hold before its screen can exist.

A picker is only as good as its data. These cases pin the rules the buttons
rely on — a second click unpicks, the two roles never touch each other, both
spellings of one id are one entry, and a pasted line of ids keeps its minus
sign — plus the boundary cases that decide whether the picker can be trusted
(caps, pruning, malformed persisted state).
"""

from __future__ import annotations

import pytest

from core.application.chat_selection import (
    MAX_BATCH,
    MAX_SELECTION,
    ROLE_SOURCE,
    ROLE_TARGET,
    STATE_OK,
    PickedChat,
    Selection,
    parse_id_batch,
    role_key,
)
from core.infrastructure.telegram.route_lists import unique_peer_key as infra_key

CHAT_A = "-288262000"
CHAT_B = "-272728282"
CHAT_C = "-1001234567890"


def chat(chat_id: str, title: str = "", source: str = "") -> PickedChat:
    return PickedChat(chat_id=chat_id, title=title or source or chat_id)


# --------------------------------------------------------------------------- #
# parse_id_batch
# --------------------------------------------------------------------------- #
def test_a_minus_sign_belongs_to_the_id_not_the_next_one():
    parsed = parse_id_batch(f"{CHAT_A}, {CHAT_B}")
    assert parsed.ids == (CHAT_A, CHAT_B)


def test_commas_spaces_newlines_and_one_id_are_the_same_grammar():
    expectation = (CHAT_A, CHAT_B)
    assert parse_id_batch(f"{CHAT_A},\n{CHAT_B}").ids == expectation
    assert parse_id_batch(f"{CHAT_A}   {CHAT_B}").ids == expectation
    assert parse_id_batch(f"{CHAT_A},{CHAT_B}").ids == expectation
    assert parse_id_batch(CHAT_A).ids == (CHAT_A,)


def test_the_persian_comma_is_a_separator_too():
    assert parse_id_batch(f"{CHAT_A}\u060c{CHAT_B}").ids == (CHAT_A, CHAT_B)


def test_both_spellings_of_one_id_collapse_into_a_single_entry():
    # -1001234567890 and 1234567890 name one channel: the second is a duplicate.
    parsed = parse_id_batch(f"{CHAT_C}, 1234567890")
    assert parsed.ids == (CHAT_C,)
    assert parsed.duplicates == ("1234567890",)


def test_shapes_that_cannot_name_a_chat_are_named_not_dropped():
    # A bare number is a plausible raw id, so it is kept; words and
    # punctuation are reported instead of being silently swallowed.
    parsed = parse_id_batch("abc 12 -456 **")
    assert parsed.ids == ("12", "-456")
    assert parsed.rejected == ("abc", "**")


@pytest.mark.parametrize("garbage", ["-", "--", "'' ' \""])
def test_punctuation_never_becomes_a_chat(garbage: str):
    parsed = parse_id_batch(garbage)
    assert parsed.empty or all(item not in ("-", "--") for item in parsed.ids)


def test_a_batch_is_bounded_so_a_paste_cannot_grow_the_state():
    many = ", ".join(f"-100{i:013d}" for i in range(1, MAX_BATCH + 5))
    parsed = parse_id_batch(many)
    assert len(parsed.ids) == MAX_BATCH
    assert parsed.rejected, "over-cap ids must be reported, not dropped silently"


def test_role_tokens_normalize():
    assert role_key("t") == ROLE_TARGET
    assert role_key("Targets") == ROLE_TARGET
    assert role_key("") == ROLE_SOURCE
    assert role_key("s") == ROLE_SOURCE


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def test_toggling_twice_is_pick_then_remove():
    selection = Selection()
    assert selection.toggle(ROLE_SOURCE, chat(CHAT_A)) is True
    assert selection.count(ROLE_SOURCE) == 1
    assert selection.toggle(ROLE_SOURCE, chat(CHAT_A)) is False
    assert selection.count(ROLE_SOURCE) == 0


def test_roles_are_independent_and_both_survive_role_switching():
    selection = Selection()
    selection.toggle(ROLE_SOURCE, chat(CHAT_A))
    selection.toggle(ROLE_TARGET, chat(CHAT_B))
    selection.toggle(ROLE_TARGET, chat(CHAT_A))       # same chat, other role
    assert selection.source_ids == (CHAT_A,)
    assert selection.target_ids == (CHAT_B, CHAT_A)
    assert selection.counts() == {ROLE_SOURCE: 1, ROLE_TARGET: 2}
    assert selection.pairs == 2


def test_a_chat_cannot_enter_one_role_twice_under_a_second_spelling():
    selection = Selection()
    assert selection.add(ROLE_SOURCE, chat(CHAT_C))
    assert not selection.add(ROLE_SOURCE, chat("1234567890"))
    assert selection.count(ROLE_SOURCE) == 1


def test_select_all_clear_and_the_ability_to_report_what_happened():
    selection = Selection()
    added = selection.select_all(ROLE_TARGET, [chat(CHAT_A), chat(CHAT_B)])
    assert added == 2
    assert selection.clear(ROLE_TARGET) == 2
    assert selection.of(ROLE_TARGET) == ()


def test_merge_reports_duplicates_and_overflow_instead_of_failing():
    selection = Selection()
    selection.add(ROLE_SOURCE, chat(CHAT_A))
    result = selection.merge(
        ROLE_SOURCE, [chat(CHAT_A), chat(CHAT_B), chat(CHAT_C)],
    )
    assert result.added == (CHAT_B, CHAT_C)
    assert result.duplicates == (CHAT_A,)
    assert not selection.has(ROLE_SOURCE, "should-not-be-there")


def test_clearing_one_role_leaves_the_other_selection_alone():
    selection = Selection()
    selection.select_all(ROLE_SOURCE, [chat(CHAT_A), chat(CHAT_B)])
    selection.toggle(ROLE_TARGET, chat(CHAT_C))
    selection.clear(ROLE_SOURCE)
    assert selection.count(ROLE_SOURCE) == 0
    assert selection.target_ids == (CHAT_C,)


def test_prune_drops_only_gone_chats_and_never_guessed_emptiness():
    selection = Selection()
    selection.toggle(ROLE_SOURCE, chat(CHAT_A))
    selection.toggle(ROLE_SOURCE, chat(CHAT_B))
    # An empty catalog is *unknown*: wiping picks on a network blip is worse
    # than rendering one stale row.
    assert selection.prune(ROLE_SOURCE, None) == 0
    assert selection.prune(ROLE_SOURCE, []) == 2
    assert selection.count(ROLE_SOURCE) == 0


def test_prune_frees_only_the_deleted_rule():
    selection = Selection()
    selection.toggle(ROLE_SOURCE, chat(CHAT_A))
    selection.toggle(ROLE_SOURCE, chat(CHAT_B))
    assert selection.prune(ROLE_SOURCE, [CHAT_A]) == 1
    assert selection.source_ids == (CHAT_A,)


def test_state_round_trips_through_the_buffer_shape():
    selection = Selection()
    selection.toggle(ROLE_SOURCE, chat(CHAT_A, title="Telegram"))
    selection.toggle(ROLE_TARGET, chat(CHAT_B))
    reborn = Selection.from_state(selection.to_state())
    assert reborn.source_ids == selection.source_ids
    assert reborn.counts() == selection.counts()


def test_a_malformed_buffer_costs_entries_not_the_whole_for_flow():
    selection = Selection.from_state({"s": [{"id": CHAT_A, "title": ""}, "junk", None],
                                     "t": "not-a-list"})
    assert selection.source_ids == (CHAT_A,)
    assert selection.of(ROLE_TARGET) == ()
    assert Selection.from_state(None).count(ROLE_SOURCE) == 0
    assert Selection.from_state(42).count(ROLE_SOURCE) == 0


def test_cap_bounds_one_role_so_state_cannot_grow_without_limit():
    selection = Selection()
    for index in range(MAX_SELECTION + 10):
        selection.add(ROLE_SOURCE, chat(f"-100{index:015d}"))
    assert selection.count(ROLE_SOURCE) == MAX_SELECTION


def test_the_state_ok_chat_is_the_only_links_target_by_state_filters():
    selection = Selection()
    ok_chat = PickedChat(CHAT_A, "ok", STATE_OK)
    locked = PickedChat(CHAT_B, "dead", "unknown")
    selection.add(ROLE_TARGET, ok_chat)
    selection.add(ROLE_TARGET, locked)
    assert [item.chat_id for item in selection.by_state(ROLE_TARGET, STATE_OK)] == [CHAT_A]


def test_overlap_names_a_chat_picked_on_both_sides():
    selection = Selection()
    selection.toggle(ROLE_SOURCE, chat(CHAT_A))
    selection.toggle(ROLE_TARGET, chat(CHAT_A))
    selection.toggle(ROLE_TARGET, chat(CHAT_B))
    assert selection.overlap() == (CHAT_A,)


def test_two_disjoint_sides_have_no_overlap():
    selection = Selection()
    selection.toggle(ROLE_SOURCE, chat(CHAT_A))
    selection.toggle(ROLE_TARGET, chat(CHAT_B))
    assert selection.overlap() == ()


def test_selection_and_route_editor_agree_on_what_one_chat_is():
    """The domain module is the rulebook; the older wizard must agree."""
    for token in (CHAT_A, CHAT_C, "1234567890", "@news", "https://t.me/news"):
        assert unique_of(token) == infra_key(token)


def unique_of(token: str) -> str:
    from core.domain.peer_ids import unique_peer_key
    return unique_peer_key(token)
