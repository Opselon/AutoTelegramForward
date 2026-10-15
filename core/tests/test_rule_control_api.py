"""Per-task API control surface (v17): settings registry, update_mask, multi-source.

These run against the REAL sqlite repositories and the REAL gRPC servicer, not
mocks — the bug this feature replaces was a persistence path that reported
success while silently writing the wrong value, which mocks cannot catch.
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.proto import autoforward_pb2 as pb

from core.domain import rule_settings as RS
from core.domain.entities import ForwardRule
from core.infrastructure.grpc_server.servicers import (
    ForwardRuleControlServicer,
    _apply_masked_update,
    _rule_pb,
    _v17_read_values,
)
from core.application.use_cases import ForwardRuleUseCases
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteForwardRuleRepository,
)

OWNER = 5094837833
OTHER = 111


class _Ctx:
    """Minimal grpc context: records what the servicer aborted with."""

    def __init__(self):
        self.code = None
        self.details = ""

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details

    async def abort(self, code, details=""):
        self.code = code
        self.details = details
        raise RuntimeError(details)


@pytest.fixture()
def env(tmp_path):
    """Real sqlite repo + the real use-case layer the servicer is wired to."""
    db = SqliteDatabase(str(tmp_path / "rules.db"))
    # forward_rules.session_id is a FK onto sessions, so the session has to exist
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES ('s1', '+1000', '', '', '', '', 1, 1, 0, 0)", ())
    repo = SqliteForwardRuleRepository(db)
    uc = ForwardRuleUseCases(repo)
    return repo, uc, ForwardRuleControlServicer(uc)


def _rule(owner=OWNER, **kw):
    base = dict(session_id="s1", source_chat_id="-100", target_chat_id="-900",
                owner_user_id=owner)
    base.update(kw)
    return ForwardRule(**base)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_every_registered_setting_round_trips_through_the_entity():
    """An entry that cannot be read back is control the API only appears to have."""
    probes = {"bool": True, "int": 7, "float": 0.35, "str": "zzz",
              "list": ["zzz"], "dict": {"zzz": "probe"}}
    for key, spec in RS.SETTING_SPECS.items():
        r = _rule()
        RS.apply_settings(r, {key: probes[spec.kind]})
        assert getattr(r, key) == probes[spec.kind], key


def test_registry_covers_both_directions_of_every_metadata_setting():
    r = _rule()
    # A write through the registry must land in the rule's stored metadata or on
    # a real field — never be swallowed.
    assert RS.apply_settings(r, {"remove_emojis": True}) == ["remove_emojis"]
    assert r.metadata.get("remove_emojis") is True
    assert RS.apply_settings(r, {"remove_emojis": True}) == []  # idempotent


def test_unknown_setting_is_rejected():
    with pytest.raises(RS.SettingError) as exc:
        RS.apply_settings(_rule(), {"not_a_setting": 1})
    assert exc.value.code == "unknown_setting"


@pytest.mark.parametrize("key,value,code", [
    ("remove_emojis", "yes", "invalid_type"),
    ("ai_max_tokens", 1.5, "invalid_type"),
    ("ai_max_tokens", 0, "out_of_range"),
    ("ai_max_tokens", 10 ** 9, "out_of_range"),
    ("ai_creativity", -1, "out_of_range"),
    ("block_voice", None, "invalid_type"),
    ("blacklist", "not-a-list", "invalid_type"),
    ("blacklist", [1, 2], "invalid_type"),
    ("replacements", ["not", "a", "dict"], "invalid_type"),
])
def test_invalid_values_are_rejected(key, value, code):
    with pytest.raises(RS.SettingError) as exc:
        RS.apply_settings(_rule(), {key: value})
    assert exc.value.code == code


def test_bool_accepts_the_json_int_forms():
    r = _rule()
    RS.apply_settings(r, {"remove_emojis": 1})
    assert r.remove_emojis is True
    RS.apply_settings(r, {"remove_emojis": 0})
    assert r.remove_emojis is False


def test_a_rejected_batch_writes_nothing():
    """Validation happens before mutation, so a bad key cannot half-apply a request."""
    r = _rule()
    before = RS.read_settings(r)
    with pytest.raises(RS.SettingError):
        RS.apply_settings(r, {"remove_emojis": True, "ai_max_tokens": -5})
    assert RS.read_settings(r) == before


def test_replace_all_resets_unlisted_settings():
    r = _rule()
    RS.apply_settings(r, {"remove_emojis": True, "header": "H"})
    RS.apply_settings(r, {"header": "H2"}, replace_all=True)
    assert r.header == "H2"
    assert r.remove_emojis is False


def test_schema_lists_every_setting_with_render_metadata():
    sch = RS.schema()
    assert sch["count"] == len(RS.SETTING_SPECS)
    assert {s["key"] for s in sch["settings"]} == set(RS.SETTING_SPECS)
    for entry in sch["settings"]:
        assert entry["kind"] in ("bool", "int", "float", "str", "list", "dict")
        assert entry["label"]
        assert "default" in entry


def test_settings_are_owner_agnostic_but_never_touch_identity_fields():
    """The settings registry must not be an escalation path to owner/tenant fields."""
    for dangerous in ("owner_user_id", "source_chat_id", "target_chat_id", "id"):
        assert dangerous not in RS.SETTING_SPECS


# --------------------------------------------------------------------------- #
# update_mask
# --------------------------------------------------------------------------- #
def test_mask_applies_zero_and_false_values():
    r = _rule(delay_seconds=30.0, is_paused=True, since_ts=999)
    proto = pb.ForwardRule(delay_seconds=0.0, is_paused=False, since_ts=0)
    _apply_masked_update(r, proto, ["delay_seconds", "is_paused", "since_ts"])
    assert r.delay_seconds == 0.0
    assert r.is_paused is False
    assert r.since_ts == 0


def test_legacy_present_only_path_cannot_express_zero():
    """Pins the reason update_mask exists: without it, zero means 'not provided'."""
    proto = pb.ForwardRule(delay_seconds=0.0, is_paused=False)
    assert not proto.delay_seconds and not proto.is_paused
    assert "delay_seconds" not in [k for k, v in proto.ListFields()]


def test_mask_is_exclusive():
    r = _rule()
    r.name = "keep me"
    proto = pb.ForwardRule(name="changed", description="also changed", priority=99)
    _apply_masked_update(r, proto, ["priority"])
    assert r.priority == 99
    assert r.name == "keep me"
    assert r.description == ""


def test_unknown_mask_field_raises_instead_of_silently_no_oping():
    with pytest.raises(ValueError, match="unknown update_mask field: nope"):
        _apply_masked_update(_rule(), pb.ForwardRule(), ["nope"])


def test_mask_round_trips_every_v17_field_through_the_wire():
    """Write each v17 field through a mask, then read it back via _rule_pb."""
    r = _rule()
    proto = pb.ForwardRule(
        source_chat_ids=["-100", "-200"],
        source_chat_names=["A", "B"],
        content_mode="TEXT_ONLY",
        delay_seconds=2.5,
        skip_history=True,
        since_ts=1700000000,
        ignore_edits=True,
        sync_edits=True,
        sync_deletes=True,
        trigger_events=["NEW_MESSAGE", "EDITED_MESSAGE"],
        topic_id="42",
        ai_secondary_config_id="sec-1",
        prompt_id="p-1",
        link_policy="STRIP_ALL",
        domain_allowlist=["a.com"],
        domain_blocklist=["b.com"],
        allowed_media_types=["photo"],
        split_long_caption=False,
        album_aggregation_window_seconds=1.25,
        intermediate_target_chat_id="-5",
        intermediate_mode="GATE",
        retry_policy_json='{"max_attempts": 3}',
        proxy_json='{"host": "p", "port": 1}',
    )
    mask = list(_MASK_NAMES)
    _apply_masked_update(r, proto, mask)
    out = _rule_pb(r)
    for name in mask:
        got, want = getattr(out, name), getattr(proto, name)
        if name.endswith("_json"):
            # compared as JSON, not as text: key order/spacing is not part of the contract
            assert json.loads(got or "{}") == json.loads(want or "{}"), name
        else:
            assert got == want, name


_MASK_NAMES = [
    "source_chat_ids", "source_chat_names", "content_mode", "delay_seconds",
    "skip_history", "since_ts", "ignore_edits", "sync_edits", "sync_deletes",
    "trigger_events", "topic_id", "ai_secondary_config_id", "prompt_id",
    "link_policy", "domain_allowlist", "domain_blocklist",
    "allowed_media_types", "split_long_caption",
    "album_aggregation_window_seconds", "intermediate_target_chat_id",
    "intermediate_mode", "retry_policy_json", "proxy_json",
]


def test_v17_read_values_survives_a_rule_with_no_optional_state():
    values = _v17_read_values(_rule())
    assert values["source_chat_ids"] == ["-100"]  # primary stays authoritative
    assert values["retry_policy_json"] == ""
    assert values["proxy_json"] == ""
    assert values["content_mode"] == "AUTO"


# --------------------------------------------------------------------------- #
# Multi-source
# --------------------------------------------------------------------------- #
def test_source_chat_ids_dedupes_and_mirrors_the_primary():
    r = _rule()
    r.source_chat_ids = ["-200", "-100", "-200"]
    assert r.source_chat_ids == ["-200", "-100"]
    assert r.source_chat_id == "-200"


def test_clearing_the_source_list_leaves_the_primary_intact():
    r = _rule()
    r.source_chat_ids = []
    assert r.source_chat_ids == ["-100"]


def test_list_active_by_source_matches_every_source_of_a_task(env):
    repo, _uc, _ = env

    async def go():
        multi = _rule()
        multi.source_chat_ids = ["-100", "-200", "-300"]
        await repo.add(multi)
        single = _rule(source_chat_id="-700")
        await repo.add(single)

        for cid, expected in (("-100", 1), ("-200", 1), ("-300", 1),
                              ("-700", 1), ("-800", 0)):
            found = await repo.list_active_by_source(cid)
            assert len(found) == expected, cid
        assert (await repo.list_active_by_source("-200"))[0].id == multi.id

    asyncio.run(go())


def test_multi_source_survives_a_real_write_and_read(env):
    repo, _uc, _ = env

    async def go():
        r = _rule()
        r.source_chat_ids = ["-100", "-200"]
        r.source_chat_names = ["First", "Second"]
        await repo.add(r)
        back = await repo.get_by_id(r.id)
        assert back.source_chat_ids == ["-100", "-200"]
        assert back.source_chat_names == ["First", "Second"]

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# RPCs
# --------------------------------------------------------------------------- #
def test_get_rule_settings_returns_snapshot_and_schema(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule())
        resp = await svc.GetRuleSettings(
            pb.RuleSettingsRequest(rule_id=r.id, owner_user_id=OWNER), _Ctx())
        assert resp.success
        assert set(json.loads(resp.settings_json)) == set(RS.SETTING_SPECS)
        assert json.loads(resp.schema_json)["count"] == len(RS.SETTING_SPECS)

    asyncio.run(go())


def test_update_rule_settings_persists(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule())
        resp = await svc.UpdateRuleSettings(pb.UpdateRuleSettingsRequest(
            rule_id=r.id, owner_user_id=OWNER,
            settings_json=json.dumps({"remove_emojis": True, "header": "Hi"}),
        ), _Ctx())
        assert resp.success and "2 setting(s)" in resp.message
        back = await repo.get_by_id(r.id)
        assert back.remove_emojis is True and back.header == "Hi"

    asyncio.run(go())


def test_a_rejected_settings_update_persists_nothing(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule())
        resp = await svc.UpdateRuleSettings(pb.UpdateRuleSettingsRequest(
            rule_id=r.id, owner_user_id=OWNER,
            settings_json=json.dumps({"remove_emojis": True, "bogus_key": 1}),
        ), _Ctx())
        assert resp.success is False and resp.error_code == "unknown_setting"
        back = await repo.get_by_id(r.id)
        assert back.remove_emojis is False

    asyncio.run(go())


def test_settings_rpcs_are_tenant_scoped(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule(owner=OWNER))
        ctx = _Ctx()
        resp = await svc.GetRuleSettings(
            pb.RuleSettingsRequest(rule_id=r.id, owner_user_id=OTHER), ctx)
        assert resp.success is False and resp.error_code == "not_found"
        ctx2 = _Ctx()
        resp2 = await svc.UpdateRuleSettings(pb.UpdateRuleSettingsRequest(
            rule_id=r.id, owner_user_id=OTHER,
            settings_json=json.dumps({"remove_emojis": True}),
        ), ctx2)
        assert resp2.success is False and resp2.error_code == "not_found"
        assert (await repo.get_by_id(r.id)).remove_emojis is False

    asyncio.run(go())


def test_settings_rpcs_fail_closed_without_an_owner(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule())
        ctx = _Ctx()
        resp = await svc.GetRuleSettings(
            pb.RuleSettingsRequest(rule_id=r.id, owner_user_id=0), ctx)
        assert resp.success is False and resp.error_code == "unauthorized"
        assert ctx.code is not None  # the client is told, not just an empty body

    asyncio.run(go())


def test_settings_schema_rpc_needs_no_rule(env):
    _, _, svc = env

    async def go():
        resp = await svc.GetRuleSettingsSchema(pb.RuleSettingsSchemaRequest(), _Ctx())
        assert resp.success
        assert json.loads(resp.schema_json)["version"] == RS.SCHEMA_VERSION

    asyncio.run(go())


def test_malformed_settings_json_is_rejected(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule())
        for bad in ("{not json", "[1,2,3]"):
            resp = await svc.UpdateRuleSettings(pb.UpdateRuleSettingsRequest(
                rule_id=r.id, owner_user_id=OWNER, settings_json=bad), _Ctx())
            assert resp.success is False

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# UpdateRule RPC with a mask
# --------------------------------------------------------------------------- #
def test_update_rule_rpc_with_mask_sets_a_zero_and_leaves_the_rest(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule(delay_seconds=45.0))
        r.name = "untouched"
        await uc.update(r)
        proto = pb.ForwardRule(id=r.id, owner_user_id=OWNER, delay_seconds=0.0)
        out = await svc.UpdateRule(
            pb.UpdateRuleRequest(rule=proto, update_mask=["delay_seconds"]), _Ctx())
        assert out.delay_seconds == 0.0
        back = await repo.get_by_id(r.id)
        assert back.delay_seconds == 0.0
        assert back.name == "untouched"

    asyncio.run(go())


def test_update_rule_rpc_with_mask_rejects_an_unknown_field(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule())
        ctx = _Ctx()
        proto = pb.ForwardRule(id=r.id, owner_user_id=OWNER)
        with pytest.raises(RuntimeError):
            await svc.UpdateRule(
                pb.UpdateRuleRequest(rule=proto, update_mask=["nope"]), ctx)
        assert ctx.code is not None

    asyncio.run(go())


def test_update_rule_rpc_without_a_mask_keeps_legacy_behaviour(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule())
        proto = pb.ForwardRule(id=r.id, owner_user_id=OWNER, name="renamed",
                               delay_seconds=0.0)
        out = await svc.UpdateRule(pb.UpdateRuleRequest(rule=proto), _Ctx())
        assert out.name == "renamed"
        # no mask, zero is indistinguishable from unset -> the old default stands
        assert out.delay_seconds == 0.0

    asyncio.run(go())


def test_update_rule_rpc_can_set_multi_source(env):
    repo, uc, svc = env

    async def go():
        r = await uc.create(_rule())
        proto = pb.ForwardRule(id=r.id, owner_user_id=OWNER,
                               source_chat_ids=["-100", "-200"])
        out = await svc.UpdateRule(
            pb.UpdateRuleRequest(rule=proto, update_mask=["source_chat_ids"]), _Ctx())
        assert list(out.source_chat_ids) == ["-100", "-200"]
        assert out.source_chat_id == "-100"
        found = await repo.list_active_by_source("-200")
        assert [x.id for x in found] == [r.id]

    asyncio.run(go())


def test_created_rule_exposes_the_settings_snapshot(env):
    repo, uc, svc = env

    async def go():
        proto = pb.ForwardRule(
            session_id="s1", source_chat_id="-100", target_chat_id="-900",
            owner_user_id=OWNER, name="snapshot",
            source_chat_ids=["-100", "-200"], delay_seconds=3.0,
            trigger_events=["NEW_MESSAGE", "EDITED_MESSAGE"],
        )
        out = await svc.CreateRule(pb.CreateRuleRequest(rule=proto), _Ctx())
        assert list(out.source_chat_ids) == ["-100", "-200"]
        assert out.delay_seconds == 3.0
        assert list(out.trigger_events) == ["NEW_MESSAGE", "EDITED_MESSAGE"]
        assert set(json.loads(out.settings_json)) == set(RS.SETTING_SPECS)

    asyncio.run(go())
