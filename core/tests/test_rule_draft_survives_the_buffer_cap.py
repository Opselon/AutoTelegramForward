"""A durable rule draft must not lose the fields a user just edited.

`ForwardRule.to_draft()` is ordered base-fields first and the AI / prompt /
paid-addon fields last. The durable buffer's generic bounding kept a *prefix*
of a mapping, so a task whose content policy pushed the draft past the 2 KB
per-value cap dropped everything after the break — 62 of 69 keys in the field —
and `ed_save` then reported success while writing the rule's old values back.
The dropped tail was exactly the AI rewrite and prompt settings the user had
just changed.

Two things are pinned here:

* a draft that must be trimmed keeps every decision-bearing key;
* the generic prefix behaviour still applies to plain mappings (a chat list).
"""

from __future__ import annotations

import json

from core.domain.entities import ForwardRule
from core.infrastructure.telegram.ui_state_manager import (
    MAX_BUFFER_VALUE,
    _bounded_draft,
    _bounded_value,
    _safe_buffer,
)

#: The keys a trimmed draft may never lose: they carry the decisions made
#: since the rule was loaded, and nothing in the editor can re-derive them.
DECISION_KEYS = (
    "source_chat_id",
    "target_chat_id",
    "content_policy",
    "ai_config_id",
    "ai_fallback_policy",
    "ai_prompt_version",
    "ai_timeout_seconds",
    "prompt_id",
    "smart_ai_rewrite",
    "paid_addons",
)


def _fat_draft() -> dict:
    """A realistic worst case: long titles plus a populated content policy."""
    draft = ForwardRule().to_draft()
    draft["source_chat_name"] = "SolHouse AI Scanner Premium Signals Channel"
    draft["target_chat_name"] = "My Private Forwarding Destination Group"
    draft["content_policy"] = {
        "blacklist_words": ["crypto", "pump", "scam", "airdrop", "presale"] * 3,
        "regex_block": [r"\b(?:buy|sell)\s+now\b", r"https?://\S+"],
        "enabled": True,
        "source_policies": {"-1004400615270": {"blacklist_words": ["spam", "ad"]}},
        "target_policies": {"-1003915313919": {"blacklist_words": ["nsfw"]}},
    }
    draft["ai_config_id"] = "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"
    draft["ai_timeout_seconds"] = 30.0
    draft["prompt_id"] = "9f8e7d6c-5b4a-3928-1706-1f2e3d4c5b6a"
    draft["smart_ai_rewrite"] = True
    return draft


def test_the_fixture_really_exceeds_the_cap():
    """Premise: this draft cannot be stored whole, so trimming must happen."""
    assert len(json.dumps(_fat_draft(), default=str)) > MAX_BUFFER_VALUE


def test_a_trimmed_draft_keeps_every_decision_bearing_key():
    draft = _fat_draft()
    trimmed, note = _bounded_draft(draft, MAX_BUFFER_VALUE)

    assert isinstance(trimmed, dict), note
    assert len(json.dumps(trimmed, default=str)) <= MAX_BUFFER_VALUE
    for key in DECISION_KEYS:
        assert key in trimmed, f"the trimmed draft lost {key} ({note})"
    # It is a trim, not a wipe: most of the record survives.
    assert len(trimmed) >= len(draft) - 3, note


def test_trimming_never_keeps_a_draft_that_cannot_be_applied():
    """A source/target-less draft would be refused by ed_save, not saved."""
    draft = _fat_draft()
    trimmed, _note = _bounded_draft(draft, MAX_BUFFER_VALUE)
    assert trimmed.get("source_chat_id") == draft["source_chat_id"]
    assert trimmed.get("target_chat_id") == draft["target_chat_id"]


def test_a_plain_mapping_still_keeps_an_ordered_prefix():
    """The generic path is unchanged for chat lists and scratchpads."""
    payload = {"chats": [{"id": i, "title": f"Chat {i}"} for i in range(60)], "page": 0}
    kept, note = _bounded_value(payload, MAX_BUFFER_VALUE)
    assert isinstance(kept, dict) and kept.get("chats")
    assert note.startswith("mapping ")
    assert len(json.dumps(kept, default=str)) <= MAX_BUFFER_VALUE


def test_the_buffer_sizes_the_draft_before_its_neighbours():
    """A draft listed after the chat list must still be stored."""
    buf = {
        "chats": [{"id": i, "title": f"Real Channel {i}"} for i in range(90)],
        "draft": _fat_draft(),
        "rule_id": "ea565d5c-ccce-4f1c-b5f5-79eca2911642",
    }
    stored = _safe_buffer(buf)
    assert stored.get("draft"), "the draft was dropped entirely"
    assert "ai_config_id" in stored["draft"]
    assert len(json.dumps(stored["draft"], default=str)) <= MAX_BUFFER_VALUE
