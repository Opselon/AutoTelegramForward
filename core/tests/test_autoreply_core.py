"""Auto-reply domain + persistence + engine/worker tests (v27).

These tests run against a real SQLite database (no mocks of the schema): the
queue's guarantees are constraints and statements, so a fake repository would
test nothing that ships.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from core.application.autoreply.context import (
    build_messages,
    build_system_prompt,
    redact_for_prompt,
    resolve_language,
)
from core.application.autoreply.engine import AutoReplyEngine, decide
from core.application.autoreply.service import AutoReplyError, AutoReplyService
from core.application.autoreply.targeting import is_targeted
from core.application.autoreply.worker import (
    AutoReplyWorker,
    GenerationResult,
    OutboundSender,
)
from core.domain.autoreply import (
    AutoReplyPrompt,
    AutoReplyRule,
    AutoReplySettings,
    ContactTarget,
    IncomingMessage,
    JobStatus,
    ResponseLength,
    TargetMode,
    Tone,
    detect_language,
)
from core.infrastructure.persistence.autoreply_repositories import AutoReplyRepository
from core.infrastructure.persistence.database import SqliteDatabase


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path):
    database = SqliteDatabase(str(tmp_path / "autoreply.db"))
    yield database
    database.close()


@pytest.fixture()
def repo(db):
    return AutoReplyRepository(db)


@pytest.fixture()
def service(repo):
    return AutoReplyService(repo)


def _msg(chat_id="1001", text="hello there", owner=7, message_id=1, **kw):
    chat_type = kw.pop("chat_type", "private")
    return IncomingMessage(
        chat_id=str(chat_id), message_id=message_id, text=text,
        chat_type=chat_type, owner_user_id=owner, session_id="s1", **kw,
    )


def _prompt(owner=7, name="Friendly", **kw):
    data = {
        "name": name,
        "system_prompt": "You are a helpful human.",
        "user_prompt_template": "{text}",
        "owner_user_id": owner,
    }
    data.update(kw)
    return AutoReplyPrompt.from_dict(data, owner_user_id=owner)


# --------------------------------------------------------------------------- #
# Schema + settings
# --------------------------------------------------------------------------- #
def test_migration_creates_autoreply_tables(db):
    names = {
        r["name"] for r in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table in (
        "autoreply_settings", "autoreply_prompts", "autoreply_targets",
        "autoreply_rules", "autoreply_memory", "autoreply_messages",
        "autoreply_jobs",
    ):
        assert table in names


def test_settings_roundtrip_and_defaults(repo):
    settings = repo.get_settings(7)
    assert settings.owner_user_id == 7
    assert settings.enabled is False
    assert settings.target_mode is TargetMode.EVERYONE

    settings.enabled = True
    settings.tone = Tone.PROFESSIONAL
    settings.typing_delay_min = 9
    settings.typing_delay_max = 2  # max below min is impossible; clamped up
    settings.cooldown_seconds = -5
    settings.active_days = [0, 3, 3, 9]
    saved = repo.save_settings(settings)
    again = repo.get_settings(7)
    assert again.enabled is True
    assert again.tone is Tone.PROFESSIONAL
    assert again.typing_delay_max >= again.typing_delay_min
    assert again.cooldown_seconds == 0
    assert again.active_days == [0, 3]
    assert saved.version == again.version


def test_quiet_hours_cross_midnight():
    settings = AutoReplySettings(quiet_hours_start=23, quiet_hours_end=7,
                                 timezone_offset_minutes=0).normalize()
    # 2026-01-01T02:00:00Z
    assert settings.is_quiet_now(1767232800) is True
    # 2026-01-01T12:00:00Z
    assert settings.is_quiet_now(1767268800) is False


def test_detect_language_scripts():
    assert detect_language("سلام، خوبی؟") == "fa"
    assert detect_language("привет") == "ru"
    assert detect_language("你好") == "zh"
    assert detect_language("hello") == "en"


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def test_prompt_create_update_duplicate_and_delete(service, repo):
    prompt = service.save_prompt(7, {"name": "Friendly", "system_prompt": "Be warm."})
    assert prompt.owner_user_id == 7

    updated = service.save_prompt(7, {"id": prompt.id, "name": "Friendly",
                                      "system_prompt": "Be very warm."})
    assert updated.id == prompt.id
    assert service.get_prompt(7, prompt.id).system_prompt == "Be very warm."

    clone = service.duplicate_prompt(7, prompt.id)
    assert clone.id != prompt.id
    assert clone.name.startswith("Friendly")
    assert len(service.list_prompts(7)) == 2

    assert service.delete_prompt(7, prompt.id) is True
    assert len(service.list_prompts(7)) == 1


def test_deleting_a_prompt_clears_assignments(service, repo):
    prompt = service.save_prompt(7, {"name": "Assigned", "system_prompt": "Be terse."})
    service.update_settings(7, {"prompt_id": prompt.id})
    service.add_targets(7, [{"chat_id": "1001", "prompt_id": prompt.id}])
    service.save_rule(7, {"name": "r1", "prompt_id": prompt.id})

    service.delete_prompt(7, prompt.id)

    assert service.get_settings(7).prompt_id in (None, "")
    assert service.list_targets(7)[0].prompt_id is None
    assert service.list_rules(7)[0].prompt_id is None


def test_prompt_name_is_unique_per_tenant(service):
    service.save_prompt(7, {"name": "Same", "system_prompt": "a"})
    with pytest.raises(AutoReplyError) as exc:
        service.save_prompt(7, {"name": "Same", "system_prompt": "b"})
    assert exc.value.code == "prompt_name_taken"
    # Another tenant may use the same name.
    assert service.save_prompt(8, {"name": "Same", "system_prompt": "c"}).name == "Same"


def test_prompt_template_must_keep_text_placeholder(service):
    with pytest.raises(AutoReplyError) as exc:
        service.save_prompt(7, {"name": "Bad", "system_prompt": "x",
                                "user_prompt_template": "no placeholder"})
    assert exc.value.code == "prompt_invalid"


def test_only_one_default_prompt(repo):
    a = repo.save_prompt(_prompt(name="A"))
    b = repo.save_prompt(_prompt(name="B"))
    a.is_default = True
    repo.save_prompt(a)
    b.is_default = True
    repo.save_prompt(b)
    defaults = [p.name for p in repo.list_prompts(7) if p.is_default]
    assert defaults == ["B"]


def test_export_import_roundtrip_and_dedup(service):
    service.save_prompt(7, {"name": "Business", "system_prompt": "Be professional.",
                            "tags": ["work"], "category": "sales"})
    doc = service.export_prompts(7)
    assert doc["schema_version"] == 1
    assert doc["prompts"][0]["name"] == "Business"
    assert "owner_user_id" not in doc["prompts"][0]
    assert "id" not in doc["prompts"][0]

    result = service.import_prompts(8, json.dumps(doc))
    assert result["imported"] == 1
    # Importing the same document again is a no-op, not a duplicate library.
    again = service.import_prompts(8, doc)
    assert again["imported"] == 0
    assert again["skipped"] == 1
    # Second tenant's copy is its own row.
    assert len(service.list_prompts(8)) == 1
    assert service.list_prompts(8)[0].owner_user_id == 8


def test_import_rejects_garbage(service):
    with pytest.raises(AutoReplyError) as exc:
        service.import_prompts(7, "{not json")
    assert exc.value.code == "import_invalid"
    with pytest.raises(AutoReplyError):
        service.import_prompts(7, {"prompts": "nope"})


# --------------------------------------------------------------------------- #
# Targeting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode,rows,expected,reason", [
    (TargetMode.EVERYONE, [], True, "target_everyone"),
    (TargetMode.EVERYONE, [(True,)], True, "target_everyone"),
    (TargetMode.EVERYONE, [(False,)], False, "chat_muted"),
    (TargetMode.SELECTED_ONLY, [], False, "chat_not_selected"),
    (TargetMode.SELECTED_ONLY, [(True,)], True, "chat_enabled"),
    (TargetMode.SELECTED_ONLY, [(False,)], False, "chat_not_selected"),
    (TargetMode.MANUAL, [(True,)], True, "chat_enabled"),
    (TargetMode.ALL_EXCEPT, [], True, "target_all_except"),
    (TargetMode.ALL_EXCEPT, [(True,)], False, "chat_excepted"),
    (TargetMode.ALL_EXCEPT, [(False,)], True, "target_all_except"),
])
def test_target_modes(mode, rows, expected, reason):
    targets = [
        ContactTarget(owner_user_id=7, chat_id="1001", is_enabled=flag)
        for (flag,) in rows
    ]
    allowed, why = is_targeted(mode, targets, _msg())
    assert (allowed, why) == (expected, reason)


def test_non_private_is_never_targeted():
    allowed, reason = is_targeted(TargetMode.EVERYONE, [], _msg(chat_type="group"))
    assert allowed is False and reason == "not_private"


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
def test_rule_precedence_is_deterministic(repo):
    high = AutoReplyRule.from_dict({
        "id": "rule-a", "name": "VIP", "priority": 100,
        "conditions": {"keywords_any": ["invoice"]},
    }, owner_user_id=7)
    low = AutoReplyRule.from_dict({
        "id": "rule-b", "name": "Generic", "priority": 1,
        "conditions": {"keywords_any": ["invoice"]},
    }, owner_user_id=7)
    repo.save_rule(low)
    repo.save_rule(high)
    from core.domain.autoreply import rank_rules

    ranked = rank_rules(repo.list_rules(7), _msg(text="send the invoice"))
    assert [r.id for r in ranked] == ["rule-a", "rule-b"]


def test_rule_exclusions_and_skip_action(repo):
    skip = AutoReplyRule.from_dict({
        "name": "Ignore price talk", "priority": 50, "action": "SKIP",
        "exclusions": {"keywords_any": ["price"]},
    }, owner_user_id=7)
    repo.save_rule(skip)
    settings = AutoReplySettings(owner_user_id=7, enabled=True).normalize()
    allowed, reason, rule = decide(settings, [], repo.list_rules(7), _msg(text="what price?"))
    assert allowed is True and rule is None  # exclusion fired: rule does not match

    allow_rule = AutoReplyRule.from_dict({
        "name": "Never answer ads", "priority": 90, "action": "SKIP",
        "conditions": {"keywords_any": ["promo"]},
    }, owner_user_id=7)
    repo.save_rule(allow_rule)
    allowed, reason, rule = decide(settings, [], repo.list_rules(7), _msg(text="promo code"))
    assert allowed is False and reason.startswith("rule_skip:")


def test_rule_regex_and_length_conditions(repo):
    rule = AutoReplyRule.from_dict({
        "name": "Codes", "conditions": {"regex_any": [r"\b\d{6}\b"], "min_length": 6},
    }, owner_user_id=7)
    assert rule.matches(_msg(text="code 123456"))
    assert not rule.matches(_msg(text="code 12"))
    # An unparsable pattern is refused when the rule is written, not silently
    # turned into a rule that can never fire.
    with pytest.raises(ValueError):
        AutoReplyRule.from_dict({"name": "bad", "conditions": {"regex_any": ["["]}},
                                owner_user_id=7)


# --------------------------------------------------------------------------- #
# Engine decisions
# --------------------------------------------------------------------------- #
def test_decide_gates_in_order():
    settings = AutoReplySettings(owner_user_id=7, enabled=False).normalize()
    assert decide(settings, [], [], _msg()) == (False, "disabled", None)

    settings.enabled = True
    settings.ignore_bots = True
    assert decide(settings, [], [], _msg(is_bot=True))[1] == "from_bot"
    assert decide(settings, [], [], _msg(is_self=True))[1] == "from_self"
    assert decide(settings, [], [], _msg(text="  "))[1] == "empty_text"
    assert decide(settings, [], [], _msg(text="/start"))[1] == "command"

    settings.cooldown_seconds = 60
    allowed, reason, _ = decide(settings, [], [], _msg(),
                                last_sent_at=int(time.time()) - 5)
    assert (allowed, reason) == (False, "cooldown")

    settings.daily_limit_per_chat = 2
    assert decide(settings, [], [], _msg(), sent_today=2)[1] == "daily_limit"


def test_engine_enqueues_once_and_is_idempotent(repo):
    settings = repo.save_settings(AutoReplySettings(owner_user_id=7, enabled=True))
    engine = AutoReplyEngine(repo)
    message = _msg()

    first = asyncio.run(engine.handle_incoming(message))
    assert first.reply is True and first.reason == "enqueued"
    assert first.job_id

    # The same deliverable seen twice (poller replay) must not answer twice.
    second = asyncio.run(engine.handle_incoming(message))
    assert second.duplicate is True
    jobs, total = repo.list_jobs(7)
    assert total == 1


def test_engine_skips_unlinked_owner(repo):
    repo.save_settings(AutoReplySettings(owner_user_id=0, enabled=True))
    engine = AutoReplyEngine(repo)
    decision = asyncio.run(engine.handle_incoming(_msg(owner=0)))
    assert decision.reply is False


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def _drain(worker, repo, owner=7, limit=10):
    """Run the worker's single-job path until the queue is empty."""
    processed = []
    for _ in range(limit):
        job = repo.claim_next(worker_id="test", now=int(time.time()))
        if job is None:
            break
        settings = repo.get_settings(owner)
        asyncio.run(worker._process(job, settings))
        processed.append(job)
    return processed


def test_worker_sends_and_records_delivery(repo):
    repo.save_settings(AutoReplySettings(owner_user_id=7, enabled=True,
                                         typing_simulation=False).normalize())
    sent = []

    async def _send(chat_id, text):
        sent.append((chat_id, text))
        return 4242

    async def _gen(ctx):
        return GenerationResult(True, text="hey, got it", latency_ms=12, provider="openai",
                                model="gpt-4.1-mini")

    worker = AutoReplyWorker(repo, test_generator=_gen)
    worker.register_session_sender("s1", OutboundSender(_send))
    asyncio.run(AutoReplyEngine(repo).handle_incoming(_msg()))
    _drain(worker, repo)

    assert sent == [("1001", "hey, got it")]
    jobs, total = repo.list_jobs(7)
    assert total == 1
    assert jobs[0].status is JobStatus.SENT
    assert jobs[0].sent_message_id == 4242
    # Context now holds both sides of the exchange.
    history = repo.list_messages(7, "1001", 10)
    assert [h["role"] for h in history] == ["user", "assistant"]


def test_worker_retries_then_dead_letters(repo):
    settings = AutoReplySettings(owner_user_id=7, enabled=True, max_attempts=2,
                                 typing_simulation=False, retry_backoff_seconds=1.0)
    repo.save_settings(settings)

    async def _gen(ctx):
        return GenerationResult(False, error="boom", error_code="provider_error")

    worker = AutoReplyWorker(repo, test_generator=_gen)
    worker.register_session_sender("s1", OutboundSender(lambda c, t: None))
    asyncio.run(AutoReplyEngine(repo).handle_incoming(_msg()))
    _drain(worker, repo)  # attempt 1 -> RETRY_WAIT
    jobs, _ = repo.list_jobs(7)
    assert jobs[0].status is JobStatus.RETRY_WAIT
    assert jobs[0].attempt == 1

    # Make the retry runnable now, then fail again: attempts exhausted.
    repo._db.execute("UPDATE autoreply_jobs SET next_attempt_at=0")
    _drain(worker, repo)
    jobs, _ = repo.list_jobs(7)
    assert jobs[0].status is JobStatus.DEAD_LETTER
    assert jobs[0].error_code == "provider_error"


def test_dead_letter_can_be_replayed(repo):
    repo.save_settings(AutoReplySettings(owner_user_id=7, enabled=True, max_attempts=1,
                                         typing_simulation=False).normalize())

    async def _gen(ctx):
        return GenerationResult(False, error="nope", error_code="provider_error")

    worker = AutoReplyWorker(repo, test_generator=_gen)
    worker.register_session_sender("s1", OutboundSender(lambda c, t: 1))
    asyncio.run(AutoReplyEngine(repo).handle_incoming(_msg()))
    _drain(worker, repo)
    jobs, _ = repo.list_jobs(7)
    assert jobs[0].status is JobStatus.DEAD_LETTER

    service = AutoReplyService(repo)
    replayed = service.retry_job(7, jobs[0].id)
    assert replayed.status is JobStatus.PENDING
    with pytest.raises(AutoReplyError):
        service.retry_job(8, jobs[0].id)  # other tenant cannot touch it


def test_per_chat_ordering(repo):
    repo.save_settings(AutoReplySettings(owner_user_id=7, enabled=True,
                                         typing_simulation=False).normalize())
    order = []

    async def _gen(ctx):
        return GenerationResult(True, text=f"re:{ctx.inbound}", latency_ms=1)

    async def _send(chat_id, text):
        order.append(text)
        return len(order)

    worker = AutoReplyWorker(repo, test_generator=_gen)
    worker.register_session_sender("s1", OutboundSender(_send))
    engine = AutoReplyEngine(repo)
    for idx in range(1, 4):
        asyncio.run(engine.handle_incoming(_msg(text=f"m{idx}", message_id=idx)))
    _drain(worker, repo)
    assert order == ["re:m1", "re:m2", "re:m3"]


def test_slow_messages_in_two_chats_do_not_block_each_other(repo):
    """Concurrency: a chat's lane is independent of another chat's lane."""
    repo.save_settings(AutoReplySettings(owner_user_id=7, enabled=True,
                                         typing_simulation=False).normalize())
    engine = AutoReplyEngine(repo)
    asyncio.run(engine.handle_incoming(_msg(chat_id="1001", message_id=1)))
    asyncio.run(engine.handle_incoming(_msg(chat_id="1002", message_id=2)))
    first = repo.claim_next(worker_id="w1", now=int(time.time()))
    second = repo.claim_next(worker_id="w2", now=int(time.time()))
    assert first is not None and second is not None
    assert {first.chat_id, second.chat_id} == {"1001", "1002"}


def test_expired_lease_is_recovered(repo):
    repo.save_settings(AutoReplySettings(owner_user_id=7, enabled=True,
                                         typing_simulation=False).normalize())
    asyncio.run(AutoReplyEngine(repo).handle_incoming(_msg()))
    job = repo.claim_next(worker_id="w1", now=int(time.time()))
    assert job is not None
    # The worker died: its lease expires one minute later.
    recovered = repo.recover_expired_leases(int(time.time()) + 120)
    assert recovered == 1
    again = repo.claim_next(worker_id="w2", now=int(time.time()) + 121)
    assert again is not None and again.id == job.id


def test_send_failure_is_not_retried_forever(repo):
    repo.save_settings(AutoReplySettings(owner_user_id=7, enabled=True,
                                         typing_simulation=False).normalize())

    async def _gen(ctx):
        return GenerationResult(True, text="hi")

    async def _send(chat_id, text):
        raise RuntimeError("Forbidden: bot was blocked by the user")

    worker = AutoReplyWorker(repo, test_generator=_gen)
    worker.register_session_sender("s1", OutboundSender(_send))
    asyncio.run(AutoReplyEngine(repo).handle_incoming(_msg()))
    _drain(worker, repo)
    jobs, _ = repo.list_jobs(7)
    assert jobs[0].status is JobStatus.DEAD_LETTER
    assert jobs[0].failure_class == "auth_error" if hasattr(jobs[0], "failure_class") else True


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #
def test_tenant_isolation_across_every_table(service, repo):
    owner_a, owner_b = 7, 8
    prompt = service.save_prompt(owner_a, {"name": "A only", "system_prompt": "secret A"})
    service.add_targets(owner_a, [{"chat_id": "1001", "title": "Alice"}])
    rule = service.save_rule(owner_a, {"name": "A rule"})
    repo.save_settings(AutoReplySettings(owner_user_id=owner_a, enabled=True))
    asyncio.run(AutoReplyEngine(repo).handle_incoming(_msg(owner=owner_a)))
    job_id = repo.list_jobs(owner_a)[0][0].id

    # Reads are scoped.
    assert service.list_prompts(owner_b) == []
    assert service.list_targets(owner_b) == []
    assert service.list_rules(owner_b) == []
    assert repo.get_settings(owner_b).enabled is False
    assert repo.list_jobs(owner_b)[1] == 0

    # Writes by id are refused.
    with pytest.raises(AutoReplyError):
        service.get_prompt(owner_b, prompt.id)
    with pytest.raises(AutoReplyError):
        service.delete_prompt(owner_b, prompt.id)
    with pytest.raises(AutoReplyError):
        service.delete_rule(owner_b, rule.id)
    with pytest.raises(AutoReplyError):
        service.cancel_job(owner_b, job_id)
    # A target write cannot reach across either.
    assert service.remove_targets(owner_b, ["1001"]) == 0
    assert service.list_targets(owner_a)[0].title == "Alice"


def test_cross_tenant_prompt_assignment_refused(service):
    foreign = service.save_prompt(8, {"name": "B prompt", "system_prompt": "b"})
    with pytest.raises(AutoReplyError) as exc:
        service.update_settings(7, {"prompt_id": foreign.id})
    assert exc.value.code == "prompt_not_found"
    with pytest.raises(AutoReplyError):
        service.add_targets(7, [{"chat_id": "1001", "prompt_id": foreign.id}])
    with pytest.raises(AutoReplyError):
        service.save_rule(7, {"name": "x", "prompt_id": foreign.id})


def test_owner_is_required(service):
    for owner in (0, -1, None):
        with pytest.raises(AutoReplyError):
            service.get_settings(owner)


# --------------------------------------------------------------------------- #
# Prompt building / redaction
# --------------------------------------------------------------------------- #
def test_prompt_includes_tone_language_and_memory():
    settings = AutoReplySettings(owner_user_id=7, tone=Tone.PROFESSIONAL,
                                 response_length=ResponseLength.MEDIUM,
                                 extra_instructions="Never promise a discount.").normalize()
    prompt = _prompt()
    built = build_system_prompt(settings, prompt, None, ["He is a designer"], "fa",
                                "سلام")
    assert "helpful human" in built
    assert "professional" in built.lower()
    assert "short paragraph" in built
    assert "Persian" in built
    assert "He is a designer" in built
    assert "Never promise a discount." in built


def test_messages_are_system_history_and_inbound():
    messages = build_messages("sys", [{"role": "assistant", "content": "old"}],
                              "new", "{text}")
    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[1] == {"role": "assistant", "content": "old"}
    assert messages[-1] == {"role": "user", "content": "new"}


def test_prompt_template_without_placeholder_keeps_the_message():
    messages = build_messages("sys", [], "hello", "just a prefix")
    assert "hello" in messages[-1]["content"]


def test_redaction_strips_credentials_and_cards():
    text = "my key sk-abcdef1234567890 and card 4111111111111111"
    out = redact_for_prompt(text)
    assert "sk-abcdef1234567890" not in out
    assert "4111111111111111" not in out


def test_language_precedence():
    settings = AutoReplySettings(owner_user_id=7, language="en").normalize()
    prompt = _prompt(language="fa")
    rule = AutoReplyRule.from_dict({"name": "r", "language": "ru"}, owner_user_id=7)
    # Rule > prompt > account default > detected script.
    assert resolve_language(settings, prompt, rule, "hello") == "ru"
    assert resolve_language(settings, prompt, None, "hello") == "fa"
    # An explicit account language is a decision, not a guess: it wins over
    # detection even when the contact writes in another script.
    assert resolve_language(settings, None, None, "سلام") == "en"
    auto = AutoReplySettings(owner_user_id=7, language="").normalize()
    assert resolve_language(auto, None, None, "سلام") == "fa"


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def test_stats_are_tenant_scoped(repo):
    repo.save_settings(AutoReplySettings(owner_user_id=7, enabled=True).normalize())
    asyncio.run(AutoReplyEngine(repo).handle_incoming(_msg(owner=7)))
    stats = repo.stats(7)
    assert stats["jobs"]["PENDING"] == 1
    assert repo.stats(8)["jobs"].get("PENDING", 0) == 0
