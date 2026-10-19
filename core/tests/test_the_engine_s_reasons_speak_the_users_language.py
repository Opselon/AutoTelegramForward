"""The routing engine's reason, said in the user's language.

`MessageRoutingEngine` answers every decision with its own token — `matched`,
`rule_paused`, `category_vip_unmatched:origin_unmatched`,
`branding_triggers_unmatched:keywords,none` — and `decide_route` answers with a
whole English sentence ("Direct clean copy from A to B"). The dry-run screen is
the one screen whose entire job is to answer "why did this message not go
through?", and it printed whichever of those came back, inside a translated
frame: a Persian user read `دلیل تصمیم: direct_copy` or
`Routed via intermediate C with branding forward (branding_vip_matched:any_matched)`.

The engine keeps its tokens — the dispatcher, the durable queue and the API
compare them, and they are the honest thing to log. `reason_text.reason_label()`
renders them, and this file holds the table to the engine: it reads the domain
module, collects every reason literal it can return, and requires each one to
come out as a translated sentence in all ten locales.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path

import pytest

from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.reason_text import (
    FAMILIES,
    PLAIN,
    SENTENCES,
    WORDS,
    reason_label,
)

CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "telegram"
    / "locales"
    / "translations.json"
)
DOMAIN = Path(__file__).resolve().parents[1] / "domain"
#: The two modules that decide, and therefore the two that name reasons.
DECIDERS = ("services.py", "branding.py")

LOCALES = ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")
PERSIAN = re.compile(r"[\u0600-\u06ff]")
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
HAN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
PLACEHOLDERS = re.compile(r"\{([^{}]+)\}")
#: `return True, "token"` / `return RoutingDecision.DROP, f"token"`.
RETURN_TOKEN = re.compile(
    r'return\s+(?:True|False|RoutingDecision\.\w+)\s*,\s*f?"([^"]+)"'
)
#: A token's placeholder names a nested value, not literal text.
PLACEHOLDER = re.compile(r"\{[^{}]*\}")

#: What the engine's placeholders stand for, when the test has to pick one.
SAMPLES = {
    "reason": "origin_unmatched",
    "loop_reason": "target_matches_source",
    "hop_reason": "branding_always",
    "vip[1]": "any_matched:regex_matched",
    "matched": "keywords",
    "detail": "keywords",
}
DEFAULT_SAMPLE = "origin_unmatched"


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def translate(lang: str):
    """A `t` that fails the test when the catalog has no such key."""

    def _t(key, **params):
        text = I18n().t(key, lang=lang, **params)
        assert text and text != key, f"{key} is missing from the {lang} catalog"
        assert "{" not in text, f"{key} left a placeholder unfilled in {lang}: {text!r}"
        return text

    return _t


def tokens_from_source() -> set:
    """Every reason literal the domain modules can return."""
    found = set()
    for name in DECIDERS:
        text = (DOMAIN / name).read_text(encoding="utf-8")
        found.update(RETURN_TOKEN.findall(text))
    return found


def concrete(literal: str) -> str:
    """A literal with its placeholders filled in the way the engine fills them."""
    def sample(match: re.Match) -> str:
        expr = match.group(0)[1:-1]
        for name, value in SAMPLES.items():
            if name in expr:
                return value
        return DEFAULT_SAMPLE

    filled = PLACEHOLDER.sub(sample, literal)
    # `"branding_triggers_matched:" + ",".join(matched)`: a family whose detail
    # is built by the caller. The panel never sees it empty, but the token is
    # still a token this table must know.
    return filled + "none" if filled.endswith(":") else filled


# --------------------------------------------------------------------------- #
# The table and the engine agree
# --------------------------------------------------------------------------- #


def test_the_scan_finds_the_engine_s_reasons():
    """A scan that found nothing would pass every check below."""
    found = tokens_from_source()
    assert len(found) > 25, f"only {len(found)} reason literals found: {sorted(found)}"
    assert "matched" in found and "rule_paused" in found
    assert any(t.startswith("branding_") for t in found)


def test_every_reason_the_engine_returns_has_a_sentence():
    missing = []
    for literal in sorted(tokens_from_source()):
        token = concrete(literal)
        written = reason_label(token, translate("fa"))
        if written == token:
            missing.append(literal)
    assert missing == [], f"reasons with no translation: {missing}"


def test_every_token_stays_out_of_the_rendered_sentence():
    """The rendered text must not contain the machine token it replaces."""
    for literal in sorted(tokens_from_source()):
        token = concrete(literal)
        written = reason_label(token, translate("fa"))
        head = token.split(":")[0]
        assert head not in written, f"{token} leaked into {written!r}"


def test_the_route_sentences_are_matched_not_dropped():
    text = (DOMAIN / "services.py").read_text(encoding="utf-8")
    for sentence, key in SENTENCES.items():
        assert sentence in text, f"{sentence!r} is no longer the engine's wording"
        assert key in catalog()["en"]
        assert catalog()["en"][key] != key


def test_the_engine_keeps_its_tokens():
    """The renderer is a view: it must not have replaced the engine's vocabulary."""
    text = (DOMAIN / "services.py").read_text(encoding="utf-8")
    for token in ("rule_inactive", "rule_paused", "media_handling_text_only",
                  "all_criteria_not_satisfied"):
        assert f'"{token}"' in text, f"{token} is no longer returned by the engine"


# --------------------------------------------------------------------------- #
# The catalog covers the table
# --------------------------------------------------------------------------- #


def table_keys() -> set:
    return set(PLAIN.values()) | set(FAMILIES.values()) | set(WORDS.values()) | set(SENTENCES.values())


def test_the_table_has_no_stray_key():
    cat = catalog()
    unknown = sorted(k for k in table_keys() if k not in cat["en"])
    assert unknown == [], f"keys the table names but no catalog defines: {unknown}"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_sentence_exists_in_every_locale(locale: str):
    cat = catalog()
    absent = [
        key for key in sorted(table_keys())
        if not str(cat.get(locale, {}).get(key, "")).strip()
    ]
    assert absent == [], f"{len(absent)} reason sentence(s) missing in {locale}: {absent[:6]}"


@pytest.mark.parametrize("locale,pattern", [
    ("fa", PERSIAN),
    ("ru", CYRILLIC),
    ("zh", HAN),
])
def test_the_sentences_are_written_in_that_locale_s_script(locale: str, pattern):
    cat = catalog()
    foreign = [key for key in sorted(table_keys()) if not pattern.search(cat[locale][key])]
    assert foreign == [], f"reason sentences still in Latin script in {locale}: {foreign}"


def test_no_sentence_fell_back_to_english():
    cat = catalog()
    for locale in LOCALES:
        if locale == "en":
            continue
        same = [key for key in sorted(table_keys()) if cat[locale][key] == cat["en"][key]]
        assert same == [], f"{locale} shows the English reason(s) for {same}"


def test_the_placeholders_match_what_the_renderer_fills():
    cat = catalog()
    for locale in LOCALES:
        for family, key in FAMILIES.items():
            params = set(PLACEHOLDERS.findall(cat[locale][key]))
            expected = {"words"} if family in (
                "all_matched", "any_matched",
                "branding_triggers_matched", "branding_triggers_unmatched",
            ) else {"reason"}
            assert params == expected, f"{key} in {locale}: {sorted(params)} != {sorted(expected)}"


def test_no_other_template_prints_a_reason_token():
    """The class, not the instance: no catalog value is a machine token."""
    cat = catalog()
    offenders = sorted(
        (table_key, value)
        for table_key, value in cat["en"].items()
        if value in PLAIN or value in SENTENCES or value.split(":")[0] in FAMILIES
    )
    assert offenders == [], f"catalog values that are engine tokens: {offenders}"


# --------------------------------------------------------------------------- #
# What the renderer does with them
# --------------------------------------------------------------------------- #


def test_a_nested_reason_reads_as_one_sentence():
    written = reason_label("loop_detected:target_matches_source", translate("fa"))
    assert catalog()["fa"]["ui_reason_target_matches_source"] in written
    assert "loop_detected" not in written


def test_two_levels_of_nesting_render():
    written = reason_label("category_vip_unmatched:UNKNOWN_ORIGIN:missing_forward_metadata",
                           translate("fa"))
    assert catalog()["fa"]["ui_reason_word_missing_forward_metadata"] in written
    assert "UNKNOWN_ORIGIN" not in written


def test_a_list_of_matched_conditions_is_named():
    written = reason_label("all_matched:origin_matched,regex_matched", translate("fa"))
    cat = catalog()["fa"]
    assert cat["ui_reason_word_origin"] in written
    assert cat["ui_reason_word_regex"] in written
    assert "," in written


def test_a_branding_hop_reason_keeps_its_own_sentence():
    written = reason_label("branding_vip_unmatched:no_match", translate("fa"))
    assert catalog()["fa"]["ui_reason_word_no_match"] in written
    assert "no_match" not in written


def test_the_route_sentence_carries_its_hop_reason():
    written = reason_label(
        "Routed via intermediate C with branding forward (branding_disabled:flag_off)",
        translate("fa"),
    )
    cat = catalog()["fa"]
    assert cat["ui_reason_route_branding"] in written
    assert cat["ui_reason_word_flag_off"] in written
    assert "Routed via" not in written


def test_the_plain_route_sentence_renders_alone():
    written = reason_label("Direct clean copy from A to B", translate("fa"))
    assert written == catalog()["fa"]["ui_reason_route_direct_copy"]


def test_an_unknown_token_is_shown_as_stored_and_logged(caplog):
    with caplog.at_level(logging.ERROR):
        written = reason_label("brand_new_reason:whatever", translate("fa"))
    assert written == "brand_new_reason:whatever"
    assert "brand_new_reason" in caplog.text


def test_an_unknown_condition_word_is_shown_as_stored_and_logged(caplog):
    with caplog.at_level(logging.ERROR):
        written = reason_label("all_matched:something_new", translate("fa"))
    assert "something_new" in written
    assert "something_new" in caplog.text


def test_an_empty_reason_renders_nothing():
    assert reason_label("", translate("fa")) == ""
    assert reason_label(None, translate("fa")) == ""


def test_the_simulator_line_renders_the_reason_in_the_language_asked_for():
    for locale, pattern in (("fa", PERSIAN), ("ru", CYRILLIC), ("zh", HAN)):
        written = reason_label("no_criteria_matched", translate(locale))
        assert pattern.search(written), f"{locale}: {written!r}"
        assert written == catalog()[locale]["ui_reason_no_criteria_matched"]


# --------------------------------------------------------------------------- #
# The frame around the reason
# --------------------------------------------------------------------------- #


def test_the_frame_line_is_translated():
    """The dry-run's own line was English: `f"\U0001F4CB **Task:** `{rule.id[:8]}` (...)"`."""
    cat = catalog()
    for locale in LOCALES:
        value = cat[locale].get("ui_sim_task_line", "")
        assert value.strip(), f"ui_sim_task_line is missing in {locale}"
        assert set(PLACEHOLDERS.findall(value)) == {"task", "source", "target"}
    assert cat["fa"]["ui_sim_task_line"] != cat["en"]["ui_sim_task_line"]


def test_the_frame_line_renders():
    written = I18n().t("ui_sim_task_line", lang="fa", task="ab12cd34", source="-1001", target="-1002")
    assert "ab12cd34" in written and "-1001" in written and "{task}" not in written


def test_every_routing_decision_has_a_label():
    """`route_label` falls back to `str(decision)`: a new member would print
    `RoutingDecision.WHATEVER` on the screen."""
    from core.domain.value_objects import RoutingDecision

    source = (Path(__file__).resolve().parents[1]
              / "infrastructure" / "telegram" / "pro_bot_ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    labelled = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "route_label"
            for target in node.targets
        ):
            for inner in ast.walk(node.value):
                if not isinstance(inner, ast.Dict):
                    continue
                for value in inner.keys:
                    if isinstance(value, ast.Attribute) and value.attr[:1].isupper():
                        labelled.add(value.attr)
    assert labelled, "route_label is gone from the dry-run screen"
    missing = sorted(set(RoutingDecision.__members__) - labelled)
    assert missing == [], f"routing decisions with no label: {missing}"
