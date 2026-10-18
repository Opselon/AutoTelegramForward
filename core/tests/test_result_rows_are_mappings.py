"""A result row is a mapping, on both engines.

SQLite returned ``sqlite3.Row``: ``row["col"]`` and ``row[0]`` work, ``row.get``
and ``row.values()`` do not. PostgreSQL returns a dict, so the reverse is true.
Code written for one engine therefore read the other's rows as ``None`` or raised
inside an ``except Exception: pass`` — which is how a user's stored language
preference came to be ignored (`_i18n_lang`, `_lang_for`, `_resolve_lang` all read
the row with `.get("language")`).

These tests pin the whole access surface, because the fix is only safe if every
shape the codebase already uses keeps working: name and positional lookup,
`keys`/`values`/`items`/`get`/`in`/`len`, iteration, `dict(row)` and `tuple(row)`.
"""

import json
import sqlite3
import time

import pytest

from core.infrastructure.persistence.database import Row, SqliteDatabase

NEW_ROW = {"user_id": 7, "language": "fa", "plan": "free"}


def _row() -> Row:
    return Row(list(NEW_ROW), list(NEW_ROW.values()))


# --------------------------------------------------------------------------- #
# the access surface
# --------------------------------------------------------------------------- #


def test_a_row_reads_by_name_and_by_position():
    row = _row()
    assert row["language"] == "fa"
    assert row[0] == 7 and row[1] == "fa" and row[2] == "free"
    assert row[-1] == "free"


def test_a_row_reads_like_a_mapping():
    row = _row()
    assert row.get("language") == "fa"
    assert row.get("not_a_column") is None
    assert row.get("not_a_column", "fallback") == "fallback"
    assert "language" in row
    assert "not_a_column" not in row
    assert set(row.keys()) == set(NEW_ROW)
    assert sorted(row.values(), key=str) == sorted(NEW_ROW.values(), key=str)
    assert dict(row.items()) == NEW_ROW
    assert dict(row) == NEW_ROW
    assert list(row) == list(NEW_ROW.values())


def test_a_row_keeps_the_positional_length():
    """`len` counts columns, as `sqlite3.Row` did - not distinct names."""
    row = Row(["a", "a"], [1, 2])
    assert len(row) == 2
    assert row[0] == 1 and row[1] == 2
    assert row["a"] == 2  # the later column wins on a duplicate name, as in dict


def test_a_missing_column_name_raises_index_error():
    """The error `sqlite3.Row` raises, so existing handlers still catch it."""
    row = _row()
    with pytest.raises(IndexError):
        row["not_a_column"]
    with pytest.raises(IndexError):
        row[99]


def test_a_row_can_be_copied_and_mutated_as_a_dict():
    row = _row()
    copy = row.to_dict()
    copy["language"] = "en"
    assert row["language"] == "fa"
    assert isinstance(copy, dict) and not isinstance(copy, Row)


def test_an_empty_result_is_none_not_an_empty_row(tmp_path):
    db = SqliteDatabase(str(tmp_path / "empty.db"))
    assert db.query_one("SELECT * FROM users WHERE user_id=?", (424242,)) is None
    assert db.query_all("SELECT * FROM users WHERE user_id=?", (424242,)) == []


# --------------------------------------------------------------------------- #
# through the database, which is what the application actually touches
# --------------------------------------------------------------------------- #


@pytest.fixture
def db(tmp_path):
    database = SqliteDatabase(str(tmp_path / "rows.db"))
    now = int(time.time())
    for uid, lang, plan in ((11, "fa", "pro"), (12, "ru", "free"), (13, "zh", "free")):
        database.execute(
            "INSERT INTO users (user_id, language, plan, created_at, updated_at) VALUES (?,?,?,?,?)",
            (uid, lang, plan, now, now),
        )
    return database


def test_query_one_returns_a_row_that_supports_get(db):
    row = db.query_one("SELECT language FROM users WHERE user_id=?", (11,))
    assert row.get("language") == "fa"
    assert row["language"] == "fa"
    assert row[0] == "fa"
    assert row.get("missing") is None


def test_query_all_returns_rows_that_support_get(db):
    rows = db.query_all("SELECT user_id, language FROM users ORDER BY user_id")
    assert [row.get("language") for row in rows] == ["fa", "ru", "zh"]
    assert [row[0] for row in rows] == [11, 12, 13]
    assert [dict(row)["language"] for row in rows] == ["fa", "ru", "zh"]


def test_the_repository_layer_still_sees_plain_dicts(db):
    """`dict(row)` conversions in the repositories keep working."""
    rows = db.query_all("SELECT * FROM users WHERE user_id=?", (12,))
    converted = [dict(row) for row in rows]
    assert converted[0]["language"] == "ru"
    assert json.dumps(converted[0])  # serializable


def test_a_row_survives_a_json_round_trip(db):
    row = db.query_one("SELECT * FROM users WHERE user_id=?", (13,))
    assert json.loads(json.dumps(dict(row)))["language"] == "zh"


def test_the_row_factory_is_wired_into_the_connection(db):
    """Not just the wrapper class: the connection must actually produce it."""
    raw = db._conn.execute("SELECT 1 AS one").fetchone()
    assert isinstance(raw, Row)
    assert raw.get("one") == 1
    assert isinstance(raw, dict)


def test_the_types_the_codebase_relies_on_are_present(db):
    """A compact statement of the contract, so a regression is one line to read."""
    row = db.query_one("SELECT * FROM users WHERE user_id=?", (11,))
    for attribute in ("get", "keys", "values", "items"):
        assert callable(getattr(row, attribute)), attribute
    assert isinstance(row, dict)
    assert isinstance(row["user_id"], int)
