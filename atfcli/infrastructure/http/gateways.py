"""HTTP adapters: the only place that knows the stack is reachable over HTTP.

Each class here implements one port from :mod:`atfcli.application.gateways` on
top of :class:`~atfcli.infrastructure.http.transport.ApiClient`. Two rules keep
the boundary honest:

* **parse once, here.** A payload becomes a domain object (``Task``, ``LogPage``,
  ``Finding``) before it leaves this module, so a renamed JSON key breaks one
  adapter instead of every command that read it.
* **never invent a field.** Where the API's envelope is not fixed (some list
  endpoints answer with a bare array, others wrap it), the fallback list is
  explicit and the miss surfaces as an empty list rather than a KeyError.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from ...application.gateways import Gateways
from ...domain.logs import LogPage, LogPolicyView, LogQuery, LogStats
from ...domain.models import (
    AdminUser,
    AuditEvent,
    DeadLetterItem,
    Finding,
    QueueSnapshot,
    Readiness,
    RoutePath,
    ServiceHealth,
    Session,
    SessionChat,
    SimulationResult,
    Task,
    items_of,
)
from ...domain.resources import AI_CONFIGS, FILTERS, PROMPTS, Resource, ResourceSpec
from .transport import ApiClient


class _HttpBase:
    def __init__(self, api: ApiClient) -> None:
        self.api = api

    def _get(self, path: str, **kw: Any) -> Any:
        return self.api.get(path, **kw)

    def _post(self, path: str, **kw: Any) -> Any:
        return self.api.post(path, **kw)

    def _put(self, path: str, **kw: Any) -> Any:
        return self.api.put(path, **kw)

    def _patch(self, path: str, **kw: Any) -> Any:
        return self.api.patch(path, **kw)

    def _delete(self, path: str, **kw: Any) -> Any:
        return self.api.delete(path, **kw)


# ---------------------------------------------------------------------- tasks
class HttpTasks(_HttpBase):
    """``/api/rules`` and the per-rule action endpoints."""

    def list_tasks(self) -> list[Task]:
        payload = self._get("/api/rules")
        return [Task.from_payload(row) for row in items_of(payload, "rules", "items", "tasks")]

    def get_task(self, task_id: str) -> Task:
        return Task.from_payload(_as_mapping(self._get(f"/api/rules/{task_id}")))

    def create_task(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _as_mapping(self._post("/api/rules", payload=dict(payload)))

    def update_task(self, task_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _as_mapping(self._put(f"/api/rules/{task_id}", payload=dict(payload)))

    def delete_task(self, task_id: str) -> Mapping[str, Any]:
        return _as_mapping(self._delete(f"/api/rules/{task_id}"))

    def task_action(self, task_id: str, action: str) -> Mapping[str, Any]:
        # One route family: toggle | pause | resume | stop | restart. `enable` and
        # `disable` are the CLI's promise; the API only has `toggle`.
        return _as_mapping(self._post(f"/api/rules/{task_id}/{action}"))

    def duplicate_task(self, task_id: str) -> Mapping[str, Any]:
        return _as_mapping(self._post(f"/api/rules/{task_id}/duplicate"))

    def set_route_path(self, task_id: str, path: RoutePath) -> Mapping[str, Any]:
        return _as_mapping(self._post(f"/api/rules/{task_id}/route-path", payload=path.to_payload()))

    def simulate(self, task_id: str, text: str, *, sender: str = "") -> SimulationResult:
        payload: dict[str, Any] = {"rule_id": task_id, "sample_text": text}
        if sender:
            payload["sample_sender_id"] = sender
        return SimulationResult.from_payload(self._post("/api/simulate", payload=payload))

    def get_settings(self, task_id: str) -> Mapping[str, Any]:
        return _as_mapping(self._get(f"/api/rules/{task_id}/settings"))

    def update_settings(
        self,
        task_id: str,
        values: Mapping[str, Any],
        *,
        replace_all: bool = False,
        resets: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        # Both verbs reach the same handler; `replace_all` and `reset` are body
        # fields, not different endpoints. Sending them as URL/method differences
        # is how "reset" used to store a literal null instead of clearing the key.
        body: dict[str, Any] = {"settings": dict(values)}
        if replace_all:
            body["replace_all"] = True
        if resets:
            body["reset"] = list(resets)
        return _as_mapping(self._patch(f"/api/rules/{task_id}/settings", payload=body))

    def settings_schema(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/rules/settings/schema"))

    def task_logs(self, task_id: str, query: LogQuery) -> LogPage:
        payload = self._get(f"/api/rules/{task_id}/logs", params=query.to_params())
        return LogPage.from_payload(payload)


# ------------------------------------------------------------------- sessions
class HttpSessions(_HttpBase):
    def list_sessions(self) -> list[Session]:
        payload = self._get("/api/sessions")
        return [Session.from_payload(row) for row in items_of(payload, "sessions", "items")]

    def session_chats(self, session_id: str) -> list[SessionChat]:
        payload = self._get(f"/api/sessions/{session_id}/chats")
        return [SessionChat.from_payload(row) for row in items_of(payload, "chats", "items", "dialogs")]

    def backup_session(self, session_id: str) -> Mapping[str, Any]:
        return _as_mapping(self._post(f"/api/sessions/{session_id}/backup"))

    def terminate_session(self, session_id: str) -> Mapping[str, Any]:
        return _as_mapping(self._delete(f"/api/sessions/{session_id}"))


# ----------------------------------------------------------------------- logs
class HttpLogs(_HttpBase):
    def query(self, query: LogQuery) -> LogPage:
        return LogPage.from_payload(self._get("/api/logs", params=query.to_params()))

    def stats(self, *, rule_id: str = "", hours: int = 0) -> LogStats:
        params = {"hours": hours} if hours else None
        payload = self._get("/api/logs/stats", params=params)
        stats = LogStats.from_payload(payload)
        return stats if not rule_id else _with_rule(stats, rule_id)

    def export(self, query: LogQuery, *, fmt: str = "json") -> bytes:
        params = {**query.params(), "format": fmt}
        body = self.api.stream("/api/logs/export", params=params)
        return body if isinstance(body, bytes) else str(body).encode("utf-8")

    def purge(self, query: LogQuery, *, older_than_days: int) -> Mapping[str, Any]:
        payload = query.purge_body(older_than_days=older_than_days)
        return _as_mapping(self._post("/api/logs/purge", payload=payload))

    def policy(self) -> LogPolicyView:
        return LogPolicyView.from_payload(self._get("/api/logs/policy"))

    def update_policy(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return _as_mapping(self._put("/api/logs/policy", payload=dict(body)))

    def health(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/logs/health"))

    def diagnostics(self, action: str, *, seconds: int = 0) -> Mapping[str, Any]:
        path = f"/api/logs/diagnostics/{action}"
        if action == "start":
            return _as_mapping(self._post(path, payload={"seconds": seconds}))
        return _as_mapping(self._post(path))


# ------------------------------------------------------------------- delivery
class HttpDelivery(_HttpBase):
    def queue(self) -> QueueSnapshot:
        return QueueSnapshot.from_payload(self._get("/api/queue"))

    def dead_letters(self) -> list[DeadLetterItem]:
        payload = self._get("/api/dlq")
        return [
            DeadLetterItem.from_payload(row)
            for row in items_of(payload, "jobs", "entries", "items", "messages")
        ]

    def retry_dead_letter(self, message_id: str) -> Mapping[str, Any]:
        return _as_mapping(self._post(f"/api/dlq/{message_id}/retry"))

    def purge_dead_letters(self) -> Mapping[str, Any]:
        return _as_mapping(self._delete("/api/dlq"))


# ------------------------------------------------------------------ resources
class HttpResources(_HttpBase):
    """One CRUD collection, shaped by its :class:`ResourceSpec`."""

    def __init__(self, api: ApiClient, spec: ResourceSpec) -> None:
        super().__init__(api)
        self.spec = spec

    @property
    def _base(self) -> str:
        return f"/api/{self.spec.plural}"

    def _list_path(self) -> str:
        # AI configs and filters live under /api/ai-configs and /api/filters;
        # prompts under /api/prompts. The spec carries the full path.
        return self.spec.path

    def list_resources(self) -> list[Resource]:
        payload = self._get(self._list_path())
        keys = (self.spec.plural, "items", "data")
        return [
            Resource.from_payload(row, spec=self.spec)
            for row in items_of(payload, *keys)
        ]

    def create_resource(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _as_mapping(self._post(self._list_path(), payload=dict(payload)))

    def update_resource(self, resource_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        method = self._put if self.spec.update_method.upper() == "PUT" else self._patch
        return _as_mapping(method(f"{self._list_path()}/{resource_id}", payload=dict(payload)))

    def delete_resource(self, resource_id: str) -> Mapping[str, Any]:
        return _as_mapping(self._delete(f"{self._list_path()}/{resource_id}"))


class HttpPrompts(HttpResources):
    def __init__(self, api: ApiClient) -> None:
        super().__init__(api, PROMPTS)

    def test_prompt(
        self, *, prompt_id: str, ai_config_id: str, sample_text: str, version: int = 0
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {
            # the handler reads `prompt_id`; `template_id` is silently ignored and
            # the request then fails its own required-field check
            "prompt_id": prompt_id,
            "ai_config_id": ai_config_id,
            "sample_text": sample_text,
        }
        if version:
            body["version"] = version
        return _as_mapping(self._post("/api/prompts/test", payload=body))


# --------------------------------------------------------------------- backup
class HttpBackup(_HttpBase):
    def export(self, *, passphrase: str = "") -> Mapping[str, Any]:
        body: dict[str, Any] = {}
        if passphrase:
            body["passphrase"] = passphrase
        return _as_mapping(self._post("/api/backup/export", payload=body))

    def restore(
        self, bundle: str, *, passphrase: str = "", overwrite: bool = False
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {"bundle": bundle, "overwrite_existing": overwrite}
        if passphrase:
            body["passphrase"] = passphrase
        return _as_mapping(self._post("/api/backup/import", payload=body))


# ---------------------------------------------------------------------- admin
class HttpAdmin(_HttpBase):
    def overview(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/admin/overview"))

    def list_users(self, *, limit: int = 50, offset: int = 0) -> list[AdminUser]:
        payload = self._get("/api/admin/users", params={"limit": limit, "offset": offset})
        return [AdminUser.from_payload(row) for row in items_of(payload, "users", "items")]

    def set_user_status(self, user_id: str, status: str) -> Mapping[str, Any]:
        return _as_mapping(
            self._post(f"/api/admin/users/{user_id}/status", payload={"status": status})
        )

    def broadcast(self, message: str) -> Mapping[str, Any]:
        return _as_mapping(self._post("/api/admin/broadcast", payload={"message": message}))

    def settings(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/admin/settings"))

    def save_settings(self, values: Mapping[str, Any]) -> Mapping[str, Any]:
        return _as_mapping(self._post("/api/admin/settings", payload=dict(values)))

    def list_admins(self) -> list[AdminUser]:
        payload = self._get("/api/admin/admins")
        return [AdminUser.from_payload(row) for row in items_of(payload, "admins", "items")]

    def add_admin(self, user_id: str, *, username: str = "") -> Mapping[str, Any]:
        body: dict[str, Any] = {"user_id": user_id}
        if username:
            body["username"] = username
        return _as_mapping(self._post("/api/admin/admins", payload=body))

    def remove_admin(self, user_id: str) -> Mapping[str, Any]:
        return _as_mapping(self._delete(f"/api/admin/admins/{user_id}"))

    def extensions(self) -> list[Mapping[str, Any]]:
        payload = self._get("/api/admin/extensions")
        return [dict(row) for row in items_of(payload, "extensions", "items")]

    def toggle_extension(self, extension_id: str) -> Mapping[str, Any]:
        return _as_mapping(self._post(f"/api/admin/extensions/{extension_id}/toggle"))


# ---------------------------------------------------------------------- audit
class HttpAudit(_HttpBase):
    def query(self, params: Mapping[str, Any]) -> list[AuditEvent]:
        payload = self._get("/api/audit/events", params=dict(params))
        return [
            AuditEvent.from_payload(row)
            for row in items_of(payload, "events", "items", "results")
        ]


# ----------------------------------------------------------------- operations
class HttpOps(_HttpBase):
    def version(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/version"))

    def service_health(self) -> list[ServiceHealth]:
        payload = self._get("/api/health/services")
        rows: Sequence[Mapping[str, Any]] = items_of(payload, "services", "items")
        return [ServiceHealth.from_payload(row) for row in rows]

    def readiness(self) -> Readiness:
        return Readiness.from_payload(self._get("/api/system/setup"))

    def env_contract(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/system/env"))

    def stats(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/stats"))

    def diagnostics(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/diagnostics"))

    def findings(self) -> list[Finding]:
        payload = self._get("/api/diagnostics")
        rows = items_of(payload, "findings", "problems", "items")
        return [Finding.from_payload(row, index=index) for index, row in enumerate(rows)]

    def pv_responder(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/pv-responder"))

    def set_pv_responder(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _as_mapping(self._post("/api/pv-responder", payload=dict(payload)))

    def client_settings(self) -> Mapping[str, Any]:
        return _as_mapping(self._get("/api/client/settings"))

    def save_client_credentials(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _as_mapping(self._post("/api/client/credentials", payload=dict(payload)))

    def ai_providers(self) -> list[Mapping[str, Any]]:
        payload = self._get("/api/ai-configs/providers")
        return [dict(row) for row in items_of(payload, "providers", "items")]

    def check_ai_key(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _as_mapping(self._post("/api/ai-configs/check-key", payload=dict(payload)))


# ----------------------------------------------------------------- composition
class HttpGateways(Gateways):
    """The real :class:`Gateways` bundle: every port over the live API."""

    def __init__(self, api: ApiClient, host: Any = None) -> None:
        self.api = api
        self.tasks = HttpTasks(api)
        self.sessions = HttpSessions(api)
        self.logs = HttpLogs(api)
        self.delivery = HttpDelivery(api)
        self.ai = HttpResources(api, AI_CONFIGS)
        self.prompts = HttpPrompts(api)
        self.filters = HttpResources(api, FILTERS)
        self.backup = HttpBackup(api)
        self.admin = HttpAdmin(api)
        self.audit = HttpAudit(api)
        self.ops = HttpOps(api)
        self.host = host


def _with_rule(stats: LogStats, rule_id: str) -> LogStats:
    if stats.raw.get("rule_id") == rule_id:
        return stats
    return replace(stats, raw={**stats.raw, "rule_id": rule_id})


def _as_mapping(payload: Any) -> dict[str, Any]:
    return dict(payload) if isinstance(payload, Mapping) else {"value": payload}
