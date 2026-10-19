"""Regression tests for the wire contracts the CLI speaks.

Every test here failed before the fix it covers, and none of them needs the live
stack: a recording transport stands in for the API, and in-memory gateways stand
in for the HTTP adapters. The point is not coverage — it is that these particular
mistakes produced *wrong answers* rather than errors, so nothing else would have
caught them.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from atfcli.application.container import build_use_cases
from atfcli.application.gateways import Gateways
from atfcli.application.use_cases import ops as ops_uc
from atfcli.application.use_cases import tasks as tasks_uc
from atfcli.domain.errors import AtfError, UsageError
from atfcli.domain.models import SimulationResult, TaskDraft
from atfcli.domain.resources import AI_CONFIGS, CATALOGUE, PROMPTS
from atfcli.infrastructure.http.gateways import HttpGateways


class RecordingApi:
    """A transport that records calls and answers with canned payloads."""

    def __init__(self, answers: Mapping[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._answers = dict(answers or {})

    def _record(self, method: str, path: str, **kw: Any) -> Any:
        self.calls.append((method.upper(), path, kw))
        answer = self._answers.get(f"{method.upper()} {path}", self._answers.get(method.upper(), {}))
        return answer

    get = lambda self, path, **kw: self._record("get", path, **kw)  # noqa: E731
    post = lambda self, path, **kw: self._record("post", path, **kw)  # noqa: E731
    put = lambda self, path, **kw: self._record("put", path, **kw)  # noqa: E731
    patch = lambda self, path, **kw: self._record("patch", path, **kw)  # noqa: E731
    delete = lambda self, path, **kw: self._record("delete", path, **kw)  # noqa: E731


def _gateways(api: Any) -> Any:
    return HttpGateways(api, host=None)


# --------------------------------------------------------------- rules: simulate
def test_simulate_posts_the_field_names_the_handler_reads() -> None:
    """`text`/`sender` are ignored by the handler; the keys are `sample_*`.

    Sending the wrong names did not fail — it ran the simulation against an empty
    message and reported "no match" for a rule that would have matched.
    """
    api = RecordingApi({"POST": {"matched": True, "match_reason": "matched", "rendered_text": "hi there"}})
    result = _gateways(api).tasks.simulate("rule-1", "hello", sender="@someone")

    method, path, kw = api.calls[-1]
    assert (method, path) == ("POST", "/api/simulate")
    assert kw["payload"]["sample_text"] == "hello"
    assert kw["payload"]["sample_sender_id"] == "@someone"
    assert "text" not in kw["payload"] and "sender" not in kw["payload"]
    # The answer is read from the rendered text, not from a `transformed_text` key.
    assert isinstance(result, SimulationResult)
    assert result.would_forward is True
    assert result.transformed_text == "hi there"
    assert result.action == ""  # no forward_mode in this answer, so no invented action


def test_simulate_without_a_sender_omits_the_key() -> None:
    api = RecordingApi({"POST": {"matched": False}})
    _gateways(api).tasks.simulate("rule-1", "hello")
    assert "sample_sender_id" not in api.calls[-1][2]["payload"]


# -------------------------------------------------------------- rules: settings
def test_settings_reset_uses_the_reset_list_not_a_null_value() -> None:
    """`{"clean_buttons": null}` stores a null; `{"reset": ["clean_buttons"]}` clears it."""
    api = RecordingApi({"PATCH": {"success": True}})
    _gateways(api).tasks.update_settings("rule-1", {}, resets=["clean_buttons"])

    method, path, kw = api.calls[-1]
    assert (method, path) == ("PATCH", "/api/rules/rule-1/settings")
    assert kw["payload"] == {"settings": {}, "reset": ["clean_buttons"]}


def test_settings_replace_sets_the_body_flag_rather_than_the_verb() -> None:
    """Both verbs reach one handler; `replace_all` is the flag that matters."""
    api = RecordingApi({"PATCH": {"success": True}})
    _gateways(api).tasks.update_settings("rule-1", {"clean_buttons": True}, replace_all=True)

    method, _, kw = api.calls[-1]
    assert method == "PATCH"
    assert kw["payload"] == {"settings": {"clean_buttons": True}, "replace_all": True}


def test_settings_set_surfaces_the_rejected_key_from_error_code() -> None:
    class Rejecting:
        def update_settings(self, task_id, values, *, replace_all=False, resets=()):
            return {"success": False, "message": "unknown setting", "error_code": "clean_buttns"}

    class StubTasks(tasks_uc.Tasks):
        def find(self, ref):
            return _task()

    use_case = StubTasks(Rejecting())
    with pytest.raises(AtfError) as caught:
        use_case.set_settings("rule-1", {"clean_buttns": True})
    assert "clean_buttns" in str(caught.value.hint or "")


# ------------------------------------------------------------- rules: lifecycle
def _task(**overrides: Any):
    payload = {
        "id": "aaaaaaaa-1111-2222-3333-444444444444",
        "name": "Sample",
        "source_chat_ids": ["-1001"],
        "target_chat_id": "-2002",
        "is_active": True,
    }
    payload.update(overrides)
    from atfcli.domain.models import Task

    return Task.from_payload(payload)


def test_enable_on_an_enabled_task_does_not_flip_it_off() -> None:
    """The API's only mutation is `toggle`, so `enable` must not call it blindly."""
    calls: list[str] = []

    class Gateway:
        def list_tasks(self):
            return [_task(is_active=True)]

        def task_action(self, task_id, action):
            calls.append(action)
            return {"is_active": False}  # what a blind toggle would do

    use_case = tasks_uc.Tasks(Gateway())
    outcome = use_case.set_state("aaaaaaaa", "enable")

    assert calls == [], "enable must not toggle a task that is already enabled"
    assert outcome.payload["is_active"] is True
    assert "already" in outcome.detail


def test_enable_corrects_a_toggle_that_flipped_the_wrong_way() -> None:
    """If the server reports the opposite state, ask once more until the promise holds."""
    calls: list[str] = []

    class Gateway:
        def list_tasks(self):
            return [_task(is_active=False)]

        def task_action(self, task_id, action):
            calls.append(action)
            # First attempt disagrees with the promise; the second one delivers it.
            return {"is_active": len(calls) > 1}

    use_case = tasks_uc.Tasks(Gateway())
    outcome = use_case.set_state("aaaaaaaa", "enable")

    assert calls == ["toggle", "toggle"]
    assert outcome.payload["is_active"] is True
    assert outcome.follow_up["enabled"] is True


def test_enable_on_a_disabled_task_acts_once_and_reports_the_server_state() -> None:
    calls: list[str] = []

    class Gateway:
        def list_tasks(self):
            return [_task(is_active=False)]

        def task_action(self, task_id, action):
            calls.append(action)
            return {"is_active": True, "success": True}

    use_case = tasks_uc.Tasks(Gateway())
    outcome = use_case.set_state("aaaaaaaa", "enable")
    assert calls == ["toggle"]
    assert outcome.payload["is_active"] is True


def test_removing_a_source_keeps_titles_positionally_aligned() -> None:
    """Dropping titles by value removed an unrelated source sharing the title."""
    sent: list[dict[str, Any]] = []

    class Gateway:
        def list_tasks(self):
            return [
                _task(source_chat_ids=["-1", "-2", "-3"], source_chat_names=["news", "news", "other"])
            ]

        def update_task(self, task_id, changes):
            sent.append(dict(changes))
            return {"success": True}

    use_case = tasks_uc.Tasks(Gateway())
    use_case.remove_source("aaaaaaaa", "-2")

    assert sent[-1]["source_chat_ids"] == ["-1", "-3"]
    # `-2` was index 1, so index 1's title goes too — and the surviving "news"
    # stays attached to `-1`.
    assert sent[-1]["source_chat_names"] == ["news", "other"]


def test_creating_a_task_echoes_the_source_and_target() -> None:
    class Gateway:
        def create_task(self, payload):
            return {"id": "bbbbbbbb-1111-2222-3333-444444444444", **payload}

        def list_tasks(self):
            return []

    outcome = tasks_uc.Tasks(Gateway()).create(TaskDraft(source="-100", target="-200", name="x"))
    assert outcome.follow_up["source"] == "-100"
    assert outcome.follow_up["target"] == "-200"


def test_task_reads_the_rule_dto_s_own_field_names() -> None:
    """`is_active` / `source_chat_name` are what the API sends. Reading
    `is_enabled` / `source_chat_title` made every task render as disabled and
    made every source title blank."""
    task = _task(is_active=True, source_chat_name="News Channel", target_chat_name="Mirror")
    assert task.enabled is True
    assert task.state.value == "on"
    assert task.source_title == "News Channel"
    assert task.target_title == "Mirror"


def test_task_draft_sends_is_active_so_a_new_task_actually_runs() -> None:
    """The proto default for `is_active` is false, so it must be sent explicitly."""
    draft = TaskDraft(source="-100", target="-200", name="x", source_title="News")
    payload = draft.to_payload()
    assert payload["is_active"] is True
    assert payload["source_chat_name"] == "News"
    assert "is_enabled" not in payload and "source_chat_title" not in payload

    off = TaskDraft(source="-100", target="-200", enabled=False).to_payload()
    assert off["is_active"] is False


def test_create_explains_a_missing_session_instead_of_the_raw_reference_error() -> None:
    """`invalid_reference` reads like a malformed id; it means "no session sees it"."""

    class Gateway:
        def create_task(self, payload):
            raise AtfError("invalid_reference: the session or referenced row does not exist for this owner")

        def list_tasks(self):
            return []

    with pytest.raises(AtfError) as caught:
        tasks_uc.Tasks(Gateway()).create(TaskDraft(source="-100", target="-200"))
    assert "--session" in (caught.value.hint or "")


# --------------------------------------------------- generic resources: wire IO
def test_ai_config_body_uses_protojson_camel_case_names() -> None:
    api = RecordingApi({"POST": {"id": "cfg-1", "name": "x", "provider": "groq", "model": "m", "apiKey": "***"}})
    gateways = _gateways(api)
    gateways.ai.create_resource({"name": "x", "provider": "groq", "model": "m", "apiKey": "sk-1", "baseUrl": "https://x"})

    _, path, kw = api.calls[-1]
    assert path == "/api/ai-configs"
    assert {"apiKey", "baseUrl"} <= set(kw["payload"])
    assert "api_key" not in kw["payload"], "snake_case keys are discarded by the API"


def test_prompt_test_posts_prompt_id_not_template_id() -> None:
    """`template_id` is not a field the handler reads; the request then fails its
    own required-field check and reports a missing prompt."""
    api = RecordingApi({"POST": {}})
    _gateways(api).prompts.test_prompt(prompt_id="sys_pro_clean", ai_config_id="cfg-1", sample_text="hi")

    _, path, kw = api.calls[-1]
    assert path == "/api/prompts/test"
    assert kw["payload"]["prompt_id"] == "sys_pro_clean"
    assert "template_id" not in kw["payload"]


def test_prompt_collection_reads_the_templates_envelope() -> None:
    api = RecordingApi({"GET": {"templates": [{"id": "t1", "name": "One"}]}})
    rows = _gateways(api).prompts.list_resources()
    assert [row.id for row in rows] == ["t1"]
    assert PROMPTS.plural == "templates"


def test_backup_import_posts_the_bundle_as_text() -> None:
    """The handler wants `{"bundle": "<text>"}`; a parsed object is refused."""
    api = RecordingApi({"POST": {}})
    _gateways(api).backup.restore('{"version": 1}')
    _, path, kw = api.calls[-1]
    assert path == "/api/backup/import"
    # The bundle travels as the file's text: the handler refuses a parsed object.
    assert kw["payload"]["bundle"] == '{"version": 1}'
    assert isinstance(kw["payload"]["bundle"], str)


# ------------------------------------------------------------- settings schema
def test_settings_schema_unwraps_the_schema_envelope_before_filtering() -> None:
    """The registry lives under `schema`; reading `settings` off the envelope
    made every `--group` look like an empty registry."""
    payload = {
        "success": True,
        "schema": {
            "count": 2,
            "groups": ["cleanup", "ai"],
            "settings": [
                {"key": "clean_buttons", "group": "cleanup", "kind": "bool", "default": False},
                {"key": "ai_model", "group": "ai", "kind": "str", "default": ""},
            ],
        },
    }

    class Gateway:
        def settings_schema(self):
            return payload

    use_case = tasks_uc.Tasks(Gateway())
    everything = use_case.settings_schema()
    assert everything["total"] == 2
    assert everything["count"] == 2
    assert everything["groups"] == ["cleanup", "ai"]

    narrowed = use_case.settings_schema(group="cleanup")
    assert [spec["key"] for spec in narrowed["settings"]] == ["clean_buttons"]


# ------------------------------------------------------------------ key probing
def test_check_ai_key_treats_a_rejected_key_as_a_failure() -> None:
    """The route answers 200 with success=true even when the provider says no."""

    class Gateway:
        def check_ai_key(self, body):
            return {
                "success": True,
                "healthy": False,
                "latency_ms": 250,
                "detail": "Invalid API Key",
                "error_code": "invalid_api_key",
            }

    answer = ops_uc.Operations(Gateway()).check_ai_key(provider="groq", api_key="sk-bogus")
    assert answer["ok"] is False
    assert answer["detail"] == "Invalid API Key"


def test_check_ai_key_uses_this_route_s_snake_case_field_names() -> None:
    sent: list[dict[str, Any]] = []

    class Gateway:
        def check_ai_key(self, body):
            sent.append(dict(body))
            return {"success": True, "healthy": True, "latency_ms": 12}

    ops_uc.Operations(Gateway()).check_ai_key(
        provider="groq", api_key="sk-1", base_url="https://api.groq.com", config_id="cfg-1"
    )
    assert sent[-1] == {
        "provider": "groq",
        "api_key": "sk-1",
        "base_url": "https://api.groq.com",
        "ai_config_id": "cfg-1",
    }


def test_check_ai_key_refuses_an_empty_key_before_the_round_trip() -> None:
    class Gateway:
        def check_ai_key(self, body):
            raise AssertionError("must not be called with an empty key")

    with pytest.raises(UsageError):
        ops_uc.Operations(Gateway()).check_ai_key(provider="groq", api_key="   ")


# ------------------------------------------------------------------ composition
def test_resource_use_cases_are_reachable_by_spec() -> None:
    """Each catalogued resource resolves to its own use case, with its own spec."""
    bundle = build_use_cases(_gateways(RecordingApi()))
    for group, spec in CATALOGUE.items():
        resolved = bundle.resources_for(spec)
        assert resolved.spec is spec, f"{group} resolved to the wrong spec"


def test_use_case_bundles_implement_the_gateway_protocols() -> None:
    """The container wires ports into use cases; a missing method fails here."""
    bundle = build_use_cases(_gateways(RecordingApi()))
    for field in ("tasks", "sessions", "logs", "delivery", "ai", "prompts", "filters", "backup", "admin", "audit", "ops"):
        assert getattr(bundle, field) is not None
    assert isinstance(Gateways, type), "Gateways must stay a runtime-checkable Protocol bundle"


def test_ai_config_spec_marks_the_key_as_editable_so_it_can_be_rotated() -> None:
    assert AI_CONFIGS.flag_map()["api_key"] == "apiKey"
    assert "apiKey" in AI_CONFIGS.editable
