"""Redis-backed coordination for the Core.

The Core is the only service that owns long-running loops: the delivery queue's
workers and watchdog, the channel poller, the session boot loop. Every one of
those loops is written to survive a restart and to be safe to run again, which
is what makes running two replicas safe-ish — but not free:

  * duplicated work: two replicas recovering the same expired leases, sweeping
    the same channels, polling the same chats;
  * duplicated traffic: two pollers reading the same channel with the same
    session, which is also the shape that gets an account rate-limited by
    Telegram;
  * confusing evidence: an incident where "the watchdog" acted twice is much
    harder to read than one where it acted once.

This package is the Redis half of the fix: a lease (a lock with a TTL and a
random token) that makes each of those loops run on ONE replica at a time.

What this package deliberately does NOT do is make delivery idempotent. A lease
can be lost while its holder believes it still has it (a long pause, a network
partition, a Redis failover), so anything whose repetition is harmful keeps its
own durable, database-backed idempotency: the delivery queue's UNIQUE(rule_id,
source_chat_id, source_message_id, target_chat_id) and its worker_id/lease_until
claim. The lease stops replicas from STARTING the same work; the database stops
a duplicated start from having an effect.

Outage policy, stated once: if Redis is not configured — or is unreachable —
`LeaseCoordinator.available` is False and every caller runs its work anyway.
That is fail-OPEN for coordination, and it is the right policy: the loops are
individually safe to run twice (that is what the queue's claim and the polling
cursors are for), whereas skipping the lease watchdog because an accelerator is
down would leave abandoned jobs claimed forever. A security limit must fail
closed; a work-avoidance optimisation must not.
"""

from __future__ import annotations

from .lease import Lease, LeaseCoordinator

__all__ = ["Lease", "LeaseCoordinator"]
