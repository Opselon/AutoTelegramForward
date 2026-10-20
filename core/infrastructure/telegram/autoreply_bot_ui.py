"""Telegram-bot surface for the v28 AI auto-reply subsystem.

Kept out of :mod:`.pro_bot_ui` (12k lines) as a mixin so the panel, its
callbacks and its three text steps live together. ``ProBotUI`` inherits it and
calls :meth:`register_autoreply_handlers` once, from ``_register``.

The Telegram panel is deliberately the shortest path to the same state the web
dashboard edits: master switch, target mode, default prompt, prompt library,
contact switches, rule switches and the activity list. Everything it writes
goes through ``AutoReplyService``, so the two surfaces cannot diverge.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

logger = logging.getLogger("atf.autoreply.botui")

#: How many rows a phone-sized panel shows before it is a wall of text.
_PAGE = 8


def _job_time(ts: int) -> str:
    if not ts:
        return "—"
    import time as _time

    return _time.strftime("%m-%d %H:%M", _time.localtime(ts))


class AutoReplyBotMixin:
    """Renderers, callbacks and FSM steps for the auto-reply panel."""

    # -- helpers ---------------------------------------------------------
    @property
    def _ar(self) -> Any:
        """The service the controller bot and gRPC share, or None."""
        return getattr(self, "_autoreply", None)

    def _ar_lang(self, uid: int) -> str:
        try:
            return self._lang_for(uid=uid) or "en"
        except Exception:
            return "en"

    def _ar_owner(self, uid: int) -> int:
        """The tenant the caller's panel writes to.

        A web account and a Telegram account are the same tenant, keyed by the
        Telegram user id; an unlinked user (0) owns nothing and is refused by
        the service rather than given a default account's settings.
        """
        return int(uid or 0)

    # -- menu ------------------------------------------------------------
    def _render_ar_menu(self, uid: int = 0, lang: Optional[str] = None
                        ) -> Tuple[str, InlineKeyboardMarkup]:
        """The panel root: status, provider, prompts, contacts, activity."""
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._ar
        if service is None:
            return _t("ui_ar_not_loaded"), self._kbd([[(self._btn("ui_back"), "pv:menu")]])
        owner = self._ar_owner(uid)
        try:
            settings = service.get_settings(owner)
            prompts = service.list_prompts(owner)
            targets = service.list_targets(owner)
            stats = service.stats(owner)
        except Exception as exc:
            logger.warning("autoreply menu failed for %s: %s", owner, exc)
            return _t("ui_ar_not_loaded"), self._kbd([[(self._btn("ui_back"), "pv:menu")]])

        status = _t("ui_ar_status_on") if settings.enabled else _t("ui_ar_status_off")
        mode = _t(f"ui_ar_mode_{str(settings.target_mode.value).lower()}")
        default_prompt = next((p.name for p in prompts if p.id == settings.prompt_id), "")
        provider = settings.ai_config_id or _t("ui_ar_none")
        enabled_contacts = sum(1 for t in targets if t.is_enabled)
        jobs = stats.get("jobs", {}) or {}

        lines = [
            _t("ui_ar_title"),
            "",
            _t("ui_ar_line_status", status=status, mode=mode),
            _t("ui_ar_line_provider", provider=provider,
               language=(settings.language or _t("ui_ar_auto"))),
            _t("ui_ar_line_prompts", count=len(prompts),
               name=(default_prompt or _t("ui_ar_builtin"))),
            _t("ui_ar_line_contacts", enabled=enabled_contacts, total=len(targets)),
            _t("ui_ar_line_activity", sent=stats.get("sent_24h", 0),
               pending=int(jobs.get("PENDING", 0)) + int(jobs.get("RETRY_WAIT", 0)),
               dead=int(jobs.get("DEAD_LETTER", 0))),
        ]
        if not settings.ai_config_id:
            lines += ["", _t("ui_ar_need_provider")]

        toggle = _t("ui_ar_btn_turn_off") if settings.enabled else _t("ui_ar_btn_turn_on")
        rows: List[list] = [
            [(toggle, "ar:toggle")],
            [(_t("ui_ar_btn_modes"), "ar:modes")],
            [(_t("ui_ar_btn_prompts"), "ar:prompts"),
             (_t("ui_ar_btn_contacts"), "ar:contacts")],
            [(_t("ui_ar_btn_rules"), "ar:rules"),
             (_t("ui_ar_btn_activity"), "ar:activity")],
            [(self._btn("ui_back"), "pv:menu")],
        ]
        return "\n".join(lines), self._kbd(rows)

    def _render_ar_modes(self, uid: int = 0, lang: Optional[str] = None
                         ) -> Tuple[str, InlineKeyboardMarkup]:
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        rows = [
            [(_t("ui_ar_mode_everyone"), "ar:mode:EVERYONE")],
            [(_t("ui_ar_mode_selected_only"), "ar:mode:SELECTED_ONLY")],
            [(_t("ui_ar_mode_all_except"), "ar:mode:ALL_EXCEPT")],
            [(_t("ui_ar_mode_manual"), "ar:mode:MANUAL")],
            [(self._btn("ui_back"), "ar:menu")],
        ]
        return _t("ui_ar_modes_title"), self._kbd(rows)

    def _render_ar_prompts(self, uid: int = 0, lang: Optional[str] = None
                           ) -> Tuple[str, InlineKeyboardMarkup]:
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._ar
        owner = self._ar_owner(uid)
        rows: List[list] = []
        try:
            settings = service.get_settings(owner)
            prompts = service.list_prompts(owner, include_archived=True)
        except Exception:
            return _t("ui_ar_not_loaded"), self._kbd([[(self._btn("ui_back"), "ar:menu")]])
        if not prompts:
            body = _t("ui_ar_prompts_empty")
        else:
            body = _t("ui_ar_prompts_title")
            for p in prompts[:_PAGE]:
                mark = "✅ " if p.id == settings.prompt_id else ""
                rows.append([(f"{mark}{p.name}"[:60], f"ar:p:{p.id}")])
            if settings.prompt_id:
                rows.append([(_t("ui_ar_btn_no_prompt"), "ar:pnone")])
            rows.append([(_t("ui_ar_btn_duplicate"), "ar:pdup")])
        rows.append([(_t("ui_ar_btn_new_prompt"), "ar:pnew")])
        rows.append([(self._btn("ui_back"), "ar:menu")])
        return body, self._kbd(rows)

    def _render_ar_contacts(self, uid: int = 0, lang: Optional[str] = None
                            ) -> Tuple[str, InlineKeyboardMarkup]:
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._ar
        owner = self._ar_owner(uid)
        try:
            settings = service.get_settings(owner)
            targets = service.list_targets(owner)
        except Exception:
            return _t("ui_ar_not_loaded"), self._kbd([[(self._btn("ui_back"), "ar:menu")]])
        rows: List[list] = []
        if targets:
            body = _t("ui_ar_contacts_title",
                      mode=_t(f"ui_ar_mode_{str(settings.target_mode.value).lower()}"),
                      enabled=sum(1 for t in targets if t.is_enabled), total=len(targets))
            for t in targets[:_PAGE]:
                label = ("✅ " if t.is_enabled else "⬜️ ") + (t.title or t.chat_id)
                rows.append([(label[:60], f"ar:t:{t.chat_id}")])
        else:
            body = _t("ui_ar_contacts_empty")
        rows.append([(_t("ui_ar_btn_add_contact"), "ar:cadd")])
        rows.append([(self._btn("ui_back"), "ar:menu")])
        return body, self._kbd(rows)

    def _render_ar_rules(self, uid: int = 0, lang: Optional[str] = None
                         ) -> Tuple[str, InlineKeyboardMarkup]:
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._ar
        owner = self._ar_owner(uid)
        try:
            rules = service.list_rules(owner)
        except Exception:
            return _t("ui_ar_not_loaded"), self._kbd([[(self._btn("ui_back"), "ar:menu")]])
        rows: List[list] = []
        if rules:
            body = _t("ui_ar_rules_title", count=len(rules))
            for r in rules[:_PAGE]:
                mark = "✅ " if r.is_enabled else "⬜️ "
                rows.append([(f"{mark}{r.name} · {r.priority}"[:60], f"ar:rt:{r.id}")])
                rows.append([(_t("ui_ar_btn_delete", name=r.name[:32]), f"ar:rdel:{r.id}")])
        else:
            body = _t("ui_ar_rules_empty")
        rows.append([(self._btn("ui_back"), "ar:menu")])
        return body, self._kbd(rows)

    def _render_ar_activity(self, uid: int = 0, lang: Optional[str] = None
                            ) -> Tuple[str, InlineKeyboardMarkup]:
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._ar
        owner = self._ar_owner(uid)
        try:
            stats = service.stats(owner)
            jobs, total = service.list_jobs(owner, limit=_PAGE)
        except Exception:
            return _t("ui_ar_not_loaded"), self._kbd([[(self._btn("ui_back"), "ar:menu")]])
        rows: List[list] = []
        if jobs:
            header = _t("ui_ar_activity_title", sent=stats.get("sent_24h", 0),
                        total=total)
            lines = [header, ""]
            for j in jobs:
                status = str(getattr(j.status, "value", j.status))
                lines.append(_t("ui_ar_job_line", status=status,
                                chat=(j.chat_title or j.chat_id)[:24],
                                time=_job_time(j.sent_at or j.created_at)))
                if status in ("DEAD_LETTER", "FAILED"):
                    rows.append([(_t("ui_ar_btn_retry", chat=(j.chat_title or j.chat_id)[:24]),
                                  f"ar:jr:{j.id}")])
            body = "\n".join(lines)
        else:
            body = _t("ui_ar_activity_empty")
        rows.append([(self._btn("ui_back"), "ar:menu")])
        return body, self._kbd(rows)

    # -- callbacks -------------------------------------------------------
    def register_autoreply_handlers(self) -> None:
        b = self.bot

        async def _render(cq: CallbackQuery, owner: int, renderer) -> None:
            lang = await self._get_user_lang(owner)
            text, kbd = renderer(owner, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ar:menu$"))
        @self._localized_handler
        async def _cb_ar_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            await _render(cq, uid, self._render_ar_menu)

        @b.on_callback_query(filters.regex(r"^ar:toggle$"))
        @self._localized_handler
        async def _cb_ar_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer(self._t("ui_invalid_user_id"), show_alert=True)
            service = self._ar
            if service is None:
                return await cq.answer(self._t("ui_ar_not_loaded"), show_alert=True)
            settings = service.get_settings(uid)
            try:
                service.update_settings(uid, {"enabled": not settings.enabled})
            except Exception as exc:
                logger.warning("autoreply toggle failed: %s", exc)
                return await cq.answer(self._t("ui_internal_error"), show_alert=True)
            key = "ui_ar_enabled_alert" if not settings.enabled else "ui_ar_disabled_alert"
            await cq.answer(self._t(key))
            await _render(cq, uid, self._render_ar_menu)

        @b.on_callback_query(filters.regex(r"^ar:modes$"))
        @self._localized_handler
        async def _cb_ar_modes(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            await _render(cq, uid, self._render_ar_modes)

        @b.on_callback_query(filters.regex(r"^ar:mode:([A-Z_]+)$"))
        @self._localized_handler
        async def _cb_ar_mode(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            mode = (cq.data or "").split(":", 2)[2]
            service = self._ar
            if service is None or not uid:
                return await cq.answer(self._t("ui_ar_not_loaded"), show_alert=True)
            try:
                saved = service.update_settings(uid, {"target_mode": mode})
            except Exception as exc:
                logger.warning("autoreply mode change failed: %s", exc)
                return await cq.answer(self._t("ui_internal_error"), show_alert=True)
            lang = await self._get_user_lang(uid)
            await cq.answer(self._t("ui_ar_mode_set", lang=lang,
                                    mode=self._t(f"ui_ar_mode_{saved.target_mode.value.lower()}",
                                                 lang=lang)))
            await _render(cq, uid, self._render_ar_menu)

        @b.on_callback_query(filters.regex(r"^ar:prompts$"))
        @self._localized_handler
        async def _cb_ar_prompts(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            await _render(cq, uid, self._render_ar_prompts)

        @b.on_callback_query(filters.regex(r"^ar:p:([0-9a-zA-Z-]+)$"))
        @self._localized_handler
        async def _cb_ar_pick_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            prompt_id = (cq.data or "").split(":", 2)[2]
            service = self._ar
            if service is None or not uid:
                return await cq.answer(self._t("ui_ar_not_loaded"), show_alert=True)
            try:
                prompt = service.set_default_prompt(uid, prompt_id)
                service.update_settings(uid, {"prompt_id": prompt.id})
            except Exception as exc:
                logger.warning("autoreply pick prompt failed: %s", exc)
                return await cq.answer(self._t("ui_internal_error"), show_alert=True)
            await cq.answer(self._t("ui_ar_prompt_default", name=prompt.name[:40]))
            await _render(cq, uid, self._render_ar_prompts)

        @b.on_callback_query(filters.regex(r"^ar:pnone$"))
        @self._localized_handler
        async def _cb_ar_prompt_none(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            service = self._ar
            if service is None or not uid:
                return await cq.answer(self._t("ui_ar_not_loaded"), show_alert=True)
            try:
                service.update_settings(uid, {"prompt_id": ""})
            except Exception as exc:
                return await cq.answer(str(exc)[:60], show_alert=True)
            await cq.answer(self._t("ui_ar_prompt_builtin"))
            await _render(cq, uid, self._render_ar_prompts)

        @b.on_callback_query(filters.regex(r"^ar:pdup$"))
        @self._localized_handler
        async def _cb_ar_duplicate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            service = self._ar
            if service is None or not uid:
                return await cq.answer(self._t("ui_ar_not_loaded"), show_alert=True)
            try:
                settings = service.get_settings(uid)
                if not settings.prompt_id:
                    return await cq.answer(self._t("ui_ar_prompt_pick_first"), show_alert=True)
                clone = service.duplicate_prompt(uid, settings.prompt_id)
            except Exception as exc:
                logger.warning("autoreply duplicate failed: %s", exc)
                return await cq.answer(self._t("ui_internal_error"), show_alert=True)
            await cq.answer(self._t("ui_ar_prompt_duplicated", name=clone.name[:40]))
            await _render(cq, uid, self._render_ar_prompts)

        @b.on_callback_query(filters.regex(r"^ar:pnew$"))
        @self._localized_handler
        async def _cb_ar_new_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer(self._t("ui_invalid_user_id"), show_alert=True)
            st = self._state(uid)
            st.step = "ar_prompt_name"
            await self._persist(uid, st)
            await cq.answer()
            await self._safe_edit(
                cq, self._t("ui_ar_prompt_ask_name"),
                reply_markup=self._kbd([[(self._btn("ui_cancel"), "ar:prompts")]]),
            )

        @b.on_callback_query(filters.regex(r"^ar:contacts$"))
        @self._localized_handler
        async def _cb_ar_contacts(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            await _render(cq, uid, self._render_ar_contacts)

        @b.on_callback_query(filters.regex(r"^ar:t:(-?\d+)$"))
        @self._localized_handler
        async def _cb_ar_toggle_contact(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            chat_id = (cq.data or "").split(":", 2)[2]
            service = self._ar
            if service is None or not uid:
                return await cq.answer(self._t("ui_ar_not_loaded"), show_alert=True)
            try:
                targets = service.list_targets(uid)
                current = next((t for t in targets if t.chat_id == chat_id), None)
                if current is None:
                    return await cq.answer(self._t("ui_ar_contact_missing"), show_alert=True)
                service.set_target_enabled(uid, chat_id, not current.is_enabled)
            except Exception as exc:
                logger.warning("autoreply contact toggle failed: %s", exc)
                return await cq.answer(self._t("ui_internal_error"), show_alert=True)
            await cq.answer(self._t("ui_ar_saved"))
            await _render(cq, uid, self._render_ar_contacts)

        @b.on_callback_query(filters.regex(r"^ar:cadd$"))
        @self._localized_handler
        async def _cb_ar_add_contact(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer(self._t("ui_invalid_user_id"), show_alert=True)
            st = self._state(uid)
            st.step = "ar_contact_add"
            await self._persist(uid, st)
            await cq.answer()
            await self._safe_edit(
                cq, self._t("ui_ar_contact_ask"),
                reply_markup=self._kbd([[(self._btn("ui_cancel"), "ar:contacts")]]),
            )

        @b.on_callback_query(filters.regex(r"^ar:rules$"))
        @self._localized_handler
        async def _cb_ar_rules(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            await _render(cq, uid, self._render_ar_rules)

        @b.on_callback_query(filters.regex(r"^ar:rt:([0-9a-zA-Z-]+)$"))
        @self._localized_handler
        async def _cb_ar_toggle_rule(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            rule_id = (cq.data or "").split(":", 2)[2]
            service = self._ar
            if service is None or not uid:
                return await cq.answer(self._t("ui_ar_not_loaded"), show_alert=True)
            try:
                rule = service.get_rule(uid, rule_id)
                service.save_rule(uid, {"id": rule.id, "name": rule.name,
                                        "is_enabled": not rule.is_enabled,
                                        "priority": rule.priority,
                                        "action": rule.action.value,
                                        "conditions": rule.conditions,
                                        "exclusions": rule.exclusions,
                                        "prompt_id": rule.prompt_id or "",
                                        "ai_config_id": rule.ai_config_id or "",
                                        "cooldown_seconds": rule.cooldown_seconds,
                                        "language": rule.language,
                                        "tone": rule.tone.value if rule.tone else "",
                                        "response_length": (rule.response_length.value
                                                            if rule.response_length else "")})
            except Exception as exc:
                logger.warning("autoreply rule toggle failed: %s", exc)
                return await cq.answer(self._t("ui_internal_error"), show_alert=True)
            await cq.answer(self._t("ui_ar_saved"))
            await _render(cq, uid, self._render_ar_rules)

        @b.on_callback_query(filters.regex(r"^ar:rdel:([0-9a-zA-Z-]+)$"))
        @self._localized_handler
        async def _cb_ar_delete_rule(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            rule_id = (cq.data or "").split(":", 2)[2]
            service = self._ar
            if service is None or not uid:
                return await cq.answer(self._t("ui_ar_not_loaded"), show_alert=True)
            try:
                service.delete_rule(uid, rule_id)
            except Exception as exc:
                return await cq.answer(str(exc)[:60], show_alert=True)
            await cq.answer(self._t("ui_ar_deleted"))
            await _render(cq, uid, self._render_ar_rules)

        @b.on_callback_query(filters.regex(r"^ar:activity$"))
        @self._localized_handler
        async def _cb_ar_activity(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            await _render(cq, uid, self._render_ar_activity)

        @b.on_callback_query(filters.regex(r"^ar:jr:([0-9a-zA-Z-]+)$"))
        @self._localized_handler
        async def _cb_ar_retry(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            job_id = (cq.data or "").split(":", 2)[2]
            service = self._ar
            if service is None or not uid:
                return await cq.answer(self._t("ui_ar_not_loaded"), show_alert=True)
            try:
                service.retry_job(uid, job_id)
            except Exception as exc:
                return await cq.answer(str(exc)[:60], show_alert=True)
            await cq.answer(self._t("ui_ar_job_retried"))
            await _render(cq, uid, self._render_ar_activity)

    # -- text steps ------------------------------------------------------
    async def ar_route_text(self, message: Message, st: Any, text: str) -> bool:
        """Handle the panel's three text inputs. Returns True when consumed."""
        uid = message.from_user.id if message.from_user else 0
        step = getattr(st, "step", "") or ""
        service = self._ar
        if not step.startswith("ar_"):
            return False
        if service is None:
            await message.reply_text(self._t("ui_ar_not_loaded"))
            return True
        lang = await self._get_user_lang(uid)

        if step == "ar_prompt_name":
            name = text.strip()[:80]
            if not name:
                await message.reply_text(self._t("ui_ar_prompt_ask_name", lang=lang))
                return True
            st.step = "ar_prompt_text"
            st.buffer["ar_prompt_name"] = name
            await self._persist(uid, st)
            await message.reply_text(
                self._t("ui_ar_prompt_ask_text", lang=lang, name=name),
                reply_markup=self._kbd([[(self._btn("ui_cancel", lang=lang), "ar:prompts")]]),
            )
            return True

        if step == "ar_prompt_text":
            name = str(st.buffer.get("ar_prompt_name") or "My prompt")
            body = text.strip()
            if len(body) < 10:
                await message.reply_text(self._t("ui_ar_prompt_too_short", lang=lang))
                return True
            try:
                prompt = service.save_prompt(uid, {
                    "name": name, "system_prompt": body,
                    "user_prompt_template": "{text}",
                })
                service.update_settings(uid, {"prompt_id": prompt.id})
            except Exception as exc:
                await message.reply_text(f"{self._t('ui_internal_error', lang=lang)}\n{str(exc)[:120]}")
                return True
            st.step = ""
            st.buffer.pop("ar_prompt_name", None)
            await self._persist(uid, st)
            text_out, kbd = self._render_ar_prompts(uid, lang=lang)
            await message.reply_text(
                self._t("ui_ar_prompt_created", lang=lang, name=prompt.name) + "\n\n" + text_out,
                reply_markup=kbd,
            )
            return True

        if step == "ar_contact_add":
            query = text.strip()
            if not query:
                await message.reply_text(self._t("ui_ar_contact_ask", lang=lang))
                return True
            session_id = ""
            try:
                sessions = await self._sessions.list_by_owner(uid) if self._sessions else []
                session_id = sessions[0].id if sessions else ""
            except Exception:
                session_id = ""
            try:
                found = await service.resolve_chat(uid, query, session_id=session_id)
            except Exception as exc:
                found = None
                logger.info("autoreply resolve failed: %s", exc)
            if not found:
                await message.reply_text(self._t("ui_ar_contact_not_found", lang=lang, query=query[:40]))
                return True
            try:
                service.add_targets(uid, [{
                    "chat_id": found.get("chat_id"), "title": found.get("title") or query,
                    "username": found.get("username") or "",
                }])
                # Adding a contact in a list-based mode is only useful if that
                # mode is on: switch to SELECTED_ONLY when the account is in
                # EVERYONE, where a new row would otherwise do nothing visible.
                settings = service.get_settings(uid)
                if settings.target_mode.value == "EVERYONE":
                    service.update_settings(uid, {"target_mode": "ALL_EXCEPT"})
            except Exception as exc:
                await message.reply_text(f"{self._t('ui_internal_error', lang=lang)}\n{str(exc)[:120]}")
                return True
            st.step = ""
            await self._persist(uid, st)
            text_out, kbd = self._render_ar_contacts(uid, lang=lang)
            await message.reply_text(
                self._t("ui_ar_contact_added", lang=lang,
                        title=(found.get("title") or query)[:60]) + "\n\n" + text_out,
                reply_markup=kbd,
            )
            return True

        return False


__all__ = ["AutoReplyBotMixin"]
