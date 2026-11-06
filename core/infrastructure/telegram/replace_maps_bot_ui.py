"""Per-task link & @mention replace maps — "replace any link / any @ID".

What this file is
-----------------
The Telegram face of two task settings the Replace engine applies in its
final pass (after the AI stage, so a rewrite cannot resurrect a replaced
value):

* ``link_replace_enabled`` + ``link_rewrite_map`` — "replace any link found":
  every link the text carries is rewritten through the map. A ``*`` key
  rewrites every link to one target, a domain key rewrites that domain, and
  a key carrying a path rewrites the URLs it prefixes.
* ``mention_rewrite_map`` — "replace any @ID": ``@old -> @new`` pairs, with
  the same ``*`` catch-all.

Both settings are registry settings (``core/domain/rule_settings.py``), so
the dashboard's settings panel and the mobile client render them
generically; this panel is the bot's dedicated editor, because pasting a
JSON object into the generic registry editor is not how anyone wants to
type five pairs on a phone.

The screen
----------
Opened from the smart menu's "🔁 Link & @replace" button:

    🔁 Replace links & @mentions
    Task: My Shop
    🔗 Link replace: ON — links are rewritten through the map
    🔗 Link map (2): old.com→new.com, *→https://t.me/me
    ❕ Mention map (1): @oldshop→@newshop

    [🔗 Replace links: ON]  [🔗 Edit link map]
    [❕ Edit @mention map]

Editing is one typed screen: send the pairs one per line (``old new`` or
``old=new``), or ``-`` to clear. Every pair is parsed the way the route
lists are — by the engine's own normalizer, so what you save is what runs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, Message

from ...domain.text_replace import normalize_mention_map

logger = logging.getLogger("atf.rmap.botui")

CB_PREFIX = "rmap"

#: Steps this panel owns. Also listed in ``ui_state_manager.UI_STEPS``.
STEP_LINKS = "rmap_links_input"
STEP_MENTIONS = "rmap_mentions_input"

RMAP_STEPS = (STEP_LINKS, STEP_MENTIONS)

BUFFER_FIELD = "rmap_field"

#: Per-line separators accepted in a typed pair, in precedence order.
PAIR_SEPARATORS = ("=", "\t", " ")

#: Bounds mirrored from the text-replace engine: a map is a handful of
#: hand-typed pairs, not a database.
MAX_PAIRS = 100
MAX_VALUE_LEN = 500


def _parse_pairs(text: str) -> Tuple[Dict[str, str], int]:
    """Parse typed ``old new`` lines into a map; returns (map, skipped).

    Accepts ``old=new``, tab- or space-separated pairs, and strips the
    leading ``@`` the user may or may not type on mention handles (the
    engine's normalizer re-applies it). A line without two parts is skipped
    and counted — never fatal, never silent: the count comes back to the
    user.
    """
    out: Dict[str, str] = {}
    skipped = 0
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "-":
            continue
        for sep in PAIR_SEPARATORS:
            if sep in line:
                old, _, new = line.partition(sep)
                old = old.strip().strip("`")
                new = new.strip().strip("`")
                if old and new and len(old) <= MAX_VALUE_LEN and len(new) <= MAX_VALUE_LEN:
                    out[old] = new
                else:
                    skipped += 1
                break
        else:
            skipped += 1
        if len(out) >= MAX_PAIRS:
            break
    return out, skipped


def _format_pairs(mapping: Dict[str, str], limit: int = 6) -> str:
    """A compact one-line preview of a map, longest pairs first."""
    if not mapping:
        return ""
    items = list(mapping.items())
    shown = [f"{k}→{v}" for k, v in items[:limit]]
    more = len(items) - len(shown)
    body = ", ".join(shown)
    if more > 0:
        body += f" (+{more})"
    return body.replace("`", "'")[:300]


class ReplaceMapsBotMixin:
    """Renderers, callbacks and FSM steps for the link/@mention map editor."""

    def _rmap_lang(self, uid: int) -> str:
        try:
            return self._lang_for(uid=uid) or "en"
        except Exception:
            return "en"

    def _rmap_clean(self, values: Dict[str, str]) -> Dict[str, str]:
        """Strip empty rows the typed screen may leave behind."""
        return {k: v for k, v in (values or {}).items() if str(k).strip() and str(v).strip()}

    # -- registration -----------------------------------------------------
    def register_replace_maps_handlers(self) -> None:
        """Mount the panel's callbacks. Called once from ``_register``."""
        b = self.bot

        @b.on_callback_query(filters.regex(r"^rmap:(lt|le|me|open):[\w-]+$"))
        @self._localized_handler
        async def _cb_rmap(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer()
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            _, verb, rule_id = raw.split(":", 2)
            async with self._user_lock(uid):
                await self._rmap_dispatch(cq, uid, verb, rule_id)

    # -- dispatch ---------------------------------------------------------
    async def _rmap_dispatch(self, cq: CallbackQuery, uid: int, verb: str, rule_id: str) -> None:
        lang = self._rmap_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        st = self._state(uid)

        # A callback that renders a screen disarms this panel's own armed
        # step, so a tap can never race the answer to a prompt.
        if verb in ("open", "lt"):
            if str(getattr(st, "step", "") or "") in RMAP_STEPS:
                st.step = ""
                st.buffer.pop(BUFFER_FIELD, None)
                await self._persist(uid, st)

        rule = await self._owned_rule(rule_id, uid)
        if rule is None:
            return await cq.answer(_t("ui_rule_not_found"), show_alert=True)

        if verb == "open":
            await cq.answer()
            return await self._rmap_render(cq, uid, rule)

        if verb == "lt":
            rule.link_replace_enabled = not bool(getattr(rule, "link_replace_enabled", False))
            await self._save_owned_rule(rule, uid)
            await cq.answer(_t("ui_rmp_toggle_on" if rule.link_replace_enabled else "ui_rmp_toggle_off"))
            return await self._rmap_render(cq, uid, rule)

        if verb == "le":
            st.buffer[BUFFER_FIELD] = "link"
            st.buffer["rmap_rule_id"] = rule.id
            st.step = STEP_LINKS
            await self._persist(uid, st)
            await cq.answer()
            return await self._safe_edit(
                cq, _t("ui_rmp_links_prompt"),
                reply_markup=self._kbd([[(self._btn("ui_cancel_back", lang=lang), f"rmap:open:{rule.id}")]]),
            )

        if verb == "me":
            st.buffer[BUFFER_FIELD] = "mention"
            st.buffer["rmap_rule_id"] = rule.id
            st.step = STEP_MENTIONS
            await self._persist(uid, st)
            await cq.answer()
            return await self._safe_edit(
                cq, _t("ui_rmp_mentions_prompt"),
                reply_markup=self._kbd([[(self._btn("ui_cancel_back", lang=lang), f"rmap:open:{rule.id}")]]),
            )

        return await cq.answer()

    # -- render -----------------------------------------------------------
    async def _rmap_render(self, cq: CallbackQuery, uid: int, rule: Any) -> None:
        lang = self._rmap_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731

        link_map = dict(getattr(rule, "link_rewrite_map", None) or {})
        mention_map = normalize_mention_map(getattr(rule, "mention_rewrite_map", None) or {})
        link_on = bool(getattr(rule, "link_replace_enabled", False))

        lines = [
            _t("ui_rmp_title"),
            _t("ui_rmp_rule", name=(getattr(rule, "name", "") or rule.id[:8]).replace("`", "'")[:40]),
            "",
            _t("ui_rmp_link_state_on" if link_on else "ui_rmp_link_state_off"),
        ]
        if link_map:
            lines.append(_t("ui_rmp_link_map", count=len(link_map), pairs=_format_pairs(link_map)))
        else:
            lines.append(_t("ui_rmp_link_map_empty"))
        if mention_map:
            lines.append(_t("ui_rmp_mention_map", count=len(mention_map), pairs=_format_pairs(mention_map)))
        else:
            lines.append(_t("ui_rmp_mention_map_empty"))
        if not link_on and link_map:
            lines.append("")
            lines.append(_t("ui_rmp_off_hint"))

        toggle_label = "ui_rmp_btn_links_on" if link_on else "ui_rmp_btn_links_off"
        rows: List[List[Tuple[str, str]]] = [
            [(self._btn(toggle_label, lang=lang), f"rmap:lt:{rule.id}")],
            [
                (self._btn("ui_rmp_btn_edit_links", lang=lang), f"rmap:le:{rule.id}"),
                (self._btn("ui_rmp_btn_edit_mentions", lang=lang), f"rmap:me:{rule.id}"),
            ],
            [(self._btn("ui_rule_back_rule", lang=lang), f"rd:{rule.id}")],
        ]
        await self._safe_edit(cq, "\n".join(lines), reply_markup=self._kbd(rows))

    # -- FSM steps --------------------------------------------------------
    async def _step_rmap_links_input(self, message: Message, st: Any, text: str) -> None:
        await self._rmap_apply_input(message, st, text, "link")

    async def _step_rmap_mentions_input(self, message: Message, st: Any, text: str) -> None:
        await self._rmap_apply_input(message, st, text, "mention")

    async def _rmap_apply_input(self, message: Message, st: Any, text: str, field: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._rmap_lang(uid)
        # The rule id is re-derived from the armed screen's stored target, not
        # from the message: the wire never chooses the write target.
        rule_id = str(st.buffer.get("rmap_rule_id") or "") if isinstance(st.buffer, dict) else ""
        st.step = ""
        st.buffer.pop("rmap_rule_id", None)
        st.buffer.pop(BUFFER_FIELD, None)
        await self._persist(uid, st)
        rule = await self._owned_rule(rule_id, uid)
        if rule is None:
            return await message.reply_text(self._t("ui_rule_not_found", lang=lang))

        raw = (text or "").strip()
        if raw == "-":
            pairs: Dict[str, str] = {}
            skipped = 0
        else:
            pairs, skipped = _parse_pairs(raw)

        if field == "link":
            rule.link_rewrite_map = self._rmap_clean(pairs)
        else:
            rule.mention_rewrite_map = self._rmap_clean(pairs)
        await self._save_owned_rule(rule, uid)

        if not pairs:
            body = self._t("ui_rmp_cleared", lang=lang)
        elif skipped:
            body = self._t("ui_rmp_skipped", lang=lang, count=len(pairs), skipped=skipped)
        else:
            body = self._t("ui_rmp_saved", lang=lang, count=len(pairs))
        await message.reply_text(
            body,
            reply_markup=self._kbd([[(self._btn("ui_rule_back_rule", lang=lang), f"rd:{rule.id}")]]),
        )


__all__ = ["ReplaceMapsBotMixin", "RMAP_STEPS", "BUFFER_FIELD"]
