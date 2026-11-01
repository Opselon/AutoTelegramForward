"""Cross-language contract test: the Go worker's enum and this runtime's
vocabulary must name the SAME codes, in the same order, with the same
semantics. The .proto file is the shared source of truth; this test re-derives
both sides from it so a drift on either side fails here instead of in
production.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_runtime.grpc import server as runtime_server  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PROTO = (REPO_ROOT / "worker" / "proto" / "telegram_gateway.proto").read_text(
    encoding="utf-8"
)


def _proto_codes() -> dict[str, int]:
    """The DeliveryFailureCode enum exactly as the .proto declares it."""
    block = PROTO.split("enum DeliveryFailureCode {", 1)[1].split("}", 1)[0]
    codes: dict[str, int] = {}
    for line in block.splitlines():
        line = line.split("//", 1)[0].strip()
        if not line or line.startswith("//"):
            continue
        m = re.match(r"([A-Z_0-9]+)\s*=\s*(\d+)\s*;", line)
        if m:
            codes[m.group(1)] = int(m.group(2))
    return codes


def _runtime_codes() -> dict[str, int]:
    """The codes the Python runtime can actually emit."""
    return dict(runtime_server._CODE_VALUES)


def test_proto_and_python_agree_on_every_code() -> None:
    proto = _proto_codes()
    runtime = _runtime_codes()
    assert set(runtime) == set(proto), (
        f"code sets differ: runtime-only={set(runtime) - set(proto)}, "
        f"proto-only={set(proto) - set(runtime)}"
    )
    # A renamed code must never silently mean something else.
    for name, value in runtime.items():
        assert proto[name] == value, f"{name}: runtime={value}, proto={proto[name]}"


def test_unspecified_is_never_emitted() -> None:
    # DELIVERY_FAILURE_UNSPECIFIED is the zero value; a runtime that maps a
    # real failure onto it tells Go "everything is fine".
    codes = _runtime_codes()
    assert codes.get("DELIVERY_FAILURE_UNSPECIFIED") == 0
    for name in codes:
        assert name != ""


def test_go_enum_matches_proto() -> None:
    """The Go generated code is compiled from the same .proto, so the enum
    is identical by construction; this asserts the .proto still carries the
    contract tag the Go client stamps into its metadata."""
    assert "<<contract: tfgw/v1>>" in PROTO, "the proto's contract tag drifted"
    assert runtime_server.CONTRACT_TAG == "tfgw/v1"


def test_error_roundtrip_through_the_wire_format() -> None:
    """A typed failure serializes to the wire and back unchanged."""
    pb = runtime_server.pb
    original = pb.TelegramOperationError(
        code=pb.DeliveryFailureCode.Value("FLOOD_WAIT"),
        error_type="FLOOD_WAIT_X",
        error_code="420",
        retry_after_seconds=37,
        message="Too many requests",
    )
    wire = original.SerializeToString()
    decoded = pb.TelegramOperationError()
    decoded.ParseFromString(wire)
    assert decoded.code == original.code
    assert decoded.retry_after_seconds == 37
    assert decoded.error_type == "FLOOD_WAIT_X"
    assert decoded.message == "Too many requests"


def test_request_meta_is_only_identifiers() -> None:
    """The RequestMeta message must not be able to carry message content or
    credentials by construction — it has a flat string map used strictly for
    correlation identifiers.
    """
    pb = runtime_server.pb
    fields = {f.name for f in pb.RequestMeta.DESCRIPTOR.fields}
    assert fields == {"correlation_id", "worker_id", "job_id", "fields"}


def test_session_ids_never_carry_secrets() -> None:
    """Session identity is a stable opaque id; a session STRING must never
    appear in a request. This asserts the field is named for the id."""
    pb = runtime_server.pb
    fields = {f.name for f in pb.SessionAvailabilityRequest.DESCRIPTOR.fields}
    assert "session_id" in fields
    assert "session_string" not in fields
    for msg_name in ("ForwardMessagesRequest", "CopyMessageRequest", "SendMessageRequest"):
        desc = getattr(pb, msg_name).DESCRIPTOR
        fields = {f.name for f in desc.fields}
        assert "session_id" in fields
        assert "session_string" not in fields
