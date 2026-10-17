"""Every per-task setting, editable from the Telegram bot.

The dashboard and the mobile app render whatever the server's registry
describes, so a setting added there appears in both without a client release.
The bot did not work that way: its panels were hand-written, and an audit of the
registry found 26 of the 72 settings had no path in the bot at all — a user who
only has Telegram could not switch on branding triggers, a regex filter or
per-source policies at all, while the same account could do it from the web.

This module renders that same registry, with the same validation, grouped and
searchable. The curated panels stay where they are: they are the fast path for
the handful of settings people change constantly, and they already work.

Two conventions matter for correctness:

* Callback data carries a short *index* into the list the user is looking at,
  never a key name, because Telegram caps callback data at 64 bytes and a rule
  id plus a key name plus an action does not fit. The keys themselves live in
  the user's FSM buffer, which is why an index is re-validated on every tap: a
  stale button from a previous screen must be refused, not applied to whatever
  now occupies that position.
* Nothing is written directly. Every change goes through the registry's
  ``apply_settings``, so a value the API would reject is rejected here too, with
  the same message, and a rejected change never leaves the rule half-updated.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ...domain.rule_settings import (
    SETTING_SPECS,
    SettingError,
    apply_settings,
    read_settings,
)

__all__ = ["RegistryEditor", "PAGE_SIZE"]

PAGE_SIZE = 8

#: Groups are shown in this order; anything the registry adds later is appended
#: in registry order, so a new group is reachable without touching this file.
GROUP_ORDER: Sequence[str] = (
    "route",
    "branding",
    "content",
    "cleanup",
    "media",
    "sending",
    "audience",
    "keywords",
    "ai",
    "automation",
)

Row = Tuple[str, str]
Keyboard = List[List[Row]]


def _truncate(text: str, limit: int = 34) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class RegistryEditor:
    """Renders the registry and applies edits, one setting at a time."""

    def __init__(
        self,
        *,
        translate: Callable[..., str],
        save: Callable[[Any], Any],
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._t = translate
        self._save = save
        self.page_size = max(1, int(page_size))

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _keys_of(self, st: Any) -> List[str]:
        buf = getattr(st, "buffer", None)
        if not isinstance(buf, dict):
            return []
        keys = buf.get("reg_keys")
        return [str(k) for k in keys] if isinstance(keys, list) else []

    def _set_keys(self, st: Any, keys: Sequence[str], group: str = "") -> None:
        if isinstance(getattr(st, "buffer", None), dict):
            st.buffer["reg_keys"] = [str(k) for k in keys]
            st.buffer["reg_group"] = group

    def _key_at(self, st: Any, idx: int) -> Optional[str]:
        """The key a tap refers to, or ``None`` if the screen has moved on."""
        keys = self._keys_of(st)
        if idx < 0 or idx >= len(keys):
            return None
        key = keys[idx]
        return key if key in SETTING_SPECS else None

    #: Public view of what a tap refers to, for callers that need the label or
    #: the kind before deciding what to ask the user for.
    def key_at(self, st: Any, idx: int) -> Optional[str]:
        return self._key_at(st, idx)

    def spec_for(self, key: str):
        return SETTING_SPECS[key]

    def label_for(self, key: str) -> str:
        return self._label(key)

    def _values(self, rule: Any) -> Dict[str, Any]:
        return read_settings(rule)

    def _label(self, key: str) -> str:
        spec = SETTING_SPECS[key]
        return spec.label or key.replace("_", " ").title()

    def _format(self, key: str, value: Any) -> str:
        spec = SETTING_SPECS[key]
        if spec.kind == "bool":
            return self._t("ui_status_on" if value else "ui_status_off")
        if spec.kind == "list":
            items = list(value or [])
            if not items:
                return self._t("ui_reg_empty")
            return _truncate(", ".join(str(i) for i in items))
        if spec.kind == "dict":
            if not value:
                return self._t("ui_reg_empty")
            try:
                rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                rendered = str(value)
            return _truncate(rendered)
        if value in ("", None):
            return self._t("ui_reg_empty")
        return _truncate(str(value))

    def _status_mark(self, key: str, value: Any) -> str:
        """Whether this task sets the value itself or leaves it at the default."""
        default = SETTING_SPECS[key].default
        return self._t("ui_reg_mark_set") if value != default else self._t("ui_reg_mark_default")

    def _groups(self) -> List[Tuple[str, List[str]]]:
        by_group: Dict[str, List[str]] = {}
        for key, spec in SETTING_SPECS.items():
            by_group.setdefault(spec.group, []).append(key)
        ordered = [g for g in GROUP_ORDER if g in by_group]
        ordered += [g for g in by_group if g not in GROUP_ORDER]
        return [(g, by_group[g]) for g in ordered]

    def changed_count(self, rule: Any) -> int:
        values = self._values(rule)
        return sum(
            1 for key, spec in SETTING_SPECS.items() if values.get(key) != spec.default
        )

    # ------------------------------------------------------------------ #
    # screens
    # ------------------------------------------------------------------ #
    def groups(self, rule: Any, st: Any) -> Tuple[str, Keyboard]:
        values = self._values(rule)
        lines = [
            self._t("ui_reg_title"),
            "",
            self._t("ui_reg_subtitle", changed=self.changed_count(rule), total=len(SETTING_SPECS)),
            "",
        ]
        rows: Keyboard = []
        for group, keys in self._groups():
            changed = sum(
                1 for k in keys if values.get(k) != SETTING_SPECS[k].default
            )
            mark = f" • {changed}" if changed else ""
            lines.append(f"• {self._t(f'ui_reg_group_{group}')}{mark}")
            rows.append([(self._t(f"ui_reg_group_{group}"), f"rge:G:{rule.id}:{group}:0")])
        rows.append([(self._t("ui_reg_btn_search"), f"rge:s:{rule.id}")])
        rows.append([(self._t("ui_rule_back_rule"), f"radv:{rule.id}")])
        return "\n".join(lines), rows

    def group(
        self, rule: Any, st: Any, group: str, page: int = 0
    ) -> Tuple[str, Keyboard]:
        keys = [k for k, spec in SETTING_SPECS.items() if spec.group == group]
        if not keys:
            return self.groups(rule, st)
        self._set_keys(st, keys, group)
        values = self._values(rule)
        pages = max(1, (len(keys) + self.page_size - 1) // self.page_size)
        page = max(0, min(int(page or 0), pages - 1))
        start = page * self.page_size
        window = keys[start : start + self.page_size]

        lines = [
            self._t("ui_reg_group_title", group=self._t(f"ui_reg_group_{group}"),
                    count=len(keys)),
            "",
        ]
        rows: Keyboard = []
        for offset, key in enumerate(window):
            idx = start + offset
            value = values.get(key, SETTING_SPECS[key].default)
            lines.append(
                f"{self._status_mark(key, value)} **{self._label(key)}**\n"
                f"   `{self._format(key, value)}`"
            )
            rows.append([(self._label(key), f"rge:k:{rule.id}:{idx}")])
        nav: List[Row] = []
        if page > 0:
            nav.append((self._t("ui_reg_prev"), f"rge:G:{rule.id}:{group}:{page - 1}"))
        if pages > 1:
            nav.append((self._t("ui_reg_page", page=page + 1, pages=pages), f"rge:G:{rule.id}:{group}:{page}"))
        if page + 1 < pages:
            nav.append((self._t("ui_reg_next"), f"rge:G:{rule.id}:{group}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([(self._t("ui_reg_btn_groups"), f"rge:g:{rule.id}")])
        return "\n".join(lines), rows

    def detail(self, rule: Any, st: Any, idx: int) -> Tuple[str, Keyboard]:
        key = self._key_at(st, idx)
        if key is None:
            return self._stale(rule, st)
        spec = SETTING_SPECS[key]
        values = self._values(rule)
        value = values.get(key, spec.default)
        lines = [
            self._t("ui_reg_key_title", label=self._label(key)),
            "",
            f"**{self._t('ui_reg_kind')}:** `{spec.kind}`",
            f"**{self._t('ui_reg_value')}:** `{self._format(key, value)}`",
            f"**{self._t('ui_reg_default')}:** `{self._format(key, spec.default)}`",
            f"**{self._t('ui_reg_status')}:** {self._status_mark(key, value)}",
        ]
        if spec.help:
            lines += ["", self._t("ui_reg_help", help=spec.help)]
        if spec.choices:
            lines += ["", self._t("ui_reg_choices", choices=", ".join(spec.choices))]
        if spec.minimum is not None or spec.maximum is not None:
            lines += [
                "",
                self._t(
                    "ui_reg_bounds",
                    minimum="—" if spec.minimum is None else spec.minimum,
                    maximum="—" if spec.maximum is None else spec.maximum,
                ),
            ]

        group = str(getattr(st, "buffer", {}).get("reg_group") or spec.group)
        rows: Keyboard = []
        if spec.kind == "bool":
            rows.append([(self._t("ui_reg_btn_toggle"), f"rge:t:{rule.id}:{idx}")])
        elif spec.choices:
            rows.append([(self._t("ui_reg_btn_choose"), f"rge:c:{rule.id}:{idx}")])
        else:
            rows.append([(self._t("ui_reg_btn_edit"), f"rge:e:{rule.id}:{idx}")])
        if value != spec.default:
            rows.append([(self._t("ui_reg_btn_reset"), f"rge:r:{rule.id}:{idx}")])
        rows.append(
            [(self._t("ui_reg_btn_back_group"),
              f"rge:G:{rule.id}:{group}:{idx // self.page_size}")]
        )
        return "\n".join(lines), rows

    def choices(self, rule: Any, st: Any, idx: int) -> Tuple[str, Keyboard]:
        key = self._key_at(st, idx)
        if key is None:
            return self._stale(rule, st)
        spec = SETTING_SPECS[key]
        if not spec.choices:
            return self.detail(rule, st, idx)
        values = self._values(rule)
        current = values.get(key, spec.default)
        rows: Keyboard = [
            [(f"{'✅ ' if choice == current else ''}{choice}",
              f"rge:v:{rule.id}:{idx}:{pos}")]
            for pos, choice in enumerate(spec.choices)
        ]
        rows.append([(self._t("ui_reg_btn_back_key"), f"rge:k:{rule.id}:{idx}")])
        lines = [
            self._t("ui_reg_choose_title", label=self._label(key)),
            "",
            self._t("ui_reg_choose_current", value=self._format(key, current)),
        ]
        return "\n".join(lines), rows

    def search(self, rule: Any, st: Any, query: str = "", page: int = 0) -> Tuple[str, Keyboard]:
        raw = " ".join(str(query or "").split())
        if not raw:
            self._set_keys(st, [], "")
            return self.groups(rule, st)
        needle = raw.lower()
        keys = [
            key
            for key, spec in SETTING_SPECS.items()
            if needle in key.lower()
            or needle in (spec.label or "").lower()
            or needle in (spec.help or "").lower()
        ]
        self._set_keys(st, keys, "")
        if not keys:
            rows: Keyboard = [[(self._t("ui_reg_btn_search"), f"rge:s:{rule.id}")]]
            rows.append([(self._t("ui_reg_btn_groups"), f"rge:g:{rule.id}")])
            return "\n".join([self._t("ui_reg_search_title", query=raw, count=0), "",
                              self._t("ui_reg_no_match", query=raw)]), rows
        values = self._values(rule)
        pages = max(1, (len(keys) + self.page_size - 1) // self.page_size)
        page = max(0, min(int(page or 0), pages - 1))
        start = page * self.page_size
        window = keys[start : start + self.page_size]
        lines = [self._t("ui_reg_search_title", query=raw, count=len(keys)), ""]
        rows = []
        for offset, key in enumerate(window):
            idx = start + offset
            value = values.get(key, SETTING_SPECS[key].default)
            lines.append(
                f"{self._status_mark(key, value)} **{self._label(key)}**\n"
                f"   `{self._format(key, value)}`"
            )
            rows.append([(self._label(key), f"rge:k:{rule.id}:{idx}")])
        nav: List[Row] = []
        if page > 0:
            nav.append((self._t("ui_reg_prev"), f"rge:q:{rule.id}:{page - 1}"))
        if pages > 1:
            nav.append((self._t("ui_reg_page", page=page + 1, pages=pages), f"rge:q:{rule.id}:{page}"))
        if page + 1 < pages:
            nav.append((self._t("ui_reg_next"), f"rge:q:{rule.id}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([(self._t("ui_reg_btn_groups"), f"rge:g:{rule.id}")])
        return "\n".join(lines), rows

    def _stale(self, rule: Any, st: Any) -> Tuple[str, Keyboard]:
        """The screen moved on: refuse rather than apply to the wrong setting."""
        return self._t("ui_reg_stale"), [[(self._t("ui_reg_btn_groups"), f"rge:g:{rule.id}")]]

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    async def toggle(self, rule: Any, st: Any, idx: int) -> str:
        key = self._key_at(st, idx)
        if key is None:
            return self._t("ui_reg_stale")
        spec = SETTING_SPECS[key]
        if spec.kind != "bool":
            return self._t("ui_reg_not_a_switch", label=self._label(key))
        values = self._values(rule)
        return await self._write(rule, {key: not bool(values.get(key, spec.default))})

    async def set_choice(self, rule: Any, st: Any, idx: int, choice_idx: int) -> str:
        key = self._key_at(st, idx)
        if key is None:
            return self._t("ui_reg_stale")
        spec = SETTING_SPECS[key]
        if not spec.choices or choice_idx < 0 or choice_idx >= len(spec.choices):
            return self._t("ui_reg_stale")
        return await self._write(rule, {key: spec.choices[choice_idx]})

    async def set_value(self, rule: Any, st: Any, idx: int, raw: str) -> str:
        key = self._key_at(st, idx)
        if key is None:
            return self._t("ui_reg_stale")
        spec = SETTING_SPECS[key]
        value: Any = raw
        if spec.kind == "list":
            # A bare "-" is the documented way to empty a list: typing an empty
            # message is not possible in Telegram, so an explicit marker is the
            # only way to express "no entries".
            if str(raw).strip() == "-":
                value = []
            else:
                parts = [p.strip() for p in str(raw).split(",")]
                value = [p for p in parts if p]
        elif spec.kind == "dict":
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError):
                return self._t("ui_reg_invalid", reason=self._t("ui_reg_not_json"))
            if not isinstance(decoded, dict):
                return self._t("ui_reg_invalid", reason=self._t("ui_reg_not_json"))
            value = decoded
        elif spec.kind == "int":
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                return self._t("ui_reg_invalid", reason=self._t("ui_reg_not_number"))
        elif spec.kind == "float":
            try:
                value = float(str(raw).strip())
            except (TypeError, ValueError):
                return self._t("ui_reg_invalid", reason=self._t("ui_reg_not_number"))
        elif str(raw).strip() == "" and spec.kind == "str" and not spec.nullable:
            return self._t("ui_reg_invalid", reason=self._t("ui_reg_empty_input"))
        return await self._write(rule, {key: value})

    async def reset(self, rule: Any, st: Any, idx: int) -> str:
        key = self._key_at(st, idx)
        if key is None:
            return self._t("ui_reg_stale")
        spec = SETTING_SPECS[key]
        return await self._write(rule, {key: spec.default}, reset=True)

    async def _write(self, rule: Any, values: Dict[str, Any], reset: bool = False) -> str:
        """Apply through the registry, then persist. One write, one message."""
        label = next(iter(values)) if values else ""
        try:
            apply_settings(rule, values)
        except SettingError as exc:
            # The registry's own message is the useful one: it names the bound or
            # the choice that was wrong.
            return self._t("ui_reg_invalid", reason=exc.args[0] if exc.args else str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return self._t("ui_reg_invalid", reason=str(exc))
        try:
            await self._save(rule)
        except Exception as exc:
            return self._t("ui_reg_save_failed", reason=str(exc))
        if reset:
            return self._t("ui_reg_reset_done", label=self._label(label))
        return self._t(
            "ui_reg_saved", label=self._label(label),
            value=self._format(label, self._values(rule).get(label)),
        )
