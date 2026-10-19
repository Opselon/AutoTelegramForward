"""Repository port interfaces (DIP: domain owns the abstraction)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..domain.entities import (
    AIConfig,
    DeliveryJob,
    DeliveryStatus,
    FilterRule,
    ForwardRule,
    MessageMapping,
    PromptTemplate,
    PromptVersion,
    TelegramSession,
)


class ConcurrencyError(Exception):
    """Raised when an update fails due to a version conflict (optimistic lock)."""
    pass


@dataclass
class ApiCredential:
    """An api_id/api_hash pair (optionally with a proxy), stored encrypted."""

    id: str
    label: str = ""
    api_id: int = 0
    api_hash: str = ""
    proxy: str = ""
    is_default: bool = False
    created_at: int = 0
    updated_at: int = 0
    # v12: credentials are per-tenant. 0 = legacy/system scope (admin only).
    owner_user_id: int = 0


@dataclass
class BotToken:
    """A bot token, stored encrypted at rest."""

    id: str
    label: str = ""
    token: str = ""
    is_active: bool = True
    created_at: int = 0
    updated_at: int = 0
    # The tenant that pasted the token. Delivery resolves a token by id alone
    # (the worker has no request context); every *human* path — listing,
    # attaching, deleting — must go through the owner-scoped methods below.
    owner_user_id: int = 0
    # v20: the settings this bot carries for every task that uses it. Behaviour
    # only — a default may never name a chat, a route or a schedule (see
    # core.domain.bot_defaults). Empty means "no defaults", which is the
    # behaviour of every bot attached before this existed.
    defaults: Dict[str, Any] = field(default_factory=dict)


class ISessionRepository(ABC):
    @abstractmethod
    async def add(self, session: TelegramSession) -> TelegramSession:
        ...

    @abstractmethod
    async def update(self, session: TelegramSession) -> TelegramSession:
        ...

    @abstractmethod
    async def get_by_id(self, session_id: str) -> Optional[TelegramSession]:
        ...

    @abstractmethod
    async def get_by_phone(self, phone: str) -> Optional[TelegramSession]:
        ...

    @abstractmethod
    async def list_all(self) -> List[TelegramSession]:
        ...

    @abstractmethod
    async def list_by_owner(self, owner_user_id: int) -> List[TelegramSession]:
        ...

    @abstractmethod
    async def list_active(self) -> List[TelegramSession]:
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        ...


class IForwardRuleRepository(ABC):
    @abstractmethod
    async def add(self, rule: ForwardRule) -> ForwardRule:
        ...

    @abstractmethod
    async def update(self, rule: ForwardRule, expected_version: Optional[int] = None) -> ForwardRule:
        ...

    @abstractmethod
    async def get_by_id(self, rule_id: str) -> Optional[ForwardRule]:
        ...

    @abstractmethod
    async def list_by_session(self, session_id: str, owner_user_id: int = 0) -> List[ForwardRule]:
        ...

    @abstractmethod
    async def list_by_owner(
        self, owner_user_id: int, limit: Optional[int] = None, offset: int = 0
    ) -> List[ForwardRule]:
        ...

    async def count_by_owner(self, owner_user_id: int) -> int:
        """How many tasks this tenant owns, without materialising them.

        Concrete, not abstract: this is the honest default for an implementation
        that has no aggregate of its own, and it keeps the interface additive.
        """
        return len(await self.list_by_owner(owner_user_id))

    @abstractmethod
    async def list_all(self) -> List[ForwardRule]:
        ...

    @abstractmethod
    async def list_active_by_source(self, source_chat_id: str) -> List[ForwardRule]:
        ...

    @abstractmethod
    async def delete(self, rule_id: str) -> bool:
        ...

    async def set_task_status(
        self,
        rule_id: str,
        *,
        target_active: bool,
        expected_state: str,
        owner_user_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Turn a task on/off, but only from the state the caller was shown.

        ``expected_state`` is a `ForwardRule.status_state()` value — the state the
        screen displayed when the button was rendered. The transition happens only
        if the stored state still matches it; otherwise nothing is written and the
        method returns False, which is how a stale screen or a concurrent change
        (dashboard pause, another device) is refused instead of silently inverted.

        Backends **must** override this with a *single conditional UPDATE*: the
        read-then-write fallback below is what let two taps collapse into one flip,
        and let a screen rendered while running re-activate a task that had since
        been paused elsewhere. Ownership is enforced in the same statement when
        ``owner_user_id`` is given.

        Concrete, not abstract, for the same reason as ``count_by_owner`` above:
        it keeps the interface additive for implementations that are not stores
        (test doubles, the backup-service fakes) while the two production stores
        override it — and ``test_task_status_end_to_end.py`` fails if either of
        them ever stops doing so.
        """
        rule = await self.get_by_id(rule_id)
        if rule is None:
            return False
        if owner_user_id is not None and rule.owner_user_id != owner_user_id:
            return False
        if rule.status_state(now) != expected_state:
            return False
        rule.is_active = target_active
        if target_active:
            rule.is_paused = False
            rule.paused_until = 0
        await self.update(rule)
        return True


class IFilterRuleRepository(ABC):
    @abstractmethod
    async def add(self, filter_rule: FilterRule) -> FilterRule:
        ...

    @abstractmethod
    async def update(self, filter_rule: FilterRule) -> FilterRule:
        ...

    @abstractmethod
    async def get_by_id(self, filter_id: str) -> Optional[FilterRule]:
        ...

    @abstractmethod
    async def list_all(self) -> List[FilterRule]:
        ...

    @abstractmethod
    async def list_by_owner(self, owner_user_id: int) -> List[FilterRule]:
        ...

    @abstractmethod
    async def delete(self, filter_id: str) -> bool:
        ...


class IAIConfigRepository(ABC):
    @abstractmethod
    async def add(self, config: AIConfig) -> AIConfig:
        ...

    @abstractmethod
    async def update(self, config: AIConfig) -> AIConfig:
        ...

    @abstractmethod
    async def get_by_id(self, config_id: str) -> Optional[AIConfig]:
        ...

    @abstractmethod
    async def list_all(self) -> List[AIConfig]:
        ...

    @abstractmethod
    async def list_by_owner(self, owner_user_id: int) -> List[AIConfig]:
        ...

    async def get_owned(self, config_id: str, owner_user_id: int) -> Optional[AIConfig]:
        """The config, only when it belongs to ``owner_user_id``.

        A config id is a *selector*, not proof of ownership: it arrives in a
        callback payload, in an imported backup or in a rule row, and any of
        those can name another tenant's config. Resolving one unscoped hands
        that tenant's provider, base_url and API key to a task that does not own
        it, so every engine-side read is owner-scoped and fails closed.

        The default compares owners after an unscoped read, which makes any
        implementation that has not overridden it safe rather than permissive;
        the SQL implementations override it with a scoped query.
        """
        config = await self.get_by_id(config_id)
        if config is None:
            return None
        return config if int(config.owner_user_id or 0) == int(owner_user_id or 0) else None

    @abstractmethod
    async def delete(self, config_id: str) -> bool:
        ...


class IPromptRepository(ABC):
    @abstractmethod
    async def add_template(self, template: PromptTemplate) -> PromptTemplate:
        ...

    @abstractmethod
    async def update_template(self, template: PromptTemplate) -> PromptTemplate:
        ...

    @abstractmethod
    async def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        ...

    @abstractmethod
    async def list_templates(self) -> List[PromptTemplate]:
        ...

    @abstractmethod
    async def list_templates_by_owner(self, owner_user_id: int) -> List[PromptTemplate]:
        ...

    @abstractmethod
    async def delete_template(self, template_id: str) -> bool:
        ...

    @abstractmethod
    async def add_version(self, version: PromptVersion) -> PromptVersion:
        ...

    @abstractmethod
    async def get_version(self, prompt_id: str, version: int) -> Optional[PromptVersion]:
        ...

    @abstractmethod
    async def list_versions(self, prompt_id: str) -> List[PromptVersion]:
        ...

    @abstractmethod
    async def activate_version(self, prompt_id: str, version: int) -> bool:
        ...

    @abstractmethod
    async def get_active_version(self, prompt_id: str) -> Optional[PromptVersion]:
        ...



class IApiCredentialRepository(ABC):
    @abstractmethod
    async def add(self, cred: ApiCredential) -> ApiCredential:
        ...

    @abstractmethod
    async def get_default(self) -> Optional[ApiCredential]:
        ...

    @abstractmethod
    async def get_default_by_owner(self, owner_user_id: int) -> Optional[ApiCredential]:
        ...

    @abstractmethod
    async def get_by_id(self, cred_id: str) -> Optional[ApiCredential]:
        ...

    @abstractmethod
    async def list_by_owner(self, owner_user_id: int) -> List[ApiCredential]:
        ...

    @abstractmethod
    async def list_all(self) -> List[ApiCredential]:
        ...

    @abstractmethod
    async def delete(self, cred_id: str) -> bool:
        ...


class IBotTokenRepository(ABC):
    @abstractmethod
    async def add(self, token: BotToken) -> BotToken:
        ...

    @abstractmethod
    async def get_by_id(self, token_id: str) -> Optional[BotToken]:
        ...

    @abstractmethod
    async def list_all(self) -> List[BotToken]:
        ...

    @abstractmethod
    async def list_active(self) -> List[BotToken]:
        ...

    @abstractmethod
    async def list_by_owner(self, owner_user_id: int) -> List[BotToken]:
        ...

    @abstractmethod
    async def get_owned(self, token_id: str, owner_user_id: int) -> Optional[BotToken]:
        ...

    @abstractmethod
    async def set_defaults(
        self, token_id: str, owner_user_id: int, defaults: Dict[str, Any]
    ) -> bool:
        """Replace one owner's bot defaults. False when the bot is not theirs."""
        ...

    @abstractmethod
    async def delete(self, token_id: str) -> bool:
        ...


class IProcessedMessageRepository(ABC):
    """Restart-safe dedupe store: which messages a rule already forwarded."""

    @abstractmethod
    async def was_processed(self, rule_id: str, chat_id: str, message_id: int) -> bool:
        ...

    @abstractmethod
    async def mark_processed(
        self, rule_id: str, chat_id: str, message_id: int, media_group_id: str = ""
    ) -> None:
        ...

    @abstractmethod
    async def purge_before(self, rule_id: str, ts: int) -> int:
        ...


class IAuthFlowRepository(ABC):
    """Persisted authentication state machine storage (audit D1 / TASK 03).

    The auth flow is the one piece of UI state that MUST survive a restart:
    losing it mid-code leaves the user staring at a bot that will not say why.
    """

    @abstractmethod
    async def get(self, user_id: int) -> Optional[dict]:
        ...

    @abstractmethod
    async def save(self, row: dict) -> bool:
        """Upsert one flow, but only if ``row["version"]`` beats what is stored.

        Returns True when the row was written and False when it was dropped
        because a *newer* revision is already persisted — the caller was
        holding a stale copy (a late callback for a flow that has moved on).
        The version is enforced in the statement, not merely carried in the
        column: without that, two concurrent transitions both write the same
        next version and the last writer wins.
        """
        ...

    @abstractmethod
    async def delete(self, user_id: int, *, version: Optional[int] = None) -> bool:
        """Remove a flow. With ``version``, only that exact revision is removed.

        Returns True when a row was deleted. The version is what keeps a stale
        ``reset()`` (based on a finished flow) from deleting a newer one that
        the user has just started.
        """
        ...

    @abstractmethod
    async def expired(self) -> list:
        """Rows whose ttl has lapsed while still in an active state."""
        ...

    @abstractmethod
    async def active(self) -> list:
        """Rows still waiting on the user (active states only)."""
        ...


class IUiStateRepository(ABC):
    """Durable per-user UI flow state — survives restarts."""

    @abstractmethod
    async def get(self, user_id: int) -> Optional[dict]:
        ...

    @abstractmethod
    async def save(self, user_id: int, step: str, buffer: dict) -> None:
        ...

    @abstractmethod
    async def clear(self, user_id: int) -> None:
        ...


class IErrorLogRepository(ABC):
    """Structured Telegram error log (exact RPC error name + severity)."""

    @abstractmethod
    async def record(
        self, category: str, error_name: str, severity: str, detail: str = "",
        recoverable: bool = False, user_id: Optional[int] = None,
        session_id: Optional[str] = None, rule_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        ...

    @abstractmethod
    async def recent(self, limit: int = 10, severity: Optional[str] = None) -> List[dict]:
        ...

    @abstractmethod
    async def counts_by_severity(self, since_ts: int = 0) -> dict:
        ...


class IMetricsRepository(ABC):
    """Rolling throughput counters for the dashboard."""

    @abstractmethod
    async def bump(self, forwarded: int = 0, filtered: int = 0, errors: int = 0) -> None:
        ...

    @abstractmethod
    async def hourly(self, hours: int = 24) -> List[dict]:
        ...

    @abstractmethod
    async def totals(self) -> dict:
        ...

    @abstractmethod
    async def prune(self, keep_hours: int = 168) -> int:
        ...


class IRuleStatsRepository(ABC):
    """Per-rule live counters on the hot path."""

    @abstractmethod
    async def bump(self, rule_id: str, forwarded: int = 0, filtered: int = 0, errors: int = 0) -> None:
        ...

    @abstractmethod
    async def get(self, rule_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    async def top_rules(self, limit: int = 10) -> List[dict]:
        ...


class IUserRepository(ABC):
    """Per-user preferences, plan and quota."""

    @abstractmethod
    async def get_or_create(self, user_id: int, language: str = "") -> dict:
        ...

    @abstractmethod
    async def get(self, user_id: int) -> Optional[dict]:
        ...

    @abstractmethod
    async def list_all(self, limit: int = 100, offset: int = 0) -> List[dict]:
        ...

    @abstractmethod
    async def count_all(self) -> int:
        ...

    @abstractmethod
    async def set_status(self, user_id: int, status: str) -> None:
        ...

    @abstractmethod
    async def set_admin(self, user_id: int, is_admin: int) -> None:
        ...

    @abstractmethod
    async def set_plan(self, user_id: int, plan: str) -> None:
        ...

    @abstractmethod
    async def set_language(self, user_id: int, language: str) -> None:
        ...

    @abstractmethod
    async def bump_quota(self, user_id: int, by: int = 1) -> None:
        ...


class IMessageMapRepository(ABC):
    """Stores source -> target message mappings for edit/delete synchronization."""

    @abstractmethod
    async def add_or_update(self, mapping: MessageMapping) -> MessageMapping:
        ...

    @abstractmethod
    async def get_by_source(self, source_chat_id: str, source_message_id: int) -> List[MessageMapping]:
        ...

    @abstractmethod
    async def list_by_source_message_id(self, source_message_id: int) -> List[MessageMapping]:
        """Every mapping for a source message id, whatever chat it came from.

        Needed because Telegram's non-channel delete update carries message ids
        only; the caller disambiguates by checking how many distinct source
        chats the id maps to.
        """
        ...

    @abstractmethod
    async def get_by_target(self, target_chat_id: str, target_message_id: int) -> Optional[MessageMapping]:
        ...

    @abstractmethod
    async def get_by_rule_and_source(
        self, rule_id: str, source_chat_id: str, source_message_id: int
    ) -> List[MessageMapping]:
        ...

    # v2.0 app update: Duplicate Filters content-level dedup.
    @abstractmethod
    async def was_content_forwarded(self, rule_id: str, content_hash: str) -> bool:
        ...

    @abstractmethod
    async def record_content_hash(self, rule_id: str, content_hash: str) -> None:
        ...

    async def claim_content_hash(self, rule_id: str, content_hash: str) -> bool:
        """Atomically claim a content fingerprint for this rule.

        True means *this caller* just claimed it and may forward; False means
        another worker (or an earlier delivery) already owns it, so the message
        is a duplicate. The unique constraint decides the winner, so two
        workers processing identical content concurrently cannot both forward.

        The default keeps third-party adapters working (read-then-write, not
        atomic); the SQL/Postgres stores override it with an atomic insert.
        """
        if await self.was_content_forwarded(rule_id, content_hash):
            return False
        await self.record_content_hash(rule_id, content_hash)
        return True

    async def release_content_hash(self, rule_id: str, content_hash: str) -> None:
        """Give a claim back.

        A claim is only earned once the message is accepted for delivery. A
        message dropped after the dedup stage (filter, keyword rule, AI drop,
        backpressure) never reached its targets, so keeping its fingerprint
        would silently discard the next legitimate post of the same content.
        """
        return None

    async def purge_content_hashes_all(self, before_ts: int) -> int:
        """Drop every content fingerprint older than ``before_ts``.

        The dedup memory must expire with the rest of the retention windows:
        otherwise an identical announcement posted months later, by a channel
        that never repeats anything, is still silently dropped forever.
        """
        return 0

    @abstractmethod
    async def purge_content_hashes(self, rule_id: str, before_ts: int) -> int:
        ...

    @abstractmethod
    async def update_delivery(
        self,
        rule_id: str,
        source_chat_id: str,
        source_message_id: int,
        target_chat_id: str,
        target_message_id: Optional[int],
        status: DeliveryStatus,
    ) -> bool:
        ...

    @abstractmethod
    async def cleanup(self, retention_seconds: int = 60 * 60 * 24 * 30) -> int:
        ...


class IDeliveryQueueRepository(ABC):
    """Durable queue for asynchronous, restart-safe, non-blocking delivery workers."""

    @abstractmethod
    async def enqueue(self, job: DeliveryJob) -> bool:
        ...

    @abstractmethod
    async def claim_batch(
        self, worker_id: str, batch_size: int = 5, lease_duration: float = 30.0
    ) -> List[DeliveryJob]:
        ...

    @abstractmethod
    async def mark_sent(self, job_id: str, target_message_id: Optional[int] = None) -> bool:
        ...

    async def mark_sent_batch(self, job_ids: List[tuple]) -> int:
        """Mark several jobs SENT in one transaction.

        Each entry is ``(job_id, target_message_id)``. Returns the number of
        rows updated. Default implementation falls back to per-job
        ``mark_sent`` so existing implementations keep working.
        """
        count = 0
        for job_id, _target_msg_id in job_ids:
            if await self.mark_sent(job_id, _target_msg_id):
                count += 1
        return count

    @abstractmethod
    async def mark_retry(self, job_id: str, error: str, backoff_seconds: float) -> bool:
        ...

    async def defer(self, job_id: str, reason: str, backoff_seconds: float) -> bool:
        """Postpone a job WITHOUT spending one of its delivery attempts.

        The queue calls this for conditions that are neither the job's fault nor
        terminal: the task is paused, the target is rate-limited, the account is
        time-limited, no live client is attached yet, the attached bot token is
        temporarily unusable. All of them resolve on their own, so counting them
        as attempts is what turns a ten-second session outage into a dead-lettered
        message reported as ``DELIVERY_EXHAUSTED`` — a healthy forward recorded as
        permanently failed, after five polls.

        Returns ``False`` when the adapter cannot postpone without consuming an
        attempt; the caller then falls back to ``mark_retry`` and says so in the
        log, rather than silently losing the distinction.
        """
        return False

    @abstractmethod
    async def mark_failed(self, job_id: str, error: str) -> bool:
        ...

    @abstractmethod
    async def mark_unknown(self, job_id: str, error: str) -> bool:
        ...

    @abstractmethod
    async def get_pending_count(self) -> int:
        ...

    @abstractmethod
    async def get_stats(self) -> dict:
        ...

    @abstractmethod
    async def recover_expired_leases(self) -> int:
        ...

    @abstractmethod
    async def get_queue_age_metrics(self) -> dict:
        """Return {'pending_count': int, 'oldest_job_age_seconds': float}."""
        ...

    @abstractmethod
    async def cleanup(self, retention_seconds: int = 60 * 60 * 24 * 7) -> int:
        """Purge terminal jobs (SENT, FAILED) older than retention_seconds."""
        ...

    # ------------------------------------------------------------------ #
    # Dead-letter queue
    #
    # A dead letter is a delivery that exhausted its retry budget. It is the
    # one queue state a user can act on, so both clients (the Telegram bot and
    # the dashboard) must read and replay it through the same owner-scoped
    # contract rather than each rolling its own SQL over `dead_letter_queue`.
    #
    # ``rule_ids`` is the caller's tenant scope in every method below:
    # ``None`` means unscoped (an internal/admin caller that has already
    # resolved its authority), while an EMPTY sequence means the caller owns
    # nothing and must therefore see nothing. An empty scope never degrades
    # into "no filter".
    # ------------------------------------------------------------------ #

    async def list_dead_letters(
        self,
        rule_ids: Optional[Sequence[str]] = None,
        limit: int = 20,
        rule_id: str = "",
        since: int = 0,
    ) -> List[Dict[str, Any]]:
        """Dead-lettered deliveries, newest first, scoped to ``rule_ids``.

        ``rule_ids`` is the tenant scope: ``None`` is unscoped (internal
        callers), and an **empty** sequence means the caller owns nothing, so it
        sees nothing — never "no filter". ``rule_id`` and ``since`` narrow
        within that scope. They are filters rather than post-processing because
        ``limit`` applies after them: filtering an already-truncated page would
        answer "no such entry" for an entry that is simply older than the page.
        """
        return []

    async def count_dead_letters(self, rule_ids: Optional[Sequence[str]] = None) -> int:
        """How many dead letters the scope holds. Same scope rules as above.

        A count is a read: it belongs here with the other reads so every caller
        answers "is anything stuck?" from one implementation.
        """
        return 0
    async def replay_dead_letter(
        self, entry_id: str, rule_ids: Optional[Sequence[str]] = None
    ) -> str:
        """Re-queue one dead letter and drop its entry.

        Returns a result code rather than raising:

        * ``requeued`` — a fresh PENDING job now exists for it.
        * ``not_found`` — no such entry, or it belongs to another tenant. The
          two are deliberately indistinguishable so an id cannot be probed.
        * ``already_pending`` — the delivery is live right now; replaying would
          deliver the message twice.
        * ``already_sent`` — the store records this message as delivered, so
          re-queueing it would repost something the target already has.
        * ``replay_failed`` — the store refused the write.

        A replay never adds a second delivery: it wakes the one that failed,
        which is why the result codes can promise at-most-once. The woken job
        goes back to PENDING with its attempt budget restored — `attempts` to
        zero and `max_attempts` to the rule's full budget — so a retried message
        gets the same room to succeed as it had the first time, and the entry
        disappears only once that write has succeeded.
        """
        return "not_found"

    async def purge_dead_letters(
        self,
        rule_ids: Optional[Sequence[str]] = None,
        rule_id: str = "",
        older_than: int = 0,
    ) -> int:
        """Delete scoped dead letters and return how many were removed.

        Deleting is irreversible and cross-tenant by accident, so the scope is
        the whole guard: an empty ``rule_ids`` removes nothing (it is *not* "no
        filter"), ``rule_id`` narrows to one rule, and ``older_than`` keeps
        recent entries for the tenant that needs them.
        """
        return 0



class ISettingsRepository(ABC):
    """Operator-editable runtime settings (key/value).

    Backs the bot-side ``web_url`` override so an operator can point the
    dashboard one-tap login link at their own domain without touching config
    files or restarting the service.
    """

    @abstractmethod
    async def get(self, key: str, default: str = "") -> str:
        ...

    @abstractmethod
    async def set(self, key: str, value: str) -> None:
        ...


def kind_filter_sql(kinds: Optional[Sequence[str]], placeholder: str = "?") -> Tuple[str, tuple]:
    """Build the ``AND kind IN (…)`` clause for a task claim query.

    Bounded and parameterized (no string interpolation of caller data), shared
    by both engines so SQLite and PostgreSQL cannot diverge on which kinds a
    replica is willing to claim.

    ``None`` means "no restriction". An EXPLICIT EMPTY sequence means "this
    replica can execute nothing" and must claim nothing — never fall back to
    claiming every kind, which is how a replica without executors would starve
    the ones that have them.
    """
    if kinds is None:
        return "", ()
    cleaned = [str(k) for k in kinds if str(k)]
    if not cleaned:
        return " AND 1=0", ()
    placeholders = ",".join([placeholder] * len(cleaned))
    return f" AND kind IN ({placeholders})", tuple(cleaned)


#: How long terminal per-user tasks are kept before the retention sweep
#: deletes them (30 days), and how many rows one sweep removes, so a large
#: backlog is drained in bounded batches instead of one long write lock.
TASK_RETENTION_SECONDS = 60 * 60 * 24 * 30
TASK_PURGE_BATCH = 500

class ITaskRepository(ABC):
    """Durable per-user task store. Ownership is part of every row."""

    @abstractmethod
    async def add(self, task: Any) -> Any:
        ...

    @abstractmethod
    async def insert_if_absent(self, task: Any) -> Optional[Any]:
        """Insert honoring the (owner, kind, idempotency_key) uniqueness.

        Returns the task that now owns that key — the freshly inserted one or
        the pre-existing row under a duplicate-key race.
        """

    @abstractmethod
    async def get(self, task_id: str) -> Optional[Any]:
        ...

    @abstractmethod
    async def list_for_owner(
        self, owner_user_id: int, status: str = "", kind: str = "",
        limit: int = 50, offset: int = 0,
    ) -> List[Any]:
        ...

    @abstractmethod
    async def count_for_owner(
        self, owner_user_id: int, status: str = "", kind: str = "",
    ) -> int:
        ...

    @abstractmethod
    async def replace(self, task: Any) -> Any:
        ...

    @abstractmethod
    async def acquire_next(
        self, lease_token: str, lease_until: int, now: int,
        kinds: Optional[Sequence[str]] = None,
    ) -> Optional[Any]:
        """Atomically claim one runnable task (PENDING/RETRY_WAIT, run_at due).

        Sets status=RUNNING and stamps the lease in the same statement so two
        workers can never hold the same task.

        ``kinds`` restricts the candidate set to the kinds this replica can
        actually execute. Without it a replica that lacks an executor still
        claims the task, rewrites the row to push it back and repeats every
        poll cycle — write amplification for a task it can never run.
        """

    @abstractmethod
    async def renew_lease(self, task_id: str, token: str, lease_until: int) -> bool:
        ...

    @abstractmethod
    async def reclaim_expired(self, now: int) -> int:
        """Return expired RUNNING leases to RETRY_WAIT so a crashed worker
        cannot hold task state hostage."""

    @abstractmethod
    async def cleanup(self, retention_seconds: int = TASK_RETENTION_SECONDS) -> int:
        """Delete terminal tasks that finished outside the retention window.

        Only SUCCEEDED/FAILED/CANCELLED rows are eligible. PENDING, RETRY_WAIT
        and RUNNING are live work no matter how old the row is — a queued task
        its owner is still waiting for, or one a worker is executing right now —
        so the predicate starts with the status and the sweep can never drop
        them. `retention_seconds <= 0` means keep everything (explicitly
        unbounded, not "purge immediately").

        At most TASK_PURGE_BATCH rows are removed per call so the delete holds
        the write lock briefly and a large backlog is drained over several
        sweeps. Returns the number of rows deleted.
        """

    @abstractmethod
    async def finish_succeeded(self, task_id: str, token: str, result_json: str,
                               now: int) -> bool:
        """Commit SUCCEEDED ONLY for the lease holder (conditional UPDATE)."""

    @abstractmethod
    async def finish_failed(self, task_id: str, token: str, error: str,
                            error_code: str, now: int) -> bool:
        ...

    @abstractmethod
    async def pause_for_retry(self, task_id: str, token: str, run_at: int,
                              error: str, error_code: str, now: int) -> bool:
        """Return a RUNNING task to RETRY_WAIT (holder-checked), clearing the
        lease so another worker can pick it up after the backoff."""

    @abstractmethod
    async def cancel_queued(self, task_id: str, owner_user_id: int,
                            now: int) -> bool:
        """CANCELLED while the task is still queue-bound (PENDING/RETRY_WAIT).

        One conditional UPDATE against the owner wins the race with a worker's
        claim: whoever commits first, the other sees the new state. The bool
        return is the commit result and MUST be honored by the caller — a
        False means nothing was cancelled (the task was claimed first), never
        a task that was cancelled. RETRY_WAIT is included because that state
        is queue-bound too: a task waiting for its next attempt runs later, so
        refusing to cancel it there would tell the owner it cannot stop work
        that has not happened yet.
        """

    @abstractmethod
    async def retry_failed(self, task_id: str, owner_user_id: int,
                           now: int) -> Optional[Any]:
        """Return a FAILED task to PENDING — atomically, or not at all.

        A conditional UPDATE (``WHERE owner_user_id=? AND status='FAILED'``)
        replaces the read-modify-write that used to rewrite every column with
        no owner or state predicate. Returns the requeued row, or None when
        the task was not FAILED (or not the caller's) so the caller can refuse
        with the truth instead of reporting a retry that did not happen.
        ``result_json`` is cleared: a requeued task must not serve its previous
        attempt's result.
        """

    @abstractmethod
    async def requeue_unclaimed(self, task_id: str, token: str, run_at: int,
                                now: int) -> bool:
        """Hand a claimed task back for someone else to run (holder-checked).

        Used when a replica has no executor for the kind it just claimed. The
        lease is cleared and the run_at nudged, so the row does not sit in
        RETRY_WAIT holding this worker's lease_token. False means the lease was
        already lost — the caller must not assume it still owns the row.
        """

