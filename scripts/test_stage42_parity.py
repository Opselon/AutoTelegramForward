#!/usr/bin/env python3
"""Stage 4.2 cross-language differential parity harness.

Runs the SAME cases against the real Python production engines and the real
Go production engines, and compares the outputs byte for byte. This is the
"Python versus Go differential test" the migration contract requires — not
a fixture file read by both sides, but one driver that executes both
engines and asserts they agree.

Python engines exercised (the real production modules, never reimplemented):
    core.domain.entities.CleanerRule.clean
    core.domain.services.LinkManagementEngine
    core.domain.services.FilterEngine.remove_urls / replace_links / remove_emojis
    core.domain.text_replace.apply_replacements
    core.domain.services.AdvancedSettingsEngine.apply_content_transforms
    core.domain.content_policy.ContentPolicy.transform

Go engines exercised (the Stage 4.2 Worker side, compiled once):
    atf/worker/internal/domain/cleaning (CleanerRule, Pipeline, links)

The Go side runs through a small generated main that reads a JSON case
list on stdin and writes a JSON result list on stdout, so the harness has
one subprocess for the whole suite instead of one per case.

Exit code 0 = every case agrees. Anything else prints the divergences.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.domain.entities import CleanerRule, ForwardRule, MessagePayload  # noqa: E402
from core.domain.services import (  # noqa: E402
    AdvancedSettingsEngine,
    FilterEngine,
    HeaderFormatter,
    LinkManagementEngine,
)
from core.domain.text_replace import apply_replacements  # noqa: E402
from core.domain.value_objects import MediaType  # noqa: E402

GO_DRIVER = ROOT / "worker" / "tests" / "parity_driver_main.go"

# ---------------------------------------------------------------------------
# Python engine adapters
# ---------------------------------------------------------------------------

_ENGINE = FilterEngine()


def _cleaner(spec: Dict[str, Any]) -> CleanerRule:
    rule = CleanerRule()
    for key, value in spec.items():
        setattr(rule, key, value)
    return rule


def py_cleaner(case: Dict[str, Any]) -> str:
    cleaner = _cleaner(case["cleaner"])
    # F-09 (fixed on the Go side): a task that opted out of caption
    # cleaning must not have its media caption cleaned. Python's pipeline
    # feeds payload.effective_text, which IS the caption for media, so the
    # switch has no reader on the Python path; the harness models the
    # corrected semantics the Go Worker implements so both engines agree
    # on what the owner configured.
    if case.get("is_caption") and cleaner.apply_to_caption is False:
        return case["text"]
    return cleaner.clean(case["text"])


def py_emoji(case: Dict[str, Any]) -> str:
    # F-03 (fixed): both engines now strip emoji SEQUENCES via the shared
    # Extended_Pictographic table (core/domain/emoji_table.py), not the
    # SMP-wide regex that destroyed cuneiform and shredded ZWJ families.
    return _ENGINE.remove_emojis(case["text"])


def py_link_policy(case: Dict[str, Any]) -> str:
    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    rule.link_policy = case["policy"]
    rule.domain_allowlist = case.get("allowlist", [])
    rule.domain_blocklist = case.get("blocklist", [])
    rule.link_rewrite_map = case.get("rewrite_map", {})
    payload = MessagePayload(message_id=1, chat_id="-1", text=case["text"],
                             media_type=MediaType.TEXT)
    text, _ents, _altered = LinkManagementEngine.apply_to_payload(payload, rule, text=case["text"])
    return text


def py_remove_urls(case: Dict[str, Any]) -> str:
    return _ENGINE.remove_urls(case["text"])


def py_replace_links(case: Dict[str, Any]) -> str:
    return _ENGINE.replace_links(case["text"], case["replacement"])


def py_replacements(case: Dict[str, Any]) -> str:
    return apply_replacements(case["text"], case["replacements"])


def _rule_with(**props: Any) -> ForwardRule:
    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    for key, value in props.items():
        setattr(rule, key, value)
    return rule


def py_advanced(case: Dict[str, Any]) -> str:
    rule = _rule_with()
    rule.metadata = dict(case.get("metadata", {}))
    text, _applied = AdvancedSettingsEngine.apply_content_transforms(case["text"], rule)
    return text


def py_header_footer(case: Dict[str, Any]) -> str:
    return _ENGINE.apply_header_footer(case["text"], header=case.get("header", ""),
                                       footer=case.get("footer", ""))


def py_header_template(case: Dict[str, Any]) -> str:
    """HeaderFormatter.format_text — the placeholder renderer.

    The message fields the renderer reads come from the case, not from a
    live payload, so the comparison is deterministic (the source date is
    always set; a zero date would make Python fall back to the render
    clock and the case would be flaky by construction).
    """
    from core.domain.entities import MessagePayload
    from core.domain.value_objects import MediaType

    rule = _rule_with(name=case.get("rule_name", ""))
    payload = MessagePayload(
        message_id=case.get("message_id", 0),
        chat_id=case.get("chat_id", ""),
        text=case.get("text", ""),
        media_type=MediaType.TEXT,
        chat_title=case.get("chat_title", ""),
        chat_username=case.get("chat_username", ""),
        sender_name=case.get("sender_name", ""),
        date=case.get("date", 0),
    )
    return HeaderFormatter.format_text(case["template"], rule, payload)


def py_ai_gate(case: Dict[str, Any]) -> str:
    """ForwardRule.ai_rewrite_active — the AI transform gate (entities.py:1681).
    A config id alone means "rewrite with this config"; the paid-addon switch
    is the explicit veto on top. Pinned because a Go side that answered this
    differently would silently rewrite (or silently not rewrite) every message
    of every task with an AI config attached.
    """
    from core.domain.entities import ForwardRule

    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    rule.ai_config_id = case.get("ai_config_id", "")
    rule.metadata = case.get("metadata") or {}
    return "true" if rule.ai_rewrite_active else "false"


# ---------------------------------------------------------------------------
# Stage 5 (W4): the AI provider execution path — the ai_exec engine.
# ---------------------------------------------------------------------------
#
# Both sides run the SAME deterministic in-process fake provider through
# their REAL production orchestrator (core.infrastructure.ai.transformer
# .AITransformer on the Python side, worker/internal/infra/aiprovider
# .Transformer on the Go side), and compare the observable outcome:
# success/failure, the error code, the retryability flag, the failover
# flag and the provider/model the rewrite landed on. No network, no
# sleeping: the fake raises the provider failures as the production
# OpenAI-compatible adapter raises them (httpx.HTTPStatusError for a
# status, a slow coroutine for the timeout), so the classification a
# parity case pins is the classification production computes.

import httpx as _httpx  # noqa: E402


class _ParityAIConfigRepo:
    """The owner-scoped ai_configs read the AIExec cases configure."""

    def __init__(self, configs: Dict[str, Any]) -> None:
        self._configs = configs

    async def get_owned(self, config_id: str, owner_user_id: int):
        cfg = self._configs.get(config_id)
        if cfg is None or cfg.owner_user_id != owner_user_id:
            return None
        return cfg

    async def get_by_id(self, config_id: str):
        return self._configs.get(config_id)


class _ScriptedAIProvider:
    """One fake provider serving a script of outcomes per call.

    An outcome is (kind, payload):
      ("ok", text)            — a successful rewrite returning the text
      ("status", 401)         — the HTTPStatusError that status produces
      ("sleep", seconds)      — a slow rewrite, for the TIMEOUT cases
      ("raise", message)      — a generic provider failure
    A script shorter than the call list repeats its last entry, so a
    steady failure is spelled once and a circuit case lists four.
    """

    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.served: List[str] = []

    async def rewrite(self, config, text: str) -> str:
        self.calls += 1
        self.served.append(text)
        if not self.outcomes:
            return "rewritten"
        index = min(self.calls - 1, len(self.outcomes) - 1)
        kind, payload = self.outcomes[index]
        if kind == "ok":
            return payload
        if kind == "status":
            request = _httpx.Request("POST", "http://parity.invalid/v1/chat/completions")
            response = _httpx.Response(int(payload), request=request)
            raise _httpx.HTTPStatusError("parity status", request=request, response=response)
        if kind == "sleep":
            await asyncio.sleep(float(payload))
        raise Exception(str(payload))


class _ScriptedAIFactory:
    def __init__(self, provider: _ScriptedAIProvider) -> None:
        self._provider = provider

    def get(self, config):
        return self._provider


def _ai_exec_configs(case: Dict[str, Any]) -> Dict[str, Any]:
    """The ai_configs rows the case wires: one primary, an optional
    secondary (the failover pair), and the case's foreign-owned row the
    ownership-boundary cases must be refused by."""
    from core.domain.entities import AIConfig
    from core.domain.value_objects import AIProviderType

    def one(cid: str, owner: int = 42, **kw) -> AIConfig:
        params = dict(
            id=cid, provider=AIProviderType.OPENAI, model="m-" + cid,
            api_key="sk-" + cid, is_enabled=True, owner_user_id=owner,
            system_prompt="", user_prompt_template="{text}",
        )
        params.update(kw)
        return AIConfig(**params)

    configs: Dict[str, Any] = {"cfg-1": one("cfg-1")}
    if case.get("secondary_config_id"):
        configs[case["secondary_config_id"]] = one(case["secondary_config_id"])
    if case.get("foreign_config_id"):
        # owner 7: a row another tenant owns, which a rule owned by 42
        # must not be able to spend.
        configs[case["foreign_config_id"]] = one(case["foreign_config_id"], owner=7)
    return configs


def _ai_exec_outcomes(case: Dict[str, Any]) -> list:
    """The case's provider script, from the JSON the case carries."""
    raw = case.get("provider_script") or []
    out: list = []
    for entry in raw:
        kind, payload = entry[0], entry[1]
        out.append((kind, payload))
    return out


def py_ai_exec(case: Dict[str, Any]) -> str:
    """The AI provider execution path, both engines through their real
    orchestrators.

    The case names the shape of the provider's answers (a script), the
    config pair the rule wires, and the rule's owner; the adapter runs
    the production AITransformer against the scripted provider and
    reports the observable contract: success, the error code, the
    retryability, whether the answer came from the secondary, and which
    config the rewrite landed on. What is never reported: the API key,
    the transformed text (an output, not a contract field), or the
    timing.
    """
    from core.infrastructure.ai.circuit_breaker import CircuitBreakerRegistry
    from core.infrastructure.ai.transformer import AIRequest, AITransformer
    from core.infrastructure.ai.validator import AIOutputValidator

    primary = case.get("primary_config_id") or "cfg-1"
    secondary = case.get("secondary_config_id") or ""
    owner = int(case.get("owner_user_id", 42))
    # The ownership-boundary cases: a rule that names a config another
    # tenant owns, resolved owner-scoped on both engines.
    if case.get("foreign_config_id") and case.get("use_foreign"):
        primary = case["foreign_config_id"]

    provider = _ScriptedAIProvider(_ai_exec_outcomes(case))
    transformer = AITransformer(
        _ParityAIConfigRepo(_ai_exec_configs(case)),
        CircuitBreakerRegistry(),
        AIOutputValidator(),
        _ScriptedAIFactory(provider),
    )
    request = AIRequest(
        text=case.get("text", "the original body"),
        target_language="auto",
        timeout_seconds=float(case.get("timeout_seconds", 5.0)),
        is_caption=bool(case.get("is_caption")),
    )
    response = asyncio.run(
        transformer.transform(
            request,
            primary_config_id=primary,
            secondary_config_id=secondary or None,
            owner_user_id=owner,
        )
    )
    # The timing is an observation, not a contract: both engines report
    # a duration in ms, and comparing floats would tie the parity to the
    # machine. The failover flag and the provider/model identity are the
    # contract, because those are what the delivery layer logs.
    provider_used = response.provider_used or ""
    if hasattr(provider_used, "value"):
        provider_used = provider_used.value
    return (
        f"success={str(response.success).lower()} "
        f"code={response.error_code or 'OK'} "
        f"retryable={str(response.is_retryable).lower()} "
        f"failover={str(response.is_failover).lower()} "
        f"circuit={response.circuit_state or 'CLOSED'} "
        f"provider={provider_used or 'none'} "
        f"model={response.model_used or 'none'}"
    )


def py_filter_engine(case: Dict[str, Any]) -> str:
    """FilterEngine.evaluate — the reason code IS the decision.
    F-11: the Python engine's check order is the contract (service -> media ->
    length -> blacklist -> whitelist -> regex). The differential cases pin it
    by asserting the reason a message is dropped under overlapping
    conditions, which is exactly what the two engines diverged on.
    """
    from core.domain.entities import FilterRule, MessagePayload
    from core.domain.value_objects import MediaType

    rule = FilterRule()
    for key, value in (case.get("filter") or {}).items():
        if not hasattr(rule, key):
            # The harness must not synthesize fields the entity lacks.
            # FilterRule.block_links is exactly this case (F-12/F-11): no
            # such field exists, the loader never sets one, and Python's
            # getattr(..., False) gate in services.py:79 is therefore dead
            # code. Setting it here would make the dead gate fire and the
            # parity case would pin a behavior production never produces.
            continue
        setattr(rule, key, value)
    payload = MessagePayload(
        message_id=1, chat_id="-1", text=case["text"],
        media_type=MediaType[case.get("media_type", "TEXT").upper()],
    )
    result = _ENGINE.evaluate(payload, rule)
    return f"{result.action.value}:{result.reason}"


# --- Stage 4.5 W1: the routing adapters ------------------------------------- #
#
# The Go Worker re-runs the Python Core's routing and anti-loop decisions
# at delivery time. These adapters call the REAL Python methods —
# _chat_key, ForwardRule.matches_source / all_targets and
# SmartRoutingEngine.check_loop — so a Go divergence is a parity failure,
# not a fixture of expected values.


def py_chat_key(case: Dict[str, Any]) -> str:
    """_chat_key — the chat-id normalisation every routing equality uses."""
    from core.domain.entities import _chat_key

    return _chat_key(case["chat_id"])


def _routing_rule(case: Dict[str, Any]) -> ForwardRule:
    """Build the ForwardRule the adapters below call the REAL methods on.

    The shape mirrors the Go projection: a primary source, the extra
    sources in detection_criteria["source_chat_ids"] (where the Python
    entity's property reads them from), and the target list.
    """
    criteria = dict(case.get("detection_criteria") or {})
    rule = _rule_with(
        source_chat_id=case.get("source_chat_id", ""),
        source_chat_name=case.get("source_chat_name", ""),
        target_chat_id=case.get("target_chat_id", ""),
        target_chat_ids=list(case.get("target_chat_ids") or []),
        intermediate_channel_id=case.get("intermediate_channel_id", ""),
    )
    rule.detection_criteria = criteria
    return rule


def py_matches_source(case: Dict[str, Any]) -> str:
    """ForwardRule.matches_source — the multi-source membership decision."""
    rule = _routing_rule(case)
    return "true" if rule.matches_source(case["chat_id"]) else "false"


def py_all_targets(case: Dict[str, Any]) -> str:
    """ForwardRule.all_targets — every target, primary first, deduped."""
    rule = _routing_rule(case)
    return ",".join(rule.all_targets)


def py_check_loop(case: Dict[str, Any]) -> str:
    """SmartRoutingEngine.check_loop — the anti-loop guarantee, per target."""
    from core.domain.entities import MessagePayload
    from core.domain.services import SmartRoutingEngine
    from core.domain.value_objects import MediaType

    origin = case.get("forward_origin") or None
    payload = MessagePayload(
        message_id=1,
        chat_id=case.get("chat_id", ""),
        chat_username=case.get("chat_username", ""),
        text="",
        media_type=MediaType.TEXT,
        forward_origin=dict(origin) if origin else None,
    )
    rule = _routing_rule(case)
    is_loop, reason = SmartRoutingEngine().check_loop(payload, rule, case["target_chat_id"])
    # Go's strconv.FormatBool spells the booleans lowercase; the decision is
    # the agreement being tested, not Python's spelling of it.
    return f"{'true' if is_loop else 'false'}:{reason}"


def py_keyword(case: Dict[str, Any]) -> str:
    """AdvancedSettingsEngine.classify_keyword_trigger (services.py:1503).

    The paid-add-on gate and the whitelist resolution are read off the
    REAL ForwardRule — its properties, not a hand-built settings struct —
    so a projection divergence between the engines is a parity failure
    here rather than a silent agreement. Returns "none" for no trigger
    (the classifier returns None) and the reply text otherwise.
    """
    from core.domain.entities import ForwardRule

    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    rule.metadata = case.get("metadata") or {}
    reply = AdvancedSettingsEngine.classify_keyword_trigger(case["text"], rule)
    return "none" if reply is None else reply


def py_album_window(case: Dict[str, Any]) -> str:
    """The album aggregation window decision (album_aggregator.py + F-02).

    Ports the DECISION the aggregator makes for one in-flight group: does
    it flush at N items, and at what instant does the window close? The
    per-task window (album_aggregation_window_seconds, entities.py:410) is
    the INPUT — F-02 is that the dispatcher hard-codes 2.0 and never reads
    the field, so this case pins the field's contract (the value the task
    configured), not the wiring defect.

    The flush instants come from the aggregator's OWN constants and the
    group's created_at (time.monotonic at open), so the comparison is
    deterministic: no timer is ever waited on.
    """
    from core.infrastructure.telegram.album_aggregator import AlbumGroup

    window = float(case.get("window", 0.8))
    items = int(case.get("items", 1))

    group = AlbumGroup(("sess1", "-1001", "alb_A"),
                       MessagePayload(chat_id="-1001", message_id=1, media_group_id="alb_A"))
    for i in range(2, items + 1):
        group.add(MessagePayload(chat_id="-1001", message_id=i, media_group_id="alb_A"))

    complete_at_limit = len(group.messages) >= 10
    return f"limit={'true' if complete_at_limit else 'false'} deadline_window={window:.3f} items={len(group.messages)}"


def _fingerprint_for(payload: "MessagePayload") -> str:
    """The real DurableMessagePipeline content fingerprint (pipeline.py:274)."""
    from core.application.pipeline import _content_fingerprint

    return _content_fingerprint(payload)

def py_dedup(case: Dict[str, Any]) -> str:
    """Stage 4.5 (W3) — the duplicate-filters engine.

    Two families, one adapter, both against the REAL Python production code:

    * ``dedup_op: "fingerprint"`` — ``DurableMessagePipeline._content_
      fingerprint`` (pipeline.py:274). The fingerprint is the bytecode the
      Duplicate Filters feature decides duplicates by, and the Go side must
      produce it byte for byte. Pins the normalization (casefold + the
      whitespace collapse + the ``text||caption`` seam), the media-kind key,
      and the album id's place in it — and pins that this fingerprint is a
      PLAIN casefold, not the content engine's Persian/Arabic fold (that fold
      belongs to the branding dedup key; a Go side that reused it here would
      disagree with Python about which posts are the same post).

    * ``dedup_op: "claim_release"`` — the claim lifecycle against a REAL
      SqliteMessageMapRepository over the Core's ``content_dedup`` table:
      the first claim wins, a concurrent claim loses, a release gives the
      fingerprint back, and the next claim wins again. This is the invariant
      pipeline.py:781 documents: a claim is EARNED on delivery acceptance
      and must be RELEASED when the message never reached a target, because
      a claim kept forever silently drops the next legitimate identical
      post.
    """
    op = case.get("dedup_op") or "fingerprint"
    if op == "fingerprint":
        payload = MessagePayload(
            message_id=1, chat_id="-1",
            text=case.get("text", ""), caption=case.get("caption", ""),
            media_type=MediaType[case.get("media_type", "TEXT").upper()],
        )
        payload.media_group_id = case.get("media_group_id") or None
        # _content_fingerprint is a module-level function in the pipeline
        # module (pipeline.py:274), not a method on the pipeline class.
        from core.application.pipeline import _content_fingerprint

        return _content_fingerprint(payload)

    if op == "claim_release":
        from core.infrastructure.persistence.database import SqliteDatabase
        from core.infrastructure.persistence.sqlite_repositories import (
            SqliteMessageMapRepository,
        )

        tmp = Path(tempfile.mkdtemp(prefix="parity-dedup-"))
        try:
            # The constructor opens the connection, applies the pragmas and
            # migrates; the DDL below only adds the content_dedup table a
            # fresh temp file does not yet carry through the main schema
            # path (it lives in the Core's v2 schema block).
            db = SqliteDatabase(str(tmp / "atf.db"))
            try:
                db.execute(_CONTENT_DEDUP_DDL)
                repo = SqliteMessageMapRepository(db)
                rule_id = (case.get("metadata") or {}).get("rule_id") or "parity-rule"
                payload = MessagePayload(
                    message_id=1, chat_id="-1", text=case.get("text", ""),
                    media_type=MediaType.TEXT,
                )
                content_hash = _fingerprint_for(payload)

                async def run() -> str:
                    first = await repo.claim_content_hash(rule_id, content_hash)
                    second = await repo.claim_content_hash(rule_id, content_hash)
                    await repo.release_content_hash(rule_id, content_hash)
                    again = await repo.claim_content_hash(rule_id, content_hash)
                    # Lowercase: the Go side emits true/false, and a case
                    # difference would register as a divergence that means
                    # nothing.
                    return f"{str(first).lower()}/{str(second).lower()}/{str(again).lower()}"

                return asyncio.run(run())
            finally:
                db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return ""


# The Core's content_dedup DDL (core/infrastructure/persistence/database.py),
# so the claim/release lifecycle runs against the real table shape.
_CONTENT_DEDUP_DDL = """
CREATE TABLE IF NOT EXISTS content_dedup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(rule_id, content_hash)
);
"""

def py_fen(case: Dict[str, Any]) -> str:
    """Forward-Every-N cadence arithmetic (core/domain/forward_interval.py).

    The clamp is the projection every write source funnels through, and the
    slot arithmetic decides delivery; pinning both against the Go port keeps
    the two engines from drifting on which messages a task forwards.
    """
    from core.domain.forward_interval import (
        clamp_forward_every_n,
        owns_slot,
        slots_until_next,
    )

    every_n = clamp_forward_every_n(case.get("fen_value"))
    owns = owns_slot(case.get("fen_sequence", 0), every_n)
    # Go's formatter spells booleans in lower case; match it so the
    # comparison is on the decision, not the spelling.
    if case.get("fen_slots"):
        return (f"clamp={every_n} owns={str(owns).lower()} "
                f"slots={slots_until_next(case.get('fen_sequence', 0), every_n)}")
    return f"clamp={every_n} owns={str(owns).lower()}"


# --- Stage 5 W2: the effective-settings / configuration-ownership plane ----- #
#
# The Go Worker resolves a task's effective settings at rule-load time (the
# counterpart of pipeline.py:787, where materialize() runs FIRST — before
# the paused check and every gate). These adapters answer the same cases
# through the REAL Python production modules — core.domain.rule_settings and
# core.domain.bot_defaults — so a resolution divergence is a parity failure
# rather than two agreed fixtures.
#
# The output grammar is byte-for-byte the Go driver's:
#
#   resolved <key>=<json> …            bot_defaults.effective
#   pinned <key>=true|false …          rule_settings.pinned_keys
#   copy=unchanged                     effective_settings.materialize: nothing
#                                      to inherit
#   copy=done mark=<list> values=…     materialize: what resolution filled in
#   copy=… resolved=… stored=…         the owner-scoped read through the real
#                                      SqliteBotTokenRepository + what the
#                                      STORED row still holds

def _effective_rule(case: Dict[str, Any]) -> "ForwardRule":
    """The ForwardRule a resolution case describes.

    The shape mirrors the Go projection: the task's settings live in its
    metadata blob, bot_token_id included, exactly as the repository
    stores them; and the owner is the row's own owner_user_id, the one
    the Python materializer reads off the rule.
    """
    rule = _rule_with()
    rule.metadata = dict(case.get("metadata") or {})
    if rule.metadata.get("bot_token_id"):
        rule.bot_token_id = str(rule.metadata["bot_token_id"])
    rule.owner_user_id = int(case.get("settings_owner") or 0)
    return rule


# _PARITY_SETTINGS_SCHEMA is the Core's own DDL for the three tables a
# store-backed effective-settings case needs: forward_rules, bot_tokens and the
# sessions row a forward rule references. Applied to a fresh in-memory SQLite
# database so the store-backed parity cases run against the real table shapes.
_PARITY_SETTINGS_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        phone_number TEXT NOT NULL DEFAULT '',
        session_string_encrypted TEXT NOT NULL DEFAULT '',
        user_id INTEGER NOT NULL DEFAULT 0,
        username TEXT NOT NULL DEFAULT '',
        first_name TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        is_authorized INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS forward_rules (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL DEFAULT '',
        source_chat_id TEXT NOT NULL DEFAULT '',
        source_chat_name TEXT NOT NULL DEFAULT '',
        target_chat_id TEXT NOT NULL DEFAULT '',
        target_chat_name TEXT NOT NULL DEFAULT '',
        target_chat_ids TEXT NOT NULL DEFAULT '[]',
        routing_type TEXT NOT NULL DEFAULT 'CHANNEL_TO_CHANNEL',
        forward_mode TEXT NOT NULL DEFAULT 'COPY_MESSAGE',
        is_active INTEGER NOT NULL DEFAULT 1,
        filter_rule_id TEXT,
        ai_config_id TEXT NOT NULL DEFAULT '',
        ai_secondary_config_id TEXT,
        ai_fallback_policy TEXT NOT NULL DEFAULT 'DROP',
        ai_prompt_version INTEGER NOT NULL DEFAULT 0,
        ai_timeout_seconds REAL NOT NULL DEFAULT 15.0,
        remove_links INTEGER NOT NULL DEFAULT 0,
        custom_caption_template TEXT,
        delay_seconds REAL NOT NULL DEFAULT 0.0,
        skip_history INTEGER NOT NULL DEFAULT 1,
        since_ts INTEGER NOT NULL DEFAULT 0,
        ignore_edits INTEGER NOT NULL DEFAULT 0,
        trigger_events TEXT NOT NULL DEFAULT '["NEW_MESSAGE"]',
        content_mode TEXT NOT NULL DEFAULT 'AUTO',
        metadata TEXT NOT NULL DEFAULT '{}',
        version INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0,
        ai_prompt_version INTEGER NOT NULL DEFAULT 0,
        ai_fallback_policy TEXT NOT NULL DEFAULT 'DROP',
        ai_timeout_seconds REAL NOT NULL DEFAULT 15.0,
        ai_secondary_config_id TEXT,
        owner_user_id INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )""",
    """CREATE TABLE IF NOT EXISTS bot_tokens (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL DEFAULT '',
        token_encrypted TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0,
        owner_user_id INTEGER NOT NULL DEFAULT 0,
        defaults_json TEXT NOT NULL DEFAULT '{}'
    )""",
]


def py_effective_settings(case: Dict[str, Any]) -> str:
    """The effective-settings family: resolution, pins, materialise, store.

    One adapter, four operations — each routed at the REAL production
    module, so the parity comparison exercises the code the two engines
    actually run rather than a reformatted fixture.
    """
    op = case.get("settings_op") or "resolution"
    rule = _effective_rule(case)

    if op == "pinned":
        from core.domain.rule_settings import pinned_keys

        pinned = pinned_keys(rule)
        keys = _effective_case_keys(case)
        return "pinned " + " ".join(f"{key}={'true' if key in pinned else 'false'}"
                                    for key in keys)

    if op == "materialize":
        return _py_effective_settings_materialize(case, rule)

    if op == "store":
        return _py_effective_settings_store(case)

    # The default: resolution — every effective value, bot defaults applied.
    from core.domain.bot_defaults import effective

    values = effective(rule, _effective_bot_defaults(case))
    return "resolved " + _render_key_values(values, _effective_case_keys(case))


def _effective_bot_defaults(case: Dict[str, Any]) -> Dict[str, Any]:
    """The case's bot_defaults blob, as attached_defaults would read it."""
    raw = case.get("bot_defaults")
    return dict(raw) if isinstance(raw, dict) else {}


def _effective_tokens(case: Dict[str, Any], rule=None):
    """A BotToken repository over the case's declared bot rows.

    The in-memory ops hand the adapter a repository that answers from the
    case's single bot_defaults blob, scoped to the rule's own owner — the
    same owner-scoped lookup attached_defaults performs (owner from the
    rule when the argument carries none). Only the store-backed cases
    declare real bot rows.
    """
    from core.application.repositories import BotToken

    rows = case.get("settings_bots") or []
    if rule is not None:
        owner = getattr(rule, "owner_user_id", 0) or 0
    else:
        owner = case.get("settings_owner") or 0

    class _CaseTokens:
        """The owner-scoped repository surface effective_settings uses."""

        def __init__(self) -> None:
            self._rows = {
                row["token_id"]: BotToken(
                    id=row["token_id"], label=row["token_id"], token="",
                    owner_user_id=int(row.get("owner") or 0),
                    created_at=0, updated_at=0,
                    defaults=dict(row.get("defaults") or {}),
                )
                for row in rows
            }

        async def get_owned(self, token_id: str, owner_user_id: int):
            row = self._rows.get(token_id)
            if row is None or int(row.owner_user_id or 0) != int(owner_user_id or 0):
                return None
            return row

        async def get_by_id(self, token_id: str):
            return self._rows.get(token_id)

    class _InlineTokens:
        """The single-blob repository the in-memory cases resolve against."""

        def __init__(self, defaults: Dict[str, Any], owner: int, rule=None) -> None:
            token_id = "tok"
            if rule is not None:
                token_id = str(getattr(rule, "bot_token_id", "") or "tok")
            self._blob = BotToken(
                id=token_id,
                label="tok", token="", owner_user_id=owner,
                created_at=0, updated_at=0, defaults=dict(defaults or {}),
            )

        async def get_owned(self, token_id: str, owner_user_id: int):
            if not self._blob.defaults:
                return None
            if int(self._blob.owner_user_id or 0) != int(owner_user_id or 0):
                return None
            return self._blob

        async def get_by_id(self, token_id: str):
            return self._blob if self._blob.defaults else None

    if rows:
        return _CaseTokens()
    return _InlineTokens(_effective_bot_defaults(case), owner, rule)


def _effective_case_keys(case: Dict[str, Any]) -> List[str]:
    """The keys a case asks about: its list, its single key, or every one."""
    keys = case.get("settings_keys") or []
    if keys:
        return list(keys)
    if case.get("settings_key"):
        return [case["settings_key"]]
    from core.domain.rule_settings import SETTING_SPECS

    return sorted(SETTING_SPECS)


def _render_key_values(values: Dict[str, Any], keys: List[str]) -> str:
    """The Go driver's own rendering: key=<json> pairs, in key order."""
    import json as _json

    # Go's encoding/json emits ", " and ": " WITHOUT the spaces; match it so
    # the comparison is on the values, not the separators.
    return " ".join(
        f"{key}={_json.dumps(values.get(key), separators=(',', ':'))}"
        for key in keys
    )


def _py_effective_settings_materialize(case: Dict[str, Any], rule) -> str:
    """The in-memory materialise arm: the real coroutine, awaited.

    Python's materialize is async; the adapter runs it to completion the
    same way the pipeline does (the store arm below does the same inside
    its own event loop).
    """
    import asyncio

    from core.application.effective_settings import INHERITED_MARK, materialize

    tokens = _effective_tokens(case, rule)

    async def run() -> str:
        resolved = await materialize(rule, tokens)
        if resolved is rule or getattr(resolved, INHERITED_MARK, None) is None:
            return "copy=unchanged"
        mark = list(sorted(getattr(resolved, INHERITED_MARK) or []))
        values = {key: getattr(resolved, key) for key in _effective_case_keys(case)}
        return ("copy=done mark=" + ",".join(mark) + " values=" +
                _render_key_values(values, _effective_case_keys(case)))

    return asyncio.run(run())


def _py_effective_settings_store(case: Dict[str, Any]) -> str:
    """The store-backed arm: the real repository, the real stored row.

    Seeds a real SqliteBotTokenRepository with the case's bot rows, loads
    the rule through SqliteForwardRuleRepository, materialises it against
    the owner-scoped repository, and reads the row back — so the
    copy-not-mutate rule is measured against the database, exactly as the
    Go side's probe measures it.
    """
    import asyncio

    from core.application.effective_settings import INHERITED_MARK, materialize
    from core.application.repositories import BotToken
    from core.infrastructure.persistence.database import SqliteDatabase
    from core.infrastructure.persistence.sqlite_repositories import (
        SqliteBotTokenRepository,
        SqliteForwardRuleRepository,
    )
    from core.infrastructure.security.crypto import CryptoService

    rows = case.get("settings_bots") or []
    owner = int(case.get("settings_owner") or 0)
    metadata = dict(case.get("metadata") or {})
    keys = _effective_case_keys(case)

    def build() -> str:
        async def run() -> str:
            db = SqliteDatabase(":memory:")
            try:
                for stmt in _PARITY_SETTINGS_SCHEMA:
                    db.execute(stmt)
                # The forward rule's session row: the repository's INSERT
                # is foreign-keyed to it, so a fresh fixture needs one.
                db.execute(
                    "INSERT INTO sessions (id, phone_number, session_string_encrypted,"
                    " user_id, username, first_name, is_active, is_authorized,"
                    " created_at, updated_at)"
                    " VALUES ('s1', '+1000', '', 0, '', '', 1, 1, 0, 0)",
                    (),
                )
                crypto = CryptoService("parity-master-key")
                tokens = SqliteBotTokenRepository(db, crypto)
                rules = SqliteForwardRuleRepository(db)
                for row in rows:
                    await tokens.add(BotToken(
                        id=row["token_id"], label=row["token_id"],
                        token="", owner_user_id=int(row.get("owner") or 0),
                        created_at=0, updated_at=0,
                        defaults=dict(row.get("defaults") or {}),
                    ))
                from core.domain.entities import ForwardRule as _FR
                from core.domain.value_objects import ForwardMode, RoutingType

                rule = _FR(
                    id="r-parity", name="parity", session_id="s1",
                    source_chat_id="-1001", target_chat_id="-1002",
                    routing_type=RoutingType.CHANNEL_TO_CHANNEL,
                    forward_mode=ForwardMode.COPY_MESSAGE,
                    owner_user_id=owner,
                )
                rule.metadata = dict(metadata)
                if metadata.get("bot_token_id"):
                    rule.bot_token_id = str(metadata["bot_token_id"])
                await rules.add(rule)

                resolved = await materialize(await rules.get_by_id("r-parity"), tokens)
                copied = getattr(resolved, INHERITED_MARK, None) is not None

                # The stored row, read back fresh after resolution.
                import json as _json

                stored_row = db.query_one(
                    "SELECT metadata FROM forward_rules WHERE id = 'r-parity'")
                stored_meta = _json.loads(stored_row["metadata"]) if stored_row else {}
                resolved_values = {key: getattr(resolved, key) for key in keys}
                stored_values = {
                    key: stored_meta.get(key)
                    for key in keys if key in stored_meta
                }
                return (f"copy={'done' if copied else 'unchanged'} "
                        f"resolved={_render_key_values(resolved_values, keys)} "
                        f"stored={_render_key_values(stored_values, keys)}")
            finally:
                db.close()

        return asyncio.run(run())

    return build()


ADAPTERS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "cleaner": py_cleaner,
    "emoji": py_emoji,
    "link_policy": py_link_policy,
    "remove_urls": py_remove_urls,
    "replace_links": py_replace_links,
    "replacements": py_replacements,
    "advanced": py_advanced,
    "header_footer": py_header_footer,
    "header_template": py_header_template,
    "filter_engine": py_filter_engine,
    "ai_gate": py_ai_gate,
    # Stage 4.5 W4: the keyword-reply trigger and the album window.
    "keyword": py_keyword,
    "album_window": py_album_window,
    # Stage 4.5 W1: the routing decisions the Go Worker re-runs at
    # delivery time (source matching, target fan-out, loop prevention).
    "chat_key": py_chat_key,
    "matches_source": py_matches_source,
    "all_targets": py_all_targets,
    "check_loop": py_check_loop,
    "fen": py_fen,
    # Stage 4.5 W3: the dedup plane (content fingerprint + claim/release).
    "dedup": py_dedup,
    # Stage 5 W2: the effective-settings / configuration-ownership plane
    # (pinned vs inherited vs spec-default, missing bot, foreign-tenant
    # bot, inheritable=False, validation-failure-skip).
    "effective_settings": py_effective_settings,
    # Stage 5 (W4): the AI provider execution path (transform + fallback).
    "ai_exec": py_ai_exec,
}


# ---------------------------------------------------------------------------
# The case list — every entry executed by BOTH engines.
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# The case list — every entry executed by BOTH engines.
# ---------------------------------------------------------------------------# ---------------------------------------------------------------------------
# The case list — every entry executed by BOTH engines.
# ---------------------------------------------------------------------------


def cases() -> List[Dict[str, Any]]:
    return [
        # --- CleanerRule.clean (CLN-FULL-01, CLN-PHONE-01, CLN-MENTION-01,
        # --- CLN-HASHTAG-01)
        {"engine": "cleaner", "text": "@bob see https://a.io/x #news 5% OFF\n\n\n@alice call +1 415 555 2671 now",
         "cleaner": {"remove_urls": True, "remove_mentions": True, "remove_hashtags": True,
                     "remove_phone_numbers": True, "remove_empty_lines": True}},
        # F-09: apply_to_caption=False must leave a media caption alone,
        # and must still clean a plain body.
        {"engine": "cleaner", "text": "cap https://a.io", "is_caption": True,
         "cleaner": {"remove_urls": True, "apply_to_caption": False}},
        {"engine": "cleaner", "text": "body https://a.io", "is_caption": False,
         "cleaner": {"remove_urls": True, "apply_to_caption": False}},
        # The default (apply_to_caption=True) keeps cleaning captions.
        {"engine": "cleaner", "text": "cap https://a.io", "is_caption": True,
         "cleaner": {"remove_urls": True}},
        {"engine": "cleaner", "text": "order 1234567890 done",
         "cleaner": {"remove_phone_numbers": True}},
        {"engine": "cleaner", "text": "ping @abc now", "cleaner": {"remove_mentions": True}},
        {"engine": "cleaner", "text": "ping @alice now", "cleaner": {"remove_mentions": True}},
        {"engine": "cleaner", "text": "trend #hello-world now", "cleaner": {"remove_hashtags": True}},
        {"engine": "cleaner", "text": "gold #signal #vip now", "cleaner": {"remove_hashtags": True}},
        {"engine": "cleaner", "text": "call +989123456789 today", "cleaner": {"remove_phone_numbers": True}},
        {"engine": "cleaner", "text": "Buy now this promo deal",
         "cleaner": {"patterns": ["Buy now", "promo"]}},
        {"engine": "cleaner", "text": "first\n\n\n\nsecond", "cleaner": {"remove_empty_lines": True}},
        {"engine": "cleaner", "text": "hello", "cleaner": {"use_regex": True, "patterns": ["(unclosed"]}},

        # --- emoji (UNI-EMOJI-01..05)
        {"engine": "emoji", "text": "\U0001F468\u200d\U0001F469\u200d\U0001F467"},
        {"engine": "emoji", "text": "5\ufe0f\u20e3"},
        {"engine": "emoji", "text": "\U00012000 cuneiform"},
        {"engine": "emoji", "text": "سلام 🌟 دنیا"},
        {"engine": "emoji", "text": "a ← b, á"},
        {"engine": "emoji", "text": "hello 🚀 world"},

        # --- link policies
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_ALL_URLS"},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_EXTERNAL_URLS", "allowlist": ["t.me"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "ALLOWLIST_ONLY", "allowlist": ["t.me"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REWRITE_LINKS", "rewrite_map": {"a.io": "b.io"}},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_SELECTED_DOMAINS", "blocklist": ["evil.com"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "CUSTOM_LINK_POLICY", "allowlist": ["t.me"], "blocklist": ["evil.com"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_BUTTON_URLS"},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "PRESERVE_ALL"},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_ALL"},  # unknown value → fail closed to preserve
        # LNK-OVERRIDE-01: a mention survives link removal.
        {"engine": "link_policy", "text": "see @bob and https://a.io", "policy": "REMOVE_ALL_URLS"},

        # --- FilterEngine.remove_urls (the content-policy rewrite path)
        {"engine": "remove_urls", "text": "see @bob and https://a.io"},
        {"engine": "remove_urls", "text": "visit https://a.io now"},
        {"engine": "remove_urls", "text": "markdown [anchor](https://a.io) link"},
        {"engine": "remove_urls", "text": "t.me/x and www.example.com/y"},

        # --- replace_links (the durable-path legacy rewrite)
        {"engine": "replace_links", "text": "visit https://a.io now", "replacement": "[LINK]"},
        {"engine": "replace_links", "text": "markdown [anchor](https://a.io) link", "replacement": "X"},

        # --- apply_replacements (RPL-* family)
        {"engine": "replacements", "text": "# sale",
         "replacements": {"#": "##", "##": "x"}},
        # The same entries in the opposite storage order compose the other
        # way: same-priority insertion order IS the observable contract
        # (RPL-PRIORITY-01's notes), and a Go map decode would silently
        # re-order it.
        {"engine": "replacements", "text": "# sale",
         "replacements": {"##": "x", "#": "##"}},
        {"engine": "replacements", "text": "discount 10% DISCOUNT 20%",
         "replacements": {"DISCOUNT": "SALE"}},
        {"engine": "replacements", "text": "discount 10% DISCOUNT 20%",
         "replacements": {"Discount": "Sale"}},
        {"engine": "replacements", "text": "hello world",
         "replacements": {"world": ""}},
        {"engine": "replacements", "text": "item: apple",
         "replacements": {r"(\w+): (\w+)": {"to": r"\1 is \2", "mode": "regex"}}},
        {"engine": "replacements", "text": "hello",
         "replacements": {r"(unclosed": {"to": "x", "mode": "regex"}}},
        {"engine": "replacements", "text": "a\\1b",
         "replacements": {"a\\1b": "KEPT"}},
        {"engine": "replacements", "text": "promo price today",
         "replacements": {"promo": {"to": "sale", "whole_word": True}}},

        # --- AI transform gate (entities.py:1681): a config id alone means
        # --- "rewrite"; the paid-addon switch is the veto.
        {"engine": "ai_gate", "ai_config_id": "", "metadata": {}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1", "metadata": {}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1", "metadata": None},
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"paid_addons": {}}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"paid_addons": {"ai_rewrite": True}}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"paid_addons": {"ai_rewrite": False}}},
        # The veto must be a dict member; a malformed (string) block is "no
        # veto recorded", and smart_ai_rewrite is not the veto.
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"paid_addons": "true"}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"smart_ai_rewrite": False}},

        # --- F-11: FilterEngine check ORDER is the contract. Python order:
        # --- service -> media -> length -> blacklist -> whitelist -> regex.
        {"engine": "filter_engine", "text": "short secret deal",
         "filter": {"min_message_length": 100, "blacklist_keywords": ["secret"],
                    "is_enabled": True}},
        {"engine": "filter_engine", "text": "hi",
         "filter": {"min_message_length": 100, "is_enabled": True}},
        # media before length: a blocked media type with an out-of-bounds body.
        {"engine": "filter_engine", "text": "x", "media_type": "PHOTO",
         "filter": {"blocked_media_types": ["photo"], "min_message_length": 100,
                    "is_enabled": True}},
        # The blocked_media_types "link" arm is the LIVE link gate at this
        # level, on both engines.
        {"engine": "filter_engine", "text": "see https://a.io/x",
         "filter": {"blocked_media_types": ["link"], "is_enabled": True}},
        # F-12/F-11: the FilterRule-level block_links flag is dead code in
        # Python (no such field); it must never fire on either side.
        {"engine": "filter_engine", "text": "see https://a.io/x",
         "filter": {"block_links": True, "is_enabled": True}},

        # --- F-03: Unicode-correct emoji removal. ZWJ families and keycaps go
        # --- as units (no dangling joiners); non-emoji SMP scripts survive.
        {"engine": "emoji", "text": "\U0001f468\u200d\U0001f469\u200d\U0001f467 family"},
        {"engine": "emoji", "text": "5\ufe0f\u20e3 five"},
        {"engine": "emoji", "text": "\U00012000 cuneiform"},
        {"engine": "emoji", "text": "\U0001f495 heart"},
        {"engine": "emoji", "text": "\u2190 arrow"},
        {"engine": "emoji", "text": "\U0001f3fb toned"},
        {"engine": "emoji", "text": "\U0001f1ee\U0001f1f7 flag"},
        {"engine": "emoji", "text": "x5\ufe0f\u20e3y"},

        # --- advanced content transforms (watermark / link buttons / affiliate)
        {"engine": "advanced", "text": "body",
         "metadata": {"watermark_text": "© me", "paid_addons": {"watermark": True}}},
        {"engine": "advanced", "text": "body",
         "metadata": {"link_buttons": [{"text": "Join", "url": "https://t.me/x"}],
                      "paid_addons": {"link_buttons": True}}},
        {"engine": "advanced", "text": "see https://a.io/x now",
         "metadata": {"affiliate_kit": {"enabled": True, "prefix": "https://aff.io/"},
                      "paid_addons": {"affiliate_kit": True}}},
        # The add-on gate: paid_addons off means the transform never runs.
        {"engine": "advanced", "text": "body",
         "metadata": {"watermark_text": "© me", "paid_addons": {}}},
        # The gate applies to the PAID add-ons only. link_buttons and
        # affiliate_kit without their entitlement render nothing; the
        # watermark (not a paid add-on) is unaffected.
        {"engine": "advanced", "text": "body",
         "metadata": {"link_buttons": [{"text": "Join", "url": "https://t.me/x"}],
                      "paid_addons": {}}},
        {"engine": "advanced", "text": "see https://a.io/x now",
         "metadata": {"affiliate_kit": {"enabled": True, "prefix": "https://aff.io/"},
                      "paid_addons": {}}},

        # --- header / footer templates (HeaderFormatter.format_text, the
        # --- Stage 4.4 F-15 fix: the durable path now renders the
        # --- placeholders the preview always promised)
        {"engine": "header_template", "template": "From [[chat_title]] on {date}",
         "chat_id": "-1001234567890", "chat_title": "News Channel", "date": 1730000000},
        {"engine": "header_template", "template": "{source_link}",
         "chat_id": "-1001234567890", "chat_username": "newschannel", "message_id": 55},
        {"engine": "header_template", "template": "{ORIGIN_NAME_URL} {ORIGIN_POST_ID} {ORIGIN_USERNAME}",
         "chat_id": "-1001234567890", "chat_username": "newschannel", "message_id": 55,
         "chat_title": "News Channel"},
        {"engine": "header_template", "template": "{rule_name} {category} {sender_name}",
         "chat_id": "-1001234567890", "chat_title": "News", "rule_name": "My Task",
         "sender_name": "Alice"},
        # A private channel: no @username, so the link takes the /c/ form.
        {"engine": "header_template", "template": "{source_link} {chat_title}",
         "chat_id": "-1001234567890", "message_id": 7},
        # An unknown placeholder stays visible; an empty value renders empty.
        {"engine": "header_template", "template": "{unknown} stays",
         "chat_id": "-1001234567890", "chat_title": "News", "date": 1730000000},

        # --- header / footer (the dedup wrapper)
        {"engine": "header_footer", "text": "body", "header": "HDR"},
        {"engine": "header_footer", "text": "HDR\n\nbody", "header": "HDR"},
        {"engine": "header_footer", "text": "body", "footer": "FTR"},
        {"engine": "header_footer", "text": "body", "header": "H", "footer": "F"},
        {"engine": "header_footer", "text": "body", "header": "", "footer": ""},

        # --- Stage 4.5 W1: routing & loop prevention (the Go Worker
        # --- re-runs these at delivery time; every adapter calls the REAL
        # --- Python method, so a divergence is a parity failure).
        # _chat_key: every spelling of one chat normalises to one key.
        {"engine": "chat_key", "chat_id": "-100123"},
        {"engine": "chat_key", "chat_id": "-100123.0"},
        {"engine": "chat_key", "chat_id": " -100123 "},
        {"engine": "chat_key", "chat_id": "-100123.7"},
        {"engine": "chat_key", "chat_id": "@username"},
        {"engine": "chat_key", "chat_id": ""},
        {"engine": "chat_key", "chat_id": "   "},
        {"engine": "chat_key", "chat_id": "not-a-number"},
        # matches_source: primary, criteria-listed extras, username spellings.
        {"engine": "matches_source", "chat_id": "-1001", "source_chat_id": "-1001"},
        {"engine": "matches_source", "chat_id": "-1002",
         "source_chat_id": "-1001",
         "detection_criteria": {"source_chat_ids": ["-1002", "-1003"]}},
        {"engine": "matches_source", "chat_id": "-1003",
         "source_chat_id": "-1001",
         "detection_criteria": {"source_chat_ids": ["-1002", "-1003"]}},
        # A float-spelled id is the same chat after _chat_key.
        {"engine": "matches_source", "chat_id": "-1002.0",
         "source_chat_id": "-1001",
         "detection_criteria": {"source_chat_ids": ["-1002"]}},
        # Usernames: with and without the @, and case never matters.
        {"engine": "matches_source", "chat_id": "@channel",
         "source_chat_id": "@channel"},
        {"engine": "matches_source", "chat_id": "channel",
         "source_chat_id": "@channel"},
        {"engine": "matches_source", "chat_id": "@CHANNEL",
         "source_chat_id": "@channel"},
        {"engine": "matches_source", "chat_id": "@NAMEDSOURCE",
         "source_chat_id": "-1001", "source_chat_name": "@namedsource"},
        {"engine": "matches_source", "chat_id": "namedsource",
         "source_chat_id": "-1001", "source_chat_name": "@namedsource"},
        # A task with no criteria list falls back to its primary source.
        {"engine": "matches_source", "chat_id": "-1001",
         "source_chat_id": "-1001", "detection_criteria": {}},
        # Unknown chats, empty ids and inactive tasks never match.
        {"engine": "matches_source", "chat_id": "-9999", "source_chat_id": "-1001"},
        {"engine": "matches_source", "chat_id": "", "source_chat_id": "-1001"},
        # all_targets: primary first, deduped, order preserved.
        {"engine": "all_targets", "target_chat_id": "-2001",
         "target_chat_ids": ["-2001", "-2002"]},
        {"engine": "all_targets", "target_chat_id": "",
         "target_chat_ids": ["-2001", "-2002"]},
        {"engine": "all_targets", "target_chat_id": "-2001",
         "target_chat_ids": []},
        # check_loop: each arm of services.py:508, including the username
        # spelling the numeric comparison alone would miss.
        {"engine": "check_loop", "chat_id": "-1001", "target_chat_id": "-1001"},
        {"engine": "check_loop", "chat_id": "-1001", "target_chat_id": "-2001"},
        {"engine": "check_loop", "chat_id": "-1001", "chat_username": "@sourcechannel",
         "target_chat_id": "@sourcechannel"},
        {"engine": "check_loop", "chat_id": "-1001", "chat_username": "@SourceChannel",
         "target_chat_id": "@SOURCECHANNEL"},
        {"engine": "check_loop", "chat_id": "-1001", "chat_username": "sourcechannel",
         "target_chat_id": "sourcechannel"},
        {"engine": "check_loop", "chat_id": "-1001",
         "forward_origin": {"from_chat_id": "-1099"}, "target_chat_id": "-1099"},
        {"engine": "check_loop", "chat_id": "-1009",
         "forward_origin": {"from_chat_id": "-1099"}, "target_chat_id": "-1099"},
        # The intermediate arm: C as target is a loop only when the message
        # came from C — the source/forward arms report it first otherwise.
        {"engine": "check_loop", "chat_id": "-1300",
         "intermediate_channel_id": "-1300", "target_chat_id": "-1300"},
        {"engine": "check_loop", "chat_id": "-1001",
         "forward_origin": {"from_chat_id": "-1300"},
         "intermediate_channel_id": "-1300", "target_chat_id": "-1300"},
        {"engine": "check_loop", "chat_id": "-1001",
         "forward_origin": {"from_chat_id": "-1099"},
         "intermediate_channel_id": "-1300", "target_chat_id": "-1300"},
        {"engine": "check_loop", "chat_id": "-1001",
         "intermediate_channel_id": "-1300", "target_chat_id": "-1300"},
        # A forward origin whose from_chat_id is absent must not crash.
        {"engine": "check_loop", "chat_id": "-1001",
         "forward_origin": {}, "target_chat_id": "-2001"},
        {"engine": "keyword", "text": "big sale today",
         "metadata": {"keyword_confirmation": False, "keyword_reply": "thanks",
                      "paid_addons": {"keyword_reply": True},
                      "content_policy": {"whitelist_words": ["sale"]}}},
        {"engine": "keyword", "text": "sale",
         "metadata": {"keyword_confirmation": True, "keyword_reply": "x" * 5000,
                      "paid_addons": {"keyword_reply": True},
                      "content_policy": {"whitelist_words": ["sale"]}}},
        {"engine": "keyword", "text": "BIG SALE TODAY",
         "metadata": {"keyword_confirmation": True, "keyword_reply": "thanks",
                      "paid_addons": {"keyword_reply": True},
                      "content_policy": {"whitelist_words": ["sale"]}}},
        {"engine": "keyword", "text": "wholesale prices only",
         "metadata": {"keyword_confirmation": True, "keyword_reply": "thanks",
                      "paid_addons": {"keyword_reply": True},
                      "content_policy": {"whitelist_words": ["sale"]}}},
        {"engine": "keyword", "text": " flash deal now",
         "metadata": {"keyword_confirmation": True, "keyword_reply": "thanks",
                      "paid_addons": {"keyword_reply": True},
                      "whitelist": ["deal"]}},
        {"engine": "keyword", "text": "nothing to see here",
         "metadata": {"keyword_confirmation": True, "keyword_reply": "thanks",
                      "paid_addons": {"keyword_reply": True},
                      "content_policy": {"whitelist_words": ["sale"]}}},
        {"engine": "keyword", "text": "فروش ویژه امروز",
         "metadata": {"keyword_confirmation": True, "keyword_reply": "ممنون",
                      "paid_addons": {"keyword_reply": True},
                      "content_policy": {"whitelist_words": ["فروش"]}}},
        {"engine": "keyword", "text": "",
         "metadata": {"keyword_confirmation": True, "keyword_reply": "thanks",
                      "paid_addons": {"keyword_reply": True},
                      "content_policy": {"whitelist_words": ["sale"]}}},
        {"engine": "album_window", "window": 0.8, "items": 1},
        {"engine": "album_window", "window": 2.0, "items": 1},
        {"engine": "album_window", "window": 2.0, "items": 10},
        {"engine": "album_window", "window": 0.8, "items": 9},
        {"engine": "album_window", "window": 0.0, "items": 3},
        {"engine": "album_window", "window": 0.05, "items": 2},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "Hello World"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "hello world"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "Hello   World"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "Hello \n World"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "ab", "caption": "cd"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "ab"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "", "caption": "caption only",
         "media_type": "PHOTO"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "x", "media_type": "PHOTO"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "x", "media_type": "VIDEO"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "x", "media_type": "PHOTO",
         "media_group_id": "g1"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "كتاب"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "کتاب"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "أحمد"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "invis\u200bible"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": "invisible"},
        {"engine": "dedup", "dedup_op": "fingerprint", "text": ""},
        {"engine": "dedup", "dedup_op": "claim_release", "text": "same announcement"},
        {"engine": "dedup", "dedup_op": "claim_release", "text": "same announcement",
         "metadata": {"rule_id": "other-rule"}},
        {"engine": "fen", "fen_value": None, "fen_sequence": 3},
        {"engine": "fen", "fen_value": 5, "fen_sequence": 6},
        {"engine": "fen", "fen_value": "", "fen_sequence": 2},
        {"engine": "fen", "fen_value": "abc", "fen_sequence": 2},
        {"engine": "fen", "fen_value": True, "fen_sequence": 1},
        {"engine": "fen", "fen_value": False, "fen_sequence": 4},
        {"engine": "fen", "fen_value": [5], "fen_sequence": 1},
        {"engine": "fen", "fen_value": {"n": 5}, "fen_sequence": 1},
        {"engine": "fen", "fen_value": "nan", "fen_sequence": 2},
        {"engine": "fen", "fen_value": "inf", "fen_sequence": 2},
        {"engine": "fen", "fen_value": "-inf", "fen_sequence": 2},
        {"engine": "fen", "fen_value": "1e400", "fen_sequence": 2},
        {"engine": "fen", "fen_value": 0, "fen_sequence": 1},
        {"engine": "fen", "fen_value": -3, "fen_sequence": 1},
        {"engine": "fen", "fen_value": 0.4, "fen_sequence": 1},
        {"engine": "fen", "fen_value": 1001, "fen_sequence": 1},
        {"engine": "fen", "fen_value": 50000, "fen_sequence": 1},
        {"engine": "fen", "fen_value": "  7  ", "fen_sequence": 8},
        {"engine": "fen", "fen_value": "1_000", "fen_sequence": 1001},
        {"engine": "fen", "fen_value": "1__000", "fen_sequence": 1},
        {"engine": "fen", "fen_value": "1000_", "fen_sequence": 1},
        {"engine": "fen", "fen_value": 5, "fen_sequence": 1},
        {"engine": "fen", "fen_value": 5, "fen_sequence": 5},
        {"engine": "fen", "fen_value": 5, "fen_sequence": 11},
        {"engine": "fen", "fen_value": 5, "fen_sequence": 12},
        {"engine": "fen", "fen_value": 4, "fen_sequence": 0},
        {"engine": "fen", "fen_value": 4, "fen_sequence": -2},
        {"engine": "fen", "fen_value": 1, "fen_sequence": 0},
        {"engine": "fen", "fen_value": 1, "fen_sequence": 17},
        {"engine": "fen", "fen_value": 5, "fen_sequence": 2, "fen_slots": True},
        {"engine": "fen", "fen_value": 5, "fen_sequence": 6, "fen_slots": True},
        {"engine": "fen", "fen_value": 5, "fen_sequence": 1, "fen_slots": True},
        {"engine": "fen", "fen_value": 3, "fen_sequence": 4, "fen_slots": True},

        # --- Stage 5 W2: the effective-settings / configuration-ownership
        # --- plane. Every case runs through the REAL registry and the REAL
        # --- bot-defaults resolver on both engines; the store-backed ones
        # --- seed a real bot_tokens table and read through the real
        # --- owner-scoped repository (composite rule store on the Go side).
        # Pinned vs inherited vs spec-default, one case per registry kind.
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "remove_emojis", "metadata": {},
         "bot_defaults": {"remove_emojis": True}},
        # The task's own (pinned) value wins even when the bot carries one.
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "remove_emojis",
         "metadata": {"remove_emojis": True},
         "bot_defaults": {"remove_emojis": False}},
        # int kind: an inheritable int landing and its range guard.
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "auto_delete_seconds", "metadata": {},
         "bot_defaults": {"auto_delete_seconds": 42}},
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "auto_delete_seconds", "metadata": {},
         "bot_defaults": {"auto_delete_seconds": 999999999}},
        # float kind.
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "ai_creativity", "metadata": {},
         "bot_defaults": {"ai_creativity": 1.5}},
        # str kind, incl. the choices gate.
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "header", "metadata": {},
         "bot_defaults": {"header": "BOT-HDR"}},
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "album_mode", "metadata": {},
         "bot_defaults": {"album_mode": "gallery"}},
        # list and dict kinds.
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "blacklist", "metadata": {},
         "bot_defaults": {"blacklist": ["spam", "  promo  ", ""]}},
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "replacements", "metadata": {},
         "bot_defaults": {"replacements": {"sale": "discount"}}},
        # inheritable=False: a bot default naming a route or a chat must
        # never reach the task (both engines refuse it).
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_keys": ["delivery_transport", "bot_token_id",
                           "source_policies", "link_buttons"],
         "metadata": {},
         "bot_defaults": {"delivery_transport": "BOT",
                          "bot_token_id": "tok-other",
                          "source_policies": {"-1001": {}},
                          "link_buttons": ["x"]}},
        # Missing bot (no bot_token_id) → registry defaults.
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "remove_emojis", "metadata": {},
         "bot_defaults": None},
        # A bot with empty defaults → registry defaults.
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "remove_emojis",
         "metadata": {"bot_token_id": "tok-1"},
         "bot_defaults": {}},
        # An unknown key in the bot's blob is skipped, never applied.
        {"engine": "effective_settings", "settings_op": "resolution",
         "settings_key": "remove_emojis", "metadata": {},
         "bot_defaults": {"remove_emojis": True, "not_a_setting": 1}},
        # pinned_keys: the decision every inheritance question turns on.
        {"engine": "effective_settings", "settings_op": "pinned",
         "settings_keys": ["remove_emojis", "header", "blacklist"],
         "metadata": {"remove_emojis": True, "blacklist": ["a"]}},
        # A value equal to the registry default is NOT a choice unless the
        # pin index records it — the gap the index exists to close.
        {"engine": "effective_settings", "settings_op": "pinned",
         "settings_keys": ["remove_emojis"],
         "metadata": {"remove_emojis": False}},
        {"engine": "effective_settings", "settings_op": "pinned",
         "settings_keys": ["remove_emojis"],
         "metadata": {"remove_emojis": False,
                      "_pinned_settings": ["remove_emojis"]}},
        # An empty mapping means "off", not a choice (the _chosen_not_default
        # arm both registries implement).
        {"engine": "effective_settings", "settings_op": "pinned",
         "settings_keys": ["power_schedule", "auto_post_scheduler"],
         "metadata": {"power_schedule": {}}},
        # materialize: nothing to inherit → the rule itself, no copy.
        {"engine": "effective_settings", "settings_op": "materialize",
         "settings_key": "remove_emojis",
         "metadata": {"bot_token_id": "tok-1"},
         "bot_defaults": {}},
        # materialize: no bot attached → no defaults, even though a blob
        # was handed in (attached_defaults' empty-token-id clause).
        {"engine": "effective_settings", "settings_op": "materialize",
         "settings_key": "remove_emojis",
         "metadata": {},
         "bot_defaults": {"remove_emojis": True}},
        # materialize: the copy carries the sorted mark and the values.
        {"engine": "effective_settings", "settings_op": "materialize",
         "settings_keys": ["remove_emojis", "header"],
         "metadata": {"header": "MINE", "bot_token_id": "tok-1"},
         "bot_defaults": {"remove_emojis": True}},
        # materialize: a validation failure is skipped, the valid keys land.
        {"engine": "effective_settings", "settings_op": "materialize",
         "settings_keys": ["remove_emojis", "auto_delete_seconds", "album_mode"],
         "metadata": {"bot_token_id": "tok-1"},
         "bot_defaults": {"remove_emojis": True, "auto_delete_seconds": 999999999,
                          "album_mode": "gallery"}},
        # --- store-backed: the owner-scoped read, measured end to end -----
        # The task's own bot: inherited, and the stored row untouched.
        {"engine": "effective_settings", "settings_op": "store",
         "settings_keys": ["remove_emojis"],
         "settings_owner": 5094837833,
         "settings_bots": [{"token_id": "tok-alice", "owner": 5094837833,
                            "defaults": {"remove_emojis": True}}],
         "metadata": {"bot_token_id": "tok-alice"}},
        # A foreign-tenant bot: NO defaults (registry defaults), because the
        # owner-scoped read simply does not find the row.
        {"engine": "effective_settings", "settings_op": "store",
         "settings_keys": ["remove_emojis"],
         "settings_owner": 5094837833,
         "settings_bots": [{"token_id": "tok-bob", "owner": 6278059256,
                            "defaults": {"remove_emojis": True}}],
         "metadata": {"bot_token_id": "tok-bob"}},
        # Two users, two bots, opposite defaults: each task resolves only
        # its owner's bot's.
        {"engine": "effective_settings", "settings_op": "store",
         "settings_keys": ["remove_emojis"],
         "settings_owner": 5094837833,
         "settings_b": [],
         "settings_bots": [
             {"token_id": "tok-alice", "owner": 5094837833,
              "defaults": {"remove_emojis": True}},
             {"token_id": "tok-bob", "owner": 6278059256,
              "defaults": {"remove_emojis": False}}],
         "metadata": {"bot_token_id": "tok-alice"}},
        # A task with no owner (a pre-ownership row) takes the unscoped
        # read and still inherits.
        {"engine": "effective_settings", "settings_op": "store",
         "settings_keys": ["remove_emojis"],
         "settings_owner": 0,
         "settings_bots": [{"token_id": "tok-legacy", "owner": 0,
                            "defaults": {"remove_emojis": True}}],
         "metadata": {"bot_token_id": "tok-legacy"}},
        # A deleted token: no defaults, never an error.
        {"engine": "effective_settings", "settings_op": "store",
         "settings_keys": ["remove_emojis"],
         "settings_owner": 5094837833,
         "settings_bots": [],
         "metadata": {"bot_token_id": "tok-gone"}},
    ]


# ---------------------------------------------------------------------------
# The Go driver
# ---------------------------------------------------------------------------

# The driver source is a committed file (worker/tests/parity_driver.go), kept
# in the repo so the Go toolchain vets it on every build instead of the
# harness generating unvetted code at run time. The harness copies every
# committed driver file into a main package under worker/tests, runs it once
# over the case list, and removes the copies.
DRIVER_SOURCE_FILE = ROOT / "worker" / "tests" / "parity_driver.go"

# Every committed file under worker/tests that declares the parity build
# tag. The tag keeps package main out of the normal build (it lives inside a
# library tree); the harness strips it from each copy it compiles.
DRIVER_SOURCES = sorted(
    (ROOT / "worker" / "tests").glob("*.go")
)

# _strip_build_tag removes the go:build / +build prefix lines (and any blank
# lines between them and the package clause) so the copied file compiles as
# part of the harness's untagged main package.
def _strip_build_tag(source: str) -> str:
    lines = source.splitlines()
    while lines and (lines[0].startswith("//go:build") or
                     lines[0].startswith("// +build") or lines[0].strip() == ""):
        lines = lines[1:]
    return "\n".join(lines) + "\n"


def run_go(cases_json: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Run the Go driver once over the whole case list."""
    driver_dir = ROOT / "worker" / "tests"
    driver_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    try:
        # Copy every committed driver file, tags stripped, so a driver
        # split across files (the effective-settings arm is one) compiles
        # as one main package.
        for source_path in DRIVER_SOURCES:
            text = source_path.read_text(encoding="utf-8")
            target = driver_dir / (source_path.stem + "_main.go")
            target.write_text(_strip_build_tag(text), encoding="utf-8")
            written.append(target)
        proc = subprocess.run(
            ["go", "run", "./tests"],
            cwd=str(ROOT / "worker"),
            input=json.dumps(cases_json).encode("utf-8"),
            capture_output=True,
            timeout=240,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
            return None
        return json.loads(proc.stdout.decode("utf-8"))
    finally:
        for copy in written:
            copy.unlink(missing_ok=True)


def main() -> int:
    case_list = cases()
    go_results = run_go(case_list)
    if go_results is None:
        print("PARITY: could not run the Go driver", file=sys.stderr)
        return 2

    by_index = {r["index"]: r for r in go_results}
    divergent = 0
    for index, case in enumerate(case_list):
        adapter = ADAPTERS[case["engine"]]
        py_out = adapter(case)
        go_out = by_index[index]["output"]
        if py_out != go_out:
            divergent += 1
            print(f"DIVERGENT case {index} ({case['engine']}):")
            # A case's identity field differs per engine (a transform case
            # carries its text, a template case its template), so print the
            # pair the case itself names.
            identity = case.get("text", case.get("template", ""))
            print(f"  input : {identity!r}")
            print(f"  python: {py_out!r}")
            print(f"  go    : {go_out!r}")
    total = len(case_list)
    print(f"Stage 4.2 differential parity: {total - divergent}/{total} cases agree")
    if divergent:
        print(f"PARITY FAILED: {divergent} divergences", file=sys.stderr)
        return 1
    print("PARITY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
