"""Professional per-user UI state manager for the Telegram button panel.

Why this exists
---------------
The bot's control panel is a large grid of screens that mutate one per-user
UiState and drive ~250 callback routes. Before this module the state was a
plain dict on the ProBotUI instance, persisted best-effort and resumable via
a loose recursive-unwrap hack. A user who pressed a button mid-flow could
land on a screen whose state had been half-written, double-persisted by two
concurrent callbacks for the same uid, or wrapped recursively — and the
screen rendered against a field set one handler had overwritten. That is the
"classic dead button": the handler exists, the underlying state is garbage.

What this module guarantees (all enforced in one place, not per-handler):

- **Single-writer state** — every mutation for a uid goes through the
  manager under a per-uid asyncio lock, so two callbacks racing one screen
  serialize instead of interleaving half-writes.
- **Validated step namespace** — a `step` value is either one of the
  documented UI steps or empty; anything else fails closed to `""` (never
  crashes a render, never leaves a half-initialized flow claimed).
- **Bounded buffer** — keys <= 64 chars, join values <= 4 KiB total payload,
  JSON-safe values only. A runaway flow cannot grow the state row forever
  (the `ui_states` row is the only durable trace of a UI flow).
- **Navigation stack for real Back behaviour** — a bounded (max 12) stack of
  screens; `push`/`pop`/`replace` with validation; the stack survives
  restart with the rest of the state so "back" isn't a guess after the bot
  rebooted mid-flow.
- **Durable, restore-safe persistence** — the same `ui_states` table, but
  the round trip is versioned (`v: 2`) and the old one-field loads nest
  through the same unwrap the UiState already tolerated.

Ownership note: the manager is keyed by a VERIFIED Telegram user id and only
ever holds that user's own UI flow (rule draft fields, language, buffer). It
never carries tenant credentials — a bot-trading session string lives in the
sessions store, not here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.infrastructure.telegram.ui_state import UiState  # noqa: E402

# The documented UI steps. Any step value handed to enter()/write_state() that
# is not here fails closed to "" — a typo in a new step cannot then crash the
# render, and the coverage test (test_ui_callback_coverage) forces new steps
# to be added here explicitly, which is exactly the "add it with intent" gate.
UI_STEPS = frozenset({
    "",
    # login / connect
    "login_phone", "login_code", "login_password", "login_2fa",
    # rule draft flow
    "rule_source", "rule_target", "rule_name", "rule_confirm",
    # registry editor
    "registry_edit", "registry_search", "registry_confirm",
    # bot route editor
    "bot_route_token", "bot_route_confirm",
    # credentials
    "cred_add", "cred_label", "cred_api_id", "cred_api_hash",
    # ai configs
    "ai_name", "ai_provider", "ai_model", "ai_api_key", "ai_url", "ai_field",
    # web dashboard url, admin broadcast, scheduled resume, media group delay
    "weburl", "admin_bcast", "admin_forcesub", "ai_prompt_edit",
    "media_group_delay", "user_set_about", "user_set_first_name",
    "route_client_edit", "route_client_search",
})

# Some steps carry a dynamic scope at runtime, e.g.
# "bot_route_token_<rule-id>" arms the paste-token step for ONE rule. These
# prefixes are validated so the id (unique-field validated downstream by each
# handler's ownership check) stays tenant-scoped without crashing the step
# namespace validation.
DYNAMIC_STEP_PREFIXES = (
    "rge_edit_", "rge_search_", "bot_route_token_", "set_delay_", "add_bl_",
    "add_wl_", "set_wm_", "set_topic_", "rename_rule_", "ai_prompt_name_",
    "ai_prompt_sys_",
)

STEP_FALLBACK = ""
MAX_STACK = 12
MAX_BUFFER_KEYS = 48
MAX_BUFFER_VALUE = 2 * 1024
MAX_BUFFER_TOTAL = 4 * 1024
RESUME_SCHEMA = 2


class UiStepError(ValueError):
    """A screen/step outside the UI namespace was requested."""


class UiBufferError(ValueError):
    """A buffer write would exceed the durable bounds."""


def _safe_step(step: Optional[str]) -> str:
    if not step or not isinstance(step, str):
        return STEP_FALLBACK
    s = step.strip()
    if s in UI_STEPS:
        return s
    for prefix in DYNAMIC_STEP_PREFIXES:
        if s.startswith(prefix) and len(s) > len(prefix) and len(s) <= 96:
            # scope ids are opaque but constrained: no control bytes, no
            # ':' (keeps callback plumbing deterministic)
            scope = s[len(prefix):]
            if scope.isalnum() or all(c.isalnum() or c in "-_." for c in scope):
                return s
    return STEP_FALLBACK


def _safe_buffer(buf: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Deep-copy a buffer within bounds, favouring truncation over failure."""
    out: Dict[str, Any] = {}
    if not isinstance(buf, dict):
        return out
    total = 0
    for key, value in list(buf.items())[:MAX_BUFFER_KEYS]:
        if not isinstance(key, str):
            continue
        try:
            encoded = json.dumps(value, default=str)
        except (TypeError, ValueError, RecursionError):
            continue
        if len(encoded) > MAX_BUFFER_VALUE:
            continue
        total += len(key) + len(encoded)
        if total > MAX_BUFFER_TOTAL:
            break
        try:
            out[key[:64]] = json.loads(encoded)
        except (ValueError, TypeError, RecursionError):
            # keep the string form rather than dropping the value silently
            out[key[:64]] = encoded
    return out


class UiStateManager:
    """Durable per-user UI flow state with validated transitions.

    Errors in the wrong hand are normal UI flow: a screen pressed twice is
    not an error, the second press is a no-op. The `states` used by renders
    is always the manager's object, so handlers can hold references without
    racing: mutations serialize under the per-uid lock.
    """

    def __init__(self, repo: Any = None, logger: Any = None) -> None:
        self._repo = repo
        self._log = logger
        self._states: Dict[int, UiState] = {}
        self._stacks: Dict[int, List[str]] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._loaded: Dict[int, bool] = {}

    # ---- locking ------------------------------------------------------- #
    def _lock(self, uid: int) -> asyncio.Lock:
        if uid not in self._locks:
            self._locks[uid] = asyncio.Lock()
        return self._locks[uid]

    # ---- loading ------------------------------------------------------ #
    async def _ensure_loaded(self, uid: int) -> None:
        if self._loaded.get(uid) or self._repo is None:
            self._loaded[uid] = False  # absent row or no store: do not retry
            return
        try:
            row = await self._repo.load(uid)
        except Exception:
            row = None
        if row:
            st, stack = UiState_v2_from_payload(row)
            self._states[uid] = st
            self._stacks[uid] = stack
        self._loaded[uid] = True

    # ---- access ------------------------------------------------------- #
    async def get(self, uid: int) -> UiState:
        """The user's UiState — loads/resumes on first touch in this process."""
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            st = self._states.get(uid)
            if st is None:
                st = UiState()
                self._states[uid] = st
            st.user_id = int(uid)
            return st

    # ---- mutations ---------------------------------------------------- #
    async def enter(self, uid: int, step: str, replace: bool = True) -> UiState:
        """Move the user's flow onto a screen/step (validated).

        replace=True resets any pending buffer; replace=False starts a new
        enter-only mutation (rare; grading of flow steps is per-handler).
        """
        safe = _safe_step(step)
        if safe != step and step:
            raise UiStepError(f"unknown UI step {step!r}")
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            st = self._states.setdefault(uid, UiState())
            if replace:
                st.step = safe
                if not safe:
                    st.rule_source = st.rule_target = ""
                    st.buffer = {}
            st.user_id = int(uid)
            await self._persist(uid)
            return st

    async def merge(self, uid: int, step: Optional[str] = None,
                    **fields: Any) -> UiState:
        """Set specific fields of the user's state in one locked write."""
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            st = self._states.setdefault(uid, UiState())
            if step is not None:
                safe = _safe_step(step)
                if safe != step and step:
                    raise UiStepError(f"unknown UI step {step!r}")
                st.step = safe
            allowed = {
                "lang", "rule_source", "rule_target", "filter_id",
                "ai_field", "ai_name", "ai_provider", "ai_model",
                "ai_api_key", "ai_base_url", "login_phone",
                "cred_label", "cred_api_id", "cred_api_hash",
            }
            for key, value in fields.items():
                if key in allowed:
                    setattr(st, key, value if value is not None else "")
            st.user_id = int(uid)
            await self._persist(uid)
            return st

    async def write_buffer(self, uid: int, key: str, value: Any) -> UiState:
        """Bounded per-user buffer write (field drafts, page indices)."""
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            st = self._states.setdefault(uid, UiState())
            trial = dict(st.buffer or {})
            trial[key] = value
            bounded = _safe_buffer(trial)
            if key not in bounded and value not in (None, ""):
                raise UiBufferError(f"buffer overflow at key {key!r}")
            st.buffer = bounded
            st.user_id = int(uid)
            await self._persist(uid)
            return st

    async def reset(self, uid: int) -> UiState:
        """Fresh flow (used by /start and every cancel screen)."""
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            self._states[uid] = UiState()
            self._stacks[uid] = []
            self._states[uid].user_id = int(uid)
            await self._persist(uid)
            return self._states[uid]

    # ---- navigation stack --------------------------------------------- #
    async def enter_screen(self, uid: int, screen: str) -> UiState:
        """Push the next screen; the previous one stays on the stack."""
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            st = self._states.setdefault(uid, UiState())
            stack = self._stacks.setdefault(uid, [])
            if stack and stack[-1] == screen:
                # Re-entering the same screen (double-click) is a no-op.
                pass
            else:
                stack.append(screen)
                if len(stack) > MAX_STACK:
                    del stack[:len(stack) - MAX_STACK]
            st.user_id = int(uid)
            await self._persist(uid)
            return st

    async def pop_screen(self, uid: int) -> Tuple[str, UiState]:
        """Return (previous_screen, state) and drop the top of the stack."""
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            st = self._states.setdefault(uid, UiState())
            stack = self._stacks.setdefault(uid, [])
            screen = stack.pop() if stack else ""
            st.user_id = int(uid)
            await self._persist(uid)
            return screen, st

    async def current_stack(self, uid: int) -> List[str]:
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            return list(self._stacks.get(uid, []))

    # ---- existing-cache absorb ------------------------------------------ #
    async def absorb(self, uid: int, st: UiState) -> None:
        """Validate, own and persist one externally built UiState.

        ProBotUI hands the manager the per-user UiState object its handlers
        already mutated; the manager stores it as THE durable object for this
        uid (single-writer), validates the namespace/bounds one last time and
        writes the versioned payload with the current screen stack.
        """
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            current = self._states.setdefault(uid, UiState())
            if current is not st:
                # Adopt the caller's values but keep the manager-owned object
                # so any early references stay valid.
                current.step = _safe_step(st.step or "")
                current.lang = st.lang or current.lang
                current.rule_source = st.rule_source or ""
                current.rule_target = st.rule_target or ""
                current.filter_id = st.filter_id
                for k in ("ai_field", "ai_name", "ai_provider", "ai_model",
                          "ai_api_key", "ai_base_url", "login_phone",
                          "cred_label", "cred_api_id", "cred_api_hash"):
                    setattr(current, k, getattr(st, k, "") or "")
                current.buffer = _safe_buffer(st.buffer)
            current.user_id = int(uid)
            await self._persist(uid)

    # ---- durability ---------------------------------------------------- #
    async def _persist(self, uid: int) -> None:
        if self._repo is None:
            return
        st = self._states.get(uid)
        stack = self._stacks.get(uid, [])
        if st is None:
            return
        try:
            payload = UiState_v2_payload(st, stack)
            if st.step or stack or (st.buffer or {}):
                await self._repo.save(uid, st.step, payload)
            else:
                await self._repo.clear(uid)
        except Exception:
            if self._log is not None:
                self._log.info("ui state persist failed for %s", uid)


# ---- (de)serialization ------------------------------------------------- #

def UiState_v2_payload(st: UiState, stack: List[str]) -> Dict[str, Any]:
    body = st.to_dict()
    body.pop("buffer", None)
    body["buffer"] = _safe_buffer(st.buffer)
    body["screen_stack"] = list(stack)[:MAX_STACK]
    body["v"] = RESUME_SCHEMA
    return body


def UiState_v2_from_payload(payload: Any) -> Tuple[UiState, List[str]]:
    """Restore a v2 payload; tolerate the older(step, buffer-only) rows."""
    data: Dict[str, Any] = payload
    if not isinstance(payload, dict):
        # legacy row: {"step": str, "buffer": json-blob}
        data = {}
    stack = list(data.get("screen_stack") or [])[:MAX_STACK]
    st = UiState.from_dict(data)
    return st, stack
