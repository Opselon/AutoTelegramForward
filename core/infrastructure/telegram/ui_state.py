"""Per-user bot UI flow state (dataclass serialized to the ui_states table)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
@dataclass
class UiState:
    """Per-user UI flow state (FSM)."""

    step: str = ""
    lang: str = ""
    user_id: int = 0
    rule_source: str = ""
    rule_target: str = ""
    filter_id: Optional[str] = None
    ai_field: str = ""
    ai_name: str = ""
    ai_provider: str = ""
    ai_model: str = ""
    ai_api_key: str = ""
    ai_base_url: str = ""
    login_phone: str = ""
    cred_label: str = ""
    cred_api_id: str = ""
    cred_api_hash: str = ""
    buffer: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize the FSM for the durable ui_states table.

        Every field a *flow* reads back is here, because a field that is not
        serialized silently resets on the next process: a screen that reopens
        after a restart used to lose `filter_id` / `ai_field` and render an
        unbound editor that wrote to nothing.

        Deliberately absent: `ai_api_key` and `cred_api_hash`. Those are
        secrets, they live in the credential/AI stores, and the `ui_states`
        payload is plain JSON in SQLite. A flow that needs one asks for it in
        the step that uses it, never by resuming it from the row.
        """
        return {
            "step": self.step,
            "lang": getattr(self, "lang", ""),
            "user_id": getattr(self, "user_id", 0),
            "rule_source": self.rule_source,
            "rule_target": self.rule_target,
            "filter_id": self.filter_id,
            "ai_field": self.ai_field,
            "ai_name": self.ai_name,
            "ai_provider": self.ai_provider,
            "ai_model": self.ai_model,
            "ai_base_url": self.ai_base_url,
            "login_phone": self.login_phone,
            "cred_label": self.cred_label,
            "cred_api_id": self.cred_api_id,
            "buffer": {k: v for k, v in self.buffer.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UiState":
        if not data:
            return cls()
        # Unwrap any recursive wrapping from past persistence anomalies
        cur = dict(data)
        while isinstance(cur.get("buffer"), dict) and any(
            k in cur["buffer"] for k in ("step", "login_phone", "cred_label", "rule_source")
        ):
            inner = cur.pop("buffer")
            for k, v in inner.items():
                if k != "buffer" and (not cur.get(k) or cur.get(k) == ""):
                    cur[k] = v
            cur["buffer"] = inner.get("buffer", {})

        return cls(
            step=cur.get("step", ""),
            lang=cur.get("lang", ""),
            user_id=int(cur.get("user_id", 0) or 0),
            rule_source=cur.get("rule_source", ""),
            rule_target=cur.get("rule_target", ""),
            filter_id=cur.get("filter_id"),
            ai_field=cur.get("ai_field", ""),
            ai_name=cur.get("ai_name", ""),
            ai_provider=cur.get("ai_provider", ""),
            ai_model=cur.get("ai_model", ""),
            ai_base_url=cur.get("ai_base_url", ""),
            login_phone=cur.get("login_phone", ""),
            cred_label=cur.get("cred_label", ""),
            cred_api_id=str(cur.get("cred_api_id", "")),
            buffer=cur.get("buffer", {}) or {},
        )
