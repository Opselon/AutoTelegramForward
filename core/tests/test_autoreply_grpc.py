"""gRPC contract tests for AutoReplyControlService (v28).

These run a real ``grpc.aio`` server with the real servicer on an ephemeral
port and call it through the generated stub. That is the only way to prove the
surface a Go API or a Flutter client will actually hit: registration, field
numbers, error codes and tenant scoping all travel through the wire format.
"""

from __future__ import annotations

import asyncio
import json
import time

import grpc
import pytest

from core.application.autoreply.engine import AutoReplyEngine
from core.application.autoreply.service import AutoReplyService
from core.domain.autoreply import AutoReplySettings
from core.infrastructure.grpc_server.autoreply_servicer import AutoReplyControlServicer
from core.infrastructure.persistence.autoreply_repositories import AutoReplyRepository
from core.infrastructure.persistence.database import SqliteDatabase
from core.proto import autoforward_pb2 as pb
from core.proto import autoforward_pb2_grpc as pb_grpc

OWNER = 7
OTHER = 8


@pytest.fixture()
def repo(tmp_path):
    db = SqliteDatabase(str(tmp_path / "grpc.db"))
    yield AutoReplyRepository(db)
    db.close()


@pytest.fixture()
async def stub(repo):
    service = AutoReplyService(repo)
    server = grpc.aio.server()
    pb_grpc.add_AutoReplyControlServiceServicer_to_server(
        AutoReplyControlServicer(service, engine=AutoReplyEngine(repo)), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield pb_grpc.AutoReplyControlServiceStub(channel), repo, service
    finally:
        await channel.close()
        await server.stop(None)


async def _status(coro) -> grpc.StatusCode:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await coro
    return exc.value.code()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
async def test_settings_defaults_and_roundtrip(stub):
    ch, repo, _ = stub
    got = await ch.GetAutoReplySettings(pb.GetAutoReplySettingsRequest(owner_user_id=OWNER))
    assert got.enabled is False
    assert got.target_mode == "EVERYONE"
    assert got.tone == "friendly"

    update = pb.AutoReplySettings(
        enabled=True, tone="professional", response_length="medium",
        temperature=0.4, max_tokens=512, cooldown_seconds=30,
        active_days=[0, 2, 4], quiet_hours_start=23, quiet_hours_end=7,
        typing_simulation=False,
    )
    saved = await ch.UpdateAutoReplySettings(pb.UpdateAutoReplySettingsRequest(
        owner_user_id=OWNER, settings=update,
        update_mask=["enabled", "tone", "response_length", "temperature",
                     "max_tokens", "cooldown_seconds", "active_days",
                     "quiet_hours_start", "quiet_hours_end", "typing_simulation"],
    ))
    assert saved.enabled and saved.tone == "professional"
    assert saved.active_days == [0, 2, 4]
    assert saved.quiet_hours_start == 23 and saved.quiet_hours_end == 7
    assert repo.get_settings(OWNER).tone.value == "professional"


async def test_update_mask_does_not_reset_other_fields(stub):
    ch, repo, _ = stub
    await ch.UpdateAutoReplySettings(pb.UpdateAutoReplySettingsRequest(
        owner_user_id=OWNER, settings=pb.AutoReplySettings(max_tokens=777),
        update_mask=["max_tokens"],
    ))
    # A second, narrower update must not clear what the first one set.
    await ch.UpdateAutoReplySettings(pb.UpdateAutoReplySettingsRequest(
        owner_user_id=OWNER, settings=pb.AutoReplySettings(enabled=True),
        update_mask=["enabled"],
    ))
    settings = repo.get_settings(OWNER)
    assert settings.enabled is True
    assert settings.max_tokens == 777


async def test_unknown_mask_fields_are_ignored(stub):
    ch, repo, _ = stub
    before = repo.get_settings(OWNER).to_dict()
    await ch.UpdateAutoReplySettings(pb.UpdateAutoReplySettingsRequest(
        owner_user_id=OWNER, settings=pb.AutoReplySettings(enabled=True),
        update_mask=["enabled", "__proto__.owner_user_id", "nonsense"],
    ))
    after = repo.get_settings(OWNER).to_dict()
    assert after["enabled"] is True
    assert after["owner_user_id"] == before["owner_user_id"]


async def test_settings_require_an_owner(stub):
    ch, _, _ = stub
    code = await _status(ch.UpdateAutoReplySettings(pb.UpdateAutoReplySettingsRequest(
        owner_user_id=0, settings=pb.AutoReplySettings(enabled=True),
    )))
    assert code == grpc.StatusCode.UNAUTHENTICATED
    # Reads without an owner answer with defaults rather than aborting, so an
    # unlinked web account can render the panel instead of an error.
    empty = await ch.GetAutoReplySettings(pb.GetAutoReplySettingsRequest(owner_user_id=0))
    assert empty.enabled is False and empty.owner_user_id == 0


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
async def test_prompt_lifecycle_over_the_wire(stub):
    ch, _, _ = stub
    created = await ch.SaveAutoReplyPrompt(pb.SaveAutoReplyPromptRequest(
        owner_user_id=OWNER,
        prompt=pb.AutoReplyPrompt(
            name="Concise", system_prompt="Answer in one sentence.",
            user_prompt_template="Reply to: {text}", tags=["work"], category="sales",
        ),
    ))
    assert created.id and created.name == "Concise"
    assert list(created.tags) == ["work"]

    listed = await ch.ListAutoReplyPrompts(pb.ListAutoReplyPromptsRequest(owner_user_id=OWNER))
    assert [p.id for p in listed.prompts] == [created.id]

    clone = await ch.DuplicateAutoReplyPrompt(pb.DuplicateAutoReplyPromptRequest(
        owner_user_id=OWNER, prompt_id=created.id, name="Concise copy",
    ))
    assert clone.id != created.id and clone.name == "Concise copy"

    await ch.DeleteAutoReplyPrompt(pb.DeleteAutoReplyPromptRequest(
        owner_user_id=OWNER, prompt_id=created.id,
    ))
    listed = await ch.ListAutoReplyPrompts(pb.ListAutoReplyPromptsRequest(owner_user_id=OWNER))
    assert [p.id for p in listed.prompts] == [clone.id]


async def test_prompt_name_collision_is_already_exists(stub):
    ch, _, _ = stub
    await ch.SaveAutoReplyPrompt(pb.SaveAutoReplyPromptRequest(
        owner_user_id=OWNER, prompt=pb.AutoReplyPrompt(name="Dup", system_prompt="x"),
    ))
    code = await _status(ch.SaveAutoReplyPrompt(pb.SaveAutoReplyPromptRequest(
        owner_user_id=OWNER, prompt=pb.AutoReplyPrompt(name="Dup", system_prompt="y"),
    )))
    assert code == grpc.StatusCode.ALREADY_EXISTS


async def test_foreign_prompt_is_not_found(stub):
    ch, _, _ = stub
    created = await ch.SaveAutoReplyPrompt(pb.SaveAutoReplyPromptRequest(
        owner_user_id=OWNER, prompt=pb.AutoReplyPrompt(name="Mine", system_prompt="x"),
    ))
    code = await _status(ch.DeleteAutoReplyPrompt(pb.DeleteAutoReplyPromptRequest(
        owner_user_id=OTHER, prompt_id=created.id,
    )))
    assert code == grpc.StatusCode.NOT_FOUND


async def test_export_import_roundtrip(stub):
    ch, _, _ = stub
    await ch.SaveAutoReplyPrompt(pb.SaveAutoReplyPromptRequest(
        owner_user_id=OWNER,
        prompt=pb.AutoReplyPrompt(name="Exported", system_prompt="Be brief."),
    ))
    export = await ch.ExportAutoReplyPrompts(pb.ExportAutoReplyPromptsRequest(
        owner_user_id=OWNER,
    ))
    document = json.loads(export.document_json)
    assert document["prompts"][0]["name"] == "Exported"

    imported = await ch.ImportAutoReplyPrompts(pb.ImportAutoReplyPromptsRequest(
        owner_user_id=OTHER, document_json=export.document_json,
    ))
    assert imported.imported == 1
    assert imported.prompts[0].name == "Exported"

    again = await ch.ImportAutoReplyPrompts(pb.ImportAutoReplyPromptsRequest(
        owner_user_id=OTHER, document_json=export.document_json,
    ))
    assert (again.imported, again.skipped) == (0, 1)


async def test_import_of_garbage_is_invalid_argument(stub):
    ch, _, _ = stub
    code = await _status(ch.ImportAutoReplyPrompts(pb.ImportAutoReplyPromptsRequest(
        owner_user_id=OWNER, document_json="{not json",
    )))
    assert code == grpc.StatusCode.INVALID_ARGUMENT


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #
async def test_target_crud_and_toggle(stub):
    ch, repo, _ = stub
    added = await ch.AddAutoReplyTargets(pb.AddAutoReplyTargetsRequest(
        owner_user_id=OWNER,
        targets=[
            pb.AutoReplyTarget(chat_id="1001", title="Alice", is_enabled=True),
            pb.AutoReplyTarget(chat_id="1002", title="Bob", is_enabled=False, note="work"),
        ],
    ))
    assert added.total == 2 and added.enabled == 1

    updated = await ch.UpdateAutoReplyTarget(pb.UpdateAutoReplyTargetRequest(
        owner_user_id=OWNER,
        target=pb.AutoReplyTarget(chat_id="1002", title="Bob", is_enabled=True, note="work"),
    ))
    assert updated.chat_id == "1002" and updated.is_enabled is True
    assert repo.get_target_by_chat("1002", OWNER).is_enabled is True

    listed = await ch.ListAutoReplyTargets(pb.ListAutoReplyTargetsRequest(
        owner_user_id=OWNER, search="Ali",
    ))
    assert [t.chat_id for t in listed.targets] == ["1001"]

    removed = await ch.RemoveAutoReplyTargets(pb.RemoveAutoReplyTargetsRequest(
        owner_user_id=OWNER, chat_ids=["1001"],
    ))
    assert removed.success
    assert [t.chat_id for t in (
        await ch.ListAutoReplyTargets(pb.ListAutoReplyTargetsRequest(owner_user_id=OWNER))
    ).targets] == ["1002"]


async def test_targets_are_paginated_and_filtered_by_mode(stub):
    ch, _, _ = stub
    await ch.AddAutoReplyTargets(pb.AddAutoReplyTargetsRequest(
        owner_user_id=OWNER,
        targets=[pb.AutoReplyTarget(chat_id=str(2000 + i), title=f"C{i}",
                                    is_enabled=(i % 2 == 0)) for i in range(6)],
    ))
    page = await ch.ListAutoReplyTargets(pb.ListAutoReplyTargetsRequest(
        owner_user_id=OWNER, limit=2, offset=2,
    ))
    assert page.total == 6 and len(page.targets) == 2

    enabled = await ch.ListAutoReplyTargets(pb.ListAutoReplyTargetsRequest(
        owner_user_id=OWNER, mode="enabled",
    ))
    assert enabled.total == 3 and all(t.is_enabled for t in enabled.targets)

    disabled = await ch.ListAutoReplyTargets(pb.ListAutoReplyTargetsRequest(
        owner_user_id=OWNER, mode="disabled",
    ))
    assert disabled.total == 3 and not any(t.is_enabled for t in disabled.targets)


async def test_resolve_chat_without_transport_says_unresolved(stub):
    ch, _, _ = stub
    resp = await ch.ResolveAutoReplyChat(pb.ResolveAutoReplyChatRequest(
        owner_user_id=OWNER, query="@alice",
    ))
    assert resp.found is True
    assert resp.target.chat_id == "alice"
    blank = await _status(ch.ResolveAutoReplyChat(pb.ResolveAutoReplyChatRequest(
        owner_user_id=OWNER, query="   ",
    )))
    assert blank == grpc.StatusCode.INVALID_ARGUMENT


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
async def test_rule_crud_reorder_and_foreign_delete(stub):
    ch, _, _ = stub
    low = await ch.SaveAutoReplyRule(pb.SaveAutoReplyRuleRequest(
        owner_user_id=OWNER,
        rule=pb.AutoReplyRule(name="Generic", priority=1,
                              conditions_json=json.dumps({"keywords_any": ["hi"]})),
    ))
    high = await ch.SaveAutoReplyRule(pb.SaveAutoReplyRuleRequest(
        owner_user_id=OWNER,
        rule=pb.AutoReplyRule(name="VIP", priority=100, action="REPLY",
                              conditions_json=json.dumps({"keywords_any": ["invoice"]}),
                              cooldown_seconds=0),
    ))
    listed = await ch.ListAutoReplyRules(pb.ListAutoReplyRulesRequest(owner_user_id=OWNER))
    assert [r.name for r in listed.rules] == ["VIP", "Generic"]
    assert json.loads(listed.rules[0].conditions_json) == {"keywords_any": ["invoice"]}

    reordered = await ch.ReorderAutoReplyRules(pb.ReorderAutoReplyRulesRequest(
        owner_user_id=OWNER, rule_ids=[low.id, high.id],
    ))
    assert [r.name for r in reordered.rules] == ["Generic", "VIP"]

    code = await _status(ch.DeleteAutoReplyRule(pb.DeleteAutoReplyRuleRequest(
        owner_user_id=OTHER, rule_id=high.id,
    )))
    assert code == grpc.StatusCode.NOT_FOUND


async def test_rule_rejects_bad_regex(stub):
    ch, _, _ = stub
    code = await _status(ch.SaveAutoReplyRule(pb.SaveAutoReplyRuleRequest(
        owner_user_id=OWNER,
        rule=pb.AutoReplyRule(name="Broken", conditions_json='{"regex_any": ["["]}'),
    )))
    assert code == grpc.StatusCode.INVALID_ARGUMENT


# --------------------------------------------------------------------------- #
# Jobs / stats / preview
# --------------------------------------------------------------------------- #
async def _enqueue(repo, chat_id="1001", message_id=1):
    from core.domain.autoreply import IncomingMessage

    repo.save_settings(AutoReplySettings(owner_user_id=OWNER, enabled=True).normalize())
    await AutoReplyEngine(repo).handle_incoming(IncomingMessage(
        chat_id=chat_id, message_id=message_id, text="hello", owner_user_id=OWNER,
        session_id="s1",
    ))
    jobs, _ = repo.list_jobs(OWNER)
    return jobs[0]


async def test_jobs_list_cancel_and_isolation(stub):
    ch, repo, _ = stub
    job = await _enqueue(repo)
    listed = await ch.ListAutoReplyJobs(pb.ListAutoReplyJobsRequest(owner_user_id=OWNER))
    assert [j.id for j in listed.jobs] == [job.id]
    assert listed.jobs[0].status == "PENDING"

    # Another tenant sees nothing and cannot act on the id.
    other = await ch.ListAutoReplyJobs(pb.ListAutoReplyJobsRequest(owner_user_id=OTHER))
    assert other.total == 0
    code = await _status(ch.CancelAutoReplyJob(pb.AutoReplyJobRequest(
        owner_user_id=OTHER, job_id=job.id,
    )))
    assert code in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.FAILED_PRECONDITION)

    assert (await ch.CancelAutoReplyJob(pb.AutoReplyJobRequest(
        owner_user_id=OWNER, job_id=job.id))).success
    assert (await ch.ListAutoReplyJobs(pb.ListAutoReplyJobsRequest(
        owner_user_id=OWNER, status="CANCELLED"))).total == 1


async def test_retry_only_applies_to_a_failed_reply(stub):
    ch, repo, _ = stub
    job = await _enqueue(repo)
    # A pending job is not a failure; asking to retry it is a client mistake.
    code = await _status(ch.RetryAutoReplyJob(pb.AutoReplyJobRequest(
        owner_user_id=OWNER, job_id=job.id,
    )))
    assert code == grpc.StatusCode.INVALID_ARGUMENT

    # A worker claims it, then the generation fails: that is the state a retry
    # is for.
    claimed = repo.claim_next(worker_id="w1")
    assert claimed is not None
    repo.mark_failed(claimed.id, claimed.lease_token, "boom", "provider_error",
                     dead_letter=True)
    assert (await ch.RetryAutoReplyJob(pb.AutoReplyJobRequest(
        owner_user_id=OWNER, job_id=job.id))).success
    requeued = await ch.ListAutoReplyJobs(pb.ListAutoReplyJobsRequest(
        owner_user_id=OWNER, status="PENDING",
    ))
    assert [j.id for j in requeued.jobs] == [job.id]


async def test_stats_shape(stub):
    ch, repo, _ = stub
    await _enqueue(repo)
    stats = await ch.GetAutoReplyStats(pb.GetAutoReplyStatsRequest(owner_user_id=OWNER))
    assert stats.pending == 1
    assert json.loads(stats.status_json)["PENDING"] == 1
    assert stats.chats_enabled == 0
    empty = await ch.GetAutoReplyStats(pb.GetAutoReplyStatsRequest(owner_user_id=0))
    assert empty.sent_total == 0


async def test_preview_without_provider_explains_itself(stub):
    ch, _, _ = stub
    resp = await ch.PreviewAutoReply(pb.PreviewAutoReplyRequest(
        owner_user_id=OWNER, inbound_text="hi there",
    ))
    assert resp.ok is False
    assert resp.error_code == "no_ai_config"
    assert "provider" in resp.error.lower()

    code = await _status(ch.PreviewAutoReply(pb.PreviewAutoReplyRequest(
        owner_user_id=OWNER, inbound_text="   ",
    )))
    assert code == grpc.StatusCode.INVALID_ARGUMENT
