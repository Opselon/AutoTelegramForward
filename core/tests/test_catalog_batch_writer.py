"""A catalog batch must be a pure addition.

`scripts/append_catalog.py` rewrote the whole catalog with `indent=1` while the
file is kept at `indent=4`: appending fifteen keys produced a 26,632-line diff
that hid every one of them, so the review surface was a reformat instead of the
translation. The writer now detects the file's own formatting; these tests hold
both halves of the contract — a valid batch adds lines and nothing else, an
invalid batch changes nothing at all.
"""

import importlib.util
import json
import pathlib
from typing import Any, Dict

CATALOG_SRC = pathlib.Path("core/infrastructure/telegram/locales/translations.json")
WRITER = pathlib.Path("scripts/append_catalog.py")


def _load_writer() -> Any:
    spec = importlib.util.spec_from_file_location("append_catalog_under_test", WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fresh_catalog(tmp_path: pathlib.Path, monkeypatch: Any) -> Any:
    module = _load_writer()
    target = tmp_path / "translations.json"
    target.write_text(CATALOG_SRC.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(module, "CAT_PATH", target)
    return module


def _entry(locales: Any, english: str = "hello") -> Dict[str, str]:
    return {lang: ("hello" if lang == "en" else f"{lang} hello") for lang in locales}


def test_a_valid_batch_adds_lines_and_changes_nothing_else(tmp_path, monkeypatch) -> None:
    """The key sorts mid-table on purpose: a key that sorts *last* also rewrites
    the previous last line's comma (JSON has no trailing comma), which is a
    modification the diff would have to carry. Mid-table is the pure case."""
    module = _fresh_catalog(tmp_path, monkeypatch)
    before = (tmp_path / "translations.json").read_text(encoding="utf-8")
    locales = json.loads(before)

    assert module.validate_and_append({"ui_test_writer_key": _entry(locales)}) == 0

    after = (tmp_path / "translations.json").read_text(encoding="utf-8")
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added = [line for line in after_lines if line not in set(before_lines)]

    assert len(added) == len(locales), added
    # the four-space/two-level indent of the file is preserved
    assert '        "ui_test_writer_key": "hello",' in after
    assert set(before_lines) <= set(after_lines)
    # trailing-newline convention is preserved as found
    assert after.endswith("\n") == before.endswith("\n")


def test_a_batch_missing_a_locale_is_rejected_and_writes_nothing(tmp_path, monkeypatch) -> None:
    module = _fresh_catalog(tmp_path, monkeypatch)
    before = (tmp_path / "translations.json").read_text(encoding="utf-8")
    locales = sorted(json.loads(before))
    incomplete = _entry(locales)
    incomplete.pop(locales[-1])

    assert module.validate_and_append({"zz_incomplete_key": incomplete}) == 1
    assert (tmp_path / "translations.json").read_text(encoding="utf-8") == before


def test_a_batch_with_a_placeholder_mismatch_is_rejected(tmp_path, monkeypatch) -> None:
    module = _fresh_catalog(tmp_path, monkeypatch)
    locales = sorted(json.loads((tmp_path / "translations.json").read_text(encoding="utf-8")))
    entry = _entry(locales)
    entry["en"] = "value {name}"
    entry[locales[1]] = "value without a placeholder"

    assert module.validate_and_append({"zz_placeholder_key": entry}) == 1
