"""The global prompt-template manager — the bot face of the AI prompt library.

What this file is
-----------------
The Telegram face of :class:`core.application.prompt_service.PromptService`.
It renders and routes; every rule it applies ("what a valid prompt is",
"who may touch it", "what disabling means") is that service's, which the
dashboard and the mobile app also reach through gRPC. There is no second
implementation here.

Until this panel existed the callback verbs were declared in the bot's
callback map with no handlers attached — the per-task prompt *picker* could
create a template, but the library itself (rename, edit wording, delete,
enable/disable, test) was unreachable from a phone. This mixin closes that:
``AI → 🧠 Prompts`` opens one list where every template can be managed.

The screens
-----------
``pl`` opens a paginated-free list (template counts are small):

    🧠 Prompt templates — 3 · 🟢 2
    Tap a prompt to manage it.

    🟢 Professional Clean · shared
    🔴 My Shop Translator

Every row opens the detail screen, where the whole set of actions lives:
enable/disable, rename, edit the system prompt / user template / description,
test against a real provider, delete (with confirmation). A disabled template
stays in the library — tasks pointing at it are rewritten with their AI
config's own prompt — so disabling is the safe "park it" switch, and
re-enabling restores exactly what was there.

System prompts (``is_system``) are shared by every account on the
installation: they can be viewed and tested here, but editing, disabling and
deleting are refused — a per-tenant toggle on a shared row would silently
change every other tenant's deliveries.

Callback data
-------------
``p<verb>[:<template id>]`` — the same verbs the callback map already
declared (``p_list``/``p_new``/``p_edit``/``p_del``/``p_test``), plus the
edit arms. Template ids are 16 hex characters, so the longest payload
(``pedesc:<id>``) is 23 bytes — far inside Telegram's 64-byte limit.

Panel state (which template an armed step is editing) lives in the caller's
``UiState.buffer``, never in callback data: it is per-user, it survives a
restart, and a callback can never choose the write target for a message.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

logger = logging.getLogger("atf.prompt.botui")

#: The name-length rule the service enforces (mirrored here so the user gets
#: a localized refusal instead of an English exception text).
MAX_NAME_LEN = 80

#: System/user prompt ceilings, mirrored from the service's validate_prompt.
MAX_SYSTEM_LEN = 8000
MAX_USER_LEN = 4000

#: The text-placeholder every user template must carry.
TEXT_PLACEHOLDER = "{text}"

#: Detail previews are clipped so one long prompt cannot push the buttons
#: off the screen.
PREVIEW_LEN = 350

STEP_NEW_NAME = "prompt_new_name"
STEP_NEW_SYS = "prompt_new_sys"
STEP_NEW_USER = "prompt_new_user"
STEP_TEST = "prompt_test_sample"
STEP_EDIT_NAME = "prompt_edit_name"
STEP_EDIT_SYS = "prompt_edit_sys"
STEP_EDIT_USER = "prompt_edit_user"
STEP_EDIT_DESC = "prompt_edit_desc"

#: The steps this panel owns. Every one is also listed in
#: ``ui_state_manager.UI_STEPS``: a step that is not there is coerced to ""
#: on persist, and the user's next message falls through to the main menu —
#: the flow looks broken even though its handler exists.
PROMPT_STEPS = (
    STEP_NEW_NAME, STEP_NEW_SYS, STEP_NEW_USER, STEP_TEST,
    STEP_EDIT_NAME, STEP_EDIT_SYS, STEP_EDIT_USER, STEP_EDIT_DESC,
)

#: The buffer keys the armed steps read. Cleared when a step settles so a
#: stale id can never outlive its flow.
BUFFER_TARGET = "prompt_target_id"
BUFFER_NEW_NAME = "prompt_new_name_value"

#: The main-menu callback data token, spelled locally so the module stays
#: importable in isolation.
CB_MAIN = "m"

#: The prompt-manager "test this template" verb. Spelled as a constant: the
#: literal `"pt"` collides with the Portuguese tag in the language-tag audit,
#: and `verb == "pt"` would read as a language comparison anyway.
CB_PRM_TEST = "pt"


def _cb(verb: str, template_id: str = "") -> str:
    return f"{verb}:{template_id}" if template_id else verb


class PromptBotMixin:
    """Renderers, callbacks and FSM steps for the prompt-template manager."""

    # -- helpers ---------------------------------------------------------
    @property
    def _prm(self) -> Any:
        return getattr(self, "_prompts", None)

    def _prm_lang(self, uid: int) -> str:
        try:
            return self._lang_for(uid=uid) or "en"
        except Exception:
            return "en"

    @staticmethod
    def _preview(text: str) -> str:
        body = (text or "").strip()
        if len(body) > PREVIEW_LEN:
            return body[:PREVIEW_LEN - 1] + "…"
        return body

    @staticmethod
    def _template_label(t: Any) -> str:
        """One list row: the enable state, the name, and the shared badge."""
        state = "🟢" if bool(getattr(t, "enabled", True)) else "🔴"
        name = (getattr(t, "name", "") or "Prompt").replace("`", "'")
        suffix = " · 🌐" if bool(getattr(t, "is_system", False)) else ""
        return f"{state} {name}{suffix}"

    def _prm_state(self, uid: int) -> Dict[str, Any]:
        st = self._state(uid)
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        return st.buffer

    # -- registration -----------------------------------------------------
    def register_prompt_handlers(self) -> None:
        """Mount the manager's callbacks. Called once from ``_register``."""
        b = self.bot

        # Explicit alternation: a bare "p" prefix would swallow other
        # callbacks ("pf" perf, "pv:*" private view, "pva" ...), so every
        # verb this panel owns is spelled out.
        @b.on_callback_query(filters.regex(
            r"^(pl|pn|pe:[\w-]+|pen:[\w-]+|ped:[\w-]+|pdc:[\w-]+"
            r"|pt:[\w-]+|pename:[\w-]+|pes:[\w-]+|peu:[\w-]+|pedesc:[\w-]+)$"
        ))
        @self._localized_handler
        async def _cb_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer()
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            verb, _, arg = raw.partition(":")
            async with self._user_lock(uid):
                await self._prm_dispatch(cq, uid, verb, arg)

    # -- dispatch ---------------------------------------------------------
    async def _prm_dispatch(self, cq: CallbackQuery, uid: int, verb: str, arg: str) -> None:
        """Route one callback. Out of the closure so tests can drive it.

        Every branch answers the callback query, even the read-only ones: a
        button Telegram considers unanswered spins for 30 seconds and then
        makes the whole panel look unresponsive.
        """
        lang = self._prm_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        st = self._state(uid)

        # A callback that renders a screen is a navigation decision, so an
        # armed prompt must be disarmed by it — otherwise the user taps
        # around while a step is waiting and their NEXT message is stored as
        # that step's answer. The verbs that ARM a step set their own step
        # below, so they are exempt.
        if verb in ("pl", "pe", "ped", "pdc", "pen"):
            if str(getattr(st, "step", "") or "") in PROMPT_STEPS:
                st.step = ""
                await self._persist(uid, st)

        if verb == "pl":
            await cq.answer()
            return await self._prm_render_list(cq, uid)

        if verb == "pn":
            if self._prm is None:
                return await cq.answer(_t("ui_prm_unavailable"), show_alert=True)
            buffer = self._prm_state(uid)
            buffer.pop(BUFFER_NEW_NAME, None)
            st.step = STEP_NEW_NAME
            await self._persist(uid, st)
            await cq.answer()
            return await self._safe_edit(
                cq, _t("ui_prm_new_name_prompt"),
                reply_markup=self._kbd([[(self._btn("ui_cancel_back", lang=lang), CB_MAIN)]]),
            )

        if verb == "pe":
            tpl = await self._prm_owned(uid, arg)
            if tpl is None:
                return await cq.answer(_t("ui_prm_not_found"), show_alert=True)
            await cq.answer()
            return await self._prm_render_detail(cq, uid, tpl)

        if verb == "pen":
            tpl = await self._prm_owned(uid, arg)
            if tpl is None:
                return await cq.answer(_t("ui_prm_not_found"), show_alert=True)
            if bool(tpl.is_system):
                return await cq.answer(_t("ui_prm_system_immutable"), show_alert=True)
            try:
                fresh = await self._prm.set_enabled(tpl.id, not bool(getattr(tpl, "enabled", True)))
            except Exception as exc:
                logger.warning("prompt enable toggle failed: %s", exc)
                return await cq.answer(_t("ui_prm_error_generic"), show_alert=True)
            await cq.answer(
                _t("ui_prm_enabled_alert") if fresh.enabled else _t("ui_prm_disabled_alert"))
            return await self._prm_render_detail(cq, uid, fresh)

        if verb == "ped":
            tpl = await self._prm_owned(uid, arg)
            if tpl is None:
                return await cq.answer(_t("ui_prm_not_found"), show_alert=True)
            if bool(tpl.is_system):
                return await cq.answer(_t("ui_prm_system_immutable"), show_alert=True)
            await cq.answer()
            body = _t("ui_prm_delete_confirm", name=(tpl.name or "")[:80])
            kbd = self._kbd([
                [(self._btn("ui_prm_delete_yes", lang=lang), _cb("pdc", tpl.id))],
                [(self._btn("ui_cancel_back", lang=lang), _cb("pe", tpl.id))],
            ])
            return await self._safe_edit(cq, body, reply_markup=kbd)

        if verb == "pdc":
            tpl = await self._prm_owned(uid, arg)
            if tpl is None:
                return await cq.answer(_t("ui_prm_not_found"), show_alert=True)
            if bool(tpl.is_system):
                return await cq.answer(_t("ui_prm_system_immutable"), show_alert=True)
            try:
                await self._prm.delete_template(tpl.id)
            except Exception as exc:
                logger.warning("prompt delete failed: %s", exc)
                return await cq.answer(_t("ui_prm_error_generic"), show_alert=True)
            await cq.answer(_t("ui_prm_deleted"))
            return await self._prm_render_list(cq, uid)

        if verb == CB_PRM_TEST:
            tpl = await self._prm_owned(uid, arg)
            if tpl is None:
                return await cq.answer(_t("ui_prm_not_found"), show_alert=True)
            buffer = self._prm_state(uid)
            buffer[BUFFER_TARGET] = tpl.id
            st.step = STEP_TEST
            await self._persist(uid, st)
            await cq.answer()
            return await self._safe_edit(
                cq, _t("ui_prm_test_ask", name=(tpl.name or "")[:80]),
                reply_markup=self._kbd([[(self._btn("ui_cancel_back", lang=lang), _cb("pe", tpl.id))]]),
            )

        if verb in ("pename", "pes", "peu", "pedesc"):
            tpl = await self._prm_owned(uid, arg)
            if tpl is None:
                return await cq.answer(_t("ui_prm_not_found"), show_alert=True)
            if bool(tpl.is_system):
                return await cq.answer(_t("ui_prm_system_immutable"), show_alert=True)
            prompts = {
                "pename": ("ui_prm_edit_name_prompt", STEP_EDIT_NAME),
                "pes": ("ui_prm_edit_sys_prompt", STEP_EDIT_SYS),
                "peu": ("ui_prm_edit_user_prompt", STEP_EDIT_USER),
                "pedesc": ("ui_prm_edit_desc_prompt", STEP_EDIT_DESC),
            }
            key, step = prompts[verb]
            buffer = self._prm_state(uid)
            buffer[BUFFER_TARGET] = tpl.id
            st.step = step
            await self._persist(uid, st)
            await cq.answer()
            return await self._safe_edit(
                cq, _t(key, name=(tpl.name or "")[:80]),
                reply_markup=self._kbd([[(self._btn("ui_cancel_back", lang=lang), _cb("pe", tpl.id))]]),
            )

        return await cq.answer()

    # -- renders ----------------------------------------------------------
    async def _prm_render_list(self, cq: CallbackQuery, uid: int) -> None:
        lang = self._prm_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        rows: List[List[Tuple[str, str]]] = []
        lines = [_t("ui_prm_title", count=0, active=0)]
        templates: list = []
        if self._prm is not None:
            try:
                templates = list(await self._prm.list_templates_by_owner(uid))
            except Exception as exc:
                logger.warning("prompt list failed: %s", exc)
        active = sum(1 for t in templates if bool(getattr(t, "enabled", True)))
        lines = [_t("ui_prm_title", count=len(templates), active=active)]
        if not templates:
            lines.append("")
            lines.append(_t("ui_prm_none"))
        else:
            lines.append("")
            lines.append(_t("ui_prm_hint"))
            for t in templates:
                rows.append([(self._template_label(t), _cb("pe", t.id))])
        rows.append([(self._btn("ui_prm_btn_new", lang=lang), "pn")])
        rows.append([(self._btn("ui_back", lang=lang), CB_MAIN)])
        await self._safe_edit(cq, "\n".join(lines), reply_markup=self._kbd(rows))

    async def _prm_render_detail(self, cq: CallbackQuery, uid: int, tpl: Any) -> None:
        lang = self._prm_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        name = (getattr(tpl, "name", "") or "Prompt").replace("`", "'")
        is_system = bool(getattr(tpl, "is_system", False))
        lines = [
            _t("ui_prm_detail_title", name=name) + (f" · {_t('ui_prm_system_tag')}" if is_system else ""),
        ]
        desc = (getattr(tpl, "description", "") or "").strip()
        if desc:
            lines.append(_t("ui_prm_detail_desc", desc=self._preview(desc)))
        lines.append(_t("ui_prm_detail_lang", code=str(getattr(tpl, "target_language", "") or "en")))
        lines.append(_t("ui_prm_detail_version", version=int(getattr(tpl, "current_version", 1) or 1)))
        lines.append("")
        lines.append(_t("ui_prm_state_on" if bool(getattr(tpl, "enabled", True)) else "ui_prm_state_off"))
        lines.append("")
        lines.append(_t("ui_prm_detail_sys", sys=self._preview(getattr(tpl, "system_prompt", ""))))
        lines.append(_t("ui_prm_detail_user", user=self._preview(getattr(tpl, "user_prompt_template", ""))))

        rows: List[List[Tuple[str, str]]] = []
        if not is_system:
            toggle = "ui_rpl_btn_disable" if bool(getattr(tpl, "enabled", True)) else "ui_rpl_btn_enable"
            rows.append([(self._btn(toggle, lang=lang), _cb("pen", tpl.id))])
            rows.append([
                (self._btn("ui_prm_btn_edit_name", lang=lang), _cb("pename", tpl.id)),
                (self._btn("ui_prm_btn_edit_desc", lang=lang), _cb("pedesc", tpl.id)),
            ])
            rows.append([
                (self._btn("ui_prm_btn_edit_sys", lang=lang), _cb("pes", tpl.id)),
                (self._btn("ui_prm_btn_edit_user", lang=lang), _cb("peu", tpl.id)),
            ])
        rows.append([(self._btn("ui_rpl_btn_test", lang=lang), _cb(CB_PRM_TEST, tpl.id))])
        if not is_system:
            rows.append([(self._btn("ui_rpl_btn_delete", lang=lang), _cb("ped", tpl.id))])
        rows.append([(self._btn("ui_prm_btn_back_list", lang=lang), "pl")])
        await self._safe_edit(cq, "\n".join(lines), reply_markup=self._kbd(rows))

    # -- ownership & validation ------------------------------------------
    async def _prm_owned(self, uid: int, template_id: str) -> Optional[Any]:
        """The template a user may act on, fail-closed.

        A system template is shared: it may be *viewed* by anyone on this
        installation (the dispatch gates the destructive verbs separately).
        A custom template answers only to its owner — another tenant's id is
        "not found", never a peek.
        """
        if self._prm is None or not template_id:
            return None
        try:
            tpl = await self._prm.get_template(template_id)
        except Exception as exc:
            logger.warning("prompt lookup failed: %s", exc)
            return None
        if tpl is None:
            return None
        if bool(getattr(tpl, "is_system", False)):
            return tpl
        owner = int(getattr(tpl, "owner_user_id", 0) or 0)
        if owner and owner != int(uid):
            return None
        return tpl

    def _prm_validate(self, name: str, system_prompt: str, user_template: str) -> Optional[str]:
        """The service's rules with localized refusals. None = valid."""
        if not name or len(name) > MAX_NAME_LEN:
            return "ui_ai_prompt_name_invalid"
        if not system_prompt.strip():
            return "ui_ai_prompt_sys_empty_error"
        if len(system_prompt) > MAX_SYSTEM_LEN:
            return "ui_prm_too_long"
        body = user_template.strip() or TEXT_PLACEHOLDER
        if TEXT_PLACEHOLDER not in body:
            return "ui_prm_needs_text_placeholder"
        if len(body) > MAX_USER_LEN:
            return "ui_prm_too_long"
        return None

    # -- FSM steps --------------------------------------------------------
    async def _step_prompt_new_name(self, message: Message, st: Any, text: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._prm_lang(uid)
        name = (text or "").strip()
        if not name or len(name) > MAX_NAME_LEN:
            return await message.reply_text(self._t("ui_ai_prompt_name_invalid", lang=lang))
        buffer = self._prm_state(uid)
        buffer[BUFFER_NEW_NAME] = name
        st.step = STEP_NEW_SYS
        await self._persist(uid, st)
        await message.reply_text(
            self._t("ui_prm_new_sys_prompt", lang=lang),
            reply_markup=self._kbd([[(self._btn("ui_cancel_back", lang=lang), CB_MAIN)]]),
        )

    async def _step_prompt_new_sys(self, message: Message, st: Any, text: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._prm_lang(uid)
        sys_text = (text or "").strip()
        if not sys_text:
            return await message.reply_text(self._t("ui_ai_prompt_sys_empty_error", lang=lang))
        if len(sys_text) > MAX_SYSTEM_LEN:
            return await message.reply_text(self._t("ui_prm_too_long", lang=lang))
        buffer = self._prm_state(uid)
        buffer["prompt_new_sys_value"] = sys_text
        st.step = STEP_NEW_USER
        await self._persist(uid, st)
        await message.reply_text(
            self._t("ui_prm_new_user_prompt", lang=lang, text=TEXT_PLACEHOLDER),
            reply_markup=self._kbd([[(self._btn("ui_cancel_back", lang=lang), CB_MAIN)]]),
        )

    async def _step_prompt_new_user(self, message: Message, st: Any, text: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._prm_lang(uid)
        buffer = self._prm_state(uid)
        raw = (text or "").strip()
        user_template = TEXT_PLACEHOLDER if raw == "-" else (raw or TEXT_PLACEHOLDER)
        name = str(buffer.get(BUFFER_NEW_NAME) or "").strip()
        sys_text = str(buffer.get("prompt_new_sys_value") or "").strip()
        problem = self._prm_validate(name, sys_text, user_template)
        if problem:
            return await message.reply_text(self._t(problem, lang=lang))
        st.step = ""
        buffer.pop(BUFFER_NEW_NAME, None)
        buffer.pop("prompt_new_sys_value", None)
        await self._persist(uid, st)
        if self._prm is None:
            return await message.reply_text(self._t("ui_prm_unavailable", lang=lang))
        try:
            tpl = await self._prm.create_template(
                name=name,
                system_prompt=sys_text,
                user_prompt_template=user_template,
                description="",
                target_language=lang,
                owner_user_id=uid,
            )
        except Exception as exc:
            logger.warning("prompt create failed: %s", exc)
            return await message.reply_text(self._t("ui_prm_error_generic", lang=lang))
        await message.reply_text(self._t("ui_prm_created", lang=lang, name=(tpl.name or "")[:80]))

    async def _step_prompt_test_sample(self, message: Message, st: Any, text: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._prm_lang(uid)
        buffer = self._prm_state(uid)
        template_id = str(buffer.pop(BUFFER_TARGET, "") or "")
        st.step = ""
        await self._persist(uid, st)
        tpl = await self._prm_owned(uid, template_id)
        if tpl is None:
            return await message.reply_text(self._t("ui_prm_not_found", lang=lang))
        sample = (text or "").strip() or "Hello, this is a sample message."
        if self._ai_transformer is None:
            return await message.reply_text(self._t("ui_prm_test_no_config", lang=lang))
        cfg_id = await self._prm_first_config(uid)
        if not cfg_id:
            return await message.reply_text(self._t("ui_prm_test_no_config", lang=lang))
        try:
            result = await self._prm.test_prompt(tpl.id, sample, cfg_id, self._ai_transformer)
        except Exception as exc:
            logger.warning("prompt test failed: %s", exc)
            return await message.reply_text(self._t("ui_prm_error_generic", lang=lang))
        if result.get("success"):
            body = self._t(
                "ui_prm_test_result", lang=lang,
                name=(tpl.name or "")[:80],
                model=str(result.get("model_used") or "?"),
                result=str(result.get("transformed_text") or ""),
            )
        else:
            body = self._t(
                "ui_prm_test_failed", lang=lang,
                name=(tpl.name or "")[:80],
                code=str(result.get("error_code") or "unknown"),
                message=str(result.get("error_message") or ""),
            )
        await message.reply_text(body[:4000])

    async def _step_prompt_edit_name(self, message: Message, st: Any, text: str) -> None:
        await self._prm_apply_edit(message, st, text, "name")

    async def _step_prompt_edit_sys(self, message: Message, st: Any, text: str) -> None:
        await self._prm_apply_edit(message, st, text, "system")

    async def _step_prompt_edit_user(self, message: Message, st: Any, text: str) -> None:
        await self._prm_apply_edit(message, st, text, "user")

    async def _step_prompt_edit_desc(self, message: Message, st: Any, text: str) -> None:
        await self._prm_apply_edit(message, st, text, "desc")

    async def _prm_apply_edit(self, message: Message, st: Any, text: str, field: str) -> None:
        """One summary-screen edit: arm a single field, apply, return.

        The fresh template is re-fetched at answer time and its
        ``current_version`` is the OCC guard, so a stale screen can never
        silently overwrite a newer edit.
        """
        uid = message.from_user.id if message.from_user else 0
        lang = self._prm_lang(uid)
        buffer = self._prm_state(uid)
        template_id = str(buffer.pop(BUFFER_TARGET, "") or "")
        st.step = ""
        await self._persist(uid, st)
        tpl = await self._prm_owned(uid, template_id)
        if tpl is None:
            return await message.reply_text(self._t("ui_prm_not_found", lang=lang))
        raw = (text or "").strip()

        kwargs: Dict[str, Any] = {
            "name": tpl.name,
            "system_prompt": tpl.system_prompt,
            "user_prompt_template": tpl.user_prompt_template,
            "description": tpl.description,
            "target_language": tpl.target_language,
            "expected_version": int(getattr(tpl, "current_version", 1) or 1),
            "change_summary": "",
        }
        if field == "name":
            if not raw or len(raw) > MAX_NAME_LEN:
                return await message.reply_text(self._t("ui_ai_prompt_name_invalid", lang=lang))
            kwargs["name"] = raw
            kwargs["change_summary"] = "renamed"
        elif field == "system":
            if not raw:
                return await message.reply_text(self._t("ui_ai_prompt_sys_empty_error", lang=lang))
            if len(raw) > MAX_SYSTEM_LEN:
                return await message.reply_text(self._t("ui_prm_too_long", lang=lang))
            kwargs["system_prompt"] = raw
            kwargs["change_summary"] = "system prompt edited"
        elif field == "user":
            body = TEXT_PLACEHOLDER if raw == "-" else raw
            if TEXT_PLACEHOLDER not in body:
                return await message.reply_text(
                    self._t("ui_prm_needs_text_placeholder", lang=lang, text=TEXT_PLACEHOLDER))
            if len(body) > MAX_USER_LEN:
                return await message.reply_text(self._t("ui_prm_too_long", lang=lang))
            kwargs["user_prompt_template"] = body
            kwargs["change_summary"] = "user template edited"
        else:  # desc
            kwargs["description"] = "" if raw == "-" else raw
            kwargs["change_summary"] = "description edited"

        try:
            fresh = await self._prm.update_template(tpl.id, **kwargs)
        except Exception as exc:
            logger.warning("prompt edit failed: %s", exc)
            return await message.reply_text(self._t("ui_prm_error_generic", lang=lang))
        await message.reply_text(
            self._t("ui_prm_saved", lang=lang, version=int(getattr(fresh, "current_version", 1) or 1)),
        )

    async def _prm_first_config(self, uid: int) -> str:
        """The owner's first enabled AI config id, or "" when there is none."""
        repo = getattr(self, "_ai", None)
        if repo is None:
            return ""
        try:
            cfgs = await repo.list_by_owner(uid)
        except Exception:
            return ""
        for cfg in cfgs or []:
            if bool(getattr(cfg, "is_enabled", False)):
                return str(getattr(cfg, "id", "") or "")
        return ""


# The main-menu callback token lives with the step constants (CB_MAIN above).

__all__ = ["PromptBotMixin", "PROMPT_STEPS", "BUFFER_TARGET", "BUFFER_NEW_NAME"]
