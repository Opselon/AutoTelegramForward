"""A dropped connection must not silence every source channel for 15 minutes.

The per-channel backoff exists for a *permanent* per-channel condition (an
invalid peer never becomes valid on its own). It counted transport failures on
the same ladder, so one `Connection lost` — which fails every channel's sweep at
once — pushed each channel to the 900s ceiling, and the ladder only clears on a
successful poll. Forwarding therefore stayed silent for up to fifteen minutes
after the connection was healthy again, which the field log shows as
`poll: … still failing after 40 attempts (GetFullChannel failed: Connection
lost); next retry in 900s`.
"""

from __future__ import annotations

import time

from core.infrastructure.telegram.channel_polling import (
    FAILURE_BACKOFF_BASE_SECONDS,
    PollFailure,
    _is_transient,
)

KEY = ("session-1", "-1004400615270")


class _Sweeper:
    """Only the backoff bookkeeping of the poller, without a Telegram client."""

    def __init__(self) -> None:
        from core.infrastructure.telegram.channel_polling import ChannelPollingService

        self._failures = {}
        self._retry_at = {}
        # Bound methods of the real class: the code under test is the real code.
        self._note_failure = ChannelPollingService._note_failure.__get__(self)
        self._note_success = ChannelPollingService._note_success.__get__(self)


def test_a_connection_drop_is_classified_as_transient():
    exc = PollFailure(
        "GetFullChannel failed: Connection lost", transient=_is_transient(
            OSError("Connection lost")
        )
    )
    assert exc.transient is True


def test_an_invalid_peer_is_not_transient():
    class RPCError(Exception):
        pass

    exc = PollFailure(
        "GetFullChannel failed: [400 CHANNEL_INVALID]",
        transient=_is_transient(RPCError("[400 CHANNEL_INVALID] The channel parameter is invalid")),
    )
    assert exc.transient is False


def test_a_transport_failure_does_not_start_the_channel_backoff():
    sweeper = _Sweeper()
    transient = PollFailure("GetFullChannel failed: Connection lost", transient=True)

    sweeper._note_failure(KEY, "-1004400615270", "SolHouse", transient)

    assert sweeper._failures.get(KEY, 0) == 0, "a transport blip advanced the channel ladder"
    assert sweeper._retry_at.get(KEY, 0.0) <= time.time(), "the channel was put on a retry timer"


def test_a_permanent_failure_still_backs_off():
    sweeper = _Sweeper()
    permanent = PollFailure("GetFullChannel failed: [400 CHANNEL_INVALID]")

    sweeper._note_failure(KEY, "-1004400615270", "SolHouse", permanent)

    assert sweeper._failures[KEY] == 1
    assert sweeper._retry_at[KEY] >= time.time() + FAILURE_BACKOFF_BASE_SECONDS - 1


def test_repeated_transport_failures_never_reach_the_ceiling():
    sweeper = _Sweeper()
    for _ in range(40):
        sweeper._note_failure(
            KEY, "-1004400615270", "SolHouse",
            PollFailure("GetFullChannel failed: Connection lost", transient=True),
        )
    assert sweeper._retry_at.get(KEY, 0.0) <= time.time()
