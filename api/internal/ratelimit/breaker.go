package ratelimit

import (
	"math/rand"
	"sync/atomic"
	"time"
)

// breakerState is the circuit breaker's observable state.
type breakerState struct {
	State       string `json:"state"`    // closed | open | half-open
	Failures    int64  `json:"failures"` // consecutive failures observed
	OpenedAt    string `json:"opened_at,omitempty"`
	Rejected    int64  `json:"rejected"` // checks short-circuited while open
	LastFailure string `json:"last_failure,omitempty"`
}

// breaker stops the API from queueing on a Redis that is gone.
//
// Without it, every request pays the full pool-timeout/read-timeout before
// falling back, so a Redis outage is amplified into an API-wide p99 spike — the
// exact failure a limiter is supposed to prevent. With it, the first few
// failures trip the breaker and the rest of the traffic skips the dead
// connection immediately until a half-open probe succeeds.
type breaker struct {
	failures  atomic.Int64
	openUntil atomic.Int64 // unix nanos; 0 = closed
	rejected  atomic.Int64
	lastFail  atomic.Int64 // unix nanos
	threshold int64
	cooldown  time.Duration
	probing   atomic.Bool // only one half-open probe at a time
}

func newBreaker(threshold int, cooldown time.Duration) *breaker {
	if threshold <= 0 {
		threshold = 5
	}
	if cooldown <= 0 {
		cooldown = time.Second
	}
	return &breaker{threshold: int64(threshold), cooldown: cooldown}
}

// open reports whether a call should be short-circuited.
func (b *breaker) open() bool {
	until := b.openUntil.Load()
	if until == 0 {
		return false
	}
	if time.Now().UnixNano() < until {
		b.rejected.Add(1)
		return true
	}
	// Cooldown elapsed: let exactly one request through as a probe.
	if b.probing.CompareAndSwap(false, true) {
		return false
	}
	b.rejected.Add(1)
	return true
}

// success closes the breaker; a single healthy answer is enough evidence that
// Redis is back, and carrying the open window any longer keeps refusing traffic
// that would now succeed.
func (b *breaker) success() {
	b.failures.Store(0)
	b.openUntil.Store(0)
	b.probing.Store(false)
}

// failure counts one failed call and trips the breaker at the threshold.
func (b *breaker) failure() {
	b.lastFail.Store(time.Now().UnixNano())
	n := b.failures.Add(1)
	if n >= b.threshold {
		b.openUntil.Store(time.Now().Add(b.cooldown).UnixNano())
		b.probing.Store(false)
	}
}

func (b *breaker) state() breakerState {
	s := breakerState{Failures: b.failures.Load(), Rejected: b.rejected.Load()}
	until := b.openUntil.Load()
	switch {
	case until == 0:
		s.State = "closed"
	case time.Now().UnixNano() < until:
		s.State = "open"
		s.OpenedAt = time.Unix(0, until).UTC().Format(time.RFC3339Nano)
	default:
		s.State = "half-open"
	}
	if ts := b.lastFail.Load(); ts != 0 {
		s.LastFailure = time.Unix(0, ts).UTC().Format(time.RFC3339Nano)
	}
	return s
}

// jitter returns a duration in [min,max): bounded retries with jitter, so a
// fleet of replicas that lost Redis does not reconnect in lockstep.
func jitter(min, max time.Duration) time.Duration {
	if max <= min {
		return min
	}
	return min + time.Duration(rand.Int63n(int64(max-min)))
}
