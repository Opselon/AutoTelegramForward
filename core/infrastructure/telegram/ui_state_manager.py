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
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.infrastructure.telegram.ui_state import UiState  # noqa: E402

logger = logging.getLogger(__name__)

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
    # rule editor: free-text inputs that a screen is waiting for. A step that
    # is not listed here is coerced to "" on persist, so the user's next
    # message falls through to the main-menu hint and the flow looks broken
    # even though its handler exists — that is exactly how "add replacement
    # rule" used to swallow its own input.
    "rule_replace_input", "rule_clean_words_input", "rule_header_input",
    "rule_footer_input", "rule_max_edit_input", "rule_edit_source",
    "rule_edit_target", "rule_editing_draft",
    # per-task routing editor (source/target lists are typed, not picked)
    "route_sources", "route_targets", "route_edit_s", "route_edit_t",
    # filter rule editor
    "filter_name", "filter_blacklist",
    # smart filters
    "smart_set_header", "smart_set_intermediate", "smart_set_keywords",
    "smart_set_regex", "smart_set_brand_words", "smart_set_brand_origins",
    "smart_test_rule",
    # private-view auto-responder
    "pv_persona_input", "pv_test_input",
    # registry editor
    "registry_edit", "registry_search", "registry_confirm",
    # bot route editor
    "bot_route_token", "bot_route_confirm",
    # credentials
    "cred_add", "cred_label", "cred_api_id", "cred_api_hash",
    "client_api_id", "client_api_hash",
    # ai configs
    "ai_name", "ai_provider", "ai_model", "ai_api_key", "ai_url", "ai_field",
    "ai_base_url", "ai_edit_host", "ai_edit_key", "ai_edit_model",
    # web dashboard url, admin broadcast, scheduled resume, media group delay
    "weburl", "weburl_input", "admin_bcast", "admin_forcesub",
    "admin_add_id", "admin_broadcast_text", "admin_forcesub_channel",
    "ai_prompt_edit", "media_group_delay", "user_set_about",
    "user_set_first_name",
    # replace-rule manager (/replace): search, field prompts and the test sample
    "replace_search", "replace_field_input", "replace_test_sample",
    # multi-source/target picker (/sel): the ids and search prompts. Declared
    # here, or the fail-closed step gate would downgrade them to "" on persist
    # and the next message would answer "use the menu" mid-flow.
    "sel_ids_s", "sel_ids_t", "sel_query_s", "sel_query_t",
    # backup / migration passphrases and the file it waits for
    "session_backup_passphrase", "migrate_passphrase",
    "migrate_import_passphrase", "migrate_await_file",
    "route_client_edit", "route_client_search",
})

# Some steps carry a dynamic scope at runtime, e.g.
# "bot_route_token_<rule-id>" arms the paste-token step for ONE rule. These
# prefixes are validated so the id (unique-field validated downstream by each
# handler's ownership check) stays tenant-scoped without crashing the step
# namespace validation.
DYNAMIC_STEP_PREFIXES = (
    "rge_edit_", "rge_search_", "bot_route_token_", "set_delay_", "set_every_n_",
    "add_bl_",
    "add_wl_", "set_wm_", "set_topic_", "rename_rule_", "ai_prompt_name_",
    "ai_prompt_sys_",
    # Three-way filter text steps: the pending regex pattern, the Long-Text
    # entry list, or the sample message for a read-only rule test.
    "tw_regex_", "tw_longtext_", "tw_test_",
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


def _bounded_value(value: Any, cap: int) -> Tuple[Any, str]:
    """The largest usable part of `value` that fits `cap` bytes.

    Truncation beats dropping: a dropped key is invisible to every later reader
    (an emptied dialog list renders as "no dialogs found", which is exactly how
    a working fetch looked broken), while a truncated one still answers the
    screen it was stored for. Returns (projection, note); (None, reason) when
    nothing usable fits.
    """
    if isinstance(value, str):
        lo, hi = 0, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(json.dumps(value[:mid], default=str)) <= cap:
                lo = mid
            else:
                hi = mid - 1
        if lo:
            return value[:lo], f"string {lo}/{len(value)} chars"
        return None, "string cannot fit the cap"

    if isinstance(value, (list, tuple)):
        lo, hi = 0, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            try:
                fits = len(json.dumps(value[:mid], default=str)) <= cap
            except (TypeError, ValueError, RecursionError):
                return None, "list is not JSON-encodable"
            if fits:
                lo = mid
            else:
                hi = mid - 1
        if lo:
            return list(value[:lo]), f"list {lo}/{len(value)} items"
        return None, "first list item cannot fit the cap"

    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for key, item in value.items():
            probe = dict(out)
            probe[key] = item
            try:
                fits = len(json.dumps(probe, default=str)) <= cap
            except (TypeError, ValueError, RecursionError):
                continue
            if not fits:
                break
            out[key] = item
        if out:
            return out, f"mapping {len(out)}/{len(value)} keys"
        return None, "first mapping entry cannot fit the cap"

    return None, "value is not JSON-encodable"


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
            projection, note = _bounded_value(value, MAX_BUFFER_VALUE)
            if projection is None:
                logger.warning(
                    "[UI_STATE:BUFFER_PRUNED] key=%s dropped: %s", key[:64], note
                )
                continue
            value = projection
            encoded = json.dumps(value, default=str)
            logger.warning(
                "[UI_STATE:BUFFER_TRUNCATED] key=%s kept %s (%d bytes, cap %d)",
                key[:64], note, len(encoded), MAX_BUFFER_VALUE,
            )
        total += len(key) + len(encoded)
        if total > MAX_BUFFER_TOTAL:
            logger.warning(
                "[UI_STATE:BUFFER_TOTAL_CAP] kept %d of %d keys (%d bytes, cap %d)",
                len(out), len(buf), total, MAX_BUFFER_TOTAL,
            )
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
        """Resume the user's flow from the durable row — exactly once.

        The repository interface is `get()/save()/clear()`. Reading it on every
        touch would replace the live object with the persisted copy, so a row
        that trails the live state (a persist that failed and was swallowed, a
        replica, a write that lost a race) would overwrite newer edits on the
        next press — the exact failure this module exists to prevent. One load
        per process is therefore the guarantee, not an optimisation.

        A load that raises is reported and left unloaded, so the next call can
        still resume instead of the failure being cached for the life of the
        process. The row is never applied over state the process already holds.
        """
        if self._loaded.get(uid):
            return
        if self._repo is None:
            self._loaded[uid] = True
            return
        try:
            row = await self._repo.get(uid)
        except Exception as exc:
            logger.warning("[UI_STATE:LOAD_FAILED] uid=%s: %s", uid, exc)
            if self._log is not None:
                try:
                    self._log.info("ui", "state", f"resume failed for {uid}: {exc}")
                except Exception:
                    pass
            return
        if row and uid not in self._states:
            payload = row.get("buffer") if isinstance(row, dict) else None
            if isinstance(payload, dict) and any(
                k in payload for k in ("step", "v", "screen_stack", "buffer")
            ):
                st, stack = UiState_v2_from_payload(payload)
            else:
                # a pre-manager row: `buffer` was the buffer itself
                st = UiState.from_dict(
                    {"step": (row or {}).get("step", ""), "buffer": payload or {}})
                stack = []
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
        """Bounded per-user buffer write (field drafts, page indices).

        An oversized value fails closed: the caller asked for a write that
        cannot be stored, and handing back a truncated buffer would mean the
        state is not what was written. (Persistence still projects a bounded
        copy - see _safe_buffer - but it never rewrites the caller's value
        behind its back.)
        """
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            st = self._states.setdefault(uid, UiState())
            try:
                requested = json.dumps(value, default=str)
            except (TypeError, ValueError, RecursionError) as exc:
                raise UiBufferError(
                    f"buffer value at {key!r} is not storable: {exc}") from exc
            trial = dict(st.buffer or {})
            trial[key] = value
            bounded = _safe_buffer(trial)
            if value not in (None, ""):
                stored = (json.dumps(bounded[key], default=str)
                          if key in bounded else None)
                if stored != requested:
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
    async def absorb(self, uid: int, st: UiState, stack: Optional[List[str]] = None,
                     restore_stack: bool = False) -> None:
        """Validate, own and persist one externally built UiState.

        ProBotUI hands the manager the per-user UiState object its handlers
        already mutated; the manager stores it as THE durable object for this
        uid (single-writer), validates the namespace/bounds one last time and
        writes the versioned payload with the current screen stack.

        `stack` + `restore_stack=True` is the bot UI's resume handover: it seeds
        the navigation stack from the payload it just read, and only when this
        process has no stack for the user yet, so a handover can restore `Back`
        after a restart but can never rewind a live stack.
        """
        async with self._lock(uid):
            await self._ensure_loaded(uid)
            if restore_stack and stack and not self._stacks.get(uid):
                self._stacks[uid] = list(stack)[:MAX_STACK]
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
