"""The CLI has to read the dead-letter row the way the API writes it.

`atf data dlq list` projected `error` and `retry_count` onto the row the core
sends. Neither name exists: the core sends `last_error` and `attempts` (the
columns its queue repository writes on both backends). Every row therefore
rendered "—" and "0" — a table that looked authoritative and told the operator
nothing about why their messages were stuck.

The refusal path is pinned too: the core answers a replay with documented
result codes, and `not_found` / `already_pending` / `already_sent` /
`replay_failed` all mean the entry was *not* re-queued. Printing "Replaying
message …" for those is the same false success the API used to return.

These are run against the real command functions with a stub context, so the
assertions are about what the CLI actually prints.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from atfcli.commands import data as cli_data  # noqa: E402
from atfcli.console import Console  # noqa: E402


class _Out:
    """A Console stand-in that records what would have been printed."""

    def __init__(self) -> None:
        self.lines: List[str] = []
        self.json_payloads: List[Any] = []
        self.tables: List[Tuple[Tuple[str, ...], List[List[str]]]] = []

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def title(self, text: str) -> None:
        self.lines.append(text)

    def json(self, payload: Any) -> None:
        self.json_payloads.append(payload)

    def table(self, headers, rows, empty: str = "") -> None:
        self.tables.append((tuple(headers), [list(r) for r in rows]))
        if not rows and empty:
            self.lines.append(empty)

    def emit(self, payload: Any, message: str) -> None:
        self.lines.append(message)


class _Api:
    def __init__(self, get=None, post=None, delete=None) -> None:
        self._get, self._post, self._delete = get, post, delete

    def get(self, path: str) -> Any:
        return self._get(path) if self._get else None

    def post(self, path: str, payload: Any = None) -> Any:
        return self._post(path, payload) if self._post else None

    def delete(self, path: str) -> Any:
        return self._delete(path) if self._delete else None


class _Ctx:
    """A stub context: the transport, the console, and the use cases over both.

    `uc` is the real composition root built on the stub transport, so these tests
    drive the same code path the CLI runs — only the transport and the printer are
    fakes. The assertions below are about what gets printed, unchanged.
    """

    def __init__(self, api: _Api) -> None:
        self.api = api
        self.out = _Out()
        self.repo = _Repo()
        self._uc = None

    @property
    def uc(self):
        if self._uc is None:
            from atfcli.application.container import build_use_cases
            from atfcli.infrastructure.http.gateways import HttpGateways

            self._uc = build_use_cases(HttpGateways(self.api))
        return self._uc

    def want_json(self) -> bool:
        return False


class _Args:
    def __init__(self, **kw) -> None:
        self.json = kw.get("json", False)
        self.id = kw.get("id", "")


class _Repo:
    """`confirm_or_fail` says yes, so the destructive path is exercised."""

    def confirm(self, question: str, default: bool = False) -> bool:
        return True


#: A row exactly as the API's own projection emits it.
_API_ROW: Dict[str, Any] = {
    "id": "dlq_job-42",
    "rule_id": "rule-a-9f3c",
    "source_chat_id": "-100111",
    "source_message_id": 7788,
    "target_chat_id": "-100777",
    "attempts": 5,
    "last_error": "telegram flood wait: retry after 30s",
    "error_category": "telegram",
    "dead_lettered_at": 1_700_000_000,
}


def _list_ctx() -> _Ctx:
    # Exactly the envelope the Go handler sends.
    return _Ctx(_Api(get=lambda path: {"jobs": [_API_ROW], "entries": [_API_ROW], "total": 1}))


def test_list_columns_come_from_the_row_the_api_sends() -> None:
    ctx = _list_ctx()
    cli_data._dlq_list(ctx, _Args(json=False))

    headers, rows = ctx.out.tables[0]
    assert headers == ("ID", "RULE", "ERROR", "RETRIES")
    assert len(rows) == 1
    row = rows[0]
    # The error text is shown, not a placeholder: the old column name `error`
    # is absent from the API's projection, so this used to be "—".
    assert "flood wait" in row[2]
    assert row[2] != "—"
    # And the attempt count is the real one, not a defaulted 0.
    assert row[3] == "5"


def test_json_output_keeps_the_rows_untouched() -> None:
    ctx = _list_ctx()
    cli_data._dlq_list(ctx, _Args(json=True))
    payload = ctx.out.json_payloads[0]
    assert payload["count"] == 1
    assert payload["dlq"][0]["last_error"] == _API_ROW["last_error"]


def test_the_envelope_is_not_rendered_as_a_row() -> None:
    """The regression: the helper returned the whole payload as one entry."""
    ctx = _list_ctx()
    cli_data._dlq_list(ctx, _Args(json=False))

    headers, rows = ctx.out.tables[0]
    assert len(rows) == 1, "the response envelope was rendered as an extra row"
    assert rows[0][0] == "dlq_job-4"[:8], rows[0]
    assert "jobs" not in rows[0][2]


def test_list_still_prints_a_placeholder_for_a_row_without_an_error() -> None:
    """A row with no error text is a different statement from a wrong column."""
    ctx = _Ctx(_Api(get=lambda path: {"jobs": [{"id": "dlq_1", "rule_id": "r", "attempts": 0}], "total": 1}))
    cli_data._dlq_list(ctx, _Args(json=False))
    row = ctx.out.tables[0][1][0]
    assert row[2] == "—" and row[3] == "0"


def test_a_refused_replay_is_reported_not_narrated_as_success() -> None:
    for code in ("not_found", "already_pending", "already_sent", "replay_failed"):
        ctx = _Ctx(_Api(post=lambda path, payload=None, code=code: {
            "success": False, "message": code, "error_code": code,
        }))
        cli_data._dlq_retry(ctx, _Args(id="dlq_job-42"))
        printed = "\n".join(ctx.out.lines)
        assert code in printed, f"{code} was not surfaced"
        assert "Replaying message" not in printed, (
            f"{code} was narrated as a successful replay"
        )


def test_a_refusal_without_a_success_field_is_still_a_refusal() -> None:
    """The live shape: the API omitted `success` on a refused replay.

    Reading `.get("success") is False` saw `None`, not False, and the CLI
    announced a replay that the core had refused.
    """
    ctx = _Ctx(_Api(post=lambda path, payload=None: {
        "message": "not_found", "error_code": "not_found",
    }))
    cli_data._dlq_retry(ctx, _Args(id="dlq_job-42"))

    printed = "\n".join(ctx.out.lines)
    assert "not_found" in printed
    assert "Replaying message" not in printed, (
        "a body without a result flag was narrated as a replay"
    )


def test_a_body_without_a_result_flag_is_not_a_purge() -> None:
    ctx = _Ctx(_Api(delete=lambda path: {"message": "purge_failed"}))
    cli_data._dlq_purge(ctx, _Args())
    printed = "\n".join(ctx.out.lines)
    assert "purge failed" in printed
    assert "Purged dead letter queue" not in printed


def test_an_accepted_replay_still_narrates_the_replay() -> None:
    ctx = _Ctx(_Api(post=lambda path, payload=None: {"success": True, "message": "requeued"}))
    cli_data._dlq_retry(ctx, _Args(id="dlq_job-42"))
    assert "Replaying message" in "\n".join(ctx.out.lines)


def test_purge_reports_the_count_or_the_failure() -> None:
    ctx = _Ctx(_Api(delete=lambda path: {"success": True, "message": "purged 3"}))
    cli_data._dlq_purge(ctx, _Args())
    assert any("purged 3" in line for line in ctx.out.lines)

    ctx = _Ctx(_Api(delete=lambda path: {"success": False, "error_code": "purge_failed"}))
    cli_data._dlq_purge(ctx, _Args())
    assert any("purge failed" in line for line in ctx.out.lines)


def _json_ctx(api: _Api) -> Tuple[_Ctx, io.StringIO]:
    """A context whose `out` is the real Console in --json mode.

    `--json` promises exactly one JSON document on stdout. The refusal paths
    reported through `out.line`, which the Console suppresses in JSON mode, so a
    refused replay printed nothing at all — a script waiting for a result body
    got an empty stream and a zero exit code.
    """
    ctx = _Ctx(api)
    buffer = io.StringIO()
    ctx.out = Console(json_mode=True, stream=buffer, color=False)
    return ctx, buffer


def test_a_refused_replay_still_emits_one_json_document() -> None:
    ctx, buffer = _json_ctx(
        _Api(post=lambda path, payload=None: {
            "success": False, "message": "not_found", "error_code": "not_found",
        }),
    )
    cli_data._dlq_retry(ctx, _Args(id="dlq_job-42"))

    payload = json.loads(buffer.getvalue())  # exactly one document, or it raises
    assert payload["success"] is False
    assert payload["error_code"] == "not_found"


def test_a_body_without_a_result_flag_still_emits_json() -> None:
    ctx, buffer = _json_ctx(
        _Api(post=lambda path, payload=None: {"message": "not_found"}),
    )
    cli_data._dlq_retry(ctx, _Args(id="dlq_job-42"))

    payload = json.loads(buffer.getvalue())
    assert payload == {"message": "not_found"}


def test_a_refused_purge_still_emits_one_json_document() -> None:
    ctx, buffer = _json_ctx(
        _Api(delete=lambda path: {"success": False, "error_code": "purge_failed"}),
    )
    cli_data._dlq_purge(ctx, _Args())

    payload = json.loads(buffer.getvalue())
    assert payload["success"] is False


def test_a_successful_replay_emits_one_json_document_too() -> None:
    ctx, buffer = _json_ctx(
        _Api(post=lambda path, payload=None: {"success": True, "message": "requeued"}),
    )
    cli_data._dlq_retry(ctx, _Args(id="dlq_job-42"))

    payload = json.loads(buffer.getvalue())
    assert payload["success"] is True
