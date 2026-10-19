"""Operations: health, readiness, environment contract, PV responder, credentials.

Two rules live here because they are rules, not formatting:

* ``PUT /api/pv-responder`` writes the *whole* config document — so a CLI that
  sends only ``{"enabled": false}`` silently erases the persona prompt and the
  delays. The use case reads the current document and merges, which is what a
  flag like ``--set enabled=false`` promises.
* the PV config's delays must satisfy ``min <= max``; the API clamps each value
  separately and would leave an inverted pair in place.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ...domain.errors import UsageError
from ...domain.models import Finding, Readiness, ServiceHealth
from ...domain.validators import valid_port

PV_FIELDS: tuple[str, ...] = (
    "enabled",
    "ai_config_id",
    "persona_prompt",
    "typing_delay_min",
    "typing_delay_max",
    "cooldown_seconds",
    "ignore_bots",
    "history_limit",
)

_API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class Operations:
    """Use cases over the deployment itself."""

    def __init__(self, gateway) -> None:
        self.gateway = gateway

    # -------------------------------------------------------------------- state
    def version(self) -> Mapping[str, Any]:
        return self.gateway.version()

    def service_health(self) -> list[ServiceHealth]:
        return self.gateway.service_health()

    def readiness(self) -> Readiness:
        return self.gateway.readiness()

    def env_contract(self) -> Mapping[str, Any]:
        return self.gateway.env_contract()

    def stats(self) -> Mapping[str, Any]:
        return self.gateway.stats()

    def findings(self) -> list[Finding]:
        return self.gateway.findings()

    def diagnostics(self) -> Mapping[str, Any]:
        return self.gateway.diagnostics()

    def port_problem(self, text: str) -> str | None:
        """Exposed so the command and the wizard share one rule."""
        return valid_port(text)

    # ----------------------------------------------------------------------- PV
    def pv_responder(self) -> Mapping[str, Any]:
        return self.gateway.pv_responder()

    def set_pv_responder(self, changes: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not changes:
            raise UsageError(
                "nothing to change",
                hint=f"fields: {', '.join(PV_FIELDS)}",
            )
        unknown = [key for key in changes if key not in PV_FIELDS]
        if unknown:
            raise UsageError(
                f"unknown PV field(s): {', '.join(sorted(unknown))}",
                hint=f"fields: {', '.join(PV_FIELDS)}",
            )
        current = dict(self.gateway.pv_responder())
        merged = {**current, **changes}
        self._check_delays(merged)
        document = dict(self.gateway.set_pv_responder(merged))
        applied = ", ".join(f"{key}={value}" for key, value in sorted(changes.items()))
        return document, {"applied": applied, "merged": merged}

    @staticmethod
    def _check_delays(document: Mapping[str, Any]) -> None:
        try:
            low = float(document.get("typing_delay_min") or 0)
            high = float(document.get("typing_delay_max") or 0)
        except (TypeError, ValueError):
            raise UsageError("typing delays must be numbers") from None
        if low > 0 and high > 0 and low > high:
            raise UsageError(
                f"typing_delay_min ({low}) is greater than typing_delay_max ({high})",
                hint="swap them, or set only one and let the server fill the other",
            )

    # --------------------------------------------------------------- credentials
    def client_settings(self) -> Mapping[str, Any]:
        return self.gateway.client_settings()

    def save_client_credentials(self, api_id: str, api_hash: str) -> Mapping[str, Any]:
        """Validate the pair the way the Go handler does, before sending it."""
        text_id = str(api_id).strip()
        if not text_id.isdigit() or int(text_id) <= 0:
            raise UsageError(
                f"api_id must be a positive integer, got {api_id!r}",
                hint="it is the number from my.telegram.org/apps",
            )
        text_hash = (api_hash or "").strip()
        if not _API_HASH_RE.match(text_hash):
            raise UsageError(
                "api_hash must be exactly 32 hexadecimal characters",
                hint="copy it from my.telegram.org/apps — it is not the same as the bot token",
            )
        return self.gateway.save_client_credentials({"api_id": int(text_id), "api_hash": text_hash})

    # ---------------------------------------------------------------------- AI
    def ai_providers(self) -> list[Mapping[str, Any]]:
        """The provider catalogue the server will accept for a config."""
        payload = self.gateway.ai_providers()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        return []

    def check_ai_key(
        self,
        *,
        provider: str,
        api_key: str = "",
        model: str = "",
        base_url: str = "",
        config_id: str = "",
    ) -> Mapping[str, Any]:
        """Probe a provider key, and normalise the answer.

        The handler replies **200 even when the key is wrong** — the verdict is
        the `success`/`healthy` pair — so treating the HTTP status as the answer
        is how a bad key used to read as healthy. Field names are snake_case on
        this route (`base_url`, `api_key`, `ai_config_id`), unlike the camelCase
        config body.
        """
        name = str(provider or "").strip()
        if not name:
            raise UsageError("a provider is required", hint="`atf ai providers` lists them")
        key = str(api_key or "").strip()
        if not key and not config_id:
            raise UsageError(
                "an API key is required", hint="pass --key, or --from-config to probe a saved config"
            )
        body: dict[str, Any] = {"provider": name}
        if model:
            body["model"] = model
        if base_url:
            body["base_url"] = base_url
        if key:
            body["api_key"] = key
        if config_id:
            body["ai_config_id"] = config_id
        answer = dict(self.gateway.check_ai_key(body))
        # `success` means the probe ran; `healthy` means the key works. A wrong key
        # still answers 200 with success=true and the provider's error in `detail`
        # — so reading success as the verdict reported a rejected key as good.
        healthy = bool(answer.get("healthy")) and not answer.get("error_code")
        return {
            "ok": healthy,
            "healthy": bool(answer.get("healthy", healthy)),
            "provider": name,
            "model": model or answer.get("model", ""),
            "latency_ms": answer.get("latency_ms", 0),
            "detail": str(answer.get("detail") or answer.get("message") or ""),
            "error_code": str(answer.get("error_code") or ""),
            "raw": answer,
        }
