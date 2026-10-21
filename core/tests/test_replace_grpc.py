"""Wire-level tests for ReplaceRuleControlService.

A real ``grpc.aio`` server, the real servicer, real SQLite repositories. That is
the only way to pin the contract the Go API and the mobile client hit: what an
absent field means on an update, that zero ownership is refused, and that a
foreign id is indistinguishable from a missing one.
"""

from __future__ import annotations

import asyncio

import grpc
import pytest

from core.application.replace_rules import ReplaceRuleService
from core.application.use_cases import ForwardRuleUseCases
from core.domain.entities import ForwardRule, TelegramSession
from core.infrastructure.grpc_server.replace_servicer import ReplaceRuleControlServicer
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteForwardRuleRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.proto import autoforward_pb2 as pb
from core.proto import autoforward_pb2_grpc as pb_grpc

OWNER = 77
OTHER = 88


@pytest.fixture()
def db(tmp_path):
    database = SqliteDatabase(str(tmp_path / "replace-grpc.db"))
    yield database
    database.close()


@pytest.fixture()
async def stub(db):
    repo = SqliteForwardRuleRepository(db)
    service = ReplaceRuleService(ForwardRuleUseCases(repo))
    server = grpc.aio.server()
    pb_grpc.add_ReplaceRuleControlServiceServicer_to_server(
        ReplaceRuleControlServicer(service), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield pb_grpc.ReplaceRuleControlServiceStub(channel), repo
    finally:
        await channel.close()
        await server.stop(None)


async def _seed(db, repo, owner=OWNER, rule_ids=("t1",)):
    session_id = f"sess-{owner}"
    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(id=session_id, phone_number="+100****0000", is_active=True,
                        is_authorized=True, owner_user_id=owner)
    )
    for index, rid in enumerate(rule_ids, start=1):
        await repo.add(ForwardRule(
            id=rid, session_id=session_id, name=f"Task {index}",
            source_chat_id=f"-10011111{index}", target_chat_id=f"-10022222{index}",
            is_active=True, owner_user_id=owner,
        ))


def _create_request(owner=OWNER, **fields):
    rule_fields = {"source": "a", "to": "b", "task_ids": ["t1"], **fields}
    # Optional fields need HasField, so only set what the caller meant.
    return pb.CreateReplaceRuleRequest(
        owner_user_id=owner,
        rule=pb.ReplaceRuleInput(**rule_fields),
    )


async def _status(coro):
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await coro
    return exc.value.code()


class TestReplaceRuleWire:
    async def test_setup(self, db, stub):
        stubbed, repo = stub
        await _seed(db, repo)
        reply = await stubbed.CreateReplaceRule(_create_request())
        assert reply.id and reply.source == "a"
        page = await stubbed.ListReplaceRules(pb.ListReplaceRulesRequest(owner_user_id=OWNER))
        assert page.total == 1 and page.counts.enabled == 1
        assert page.rules[0].task_ids == ["t1"]

    async def test_zero_owner_is_refused(self, db, stub):
        stubbed, repo = stub
        await _seed(db, repo)
        code = await _status(stubbed.CreateReplaceRule(pb.CreateReplaceRuleRequest(
            owner_user_id=0, rule=pb.ReplaceRuleInput(source="a", to="b",
                                                      task_ids=["t1"]))))
        assert code == grpc.StatusCode.INVALID_ARGUMENT

    async def test_page_shape_travels_through_the_wire(self, db, stub):
        stubbed, repo = stub
        await _seed(db, repo)
        for i in range(3):
            await stubbed.CreateReplaceRule(_create_request(
                name=f"R{i}", source=f"s{i}", to=f"o{i}", task_ids=["t1"]))
        page = await stubbed.ListReplaceRules(pb.ListReplaceRulesRequest(
            owner_user_id=OWNER, query="R1", page=1, per_page=1))
        assert page.total == 1 and page.pages == 1
        assert page.rules[0].name == "R1"
        assert page.tasks[0].id == "t1"
        counts = await stubbed.ListReplaceRules(pb.ListReplaceRulesRequest(owner_user_id=OWNER))
        assert counts.total == 3

    async def test_partial_update_leaves_unsent_fields_alone(self, db, stub):
        stubbed, repo = stub
        await _seed(db, repo)
        created = await stubbed.CreateReplaceRule(_create_request(
            name="Full", source="promo", to="OFFER"))
        # A disable that sends NO other field must not blank the pattern.
        off = await stubbed.SetReplaceRuleEnabled(pb.SetReplaceRuleEnabledRequest(
            owner_user_id=OWNER, rule_id=created.id, enabled=False))
        assert off.enabled is False
        assert off.source == "promo" and off.to == "OFFER"
        # An update that only renames must leave mode and task list intact.
        renamed = await stubbed.UpdateReplaceRule(pb.UpdateReplaceRuleRequest(
            owner_user_id=OWNER, rule_id=created.id,
            rule=pb.ReplaceRuleInput(name="Renamed", task_ids=["t1"]),
        ))
        assert renamed.name == "Renamed"
        assert renamed.source == "promo" and list(renamed.task_ids) == ["t1"]
        stored = (await repo.get_owned("t1", OWNER)).replacements[created.id]
        assert stored["enabled"] is False and stored["to"] == "OFFER"

    async def test_test_preview_travels_the_wire(self, db, stub):
        stubbed, repo = stub
        await _seed(db, repo)
        draft = await stubbed.TestReplaceRule(pb.TestReplaceRuleRequest(
            owner_user_id=OWNER, sample="PROMO 50",
            rule=pb.ReplaceRuleInput(source="promo", to="OFFER"),
        ))
        assert draft.output == "OFFER 50" and draft.matches == 1

    async def test_foreign_rule_id_is_not_found(self, db, stub):
        stubbed, repo = stub
        await _seed(db, repo, OWNER)
        await _seed(db, repo, OTHER, ("o1",))
        created = await stubbed.CreateReplaceRule(_create_request())
        code = await _status(stubbed.GetReplaceRule(
            pb.GetReplaceRuleRequest(owner_user_id=OTHER, rule_id=created.id)))
        assert code == grpc.StatusCode.NOT_FOUND
        code = await _status(stubbed.DeleteReplaceRule(
            pb.DeleteReplaceRuleRequest(owner_user_id=OTHER, rule_id=created.id)))
        assert code == grpc.StatusCode.NOT_FOUND
        # The owner still has it.
        still = await stubbed.GetReplaceRule(
            pb.GetReplaceRuleRequest(owner_user_id=OWNER, rule_id=created.id))
        assert still.id == created.id

    async def test_delete_returns_removed_copy_count(self, db, stub):
        stubbed, repo = stub
        await _seed(db, repo, rule_ids=("t1", "t2"))
        created = await stubbed.CreateReplaceRule(_create_request(task_ids=["t1", "t2"]))
        gone = await stubbed.DeleteReplaceRule(pb.DeleteReplaceRuleRequest(
            owner_user_id=OWNER, rule_id=created.id))
        assert gone.ok is True and gone.removed == 2
