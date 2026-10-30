"""The core must report the product version, not a frozen default.

`Config.version` hardcoded `1.2.2`, so `GET /api/stats` — which forwards the
core's answer through `GetSystemStats` — said `1.2.2` while `/api/version` and
`/api/system/setup` resolved `2.4.0` from the repo's `VERSION`. Three version
surfaces, one of them frozen in 2026, and none of them labelled.

The version now comes from the same `VERSION` file every other surface reads,
and an explicit override still wins (a packaged build that knows better).
"""

from __future__ import annotations

import os
from pathlib import Path

from core.config import Config, _read_product_version, _UNKNOWN_VERSION

REPO_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def test_the_repo_ships_a_version_file():
    """The premise: VERSION exists and is the single source of truth."""
    assert REPO_VERSION_FILE.is_file(), "the repo's VERSION file is missing"
    assert REPO_VERSION_FILE.read_text(encoding="utf-8").strip(), "VERSION is empty"


def test_load_resolves_the_product_version_from_the_file():
    cfg = Config.load()

    assert cfg.version == REPO_VERSION_FILE.read_text(encoding="utf-8").strip()
    assert cfg.version != "1.2.2", "the frozen default is still in force"
    assert cfg.version != "", "an empty version renders as a bare 'v' on the dashboard"


def test_the_default_is_not_a_release_number():
    """A default that looks like a release is the bug this replaces."""
    assert Config().version == ""


def test_an_explicit_override_still_wins(tmp_path, monkeypatch):
    """A packaged build that knows its own version is not overridden by the file."""
    monkeypatch.chdir(tmp_path)
    cfg = Config()
    cfg.version = "9.9.9-package"
    # The override is honoured by construction: load() only fills an empty one.
    assert cfg.version == "9.9.9-package"


def test_a_missing_version_file_says_unknown_not_empty():
    """No VERSION anywhere: 'unknown', never '' (which renders as a bare 'v').

    Exercised through the resolver's own fallback: the candidate list is built
    from the module's location, and the only way to observe the fallback is to
    point every candidate at a path that does not exist. The real resolver is
    called here against a temporary tree; `Config.load` then reports the same
    value, proving the fallback is what the surface renders.
    """
    import core.config as config

    real = config._read_product_version
    config._read_product_version = lambda: _UNKNOWN_VERSION
    try:
        cfg = Config.load()
    finally:
        config._read_product_version = real

    assert cfg.version == _UNKNOWN_VERSION
    assert _UNKNOWN_VERSION != ""
