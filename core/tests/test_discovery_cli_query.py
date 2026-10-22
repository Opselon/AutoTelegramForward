"""The `/getchannel`-family command grammar and the unique-id contract.

Everything here is the pure application layer: parsing the one-line command
line (`-id`, `-all`, a page number) and matching either spelling of one
numeric chat id. No Telegram client, no database, no bot — the same rules
the bot, the dashboard and any future surface consume.
"""
from __future__ import annotations

import pytest

from core.application.discovery import (
    ChatListQuery,
    ChatQueryError,
    DialogCatalog,
    parse_chat_list_query,
    unique_chat_id,
)


# --------------------------------------------------------------------------- #
# The grammar: `[(-id | --id)] [(-all | --all)] [PAGE]`
# --------------------------------------------------------------------------- #

def test_a_bare_command_is_the_default_query():
    for raw in ("", "   ", "\t"):
        assert parse_chat_list_query(raw) == ChatListQuery(), raw


@pytest.mark.parametrize("flag", ["-id", "--id"])
def test_the_id_flag_is_accepted_single_and_double_dash(flag):
    assert parse_chat_list_query(flag).with_ids is True


def test_the_all_flag_widens_the_kind_filter():
    for command, expected in (
        ("-id", ChatListQuery(with_ids=True)),
        ("-all", ChatListQuery(all_kinds=True)),
        ("-all -id", ChatListQuery(with_ids=True, all_kinds=True)),
        ("--all --id 3", ChatListQuery(with_ids=True, all_kinds=True, page=2)),
        ("2 -id", ChatListQuery(with_ids=True, page=1)),
    ):
        assert parse_chat_list_query(command) == expected, command


def test_a_page_is_one_based_and_bounded_to_three_digits():
    assert parse_chat_list_query("1").page == 0
    assert parse_chat_list_query("999").page == 998
    for bad in ("0", "007", "1234"):
        with pytest.raises(ChatQueryError) as exc:
            parse_chat_list_query(bad)
        assert exc.value.code == "bad_flag", bad


def test_an_unknown_token_is_refused_and_named():
    for raw in ("hello", "-buy", "-ID", "-Id", "-id:1", "--idonly"):
        with pytest.raises(ChatQueryError) as exc:
            parse_chat_list_query(raw)
        assert exc.value.code == "bad_flag", raw


def test_an_unknown_flag_refusal_names_the_offender():
    with pytest.raises(ChatQueryError) as exc:
        parse_chat_list_query("-summery")
    assert exc.value.detail == "-summery"


def test_a_repeated_flag_is_ambiguous_not_an_accumulator():
    for raw in ("-id -id", "--id -id", "-all -all", "2 3"):
        with pytest.raises(ChatQueryError) as exc:
            parse_chat_list_query(raw)
        assert exc.value.code == "bad_flag", raw


# --------------------------------------------------------------------------- #
# Unique ids: `-100…` (Bot-API) and the raw unique id are one chat
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "spelling,expected",
    [
        ("-1001206461239", "1206461239"),
        ("1206461239", "1206461239"),
        ("5094837833", "5094837833"),
        ("-1206461239", "-1206461239"),  # a raw user-style negative id stays
        ("-100", "-100"),  # bare prefix is a literal, not a chat id
        ("@news", "@news"),  # a handle is the route-list contract's job
        ("", ""),
        ("not-an-id", "not-an-id"),
    ],
)
def test_unique_chat_id_normalizes_only_the_bot_api_prefix(spelling, expected):
    assert unique_chat_id(spelling) == expected


CATALOG = DialogCatalog.from_dicts(
    [
        {"id": "-1006831115", "title": "بارینش نیوز", "kind": "channel", "username": ""},
        {"id": "5094837833", "title": "Saved Self", "kind": "user", "username": "me"},
    ]
)


def test_a_raw_unique_id_resolves_a_bot_api_channel():
    found = CATALOG.find("6831115")
    assert found is not None and found.id == "-1006831115"


def test_the_bot_api_spelling_resolves_the_same_entry():
    found = CATALOG.find("-1006831115")
    assert found is not None and found.id == "-1006831115"


def test_ids_that_differ_are_not_collapsed():
    assert CATALOG.find("6831116") is None
    assert CATALOG.find("-100683111500") is None
