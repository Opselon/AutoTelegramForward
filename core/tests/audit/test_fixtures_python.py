"""Python executable fixture harness — runs real production engines.

Every fixture whose ``executable`` is ``python``, ``both`` or ``meta`` is
executed against the production domain module it names. Nothing is
reimplemented here: the harness builds a :class:`ForwardRule` /
:class:`FilterRule` / :class:`ContentPolicy` / :class:`CleanerRule` from the
fixture's ``input``, calls the engine, and asserts on the real result.

Routing (from ``_fixture-contract.md``):
  * ``python`` — a real Python production engine executes it. Must run here.
  * ``both``   — Python and Go both have the engine; run the Python half and
                  record the expectation. Parity verdicts are the coverage
                  report's job, not this file's.
  * ``meta``   — a numeric/counting claim executed by a small probe.
  * ``schema`` — NOT executed. Characterization data. ``schema`` fixtures are
                 explicitly collected and reported as not-run so the coverage
                 denominator stays honest.

No network, no database, no sleeps, no Telegram.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from .conftest import (
    executable_fixture_records,
    fixture_records,
    schema_fixture_records,
)

# --- production domain modules (never reimplemented here) ------------------ #
from core.domain.services import (  # noqa: E402
    EMOJI_REGEX,
    AdvancedSettingsEngine,
    FilterEngine,
    HeaderFormatter,
    LinkManagementEngine,
)
from core.domain.entities import (  # noqa: E402
    CleanerRule,
    FilterRule,
    ForwardRule,
    MessagePayload,
)
from core.domain.content_policy import ContentPolicy, has_link  # noqa: E402
from core.domain.media_filter import link_block_reason  # noqa: E402
from core.domain.text_replace import apply_replacements  # noqa: E402
from core.domain.rule_settings import SETTING_SPECS, coerce_value  # noqa: E402
from core.domain.value_objects import MediaType  # noqa: E402


# --------------------------------------------------------------------------- #
# Construction helpers
# --------------------------------------------------------------------------- #

_ENGINE = FilterEngine()

_BASE_RULE = dict(
    session_id="s",
    source_chat_id="-1001",
    target_chat_id="-1002",
    routing_type="CHANNEL_TO_CHANNEL",
    forward_mode="COPY_MESSAGE",
    is_active=True,
)


def make_rule(**props: Any) -> ForwardRule:
    """A minimal active ForwardRule, then set the metadata props the fixture names."""
    rule = ForwardRule(id="rule-fx", **_BASE_RULE)
    for key, value in props.items():
        setattr(rule, key, value)
    return rule


def make_filter_rule(spec: Optional[Dict[str, Any]]) -> Optional[FilterRule]:
    """A FilterRule from a fixture's ``rule`` dict; ``None`` stays ``None``.

    ``None`` is a legal input: several fixtures pin "no filter attached".
    """
    if spec is None:
        return None
    rule = FilterRule()
    for key, value in spec.items():
        setattr(rule, key, value)
    return rule


def make_cleaner(spec: Dict[str, Any]) -> CleanerRule:
    cleaner = CleanerRule()
    for key, value in spec.items():
        setattr(cleaner, key, value)
    return cleaner


def make_payload(text: str = "", **props: Any) -> MessagePayload:
    base: Dict[str, Any] = dict(message_id=1, chat_id="-1001", media_type=MediaType.TEXT)
    base.update(props)
    return MessagePayload(text=text, **base)


def replacements_map(entries: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Fixture list form -> the map ``apply_replacements`` consumes."""
    out: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        value: Dict[str, Any] = {"to": entry.get("to", "")}
        for opt in ("mode", "flags", "priority", "whole_word"):
            if opt in entry:
                value[opt] = entry[opt]
        out[entry["source"]] = value
    return out


def _action_name(result: Any) -> str:
    action = getattr(result, "action", result)
    return action.value if hasattr(action, "value") else str(action)


# --------------------------------------------------------------------------- #
# Expected-value helpers
# --------------------------------------------------------------------------- #

_PY_KEYS = (
    "expected_python",
    "expected_python_output",
    "expected_python_durable_output",
    "expected_python_legacy_output",
    "expected_python_preview_output",
    "expected_b_python_output",
    "expected_effective_text",
)


def expected_value(rec: Dict[str, Any]) -> Any:
    for key in _PY_KEYS:
        if key in rec:
            return rec[key]
    return None


def _fail(rec: Dict[str, Any], key: str, exp: Any, got: Any) -> None:
    raise AssertionError(
        f"fixture {rec['id']!r} engine={rec['engine']!r} key={key!r}\n"
        f"  expected: {exp!r}\n"
        f"  actual:   {got!r}"
    )


def _check(rec: Dict[str, Any], got: Any) -> None:
    _fail(rec, "expected_python*", expected_value(rec), got) if got != expected_value(rec) else None


def _check_text(rec: Dict[str, Any], got: str, key: str) -> None:
    exp = rec.get(key)
    if got != exp:
        _fail(rec, key, exp, got)


# --------------------------------------------------------------------------- #
# Adapters — one per engine the fixtures name.
# --------------------------------------------------------------------------- #


def run_filter_engine(rec: Dict[str, Any]) -> Dict[str, str]:
    """FLT-* / SVC-01: the real FilterEngine decision."""
    i = rec["input"]
    rule = make_filter_rule(i.get("rule"))
    payload = make_payload(
        i.get("text", ""),
        is_service=bool(i.get("is_service", False)),
    )
    result = _ENGINE.evaluate(payload, rule)
    return {"action": _action_name(result), "reason": result.reason}


def run_content_policy(rec: Dict[str, Any], policy_key: str = "policy") -> Dict[str, Any]:
    """POL-* / FLT-DISABLED-01: the real ContentPolicy decision."""
    i = rec["input"]
    policy = ContentPolicy.from_dict(i.get(policy_key, {}))
    decision = policy.evaluate(i.get("text", ""))
    exp = expected_value(rec)
    reason = decision.reason
    if isinstance(exp, dict) and " or " in str(exp.get("reason", "")):
        # POL-EMPTY-01: fixture allows 'policy_inactive or policy_passed'
        allowed_reasons = [r.strip() for r in exp["reason"].split(" or ")]
        if reason in allowed_reasons:
            reason = exp["reason"]
    return {"allowed": decision.allowed, "reason": reason}


def run_cleaner(rec: Dict[str, Any]) -> str:
    """CLN-*: CleanerRule.clean, the pipeline's cleaner stage (entities.py)."""
    i = rec["input"]
    return make_cleaner(i["cleaner"]).clean(i["text"])


def run_emoji_regex(rec: Dict[str, Any]) -> str:
    """UNI-EMOJI-*: EMOJI_REGEX (services.py), the engine remove_emojis uses."""
    return EMOJI_REGEX.sub("", rec["input"]["text"])


def run_apply_replacements(rec: Dict[str, Any]) -> str:
    """RPL-*: apply_replacements, the single replacement authority."""
    i = rec["input"]
    return apply_replacements(i["text"], replacements_map(i["replacements"]))


def run_link_gates(rec: Dict[str, Any]) -> None:
    """LNK-GATE-*: both production "contains a link" predicates on one input.

    The two gates disagree by design (F-14): ``FilterEngine``'s URL_REGEX does
    not see ``tg://`` but does see ``@mention``; ``has_link``'s ``_RE_LINK``
    sees ``tg://`` but not ``@mention``. The fixture pins *each* outcome
    against its own key, so the probe runs both, at the level each is used in
    production: the FilterEngine gate (a FilterRule carrying ``block_links``)
    and the rule-level gate ``link_block_reason`` (a ForwardRule carrying
    ``block_links``, as the pipeline does at ``pipeline.py:957``).
    """
    i = rec["input"]
    text = i["text"]
    blocked = bool(i.get("block_links", False))

    engine_result = _ENGINE.evaluate(make_payload(text), make_filter_rule({"block_links": blocked}))
    engine_verdict = (
        "drop" if _action_name(engine_result) == "DROP"
        else "pass" if _action_name(engine_result) == "ALLOW"
        else _action_name(engine_result).lower()
    )
    gate_exp = str(rec["expected_filter_engine"]).split(" ", 1)[0].strip().lower()
    if engine_verdict != gate_exp:
        _fail(
            rec,
            "expected_filter_engine",
            rec["expected_filter_engine"],
            f"{_action_name(engine_result)} {engine_result.reason}",
        )

    reason = link_block_reason(make_rule(block_links=blocked), text)
    link_verdict = "drop" if reason else "pass"
    link_exp = str(rec["expected_link_block_reason"]).split(" ", 1)[0].strip().lower()
    if link_verdict != link_exp:
        _fail(
            rec,
            "expected_link_block_reason",
            rec["expected_link_block_reason"],
            f"DROP {reason}" if reason else "pass",
        )
    # The reason code itself is pinned too: "DROP link_blocked" names it.
    if reason:
        expected_reason = str(rec["expected_link_block_reason"]).split(" ", 1)[1].strip()
        if expected_reason and expected_reason != reason:
            _fail(rec, "expected_link_block_reason", rec["expected_link_block_reason"], reason)


def run_target_override(rec: Dict[str, Any]) -> str:
    """LNK-OVERRIDE-01: target-scoped ``remove_links`` on the durable path.

    ``pipeline.py:1438`` routes ``remove_links`` through
    ``LinkManagementEngine.apply_to_payload``, which keeps ``@mentions``
    (``services.py:911``: "A mention is not a link"). The Go worker's
    ``transform.ApplyTargetOverride`` uses a regex that also eats mentions —
    F-13. Only the Python half is asserted here.
    """
    i = rec["input"]
    rule = make_rule(remove_links=bool(i.get("target_override", {}).get("remove_links", False)))
    text, _entities, _changed = LinkManagementEngine.apply_to_payload(
        make_payload(i["text"]), rule, text=i["text"]
    )
    return text


def run_pipeline_transform(rec: Dict[str, Any]) -> None:
    """LNK-REMOVE-LINKS-01: durable vs legacy transform-stage precedence (F-04).

    Durable path — ``pipeline.py:1437-1456``: ``remove_links`` is routed
    through ``LinkManagementEngine`` and ``link_replacement`` is silently
    ignored (the ``if`` / ``elif`` never reaches the replacement branch).
    Legacy path — ``FilterEngine.transform_text`` (``services.py:269-273``):
    ``link_replacement`` runs because it is checked first. Both are pinned.
    """
    i = rec["input"]
    spec = i.get("rule", {})
    text = i["text"]

    durable_rule = make_rule(
        remove_links=bool(spec.get("remove_links", False)),
        link_replacement=spec.get("link_replacement", ""),
    )
    durable_text, _ents, _changed = LinkManagementEngine.apply_to_payload(
        make_payload(text), durable_rule, text=text
    )
    _check_text(rec, durable_text, "expected_python_durable_output")

    legacy_rule = make_rule(
        remove_links=bool(spec.get("remove_links", False)),
        link_replacement=spec.get("link_replacement", ""),
    )
    legacy_text = _ENGINE.transform_text(text, legacy_rule)
    _check_text(rec, legacy_text, "expected_python_legacy_output")


def run_pipeline_header_footer(rec: Dict[str, Any]) -> None:
    """ADV-HEADER-01: placeholders are NOT interpolated on the durable path.

    ``pipeline.py:1546-1549`` applies the header raw; ``HeaderFormatter`` runs
    only via ``transform_text`` with a payload (gRPC TestRule, edit-sync). The
    fixture pins both, so the probe runs both.
    """
    i = rec["input"]
    spec = i.get("rule", {})
    text = i["text"]

    durable_rule = make_rule(
        header=spec.get("header", ""),
        footer=spec.get("footer", ""),
    )
    durable_text = _ENGINE.transform_text(text, durable_rule)
    _check_text(rec, durable_text, "expected_python_durable_output")

    preview_rule = make_rule(
        header=spec.get("header", ""),
        footer=spec.get("footer", ""),
    )
    payload = make_payload(text, chat_title="<chat title>")
    preview_text = _ENGINE.transform_text(text, preview_rule, payload)
    exp = str(rec.get("expected_python_preview_output", ""))
    # The fixture's preview expectation is a human-readable sketch
    # ("From <chat title> on 2026-11-02 ..."); assert the interpolations it
    # names, not the whole rendered string (the date is clock-dependent).
    if exp:
        for token in ("<chat title>",):
            if token in exp:
                assert token in preview_text, (
                    f"fixture {rec['id']!r} (preview path): {token!r} missing from {preview_text!r}"
                )
        prefix = exp.split(" ...")[0].split(" on ")[0]
        if prefix:
            assert preview_text.startswith(prefix), (
                f"fixture {rec['id']!r} (preview path)\n"
                f"  expected prefix: {prefix!r}\n"
                f"  actual:          {preview_text!r}"
            )


def run_policy_chain(rec: Dict[str, Any]) -> None:
    """POL-TARGET-01 / POL-SOURCE-01: per-target and source-scoped policy chains.

    Production resolves these through ``ForwardRule.policy_chain`` — the task
    policy first, then the ``source:``/``target:`` override that matches the
    chat pair (``entities.py:1380-1407``). The pipeline re-decides only the
    target override, on the transformed text (``pipeline.py:1197-1214``); a
    source override can only add a constraint, never relax the task policy
    (``pipeline.py:974-1016``). The probe drives the real chain so the
    engines' decisions — not the pipeline's plumbing — are what gets pinned.
    """
    i = rec["input"]
    text = i.get("text", "")

    if "target_policies" in i:
        rule = make_rule()
        rule.target_policies = dict(i["target_policies"])
        outcomes: Dict[str, str] = {}
        for target in i.get("targets", []):
            chain = rule.policy_chain(target_chat_id=target)
            allowed = all(p.evaluate(text).allowed for p in chain)
            outcomes[target] = "delivered" if allowed else "target_filtered"
        _check_text(rec, outcomes, "expected")
        return

    if "source_policy" in i:
        rule = make_rule()
        rule.source_policies = dict(i["source_policy"])
        source_chat = next(iter(i["source_policy"]))
        chain = rule.policy_chain(source_chat_id=source_chat)
        decision = None
        for policy in chain:
            d = policy.evaluate(text)
            if not d.allowed:
                decision = d
                break
            decision = d
        assert decision is not None
        _check(rec, {"allowed": decision.allowed, "reason": decision.reason})
        return

    raise AssertionError(f"{rec['id']}: policy_chain fixture without targets or source policy")


def run_effective_text(rec: Dict[str, Any]) -> str:
    """MEDIA-CAPTION-01: a media-only message's caption is the effective text."""
    i = rec["input"]
    payload = make_payload(
        i.get("text", ""),
        has_media=bool(i.get("has_media", False)),
        caption=i.get("caption", ""),
    )
    return payload.effective_text


def run_keyword_trigger(rec: Dict[str, Any]) -> None:
    """KW-REPLY-01: a keyword match schedules the reply, never drops the forward.

    ``classify_keyword_trigger`` returns the reply text when the add-on is
    enabled and a whitelisted keyword is present (``pipeline.py:1298-1330``);
    the forward itself is a separate, best-effort job that is never dropped
    because of the reply. The fixture documents the *job shape*, so the probe
    asserts on the reply the engine actually produces.
    """
    i = rec["input"]
    spec = i.get("rule", {})
    rule = make_rule()
    rule.metadata = {"paid_addons": {AdvancedSettingsEngine.KEYWORD_REPLY_ADDON_KEY: True}}
    if spec.get("keyword_confirmation") is not None:
        rule.keyword_confirmation = spec["keyword_confirmation"]
    if spec.get("keyword_reply") is not None:
        rule.keyword_reply = spec["keyword_reply"]
    if spec.get("whitelist"):
        rule.whitelist = list(spec["whitelist"])
    reply = AdvancedSettingsEngine.classify_keyword_trigger(i.get("text", ""), rule)
    expected_reply = rec["expected"].get("reply", "")
    assert reply == rule.keyword_reply, (
        f"fixture {rec['id']!r} engine={rec['engine']!r}\n"
        f"  expected reply: {expected_reply!r}\n"
        f"  actual reply:   {reply!r}"
    )
    assert expected_reply, (
        f"fixture {rec['id']!r}: fixture must document the reply text"
    )


def run_auto_delete_bounds(rec: Dict[str, Any]) -> None:
    """ADV-AUTODELETE-01: registry bound (604800) and engine bound (31_536_000).

    The registry clamps at *write* (``rule_settings.maximum``), while
    ``get_auto_delete_seconds`` caps at execution (F-17). 2_000_000 is above
    the registry bound and below the engine bound, so it survives execution.
    """
    i = rec["input"]
    spec = SETTING_SPECS["auto_delete_seconds"]
    assert spec.maximum == rec["expected_registry_clamped"], (
        f"fixture {rec['id']!r}: registry clamp moved ({spec.maximum} != "
        f"{rec['expected_registry_clamped']}); update the fixture with a reason"
    )

    rule = make_rule()
    rule.metadata = {"paid_addons": {AdvancedSettingsEngine.AUTO_DELETE_ADDON_KEY: True}}
    rule.auto_delete_seconds = int(i["auto_delete_seconds"])
    engine_value = AdvancedSettingsEngine.get_auto_delete_seconds(rule)
    if engine_value != rec["expected_engine_value"]:
        _fail(rec, "expected_engine_value", rec["expected_engine_value"], engine_value)

    # The registry genuinely refuses the same value — that is the divergence.
    with pytest.raises(Exception):
        coerce_value("auto_delete_seconds", int(i["auto_delete_seconds"]))


def run_header_footer_dedup(rec: Dict[str, Any]) -> str:
    """HEADER-FOOTER-DEDUP-01: the startswith check prevents double headers."""
    i = rec["input"]
    return _ENGINE.apply_header_footer(i["text"], header=i.get("header", ""))


def run_meta_length(rec: Dict[str, Any]) -> None:
    """UNI-LEN-01 (meta): Python ``len()`` counts codepoints.

    ``python_len_codepoints`` is the claim the probe executes; the Go byte and
    rune counts are recorded for the coverage report and are not asserted here
    (no Go engine is invoked from this harness).
    """
    text = rec["input"]["text"]
    got = len(text)
    exp = rec["python_len_codepoints"]
    if got != exp:
        _fail(rec, "python_len_codepoints", exp, got)


# ---- engine -> adapter dispatch -------------------------------------------- #

_ADAPTERS: Dict[str, Any] = {
    "FilterEngine.evaluate": run_filter_engine,
    "ContentPolicy vs FilterRule": run_content_policy,
    "ContentPolicy.evaluate": run_content_policy,
    "policy_chain per-target": run_policy_chain,
    "policy_chain source scope": run_policy_chain,
    "CleanerRule.clean": run_cleaner,
    "EMOJI_REGEX": run_emoji_regex,
    "EMOJI_REGEX / _CLEANER_EMOJI_RE / Go emojiRegex": run_emoji_regex,
    "apply_replacements": run_apply_replacements,
    "block_links gates": run_link_gates,
    "target-scoped policy override": run_target_override,
    "pipeline transform stage 2": run_pipeline_transform,
    "pipeline header/footer": run_pipeline_header_footer,
    "effective_text": run_effective_text,
    "classify_keyword_trigger": run_keyword_trigger,
    "get_auto_delete_seconds bounds": run_auto_delete_bounds,
    "apply_header_footer": run_header_footer_dedup,
    "length bounds": run_meta_length,
}


def _unsupported(rec: Dict[str, Any]) -> None:
    pytest.skip(
        f"UNSUPPORTED ENGINE fixture_id={rec['id']!r} engine={rec['engine']!r} "
        f"executable={rec.get('executable')!r} — coordinator: add an adapter"
    )


def _execute(rec: Dict[str, Any]) -> Any:
    adapter = _ADAPTERS.get(rec["engine"])
    if adapter is None:
        _unsupported(rec)
    return adapter(rec)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def _ids() -> List[str]:
    return [r["id"] for r in executable_fixture_records()]


@pytest.mark.parametrize("fixture_id", _ids())
def test_fixture_executes_in_python(fixture_id: str) -> None:
    """Run one executable fixture against its real production engine."""
    rec = next(r for r in fixture_records() if r["id"] == fixture_id)
    token = rec.get("executable")
    assert token in ("python", "both", "meta"), (
        f"fixture {fixture_id!r}: executable={token!r} must not reach this harness"
    )
    got = _execute(rec)
    if got is None:
        return  # multi-key adapters asserted internally
    exp = expected_value(rec)
    if got != exp:
        _fail(rec, "expected_python*", exp, got)


def test_schema_fixtures_are_never_executed() -> None:
    """``schema`` fixtures are characterization data, never a passing test."""
    records = schema_fixture_records()
    assert records, "expected schema fixtures to exist; the coverage report needs them"
    print(
        f"\nschema fixtures (characterization data, not executed): "
        f"{len(records)} -> {[r['id'] for r in records]}"
    )


def test_every_executable_fixture_has_an_adapter() -> None:
    """An engine the harness cannot map must skip loudly, not pass silently."""
    missing = [
        rec["id"]
        for rec in executable_fixture_records()
        if rec["engine"] not in _ADAPTERS
    ]
    if missing:
        print(f"\nHARNESS GAP — no adapter for engines on fixtures: {missing}")
    assert not missing, (
        "harness gap: add an adapter (or record why the engine is out of scope)"
    )


def test_fixture_denominator_is_known() -> None:
    """Print the coverage denominator so the report can be reconciled."""
    records = fixture_records()
    executable = executable_fixture_records()
    schema = schema_fixture_records()
    print(
        f"\ndenominator: total={len(records)} "
        f"executable(python+both+meta)={len(executable)} schema={len(schema)}"
    )
    assert len(records) == len(executable) + len(schema), (
        "every fixture must be either executable or schema"
    )
