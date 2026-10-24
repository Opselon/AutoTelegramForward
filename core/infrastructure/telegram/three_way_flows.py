"""Three-way content filter flows for the Telegram bot: Keyword, Regex and
Long-Text Matching.

This module is the *presentation* layer for the three user-facing options the
Whitelist and Blacklist menus now offer. It owns conversation state, keyboard
rendering and localization — nothing more. Every matching decision is made by
the canonical domain primitive :func:`core.domain.content_policy.preview_match`,
so a bot-side "test" can never disagree with production filtering.

State
-----
The flows ride the existing ``UiState`` machinery: ``st.step`` is a prefixed
token (``rgx_<rule_id>``, ``lt_<rule_id>``, ``ts_<rule_id>``) and draft values
live in ``st.buffer``. Ownership is re-validated on *every* callback and step
through ``_owned_rule`` — callback data is user-controlled and is never trusted
as authorization.

Persistence
-----------
Long-text entries land on the task's content policy
(``blacklist_words`` / ``whitelist_words`` + ``whitelist_mode``); regex rules
land on ``regex_block`` / ``regex_require``. A legacy ``FilterRule`` row, when
one is attached, is kept in sync for the stage-3 engine. No new tables, no new
fields, no redundant boolean.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...domain.content_policy import (
    MatchPreview,
    compile_user_regex,
    preview_match,
    split_word_list,
)
from ...domain.entities import ForwardRule

logger = logging.getLogger(__name__)

#: The three matching modes a Long-Text rule offers. The domain accepts
#: ``any`` | ``all`` | ``phrase``; anything else would be re-interpreted by
#: ``preview_match`` as ``any``, so the UI offers exactly the supported set.
LONGTEXT_MODES: Tuple[str, ...] = ("any", "all", "phrase")

#: Regex examples offered as one-tap templates. Every entry is compiled by the
#: domain's guarded compiler before it is ever offered, so a user can never
#: pick a pattern the runtime would refuse.
REGEX_EXAMPLES: Tuple[Tuple[str, str], ...] = (
    ("^BUY XAUUSD$", "The entire message must match."),
    (r"\b(BUY|SELL)\b", "Either word, as whole words."),
    (r"XAUUSD\s+BUY", "XAUUSD, then one or more spaces, then BUY."),
)

_MAX_ENTRIES = 64
_MAX_PATTERN_LEN = 256


def _fmt_entries(entries: Sequence[str], limit: int = 12) -> str:
    """A compact, markdown-safe rendering of a rule's entries."""
    clean = [str(e) for e in entries if str(e).strip()]
    if not clean:
        return "—"
    shown = ", ".join(f"`{e}`" for e in clean[:limit])
    if len(clean) > limit:
        shown += f" (+{len(clean) - limit})"
    return shown


def _mode_label(mode: str) -> str:
    return str(mode or "any").strip().lower() or "any"


def _translate(ui, key: str, lang: Optional[str] = None, **kw) -> str:
    """``ui._t`` with the caller's locale, falling back to English."""
    return ui._t(key, lang=lang or ui._lang(), **kw)


class ThreeWayFlows:
    """The Regex / Long-Text / Test flows, bound to one ``ProBotUi`` instance.

    The class deliberately holds no state of its own beyond the parent
    reference: all conversation state lives in the existing per-user
    ``UiState``, persisted through the parent's ``_persist``.
    """

    def __init__(self, ui: Any) -> None:
        self._ui = ui

    # ------------------------------------------------------------------ utils

    def _t(self, key: str, lang: Optional[str] = None, **kw) -> str:
        return self._ui._t(key, lang=lang, **kw)

    def _lang(self) -> str:
        return getattr(self._ui, "_lang", None) and self._ui._lang() or "en"

    async def _rule(self, rule_id: str, uid: int) -> Optional[ForwardRule]:
        return await self._ui._owned_rule(rule_id, uid)

    def _kbd(self, rows: List[List[Tuple[str, str]]]):
        return self._ui._kbd(rows)

    # ------------------------------------------------------- rule harvesting

    def _policy_of(self, rule: ForwardRule) -> Dict[str, Any]:
        """The task content policy as a plain, mutable dict.

        Reads through ``ForwardRule.content_policy`` so legacy metadata keys
        are folded in exactly the way the runtime does it — a UI draft and the
        production evaluator see the same configuration.
        """
        policy = rule.content_policy
        cfg = policy.to_dict() if hasattr(policy, "to_dict") else {}
        return cfg or {}

    def _apply_policy(self, rule: ForwardRule, cfg: Dict[str, Any], uid: int) -> None:
        """Write a policy dict back onto the task and keep the legacy filter row in sync."""
        lists = ("blacklist_words", "whitelist_words", "regex_block", "regex_require")
        for key in lists:
            val = cfg.get(key)
            if isinstance(val, list):
                cfg[key] = [str(v) for v in val][:_MAX_ENTRIES]
        if cfg.get("whitelist_mode"):
            cfg["whitelist_mode"] = _mode_label(cfg["whitelist_mode"])
        rule.content_policy_config = cfg

        # The stage-3 ``FilterEngine`` still reads the legacy row. Keep the
        # word lists mirrored there so both stages enforce the same rule.
        fr = self._legacy_filter_row(rule)
        if fr is not None:
            wl = list(cfg.get("whitelist_words") or [])
            bl = list(cfg.get("blacklist_words") or [])
            rx = list(cfg.get("regex_block") or [])
            if wl:
                fr.whitelist_keywords = wl
            if bl:
                fr.blacklist_keywords = bl
            if rx:
                fr.regex_patterns = rx

    def _legacy_filter_row(self, rule: ForwardRule) -> Optional[Any]:
        """The task's legacy FilterRule row, when one is attached."""
        fid = getattr(rule, "filter_rule_id", None)
        if not fid or self._ui._filters is None:
            return None
        try:
            row = self._ui._filters.get(fid)  # awaited dataclass fetch
        except Exception:
            logger.debug("legacy filter row lookup failed", exc_info=True)
            return None
        return row

    # ------------------------------------------------------------- renderers

    def render_regex_prompt(self, rule: ForwardRule) -> Tuple[str, Any]:
        lang = self._lang()
        text = self._t("ui_prompt_add_regex", lang=lang)
        rows = [
            [(self._t("ui_regex_example_btn", lang=lang), f"rgx_ex:{rule.id}")],
            [(self._t("ui_cancel_back", lang=lang), f"rfl_bl:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def render_regex_direction(self, rule: ForwardRule, pattern: str) -> Tuple[str, Any]:
        lang = self._lang()
        text = (
            self._t("ui_prompt_add_regex", lang=lang)
            + "\n"
            + self._t("ui_regex_blocked_or_required", lang=lang)
        )
        rows = [
            [
                (self._t("ui_regex_btn_block", lang=lang), f"rgx_blk:{rule.id}"),
                (self._t("ui_regex_btn_require", lang=lang), f"rgx_req:{rule.id}"),
            ],
            [(self._t("ui_cancel_back", lang=lang), f"rfl_bl:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def render_longtext_prompt(self, rule: ForwardRule) -> Tuple[str, Any]:
        lang = self._lang()
        text = self._t("ui_prompt_add_longtext", lang=lang)
        rows = [
            [(self._t("ui_cancel_back", lang=lang), f"rfl_bl:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def render_longtext_mode(self, rule: ForwardRule, entries: List[str]) -> Tuple[str, Any]:
        lang = self._lang()
        text = (
            self._t("ui_prompt_add_longtext", lang=lang)
            + "\n"
            + self._t("ui_longtext_choose_mode", lang=lang)
        )
        rows = [
            [
                (self._t("ui_longtext_mode_any", lang=lang), f"lt_any:{rule.id}"),
                (self._t("ui_longtext_mode_all", lang=lang), f"lt_all:{rule.id}"),
                (self._t("ui_longtext_mode_phrase", lang=lang), f"lt_phr:{rule.id}"),
            ],
            [(self._t("ui_cancel_back", lang=lang), f"rfl_bl:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def render_test_prompt(self, rule: ForwardRule) -> Tuple[str, Any]:
        lang = self._lang()
        cfg = self._policy_of(rule)
        kind = "blacklist" if (cfg.get("blacklist_words") or cfg.get("regex_block")) else "whitelist"
        text = (
            self._t("ui_prompt_test_sample", lang=lang)
            + "\n"
            + self._t(
                "ui_test_effective_config",
                lang=lang,
                kind=kind,
                entries=_fmt_entries(
                    cfg.get("blacklist_words") or cfg.get("whitelist_words") or cfg.get("regex_block") or ()
                ),
                mode=_mode_label(cfg.get("whitelist_mode") or "any"),
            )
        )
        rows = [
            [(self._t("ui_cancel_back", lang=lang), f"rfl_bl:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def render_test_result(self, rule: ForwardRule, preview: MatchPreview) -> Tuple[str, Any]:
        lang = self._lang()
        entries = preview.matched or preview.missing or preview.entries
        mode = _mode_label(preview.mode)
        if preview.allowed:
            text = self._t(
                "ui_test_result_match", lang=lang, matched=_fmt_entries(entries), mode=mode
            )
        elif preview.reason.startswith("blacklist:") or preview.reason.startswith("regex_block:"):
            text = self._t(
                "ui_test_result_blocked", lang=lang, matched=_fmt_entries(entries)
            )
        else:
            text = self._t(
                "ui_test_result_no_match",
                lang=lang,
                missing=_fmt_entries(preview.missing or entries),
                mode=mode,
            )
        rows = [
            [(self._t("ui_filters_btn_test", lang=lang), f"rfl_test:{rule.id}")],
            [(self._t("ui_cancel_back", lang=lang), f"rfl_bl:{rule.id}")],
        ]
        return text, self._kbd(rows)

    # --------------------------------------------------------------- actions

    def start_regex(self, rule: ForwardRule) -> Tuple[str, Any]:
        return self.render_regex_prompt(rule)

    def validate_pattern(self, pattern: str) -> Optional[str]:
        """A readable invalid-pattern diagnostic, or None when the pattern is usable."""
        pat = (pattern or "").strip()
        if not pat:
            return "empty"
        if len(pat) > _MAX_PATTERN_LEN:
            return "too_long"
        if compile_user_regex(pat) is None:
            return "invalid"
        return None

    async def save_regex(
        self, rule: ForwardRule, pattern: str, direction: str, uid: int
    ) -> Tuple[bool, str]:
        """Persist a regex rule. Returns (ok, reason) — never raises on bad input."""
        diag = self.validate_pattern(pattern)
        if diag is not None:
            return False, diag
        cfg = self._policy_of(rule)
        key = "regex_block" if direction == "block" else "regex_require"
        patterns = [p for p in (cfg.get(key) or []) if p != pattern]
        patterns.append(pattern)
        cfg[key] = patterns
        self._apply_policy(rule, cfg, uid)
        await self._ui._save_owned_rule(rule, uid)
        logger.info(
            "[BOT:FILTER_REGEX] rule=%s dir=%s entries=%d",
            str(getattr(rule, "id", "?"))[:8], direction, len(patterns),
        )
        return True, key

    async def save_longtext(
        self, rule: ForwardRule, raw: str, mode: str, uid: int, direction: str
    ) -> Tuple[bool, int]:
        """Persist a Long-Text rule onto the task policy. Returns (ok, entry_count)."""
        entries = [w for w in split_word_list(raw) if w.strip()]
        if not entries:
            return False, 0
        entries = entries[:_MAX_ENTRIES]
        cfg = self._policy_of(rule)
        key = "blacklist_words" if direction == "blacklist" else "whitelist_words"
        existing = [w for w in (cfg.get(key) or []) if w not in entries]
        cfg[key] = existing + entries
        if direction == "whitelist":
            cfg["whitelist_mode"] = _mode_label(mode)
        self._apply_policy(rule, cfg, uid)
        await self._ui._save_owned_rule(rule, uid)
        logger.info(
            "[BOT:FILTER_LONGTEXT] rule=%s dir=%s mode=%s entries=%d",
            str(getattr(rule, "id", "?"))[:8], direction, _mode_label(mode), len(entries),
        )
        return True, len(entries)

    def test(self, rule: ForwardRule, sample: str) -> MatchPreview:
        """The canonical read-only preview of this task's effective policy."""
        cfg = self._policy_of(rule)
        return preview_match(
            sample or "",
            whitelist=cfg.get("whitelist_words") or (),
            blacklist=cfg.get("blacklist_words") or (),
            mode=_mode_label(cfg.get("whitelist_mode") or "any"),
            regex_block=cfg.get("regex_block") or (),
            regex_require=cfg.get("regex_require") or (),
        )
