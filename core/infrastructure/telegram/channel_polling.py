"""Periodic channel polling — the reliable replacement for missing push updates.

Why this exists
---------------
Telegram only pushes `UpdateNewChannelMessage` for a channel while that channel
shows `unread_count > 0` for the session. The moment a session's read-marker
catches up to the top message of a channel, Telegram stops pushing that
channel entirely and expects the client to poll.

ATF's sessions routinely have their channel read-state fully caught up
(probes, status dashes, health checks all read the dialog list), which
silently flips every forwarding source channel to unread=0 and stops all push
delivery. The rule, the pipeline, the queue and the client all stay healthy —
no error is ever logged — yet nothing is forwarded.

This service polls each active source channel on a short interval and replays
any post the session has not seen yet through the same dispatcher path a live
push would use. It is the delivery mechanism that does not depend on
Telegram's read-state mood, and it also covers restarts: whatever arrived
while the core was down is picked up on the first sweep.

Idempotency: every replay goes through the pipeline's dedup layer
(processed_messages), so overlap with push or with a previous sweep is
harmless.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("atf.boot")

# How often to sweep every monitored source channel.
POLL_INTERVAL_SECONDS = 15.0

# Never pull more than this many posts per channel per sweep.
MAX_POSTS_PER_SWEEP = 100

# How long a sweep waits for a channel's replay lane to hand every post to the
# pipeline before giving up. The cursor only advances once the lane is drained,
# so a timeout is safe: the same posts are read again next sweep and the
# pipeline's dedup layer drops the ones already accepted.
REPLAY_DRAIN_TIMEOUT_SECONDS = 120.0

# --- Ingestion gap recovery (audit finding #1) ------------------------------- #
# A source id the poller asked for but a truncated GetChannelDifference did not
# return becomes durable recovery work: it is re-fetched with a narrow
# min_id=max_id range until either the pipeline sees it (resolved) or the
# bounded budget below is spent (marked FAILED, visibly). A numeric gap in
# channel ids is not proof of a missing message — deleted and service posts
# leave gaps — so the budget is what turns "we kept trying" into an actionable
# terminal state instead of an infinite retry loop.
GAP_MAX_ATTEMPTS = 5


def _gap_backoff(attempt: int) -> float:
    """Bounded exponential backoff with jitter for gap recovery attempts."""
    import random
    return float(min(3600.0, (2.0 ** max(1, attempt)) + random.uniform(1.0, 5.0)))

# A channel the session cannot resolve (`PEER_ID_INVALID`) or that is not a
# channel at all (`CHANNEL_INVALID`) fails on every sweep. Retrying it every
# 15s produced thousands of identical WARN lines and kept hammering Telegram
# with a request that cannot succeed, so the sweeper backs off per channel:
# first retry after a minute, doubling up to this ceiling. Any successful poll
# clears the backoff immediately.
FAILURE_BACKOFF_BASE_SECONDS = 60.0
FAILURE_BACKOFF_MAX_SECONDS = 900.0


class PollFailure(Exception):
    """A channel poll failed in a way that should be retried later, not now."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        #: True when the failure was the *transport* (the MTProto connection
        #: dropped, a timeout, a flood wait) rather than the channel. A
        #: transport failure says nothing about this channel, so it must not
        #: drive the channel's permanent backoff: one dropped connection used to
        #: put every source channel on a 900s retry ladder at once, and the
        #: ladder only clears on a successful poll — so a single blip could
        #: silence forwarding for 15 minutes after the connection was fine
        #: again.
        self.transient = transient


#: Substrings that mark a failure as *transport*, not channel-specific. The
#: connection layer reports these as plain exceptions (`Connection lost`,
#: `Timeout`, `[10] Retrying ...`) rather than typed RPC errors, so a substring
#: check is the only signal available; it is deliberately narrow and only ever
#: used to *soften* a backoff, never to declare a success.
_TRANSIENT_MARKERS = (
    "connection lost",
    "connection reset",
    "connection aborted",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "retrying",
    "flood wait",
    "floodwait",
    "service unavailable",
    "network is unreachable",
)


def _is_transient(exc: BaseException) -> bool:
    """True when a poll failure came from the transport, not from the channel.

    A dropped MTProto connection fails *every* channel's sweep at once, so it
    must not advance each channel's permanent backoff ladder: doing that turned
    one blip into a 900s silence per source channel after the connection was
    already healthy again (the ladder only clears on a successful poll).
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _is_channel_peer_id(chat_id: object) -> bool:
    """Whether a stored source chat id names a channel/supergroup peer.

    Telegram's peer numbering decides which MTProto RPCs are legal at all:

    * ``-100<channel_id>`` — channel or supergroup (a *channel* peer), the
      only peer ``channels.GetFullChannel`` / ``updates.GetChannelDifference``
      accept.
    * ``-<id>`` without the ``-100`` mark — a basic group, which is a chat
      peer, never a channel one.
    * positive — a user or a bot.

    Rules are created against all three shapes, so the poller must know which
    it is looking at before issuing a channel-only call. The id is stored as
    text (``forward_rules.source_chat_id`` is TEXT), so every branch parses.
    """
    text = str(chat_id or "").strip()
    if not text:
        return False
    try:
        value = int(text)
    except ValueError:
        # A @username or other non-numeric identifier: resolve_peer can answer
        # it, and it is not known to be a channel, so treat it as not-a-channel
        # rather than guessing. (Channel polling keys on the marked id.)
        return False
    if value >= 0:
        return False  # user / bot
    # The -100 mark names a channel/supergroup; a bare negative id is a basic
    # group. Channel ids and basic-group ids share the same numeric space, so
    # the mark — not magnitude — is the discriminator. Ids shorter than the
    # mark itself ("-1005") cannot be marked channel ids (the channel id
    # behind the mark must be at least 1, i.e. "-1001" is the minimum real
    # form), while "-5191245716" has no mark and is a basic group.
    digits = str(-value)
    if not digits.startswith("100"):
        return False
    return len(digits) > 3  # at least one digit of channel id behind the mark



class _OrderedReplayLane:
    """A per-channel FIFO that hands replayed posts to the pipeline in order.

    A channel difference is a *sequence*: posts carry a total order (ascending
    message id) and the target channel must receive them in exactly that order.
    Scheduling every post as its own task — the previous behaviour — let a
    50-post burst race through parsing and the pipeline concurrently, so the
    durable queue was filled in completion order and the target channel received
    the burst shuffled.

    One consumer per channel keeps the order exact (submit order is message-id
    order) while leaving the sweep loop free to keep polling other channels.

    ``accepted_ids`` (audit finding #2) records the ids the pipeline actually
    *took*: the cursor may only advance over those. A post whose dispatch
    raised, or that the lane worker could not parse, stays outside the
    watermark and is retried by the gap sweep instead of being skipped.
    """

    def __init__(self, key: Tuple[str, str], worker) -> None:
        self.key = key
        self._worker = worker
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self.submitted = 0
        self.completed = 0
        self.failed = 0
        # Set of source message ids the pipeline accepted (handed to the
        # registered handler without raising). Populated by _drain.
        self.accepted_ids: set = set()

    def submit(self, item) -> None:
        self._queue.put_nowait(item)
        self.submitted += 1
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            # The source id is what the cursor keys on, so the lane records it
            # for every item it hands over: success adds it to accepted_ids,
            # failure does not, and the poller advances only over the accepted.
            mid = int(getattr(item[2], "id", 0) or 0) if len(item) > 2 else 0
            try:
                await self._worker(item)
                self.completed += 1
                if mid:
                    self.accepted_ids.add(mid)
            except Exception:
                # One bad post must never kill the lane: the posts behind it are
                # still ordered work that has to go out.
                self.failed += 1
                logger.exception("poll: ordered replay failed for %r", self.key)
            finally:
                self._queue.task_done()

    async def join(self, timeout: Optional[float] = None) -> bool:
        """Wait until every submitted post has been handed to the pipeline.

        Returns True when the queue drained, False on timeout.
        """
        if self._queue.empty():
            return True
        if timeout is None:
            timeout = REPLAY_DRAIN_TIMEOUT_SECONDS
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "poll: replay lane %r still has %d post(s) pending after %.0fs",
                self.key, self._queue.qsize(), timeout,
            )
            return False

    def idle(self) -> bool:
        return self._queue.empty() and (self._task is None or self._task.done())

    def pending(self) -> int:
        return self._queue.qsize()


class ChannelPollingService:
    """Poll source channels for new posts when Telegram will not push them."""

    def __init__(
        self,
        pool,
        rules,
        dispatcher,
        state_store=None,
        interval: float = POLL_INTERVAL_SECONDS,
        lease_coordinator=None,
        gap_repo=None,
        log_client=None,
    ) -> None:
        self._pool = pool
        self._rules = rules
        self._dispatcher = dispatcher
        # Per-channel cursor: chat_id -> highest message id already replayed.
        # Kept out of the DB on purpose — the pipeline's dedup layer is the
        # authority; this only avoids re-reading the same tail every sweep.
        self._state: Dict[str, int] = {}
        self._interval = max(2.0, float(interval))
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_top: Dict[str, int] = {}
        self._state_store = state_store
        # Optional Redis lease coordinator (core/infrastructure/redis). With
        # one, a single replica sweeps at a time; without one (or when Redis is
        # unreachable) every replica sweeps, as before.
        self._lease = lease_coordinator
        # Audit finding #1: durable recovery work for the ids a truncated
        # channel difference did not return. ``None`` keeps the poller's
        # pre-existing behaviour for every embedding that does not wire it
        # (the cursor still only advances over what the pipeline accepted).
        self._gaps = gap_repo
        # Central structured logger (optional). Every gap, recovery and poll
        # failure emits a structured event through it — with the raw Telegram
        # error attached — so a future operator can answer "why was this gap
        # created?" end to end instead of grepping stdout.
        self._log_client = log_client
        # Ordered replay lanes, keyed by (session_id, chat_id). One live lane per
        # polled channel is what guarantees a burst leaves in source order.
        self._lanes: Dict[Tuple[str, str], _OrderedReplayLane] = {}
        # Per-channel failure backoff: failures[key] counts consecutive failed
        # sweeps, retry_at[key] is the next epoch second worth trying. A bad
        # source (invalid peer, non-channel id) is one WARN, not one per sweep.
        self._failures: Dict[Tuple[str, str], int] = {}
        self._retry_at: Dict[Tuple[str, str], float] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        await self._restore_cursors()
        self._task = asyncio.create_task(self._run(), name="atf-channel-poll")
        logger.info(
            "channel polling started (interval=%.0fs)", self._interval
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if await self._may_sweep():
                    await self.sweep_all()
                # Audit finding #1: replay the ids a truncated difference did
                # not return. This is the second half of the fix — detecting
                # the gap is not enough; the ids must be re-fetched or they
                # are still lost, just with a row saying so.
                if self._gaps is not None:
                    await self._recover_gaps()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The loop must survive a bad sweep; the next one will retry.
                logger.warning("channel poll sweep failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    async def _recover_gaps(self, limit: int = 50) -> int:
        """Retry the ingestion gaps whose backoff has elapsed.

        A gap row is recovery work, not a loss record: the id may be a deleted
        or service post that will never come back, so each attempt that still
        returns nothing consumes a bounded retry slot, and the row is marked
        FAILED with an actionable reason once the budget is spent — visibly,
        never silently. An id that *is* returned is dispatched through the
        normal lane and the row is resolved (deleted) by the pipeline observer.

        The fetch is a narrow ``MessageRange(min_id=max_id=id)`` request, so
        one Telegram call answers "does this post exist?" without pulling a
        whole slice of channel history.
        """
        if self._gaps is None:
            return 0
        try:
            due = await self._gaps.due_gaps(time.time(), limit=limit)
        except Exception as exc:
            logger.warning("poll: gap sweep could not read due gaps: %s", exc)
            return 0
        if not due:
            return 0

        # Group by (session, chat) so each channel's posts stay ordered and
        # each client is resolved once.
        by_channel: Dict[Tuple[str, str], List[dict]] = {}
        for g in due:
            by_channel.setdefault((str(g["session_id"]), str(g["source_chat_id"])), []).append(g)

        processed = 0
        for (session_id, chat_id), rows in by_channel.items():
            client = self._pool.get(session_id) if self._pool is not None else None
            if client is None:
                # The session is offline; leave the rows alone — the backoff
                # schedule already Defers them and a missing client must not
                # burn the retry budget.
                continue
            for g in rows:
                processed += await self._recover_one_gap(client, session_id, chat_id, g)
        return processed

    async def _recover_one_gap(self, client, session_id: str, chat_id: str, gap: dict) -> int:
        """Fetch one missing source id and dispatch it if it exists.

        Returns 1 when the attempt was made (whether the post exists or not),
        0 when the attempt could not even be issued.
        """
        assert self._gaps is not None
        gap_id = str(gap.get("id") or "")
        mid = int(gap.get("source_message_id") or 0)
        try:
            from pyrogram.raw.functions.updates import GetChannelDifference
            from pyrogram.raw.types import ChannelMessagesFilter, MessageRange

            peer = None
            try:
                target_peer = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
                peer = await client.resolve_peer(target_peer)
            except Exception as exc:
                raise PollFailure(f"cannot resolve peer: {exc}", transient=_is_transient(exc)) from exc

            full_pts = 0
            try:
                from pyrogram.raw.functions.channels import GetFullChannel
                full = await client.invoke(GetFullChannel(channel=peer))
                full_pts = getattr(getattr(full, "full_chat", None), "pts", 0) or 0
            except Exception:
                pass  # pts is only a hint for the difference call

            msg_filter = ChannelMessagesFilter(ranges=[MessageRange(min_id=mid, max_id=mid)])
            diff = await client.invoke(
                GetChannelDifference(channel=peer, filter=msg_filter, pts=full_pts, limit=1)
            )
            raw_messages = getattr(diff, "new_messages", None) or []
            if raw_messages:
                users = {u.id: u for u in (getattr(diff, "users", None) or [])}
                chats = {c.id: c for c in (getattr(diff, "chats", None) or [])}
                for raw in raw_messages:
                    logger.info(
                        "[POLL GAP] recovered post id=%s from %s — dispatching through the pipeline",
                        mid, chat_id,
                    )
                    self._emit("info", "INGESTION_GAP_RECOVERED", (
                        f"channel {chat_id}: gap id={mid} recovered — dispatching through the pipeline"
                    ), {
                        "chat_id": str(chat_id), "session_id": str(session_id),
                        "source_message_id": mid, "raw_error": "",
                        "retryable": False,
                    })
                    self._replay_one(client, session_id, raw, users, chats, chat_id=chat_id)
                # The row is left to the pipeline observer to resolve: only the
                # pipeline seeing the id proves it was not lost.
                return 1
            # Nothing at that id: a deleted/service post, most likely. Consume
            # a bounded retry slot; exhaustion marks it FAILED, visibly.
            exhausted = int(gap.get("attempts", 0) or 0) + 1 >= GAP_MAX_ATTEMPTS
            await self._gaps.mark_gap_attempt(
                gap_id, "id not returned by Telegram (deleted or service post)",
                _gap_backoff(int(gap.get("attempts", 0) or 0) + 1),
                exhausted=exhausted,
            )
            if exhausted:
                logger.error(
                    "[POLL GAP] source %s id=%s could not be recovered after %d attempt(s) — "
                    "marked FAILED (the post is most likely deleted; no delivery is owed)",
                    chat_id, mid, GAP_MAX_ATTEMPTS,
                )
                self._emit("error", "INGESTION_GAP_EXHAUSTED", (
                    f"channel {chat_id}: id={mid} not returned after {GAP_MAX_ATTEMPTS} "
                    f"attempt(s) — marked FAILED (deleted or service post; no delivery owed)"
                ), {
                    "chat_id": str(chat_id), "session_id": str(session_id),
                    "source_message_id": mid, "attempts": GAP_MAX_ATTEMPTS,
                    "raw_error": "ID_NOT_RETURNED",
                    "error_detail": "GetChannelDifference returned no message at this id",
                    "retryable": False,
                })
            return 1
        except PollFailure as exc:
            transient = bool(getattr(exc, "transient", False))
            attempts = int(gap.get("attempts", 0) or 0) + 1
            exhausted = attempts >= GAP_MAX_ATTEMPTS
            await self._gaps.mark_gap_attempt(
                gap_id, f"recovery failed: {exc}",
                _gap_backoff(attempts) if transient else self._interval * 4,
                exhausted=exhausted and not transient,
            )
            if exhausted and not transient:
                logger.error(
                    "[POLL GAP] source %s id=%s permanently unrecoverable after %d attempt(s): %s",
                    chat_id, mid, GAP_MAX_ATTEMPTS, exc,
                )
                # Attach the raw API error: an operator reading the central log
                # sees the Telegram-level cause (the RPC and its message), not
                # a paraphrase of it.
                from .errors import extract as _extract_error
                try:
                    event = _extract_error(exc, method="updates.GetChannelDifference",
                                           operation="gap_recovery", chat_id=chat_id,
                                           session_id=session_id, attempt=attempts)
                    raw = str(getattr(event, "error_type", "") or "")
                    raw_msg = str(getattr(event, "error_message", "") or "")[:300]
                except Exception:
                    raw, raw_msg = type(exc).__name__, str(exc)[:300]
                self._emit("error", "INGESTION_GAP_PERMANENT_FAILURE", (
                    f"channel {chat_id}: id={mid} permanently unrecoverable after "
                    f"{GAP_MAX_ATTEMPTS} attempt(s): {raw_msg}"
                ), {
                    "chat_id": str(chat_id), "session_id": str(session_id),
                    "source_message_id": mid, "attempts": attempts,
                    "raw_error": raw or type(exc).__name__,
                    "error_detail": raw_msg,
                    "retryable": False,
                })
            return 1
        except Exception as exc:
            logger.warning("poll: gap recovery for %s id=%s failed: %s", chat_id, mid, exc)
            return 0

    async def _may_sweep(self) -> bool:
        """True when this replica is the one sweeping channels.

        Two replicas polling the same channels read the same posts and push
        them through the same dispatcher; the queue's duplicate guard catches
        the delivery, but the wasted reads are the kind of traffic that gets an
        account rate-limited by Telegram. One sweeper at a time is the fix, and
        a deployment without a coordinator keeps the old behaviour.
        """
        if self._lease is None:
            return True
        try:
            # TTL is three sweeps: long enough that a slow sweep does not lose
            # the lease mid-pass, short enough that a crashed poller frees it
            # quickly.
            return bool(await self._lease.try_hold("channel-poll", ttl=max(30.0, self._interval * 3)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("channel poll lease unavailable (%s): sweeping anyway", exc)
            return True

    # ------------------------------------------------------------------ #
    # Sweeps
    # ------------------------------------------------------------------ #
    async def sweep_all(self) -> Tuple[int, int]:
        """One pass over every live session's active source channels."""
        sessions = self._pool.list_sessions() if hasattr(self._pool, "list_sessions") else []
        if not sessions:
            return (0, 0)

        replayed = 0
        checked = 0
        for session_id in sessions:
            client = self._pool.get(session_id)
            if client is None:
                continue
            try:
                channels = await self._source_channels(session_id)
            except Exception as exc:
                logger.debug("poll: cannot list channels for %s: %s", session_id[:8], exc)
                continue

            for chat_id, chat_name in channels:
                key = (session_id, chat_id)
                if self._retry_at.get(key, 0.0) > time.time():
                    # Backing off from a channel Telegram already refused; the
                    # previous failure was logged once (see _note_failure).
                    continue
                checked += 1
                try:
                    n = await self._poll_channel(client, session_id, chat_id, chat_name)
                except Exception as exc:
                    self._note_failure(key, chat_id, chat_name, exc)
                    continue
                replayed += n
                self._note_success(key)
                if n:
                    logger.info(
                        "poll: replayed %d new post(s) from %s (%s)", n, chat_name or chat_id, chat_id
                    )
        return (replayed, checked)

    def _note_failure(self, key: Tuple[str, str], chat_id: str, chat_name: str,
                      exc: Exception) -> None:
        """Record a failed sweep for one channel, with escalating backoff.

        The first failure is a WARNING because it is new information; repeats
        of the same permanent condition (an invalid peer never becomes valid on
        its own) are DEBUG with a periodic WARNING so the log stays truthful
        without drowning in it.

        A *transient* failure (the transport dropped) is different: it says
        nothing about this channel, and it hits every channel at once. Counting
        it on the channel's ladder silenced all forwarding for 15 minutes after
        one blip, so it retries on the next sweep instead.
        """
        if getattr(exc, "transient", False):
            # Debug only: the connection layer already logged the drop at
            # WARNING, and one line per channel per blip is the log storm the
            # per-channel backoff was introduced to stop.
            logger.debug(
                "poll: %s (%s) skipped this sweep (transport): %s",
                chat_name or chat_id, chat_id, exc,
            )
            return
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        delay = min(
            FAILURE_BACKOFF_MAX_SECONDS,
            FAILURE_BACKOFF_BASE_SECONDS * (2 ** min(count - 1, 8)),
        )
        self._retry_at[key] = time.time() + delay
        label = f"{chat_name or chat_id} ({chat_id})"
        if count == 1:
            logger.warning(
                "poll: %s failed (%s); backing off %.0fs before retrying",
                label, exc, delay,
            )
        elif count % 10 == 0:
            logger.warning(
                "poll: %s still failing after %d attempts (%s); next retry in %.0fs",
                label, count, exc, delay,
            )
        else:
            logger.debug("poll: %s failed again (%s); next retry in %.0fs", label, exc, delay)

    def _note_success(self, key: Tuple[str, str]) -> None:
        """Clear a channel's backoff after any successful poll."""
        if self._failures.pop(key, None) is not None:
            logger.info("poll: %s recovered; polling resumed", key[1])
        self._retry_at.pop(key, None)

    async def _source_channels(self, session_id: str) -> List[Tuple[str, str]]:
        """Active source channels for this session's rules."""
        try:
            all_rules = await self._rules.list_all()
        except Exception as exc:
            logger.debug("poll: cannot list rules: %s", exc)
            return []

        seen: Dict[str, str] = {}
        for rule in all_rules:
            if not getattr(rule, "is_active", False):
                continue
            if getattr(rule, "is_paused", False):
                continue
            rid = getattr(rule, "session_id", "") or ""
            if rid and rid != session_id:
                continue
            src = str(getattr(rule, "source_chat_id", "") or "")
            if src and src not in seen:
                seen[src] = getattr(rule, "source_chat_name", "") or src
        return list(seen.items())

    async def _poll_channel(
        self, client, session_id: str, chat_id: str, chat_name: str
    ) -> int:
        """Pull posts newer than the channel cursor and replay them."""
        from pyrogram.raw.functions.updates import GetChannelDifference
        from pyrogram.raw.types import ChannelMessagesFilter, MessageRange

        target_peer = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        try:
            peer = await client.resolve_peer(target_peer)
        except Exception:
            try:
                peer = await client.resolve_peer(chat_id)
            except Exception as exc:
                raise PollFailure(f"cannot resolve peer: {exc}") from exc

        if not _is_channel_peer_id(chat_id):
            # A channel-only MTProto RPC on a non-channel peer is a permanent
            # error: Telegram answers 400 CHANNEL_INVALID for every user, bot
            # and basic-group id, on every sweep, forever. Source rules do get
            # created against basic groups and bots (their messages arrive via
            # ordinary updates), so the poller must branch here rather than
            # hammer GetFullChannel against a peer that can never satisfy it.
            # The message stream itself is unaffected: live updates still
            # deliver these chats, only the polling fallback is skipped.
            logger.debug(
                "poll: %s (%s) is not a channel peer; skipping channel polling "
                "(live updates still deliver this source)",
                chat_name or chat_id, chat_id,
            )
            return 0

        # The channel pts is required by getChannelDifference.
        try:
            from pyrogram.raw.functions.channels import GetFullChannel

            full = await client.invoke(GetFullChannel(channel=peer))
        except Exception as exc:
            raise PollFailure(
                f"GetFullChannel failed: {exc}", transient=_is_transient(exc)
            ) from exc
        pts = getattr(getattr(full, "full_chat", None), "pts", 0) or 0

        cursor = self._state.get(chat_id, 0)
        try:
            top = await self._channel_top_message(client, chat_id)
        except Exception as exc:
            raise PollFailure(
                f"cannot read top message: {exc}", transient=_is_transient(exc)
            ) from exc

        logger.info(
            "[POLL TRACE] channel=%s (%s) cursor=%s top_msg_id=%s pts=%s",
            chat_name or chat_id, chat_id, cursor, top, pts,
        )

        if top is None or top <= cursor:
            # Nothing newer than what we already replayed.
            return 0

        lo = max(1, cursor + 1)
        hi = top
        logger.info(
            "[POLL TRACE] requesting difference for %s: range=[%d..%d] (pts=%s)",
            chat_name or chat_id, lo, hi, pts,
        )
        msg_filter = ChannelMessagesFilter(ranges=[MessageRange(min_id=lo, max_id=hi)])
        try:
            diff = await client.invoke(
                GetChannelDifference(
                    channel=peer, filter=msg_filter, pts=pts, limit=MAX_POSTS_PER_SWEEP
                )
            )
        except Exception as exc:
            raise PollFailure(
                f"GetChannelDifference failed: {exc}", transient=_is_transient(exc)
            ) from exc

        raw_messages = getattr(diff, "new_messages", None) or []
        messages = sorted(raw_messages, key=lambda m: getattr(m, "id", 0) or 0)
        returned_ids = [int(getattr(m, "id", 0) or 0) for m in messages]
        logger.info(
            "[POLL TRACE] channel=%s difference returned %d raw message(s)",
            chat_name or chat_id, len(messages),
        )

        # --- Audit finding #1: a truncated difference must not move the
        # cursor past the ids Telegram did not return. ----------------------- #
        # `lo..top` is what we ASKED for; `returned_ids` is what we GOT BACK.
        # A numeric gap is NOT proof of a missing message — deleted and service
        # posts leave legitimate gaps — so missing ids become durable *recovery
        # work* (recorded, retried with bounded backoff, resolved only when the
        # pipeline actually sees the id), never an immediate loss record. The
        # cursor itself advances only to the highest id the pipeline accepted.
        missing = self._missing_ids(lo, hi, returned_ids)
        if missing and self._gaps is not None:
            newly_recorded = 0
            try:
                newly_recorded = await self._gaps.record_gaps(session_id, chat_id, missing)
            except Exception as exc:
                logger.warning(
                    "[POLL GAP] channel=%s (%s): could not record %d missing id(s) "
                    "as recovery work: %s",
                    chat_name or chat_id, chat_id, len(missing), exc,
                )
            if newly_recorded > 0:
                # Only *newly discovered* truncation is a warning. Re-noticing
                # the same ids every sweep is not news — the gap sweep already
                # owns them, and logging it again only hides real signal.
                logger.warning(
                    "[POLL GAP] channel=%s (%s): difference returned %d of the %d id(s) "
                    "requested [%d..%d]; %d new id(s) missing (%s) recorded as durable "
                    "recovery work",
                    chat_name or chat_id, chat_id, len(returned_ids),
                    hi - lo + 1, lo, hi, newly_recorded,
                    self._describe_missing(missing),
                )
                self._emit("warn", "INGESTION_GAP_CREATED", (
                    f"channel {chat_id}: GetChannelDifference returned {len(returned_ids)} "
                    f"of {hi - lo + 1} requested id(s) [{lo}..{hi}]; "
                    f"{newly_recorded} recorded as durable recovery work"
                ), {
                    "chat_id": str(chat_id), "chat_name": str(chat_name or ""),
                    "session_id": str(session_id),
                    "requested_range": f"{lo}..{hi}",
                    "returned_count": int(len(returned_ids)),
                    "missing_ids": [int(i) for i in missing[:20]],
                    "missing_count": int(newly_recorded),
                    "raw_error": "DIFFERENCE_TRUNCATED",
                    "retryable": True,
                })
            elif missing:
                # Known gaps: trace-level, not a warning, so the poller is
                # quiet while the sweep works.
                logger.info(
                    "[POLL GAP] channel=%s (%s): %d missing id(s) already recorded as "
                    "recovery work; leaving them to the gap sweep",
                    chat_name or chat_id, chat_id, len(missing),
                )
        elif missing:
            # No gap store wired: report the truncation loudly rather than
            # silently advancing past unrecovered ids.
            logger.warning(
                "[POLL GAP] channel=%s (%s): difference returned %d of the %d id(s) "
                "requested [%d..%d]; %d id(s) missing (%s) and no gap store is "
                "configured — advancing the cursor only over delivered ids",
                chat_name or chat_id, chat_id, len(returned_ids),
                hi - lo + 1, lo, hi, len(missing), self._describe_missing(missing),
            )

        if not messages:
            # Nothing was returned. Keep the cursor moving so an empty
            # difference does not refetch — but never past ids we have not
            # accounted for. A gap that is still *open* (PENDING/RETRYING)
            # owns the id: hold the cursor at the oldest open one so the sweep
            # keeps retrying. A *terminal* gap (RESOLVED — the post was
            # delivered; FAILED — the budget was spent, so the post is gone
            # and no delivery is owed) is accounted for: advancing over it is
            # correct, and refusing to is what made a channel with exhausted
            # gaps re-request the same ids forever.
            if missing and self._gaps is not None:
                open_below = await self._open_gaps_below(session_id, chat_id, hi)
                if open_below:
                    # Do not advance past the oldest still-open gap; the sweep
                    # owns it. Log it once at the *recovery* end, not here, so
                    # the poller is quiet while the sweep works.
                    logger.info(
                        "[POLL GAP] channel=%s (%s): %d of %d missing id(s) still open, "
                        "holding the cursor at %s for the gap sweep",
                        chat_name or chat_id, chat_id, len(open_below), len(missing),
                        open_below[0],
                    )
                else:
                    # Every missing id is terminal (RESOLVED or FAILED): the
                    # work is done, and the cursor must move on or this poll
                    # re-asks for the same ids every sweep forever.
                    self._state[chat_id] = top
                    await self._persist_cursors()
            else:
                self._state[chat_id] = top
                await self._persist_cursors()
            return 0

        users = {u.id: u for u in (getattr(diff, "users", None) or [])}
        chats = {c.id: c for c in (getattr(diff, "chats", None) or [])}

        replayed = 0
        for raw in messages:
            logger.info(
                "[POLL REPLAY] dispatching post id=%s from %s (%s) through pipeline",
                getattr(raw, "id", "?"), chat_name or chat_id, chat_id,
            )
            if self._replay_one(client, session_id, raw, users, chats, chat_id=chat_id):
                replayed += 1
        if replayed:
            # Wait for the lane to hand every post to the pipeline *before*
            # advancing the cursor. Advancing first would let a crash (or a
            # restart) skip a tail that was never accepted; the cursor is the
            # only record of what has been seen.
            lane = self._lane_for(session_id, chat_id)
            if await lane.join():
                # Audit finding #2: a lane that *failed* posts must not let the
                # cursor advance past them. `completed` is what the pipeline
                # accepted; `failed` is what it dropped (a parse failure, a
                # missing handler). Advance to the highest id the pipeline
                # actually took, and leave the rest for the next sweep / the
                # gap sweep to retry — never silently past them.
                accepted = [i for i in returned_ids if i in lane.accepted_ids]
                new_cursor = max(accepted) if accepted else 0
                failed = [i for i in returned_ids if i not in lane.accepted_ids]
                if failed and self._gaps is not None:
                    try:
                        await self._gaps.record_gaps(session_id, chat_id, failed)
                    except Exception as exc:
                        logger.warning(
                            "[POLL GAP] channel=%s (%s): could not record %d failed "
                            "dispatch id(s) as recovery work: %s",
                            chat_name or chat_id, chat_id, len(failed), exc,
                        )
                    logger.warning(
                        "[POLL GAP] channel=%s (%s): %d post(s) failed ingestion "
                        "(%s) — recorded as recovery work, cursor held at %s",
                        chat_name or chat_id, chat_id, len(failed),
                        self._describe_missing(failed), new_cursor or lo - 1,
                    )
                if new_cursor > self._state.get(chat_id, 0):
                    self._state[chat_id] = new_cursor
                    await self._persist_cursors()
            else:
                logger.warning(
                    "poll: not advancing cursor for %s (%s) — %d post(s) still in the replay lane",
                    chat_name or chat_id, chat_id, lane.pending(),
                )
        self._prune_lanes()
        return replayed

    async def _channel_top_message(self, client, chat_id: str) -> Optional[int]:
        """Newest post id of a channel via a one-shot history read.

        Uses get_chat_history rather than the dialog list: the read-marker in
        the dialog list is exactly what Telegram uses to decide whether to
        push, so it is not a reliable indicator of what exists.
        """
        target = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        try:
            async for m in client.get_chat_history(target, limit=1):
                return int(getattr(m, "id", 0) or 0)
        except Exception:
            try:
                async for m in client.get_chat_history(chat_id, limit=1):
                    return int(getattr(m, "id", 0) or 0)
            except Exception as exc:
                logger.warning("poll: history read of %s failed: %s", chat_id, exc)
        return None

    # ------------------------------------------------------------------ #
    # Cursor persistence (survives restarts)
    # ------------------------------------------------------------------ #
    async def _restore_cursors(self) -> None:
        if self._state_store is None:
            return
        try:
            loaded = await self._state_store.load_channel_cursors()
            self._state.update({str(k): int(v) for k, v in (loaded or {}).items()})
        except Exception as exc:
            logger.warning("poll: cannot restore cursors: %s", exc)

    async def _persist_cursors(self) -> None:
        if self._state_store is None:
            return
        try:
            await self._state_store.save_channel_cursors(dict(self._state))
        except Exception as exc:
            logger.warning("poll: cannot persist cursors: %s", exc)

    def _lane_for(self, session_id: str, chat_id: str) -> _OrderedReplayLane:
        key = (str(session_id), str(chat_id))
        lane = self._lanes.get(key)
        if lane is None:
            lane = _OrderedReplayLane(key, self._dispatch_one)
            self._lanes[key] = lane
        return lane

    def _prune_lanes(self) -> None:
        """Drop lanes that have nothing pending so a long-lived process does not
        accumulate one lane object per channel ever polled."""
        for key in [k for k, lane in self._lanes.items() if lane.idle()]:
            self._lanes.pop(key, None)

    # ------------------------------------------------------------------ #
    # Truncated-difference detection (audit finding #1)
    # ------------------------------------------------------------------ #
    def _emit(self, level: str, event_code: str, message: str, detail: dict) -> None:
        """Send one structured event to the central logger, best effort.

        Diagnostics must never break the poller: a logger outage turns into a
        dropped record (the ``LogClient`` itself is bounded and counts what it
        drops), never into a missed source message.
        """
        if self._log_client is None:
            return
        try:
            getattr(self._log_client, level, self._log_client.info)(
                "ingestion", "poll", message, {**detail, "event_code": event_code},
            )
        except Exception:
            pass

    async def _open_gaps_below(self, session_id: str, chat_id: str, below: int) -> List[int]:
        """Ids in this channel that are still unaccounted for, at or below ``below``.

        Terminal gap states are excluded on purpose: a ``RESOLVED`` row means
        the pipeline delivered the post and a ``FAILED`` row means the
        recovery budget was spent on a post that does not exist (deleted or
        service). Both are *accounted for*, so neither may hold the cursor —
        otherwise a channel whose gaps are all exhausted polls the same
        range every sweep and logs the same warning forever.
        """
        if self._gaps is None:
            return []
        try:
            rows = await self._gaps.open_gaps(session_id, chat_id, below=int(below))
        except Exception as exc:
            logger.warning(
                "[POLL GAP] channel=%s (%s): could not read open gaps (%s); "
                "holding the cursor so no id is skipped",
                chat_id, chat_id, exc,
            )
            # Fail closed: if the read is impossible, do not advance past ids
            # we cannot account for.
            return [-1]
        return [int(r.get("source_message_id") or 0) for r in rows or ()]

    @staticmethod
    def _missing_ids(lo: int, hi: int, returned_ids: List[int]) -> List[int]:
        """Ids the sweep asked for but the difference did not return.

        Deliberately a *set difference over the requested range*, not a scan
        for every numeric gap in channel history: a channel's id sequence
        legitimately skips (deleted posts, service messages), so the only ids
        that are unaccounted for are the ones inside the range this sweep
        explicitly requested and Telegram did not serve. Those become recovery
        work; ids outside what we asked for are none of our business yet.
        """
        if hi < lo:
            return []
        got = {int(i) for i in returned_ids if i}
        return [i for i in range(lo, hi + 1) if i not in got]

    @staticmethod
    def _describe_missing(ids: List[int]) -> str:
        """Compact, range-compressed description for log lines."""
        if not ids:
            return "none"
        out: List[str] = []
        start = prev = ids[0]
        for i in ids[1:]:
            if i == prev + 1:
                prev = i
                continue
            out.append(f"{start}" if start == prev else f"{start}..{prev}")
            start = prev = i
        out.append(f"{start}" if start == prev else f"{start}..{prev}")
        return ",".join(out)

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def _replay_one(
        self, client, session_id: str, raw_message, users: dict, chats: dict,
        chat_id: str = "",
    ) -> bool:
        """Submit one replayed post to its channel's ordered lane.

        True when the post was accepted. Posts for one channel are handed to the
        pipeline strictly in ascending message-id order, one at a time — the
        sweep itself does not block on them.
        """
        try:
            lane = self._lane_for(session_id, chat_id or str(getattr(raw_message, "chat_id", "") or ""))
            lane.submit((client, session_id, raw_message, users, chats))
            return True
        except Exception as exc:
            logger.error("poll: dispatch schedule failed for post %s: %s", getattr(raw_message, "id", "?"), exc)
            return False

    async def _dispatch_one(self, item) -> None:
        """Lane worker: parse and hand one post to the registered handler."""
        client, session_id, raw_message, users, chats = item
        handler = None
        getter = getattr(self._dispatcher, "message_handler", None)
        if callable(getter):
            try:
                handler = getter(session_id)
            except Exception as h_err:
                logger.warning("poll: getter(session_id) error for %s: %s", session_id[:8], h_err)
                handler = None
        if handler is None:
            raise RuntimeError(
                f"no message handler registered for session {session_id[:12]} — "
                f"cannot dispatch post {getattr(raw_message, 'id', '?')}"
            )
        from pyrogram.types import Message as PyroMessage

        # pyrogram's Message._parse is async in this version.
        msg = await PyroMessage._parse(client, raw_message, users, chats)  # noqa: SLF001
        if msg is None:
            raise RuntimeError(f"parsed post {getattr(raw_message, 'id', '?')} returned None")
        await handler(client, msg)
