"""Durable storage for the operator-set log collection policy.

The policy is enforced per producer, and the producer that enforces it here is
Core. A change an operator makes must therefore outlive a restart *of the process
that made it* — otherwise "turn on INFO for ten minutes" silently reverts at the
next deploy and the operator has no way to tell that from "the change never
worked".

It reuses the `app_settings` key/value table the rest of Core already keeps
runtime settings in: a policy in a file of its own would be a second place to
look and a second way to get it wrong. Reads and writes go through
`SqliteDatabase`, which serialises them under the database lock; the write is a
single upsert, so a crash mid-change cannot leave a half-written policy.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger("atf.core")

#: `app_settings` key holding the persisted policy.
LOG_POLICY_KEY = "log_policy"


class LogPolicyStore:
    """Loads and saves the policy snapshot an operator has set.

    Every failure path is non-fatal and loud. A corrupt row must not stop Core
    from starting — the environment defaults are the conservative answer — but it
    must not be silent either, because "the setting was ignored" and "the setting
    is in force" look identical from the outside.
    """

    def __init__(self, db) -> None:
        self._db = db

    def load(self) -> Optional[Dict[str, object]]:
        """The stored state, or None when nothing valid is stored."""
        try:
            row = self._db.query_one(
                "SELECT value FROM app_settings WHERE key=?", (LOG_POLICY_KEY,)
            )
        except Exception as exc:  # pragma: no cover - depends on a broken database
            logger.warning(
                "[PIPELINE:log_policy] could not read the persisted policy (%s);"
                " falling back to the configured defaults",
                exc,
            )
            return None
        raw = (row["value"] if row else "") or ""
        if not str(raw).strip():
            return None
        try:
            state = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[PIPELINE:log_policy] persisted policy is not valid JSON (%s);"
                " falling back to the configured defaults",
                exc,
            )
            return None
        if not isinstance(state, dict):
            logger.warning(
                "[PIPELINE:log_policy] persisted policy is a %s, expected an object;"
                " falling back to the configured defaults",
                type(state).__name__,
            )
            return None
        return state

    def save(self, state: Dict[str, object]) -> bool:
        """Persist `state`; returns whether it is durable now.

        The caller reports this back, so a change that was accepted but could not
        be written says so instead of implying it will survive a restart.
        """
        try:
            payload = json.dumps(state, sort_keys=True)
        except (TypeError, ValueError) as exc:
            logger.error("[PIPELINE:log_policy] policy is not serialisable: %s", exc)
            return False
        try:
            self._db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (LOG_POLICY_KEY, payload, int(time.time())),
            )
        except Exception as exc:
            logger.error(
                "[PIPELINE:log_policy] could not persist the policy change: %s", exc
            )
            return False
        return True

    def restore_into(self, policy) -> bool:
        """Apply the stored state to `policy`; returns whether anything applied."""
        state = self.load()
        if not state:
            return False
        applied = policy.import_state(state)
        if applied:
            levels = state.get("configured_levels") or []
            readable = ", ".join(str(l) for l in levels) if isinstance(levels, list) else str(levels)
            logger.info(
                "[PIPELINE:log_policy] restored the persisted collection policy:"
                " levels=%s verbose_rate=%s/s diagnostic_until=%s",
                readable,
                state.get("verbose_rate_per_second"),
                state.get("diagnostic_until") or 0,
            )
        return applied
