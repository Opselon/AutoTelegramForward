"""The i18n audit must actually detect the defects it claims to detect.

A scanner that only ever prints "clean" is indistinguishable from a broken one,
so every finding class is proven here by injecting the defect into a throwaway
copy of the tree and asserting the engine reports exactly that class. The
fixture starts from the real catalog (1.5k keys × 10 locales), because a toy
catalog would not exercise the placeholder/parity paths at all.

Mutation coverage:

    HARDCODED_USER_TEXT      literal in send_message, in an f-string, and in a
                             `_kbd([[("Label", data)]])` tuple
    MISSING_KEY              `_t("ui_key_that_does_not_exist")`
    PLACEHOLDER_MISMATCH     a locale drops `{count}`
    VALUE_LEAK               Persian text under `es`
    UNTRANSLATED_VALUE       English prose left verbatim under `fr`
    GLOBAL_LANGUAGE_STATE    `self.i18n.current` read on a render path
    UNWRAPPED_HANDLER        a handler with a context-dependent `_t` call
    CALLBACK_DATA_FROM_TEXT  `callback_data=self._t("ui_yes")`
    UNUSED_KEY               a key nothing references
    baseline                 a pinned finding stops failing; a new one fails

The false-positive tests matter just as much: technical tokens, callback-data
identifiers, digit keypads and a handler that resolves `lang` explicitly must
all stay silent.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import i18n_audit as audit_mod  # noqa: E402

CATALOG = REPO / "core/infrastructure/telegram/locales/translations.json"

CLEAN_MODULE = '''"""Synthetic bot module used by the audit tests."""
from pyrogram import Client, filters


class SampleUI:
    def __init__(self, bot, i18n):
        self.bot = bot
        self.i18n = i18n

        @bot.on_message(filters.private)
        @self._localized_handler
        async def _on_text(client, message):
            await message.reply_text(self._t("ui_help"))

    def _t(self, key, lang=None, **kw):
        return self.i18n.t(key, lang=lang or "en", **kw)

    def _localized_handler(self, handler):
        return handler
'''


def build_repo(tmp_path: Path) -> Path:
    """A minimal tree the audit can walk: catalog + one clean module."""
    root = tmp_path / "repo"
    locale_dir = root / "core/infrastructure/telegram/locales"
    locale_dir.mkdir(parents=True)
    shutil.copy(CATALOG, locale_dir / "translations.json")
    shutil.copy(REPO / "core/infrastructure/telegram/i18n.py",
                root / "core/infrastructure/telegram/i18n.py")
    (root / "core/infrastructure/telegram/ui_sample.py").write_text(CLEAN_MODULE, encoding="utf-8")
    return root


def findings_for(root: Path, kind: str = "") -> list:
    result = audit_mod.audit(root)
    return [f for f in result.findings if not kind or f.kind == kind]


def append(root: Path, code: str) -> None:
    path = root / "core/infrastructure/telegram/ui_sample.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + code, encoding="utf-8")


def edit_catalog(root: Path, mutate) -> None:
    path = root / "core/infrastructure/telegram/locales/translations.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# baseline health: the clean fixture is clean
# --------------------------------------------------------------------------- #
def test_a_clean_fixture_has_no_critical_or_major_findings(tmp_path):
    root = build_repo(tmp_path)
    result = audit_mod.audit(root)
    serious = [f for f in result.findings if f.severity in {"critical", "major"}]
    assert serious == [], f"unexpected findings: {[(f.kind, f.detail) for f in serious]}"
    assert result.stats["handlers"] == 1
    assert result.stats["wrapped_handlers"] == 1


def test_the_real_tree_has_no_critical_findings():
    """The repository itself must pass what CI enforces."""
    result = audit_mod.audit(REPO)
    critical = [f for f in result.findings if f.severity == "critical"]
    assert critical == [], [(f.path, f.line, f.detail) for f in critical]


# --------------------------------------------------------------------------- #
# HARDCODED_USER_TEXT — three shapes the old guard missed
# --------------------------------------------------------------------------- #
def test_literal_in_send_message_is_flagged(tmp_path):
    root = build_repo(tmp_path)
    append(root, '''
    async def _plain(self, message):
        await message.reply_text("Settings saved, restarting the bot now")
''')
    hits = findings_for(root, "HARDCODED_USER_TEXT")
    assert len(hits) == 1
    assert "Settings saved" in hits[0].evidence


def test_prose_in_an_f_string_is_flagged(tmp_path):
    """A regex over literals sees one opaque JoinedStr; a human sees English."""
    root = build_repo(tmp_path)
    append(root, '''
    async def _f(self, message, count):
        await message.reply_text(f"Deleted {count} messages from the queue")
''')
    hits = findings_for(root, "HARDCODED_USER_TEXT")
    assert len(hits) == 1
    assert "Deleted" in hits[0].evidence


def test_literal_keyboard_label_is_flagged(tmp_path):
    """`_kbd([[("Delete", data)]])` never appears in a visible call."""
    root = build_repo(tmp_path)
    append(root, '''
    def _kbd(self, rows):
        return rows

    def _menu(self):
        return self._kbd([[("Delete everything", "purge:all")]])
''')
    hits = findings_for(root, "HARDCODED_USER_TEXT")
    assert [h.detail for h in hits] == ["keyboard label"]
    assert "Delete everything" in hits[0].evidence


def test_keypad_digits_and_callback_data_are_not_flagged(tmp_path):
    """False-positive guard: a numeric keypad and machine tokens are not copy."""
    root = build_repo(tmp_path)
    append(root, '''
    def _kbd(self, rows):
        return rows

    def _pad(self):
        return self._kbd([[("1", "k:1"), ("2", "k:2")], [("https://example.com", "url")]])

    async def _cb(self, cq):
        await cq.answer()
''')
    assert findings_for(root, "HARDCODED_USER_TEXT") == []


def test_translated_label_with_english_tail_is_flagged(tmp_path):
    root = build_repo(tmp_path)
    append(root, '''
    async def _mixed(self, message):
        await message.reply_text(self._t("ui_help") + " (English note)")
''')
    hits = findings_for(root, "HARDCODED_USER_TEXT")
    assert hits and "English note" in hits[0].evidence


# --------------------------------------------------------------------------- #
# keys
# --------------------------------------------------------------------------- #
def test_missing_key_is_critical(tmp_path):
    root = build_repo(tmp_path)
    append(root, '''
    async def _missing(self, message):
        await message.reply_text(self._t("ui_this_key_does_not_exist"))
''')
    hits = findings_for(root, "MISSING_KEY")
    assert len(hits) == 1
    assert hits[0].severity == "critical"
    assert hits[0].detail == "ui_this_key_does_not_exist"


def test_a_key_forwarded_through_a_parameter_is_not_unused(tmp_path):
    """`title_key="ui_x"` is a reference even when `_t` sees only a name."""
    root = build_repo(tmp_path)
    key = sorted(json.loads(CATALOG.read_text(encoding="utf-8"))["en"])[0]
    append(root, f'''
    def _panel(self, title_key):
        return self._t(title_key)

    def _show(self):
        return self._panel(title_key="{key}")
''')
    unused = {f.detail for f in findings_for(root, "UNUSED_KEY")}
    assert key not in unused


def test_unused_key_is_reported_once(tmp_path):
    root = build_repo(tmp_path)
    edit_catalog(root, lambda data: data["en"].__setitem__("ui_dead_weight_key", "Dead"))
    unused = {f.detail for f in findings_for(root, "UNUSED_KEY")}
    assert "ui_dead_weight_key" in unused
    assert all(f.severity == "minor" for f in findings_for(root, "UNUSED_KEY"))


# --------------------------------------------------------------------------- #
# catalog defects
# --------------------------------------------------------------------------- #
def test_placeholder_mismatch_is_flagged(tmp_path):
    root = build_repo(tmp_path)
    key = next(k for k, v in json.loads(CATALOG.read_text(encoding="utf-8"))["en"].items() if "{count}" in v)
    edit_catalog(root, lambda data: data["fa"].__setitem__(key, data["fa"][key].replace("{count}", "3")))
    hits = findings_for(root, "PLACEHOLDER_MISMATCH")
    assert [h.detail for h in hits] == [f"fa: {key}"]
    assert hits[0].severity == "critical"


def test_foreign_script_in_a_latin_locale_is_a_leak(tmp_path):
    root = build_repo(tmp_path)
    key = sorted(json.loads(CATALOG.read_text(encoding="utf-8"))["en"])[0]
    edit_catalog(root, lambda data: data["es"].__setitem__(key, "ذخیره شد"))
    hits = findings_for(root, "VALUE_LEAK")
    assert any(h.detail == f"es: {key}" for h in hits)


def test_english_prose_left_in_a_locale_is_untranslated(tmp_path):
    root = build_repo(tmp_path)
    key = next(k for k, v in json.loads(CATALOG.read_text(encoding="utf-8"))["en"].items()
               if len(v.split()) >= 3 and "{" not in v and k.startswith("ui_"))
    edit_catalog(root, lambda data: data["fr"].__setitem__(key, data["en"][key]))
    hits = findings_for(root, "UNTRANSLATED_VALUE")
    assert any(h.detail == f"fr: {key}" for h in hits)


def test_brand_names_are_not_untranslated(tmp_path):
    """'VIP Pro' is a plan name; flagging it would train people to ignore the gate."""
    root = build_repo(tmp_path)
    edit_catalog(root, lambda data: data["de" if False else "it"].__setitem__("ui_plan_pro", "VIP Pro"))
    assert findings_for(root, "UNTRANSLATED_VALUE") == []


# --------------------------------------------------------------------------- #
# runtime language state
# --------------------------------------------------------------------------- #
def test_reading_the_process_wide_language_is_critical(tmp_path):
    root = build_repo(tmp_path)
    append(root, '''
    def _bad(self):
        return self.i18n.current
''')
    hits = findings_for(root, "GLOBAL_LANGUAGE_STATE")
    assert len(hits) == 1 and hits[0].severity == "critical"


def test_callback_data_built_from_a_translation_is_critical(tmp_path):
    root = build_repo(tmp_path)
    append(root, '''
    def _menu(self):
        return [{"text": "x", "callback_data": self._t("ui_yes")}]
''')
    hits = findings_for(root, "CALLBACK_DATA_FROM_TEXT")
    assert len(hits) == 1


def test_handler_without_language_binding_is_flagged(tmp_path):
    root = build_repo(tmp_path)
    append(root, '''
    def register(self, bot):
        @bot.on_message(filters.private)
        async def _unbound(client, message):
            await message.reply_text(self._t("ui_help"))
        return _unbound
''')
    hits = findings_for(root, "UNWRAPPED_HANDLER")
    assert [h.detail for h in hits] == ["_unbound"]


def test_handler_that_passes_lang_explicitly_is_not_flagged(tmp_path):
    """Resolving the language per message is as isolated as the decorator."""
    root = build_repo(tmp_path)
    append(root, '''
    def register2(self, bot):
        @bot.on_message(filters.private)
        async def _bound(client, message):
            lang = self._t.__self__ and "en"
            await message.reply_text(self.i18n.t("ui_help", lang=lang))
        return _bound
''')
    assert findings_for(root, "UNWRAPPED_HANDLER") == []


# --------------------------------------------------------------------------- #
# baseline + exit codes
# --------------------------------------------------------------------------- #
def test_baseline_pins_known_debt_and_still_fails_on_new_debt(tmp_path):
    root = build_repo(tmp_path)
    append(root, '''
    async def _legacy(self, message):
        await message.reply_text("Legacy English screen nobody fixed yet")
''')
    baseline = root / "baseline.json"
    assert audit_mod.main(["--root", str(root), "--baseline", str(baseline), "--update-baseline"]) == 0

    # the pinned finding no longer fails the run
    assert audit_mod.main(["--root", str(root), "--baseline", str(baseline), "--fail-on", "critical"]) == 0

    # a *new* defect does
    append(root, '''
    async def _fresh(self, message):
        await message.reply_text("Brand new hardcoded sentence")
''')
    assert audit_mod.main(["--root", str(root), "--baseline", str(baseline), "--fail-on", "critical"]) == 1


def test_json_report_lists_counts_and_fingerprints(tmp_path, capsys):
    root = build_repo(tmp_path)
    append(root, '''
    async def _x(self, message):
        await message.reply_text(self._t("ui_nope_not_here"))
''')
    out = tmp_path / "report.json"
    audit_mod.main(["--root", str(root), "--json", str(out), "--fail-on", "none"])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["counts"]["MISSING_KEY"] == 1
    finding = next(f for f in payload["findings"] if f["kind"] == "MISSING_KEY")
    assert len(finding["fingerprint"]) == 16
    assert finding["severity"] == "critical"


def test_a_locale_in_the_catalog_but_not_the_registry_is_drift(tmp_path):
    """A table no `_t` call can ever select is 1,561 translations nobody reaches."""
    root = build_repo(tmp_path)
    edit_catalog(root, lambda data: data.update(de={"zh": "어"}))
    hits = findings_for(root, "LOCALE_REGISTRY_DRIFT")
    assert len(hits) == 1
    assert "de" in hits[0].detail


def test_a_broken_catalog_exits_two(tmp_path):
    root = build_repo(tmp_path)
    (root / "core/infrastructure/telegram/locales/translations.json").write_text("{}", encoding="utf-8")
    assert audit_mod.main(["--root", str(root)]) == 2


def test_a_resolver_that_defaults_to_hardcoded_english_is_reported(tmp_path):
    """The dashboard defect: a resolver answering "en" with no update scope."""
    root = build_repo(tmp_path)
    append(root, '''
    def _resolve_lang(self, uid=None, user_obj=None):
        if uid:
            pass
        return "en"
''')
    hits = findings_for(root, "BARE_DEFAULT_LANGUAGE")
    assert len(hits) == 1, [h.detail for h in hits]
    assert "_resolve_lang" in hits[0].detail
    assert hits[0].severity == "minor"


def test_a_resolver_that_reads_the_update_scope_is_not_reported(tmp_path):
    root = build_repo(tmp_path)
    append(root, '''
    def _resolve_lang(self, uid=None, user_obj=None):
        if uid:
            pass
        return _BOT_RENDER_LANGUAGE.get() or "en"
''')
    assert findings_for(root, "BARE_DEFAULT_LANGUAGE") == []
