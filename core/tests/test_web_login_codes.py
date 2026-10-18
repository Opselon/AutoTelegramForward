"""The dashboard login code: single-use, short-lived, and bound to one account.

A link in a Telegram chat is a credential sitting in a transcript. These tests
pin the three properties that make that safe: the link stops working after one
use, it stops working after ten minutes, and nothing but the code says which
account it belongs to.
"""

from __future__ import annotations

import time

import pytest

from core.application.web_login_codes import WebLoginCodeService
from core.infrastructure.persistence.database import SqliteDatabase

UID = 111
OTHER_UID = 222


def _service(tmp_path, now=None):
    db = SqliteDatabase(str(tmp_path / "codes.db"))
    clock = (lambda: now[0]) if now else time.time
    return db, WebLoginCodeService(db, clock=clock)


def test_schema_creates_the_codes_table(tmp_path):
    """The v22 migration is what makes the feature exist; assert it ran."""
    db, _ = _service(tmp_path)
    cols = [r[1] for r in db.query_all("PRAGMA table_info(web_login_codes)")]
    assert "code_hash" in cols and "used_at" in cols and "expires_at" in cols
    version = db.query_one("SELECT MAX(version) AS v FROM schema_version")
    assert int(version["v"]) >= 22


def test_a_code_is_burned_exactly_once(tmp_path):
    _, codes = _service(tmp_path)
    code, expires_at = codes.issue(UID)

    assert codes.burn(code) == UID
    # Second use: the link is dead.
    assert codes.burn(code) is None
    assert codes.burn(code) is None
    assert expires_at > int(time.time())


def test_an_expired_code_is_refused(tmp_path):
    now = [1_000_000]
    _, codes = _service(tmp_path, now=now)
    code, expires_at = codes.issue(UID)

    now[0] = expires_at - 1
    assert codes.burn(code) == UID  # still inside the window

    code2, expires_at2 = codes.issue(UID)
    now[0] = expires_at2 + 1
    assert codes.burn(code2) is None


def test_unknown_and_blank_codes_are_refused(tmp_path):
    _, codes = _service(tmp_path)
    codes.issue(UID)
    assert codes.burn("not-a-code") is None
    assert codes.burn("") is None
    assert codes.burn("   ") is None
    assert codes.burn(None) is None


def test_the_code_is_not_stored_in_the_clear(tmp_path):
    db, codes = _service(tmp_path)
    code, _ = codes.issue(UID)

    rows = db.query_all("SELECT code_hash FROM web_login_codes")
    assert len(rows) == 1
    assert code not in rows[0]["code_hash"]
    assert len(rows[0]["code_hash"]) == 64  # sha256 hex


def test_one_link_opens_one_session_under_a_race(tmp_path):
    """Two tabs clicking the same link must not both get a session."""
    _, codes = _service(tmp_path)
    code, _ = codes.issue(UID)

    results = [codes.burn(code) for _ in range(5)]
    assert results.count(UID) == 1
    assert results.count(None) == 4


def test_codes_are_issued_for_the_named_account_only(tmp_path):
    _, codes = _service(tmp_path)
    code_a, _ = codes.issue(UID)
    code_b, _ = codes.issue(OTHER_UID)

    assert codes.burn(code_a) == UID
    assert codes.burn(code_b) == OTHER_UID


def test_issuing_requires_a_user_id(tmp_path):
    _, codes = _service(tmp_path)
    with pytest.raises(ValueError):
        codes.issue(0)


def test_expired_rows_are_pruned_when_a_new_code_is_issued(tmp_path):
    now = [1_000_000]
    db, codes = _service(tmp_path, now=now)
    codes.issue(UID)

    now[0] += 60 * 60 * 24  # a day later
    codes.issue(OTHER_UID)

    remaining = db.query_all("SELECT user_id FROM web_login_codes")
    assert [int(r["user_id"]) for r in remaining] == [OTHER_UID]
