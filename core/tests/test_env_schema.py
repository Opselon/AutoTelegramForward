"""The `.env` document and its schema, as behaviour rather than as a dict.

Every test here corresponds to a way a configuration edit can destroy something
the operator cannot retype: a comment block, an undocumented key, a line the
parser did not understand, or the value of a key that appears twice. The document
type exists so those are *impossible*, so this file asserts the impossible did
not happen rather than that a particular function was called.
"""

from __future__ import annotations

import pytest

from atfcli.domain import envschema
from atfcli.domain.envschema import EnvDocument


# ------------------------------------------------------------------- parsing
def test_comments_blank_lines_and_unknown_lines_survive_a_round_trip() -> None:
    text = (
        "# why this deployment exists\n"
        "ATF_BOT_TOKEN=123456:AA-not-a-real-token\n"
        "\n"
        "# a second block\n"
        "ATF_WEB_PORT=8088\n"
        "  export LEGACY_THING=kept\n"
        "a line that is not an assignment\n"
    )
    rendered = EnvDocument.parse(text).with_values({"ATF_WEB_PORT": "8090"}).render()

    assert "# why this deployment exists" in rendered
    assert "# a second block" in rendered
    assert "  export LEGACY_THING=kept" in rendered
    assert "a line that is not an assignment" in rendered
    assert "ATF_WEB_PORT=8090" in rendered
    assert "ATF_WEB_PORT=8088" not in rendered


def test_values_are_unquoted_and_quoted_values_round_trip() -> None:
    document = EnvDocument.parse('A="hello world"\nB=plain\nC="say \\"hi\\""\n')
    assert document.values() == {"A": "hello world", "B": "plain", "C": 'say "hi"'}

    rendered = document.with_values({"A": "hello world", "D": "two words"}).render()
    assert 'A="hello world"' in rendered
    assert 'D="two words"' in rendered
    # Round-trip: what was rendered parses back to what was set.
    assert EnvDocument.parse(rendered).values()["A"] == "hello world"
    assert EnvDocument.parse(rendered).values()["D"] == "two words"


def test_last_assignment_wins_and_duplicates_are_reported() -> None:
    document = EnvDocument.parse("ATF_WEB_PORT=8080\nATF_WEB_PORT=8090\n")
    assert document.values()["ATF_WEB_PORT"] == "8090"
    assert document.duplicates() == ["ATF_WEB_PORT"]


def test_patching_rewrites_every_occurrence_so_the_effective_value_is_the_one_set() -> None:
    """The bug this prevents: set reports success, the old value stays effective."""
    document = EnvDocument.parse("ATF_WEB_PORT=8080\n# note\nATF_WEB_PORT=8090\n")
    patched = document.with_values({"ATF_WEB_PORT": "9000"})
    assert patched.occurrences("ATF_WEB_PORT") == 2
    assert patched.values()["ATF_WEB_PORT"] == "9000"
    assert "# note" in patched.render()


def test_render_is_stable_and_ends_with_exactly_one_newline() -> None:
    for text in ("A=1", "A=1\n", "A=1\n\n\n", ""):
        rendered = EnvDocument.parse(text).render()
        if text.strip():
            assert rendered.endswith("\n") and not rendered.endswith("\n\n")
            assert EnvDocument.parse(rendered).render() == rendered
        else:
            assert rendered == ""


def test_an_empty_value_is_written_as_an_empty_assignment() -> None:
    rendered = EnvDocument.parse("A=1\n").with_values({"B": ""}).render()
    assert "B=\n" in rendered
    assert EnvDocument.parse(rendered).values()["B"] == ""


# ---------------------------------------------------------------- validation
def test_key_shape_is_the_injection_boundary() -> None:
    assert envschema.validate_key("ATF_WEB_PORT") is None
    for bad in ("", "lower", "1LEADING", "WITH-DASH", "WITH SPACE", "A=B", "A\nB", "A#B"):
        assert envschema.validate_key(bad), bad


def test_a_value_cannot_smuggle_a_second_assignment() -> None:
    problem = envschema.validate_value("ATF_WEB_PORT", "8090\nATF_DISABLE_BOT=0")
    assert problem and "single line" in problem


def test_documented_values_are_checked_by_kind_range_and_choice() -> None:
    assert envschema.validate_value("ATF_WEB_PORT", "8090") is None
    assert "port" in (envschema.validate_value("ATF_WEB_PORT", "99999") or "")
    assert "port" in (envschema.validate_value("ATF_WEB_PORT", "http") or "")
    assert envschema.validate_value("ATF_RATELIMIT_ALGO", "tb") is None
    assert envschema.validate_value("ATF_RATELIMIT_ALGO", "fixed") is not None
    assert envschema.validate_value("ATF_TLS_EMAIL", "ops@example.com") is None
    assert envschema.validate_value("ATF_TLS_EMAIL", "not-an-email") is not None
    assert envschema.validate_value("ADMIN_IDS", "5094837833,5094837834") is None
    assert envschema.validate_value("ADMIN_IDS", "5094837833,not-a-number") is not None


def test_placeholder_credentials_are_refused_before_they_are_written() -> None:
    for key, placeholder in envschema.PLACEHOLDERS.items():
        problem = envschema.validate_value(key, placeholder)
        assert problem, key
        assert "placeholder" in problem or "token" in problem


def test_undocumented_keys_are_allowed_by_default_and_refused_when_strict() -> None:
    assert envschema.validate_value("ATF_TOTALLY_UNKNOWN", "1") is None
    strict = envschema.validate_value("ATF_TOTALLY_UNKNOWN", "1", allow_unknown=False)
    assert strict and "not a documented setting" in strict


def test_schema_json_never_carries_a_secret_default() -> None:
    document = envschema.specs_json()
    by_name = {row["name"]: row for row in document}
    assert by_name["ATF_BOT_TOKEN"]["secret"] is True
    assert by_name["ATF_BOT_TOKEN"]["default"] == "[REDACTED]"
    assert by_name["ATF_MASTER_KEY"]["secret"] is True
    for row in document:
        if row["secret"]:
            assert row["default"] == "[REDACTED]"


def test_every_schema_key_validates_against_its_own_default() -> None:
    """A documented default that its own rule refuses would be a trap."""
    for spec in envschema.SCHEMA:
        if spec.default:
            assert spec.validate(spec.default) is None, spec.name


def test_required_keys_are_the_ones_setup_asks_for() -> None:
    required = set(envschema.required_keys())
    assert {"ATF_BOT_TOKEN", "ATF_API_ID", "ATF_API_HASH", "ATF_MASTER_KEY", "ADMIN_USER_ID"} <= required


@pytest.mark.parametrize("group", list(envschema.GROUPS))
def test_every_group_has_a_description_for_each_key(group: str) -> None:
    specs = envschema.specs_in(group)
    assert specs
    for spec in specs:
        assert spec.description, spec.name
        assert envschema.describe(spec.name)
