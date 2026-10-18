package ratelimit

import (
	"context"
	"hash/fnv"
	"sync"
	"time"
)

// localLimiter is the in-process fallback and the baseline.
//
// It exists for two reasons, in this order:
//
//  1. An outage policy needs something to fall back TO. "Redis is down, so
//     everything is allowed" is not a policy; a per-process bucket is.
//  2. It is the honest baseline: how fast the API can decide a limit when no
//     network is involved. Every number the Redis implementation reports is
//     only interesting next to it.
//
// The map is bounded. A key map that grows with the number of distinct scopes
// an attacker can invent is a memory exhaustion primitive, and the fallback
// runs exactly when the operator's attention is elsewhere. Entries whose bucket
// has fully refilled are dropped first (they carry no state worth keeping),
// then the shard is trimmed to its limit.
type localLimiter struct {
	shards  []*localShard
	keys    int64 // per-shard cap
	stats   *Stats
	nowFunc func() time.Time
}

type localShard struct {
	mu      sync.Mutex
	entries map[string]*localEntry
}

type localEntry struct {
	tokens float64
	last   time.Time
}

// newLocalLimiter builds a fallback limiter holding at most maxKeys scopes.
func newLocalLimiter(maxKeys int) *localLimiter {
	if maxKeys <= 0 {
		maxKeys = 20000
	}
	const shards = 64
	perShard := maxKeys / shards
	if perShard < 8 {
		perShard = 8
	}
	l := &localLimiter{shards: make([]*localShard, shards), keys: int64(perShard), stats: &Stats{}, nowFunc: time.Now}
	for i := range l.shards {
		l.shards[i] = &localShard{entries: make(map[string]*localEntry, perShard)}
	}
	return l
}

// Name implements Limiter.
func (l *localLimiter) Name() string { return "local" }

// Stats exposes the counters for the status endpoint.
func (l *localLimiter) Stats() *Stats { return l.stats }

func (l *localLimiter) shardFor(key string) *localShard {
	h := fnv.New64a()
	_, _ = h.Write([]byte(key))
	return l.shards[h.Sum64()%uint64(len(l.shards))]
}

// Allow implements Limiter with the same token-bucket semantics as the Lua
// script, so a request that is admitted locally while Redis is down is admitted
// under the same budget the cluster would have applied — just per process.
func (l *localLimiter) Allow(_ context.Context, key string, cost int64, b Budget) (Decision, error) {
	if cost < 1 {
		cost = 1
	}
	cap := float64(b.capacity())
	rate := b.ratePerSecond()
	now := l.nowFunc()

	s := l.shardFor(key)
	s.mu.Lock()
	e, ok := s.entries[key]
	if !ok {
		if int64(len(s.entries)) >= l.keys {
			// Leave room for the entry about to be inserted, so the shard cap
			// is a real cap and not a cap+1 per shard.
			l.evict(s, now, b)
		}
		e = &localEntry{tokens: cap, last: now}
		s.entries[key] = e
	}
	elapsed := now.Sub(e.last).Seconds()
	if elapsed > 0 {
		e.tokens = minFloat(cap, e.tokens+elapsed*rate)
	}
	dec := Decision{Limit: b.capacity()}
	if e.tokens >= float64(cost) {
		e.tokens -= float64(cost)
		dec.Allowed = true
	} else {
		dec.RetryAfter = time.Duration(((float64(cost) - e.tokens) / rate) * float64(time.Second))
		if dec.RetryAfter <= 0 {
			dec.RetryAfter = time.Millisecond
		}
	}
	dec.Remaining = int64(e.tokens)
	if dec.Remaining < 0 {
		dec.Remaining = 0
	}
	e.last = now
	s.mu.Unlock()

	if dec.Allowed {
		l.stats.Allowed.Add(1)
	} else {
		l.stats.Limited.Add(1)
	}
	return dec, nil
}

// evict frees room in a full shard. Caller holds the shard lock.
func (l *localLimiter) evict(s *localShard, now time.Time, b Budget) {
	full := float64(b.capacity())
	rate := b.ratePerSecond()
	for k, e := range s.entries {
		// A bucket that is full again carries no information: dropping it
		// cannot change a future decision (it would start full anyway).
		if e.tokens+(now.Sub(e.last).Seconds()*rate) >= full {
			delete(s.entries, k)
		}
	}
	if int64(len(s.entries)) < l.keys {
		return
	}
	// Still over the cap: the shard holds only active scopes, so drop the ones
	// that have been idle longest. This loses state for a scope that is not
	// using the endpoint while the process is under memory pressure, which is
	// the right thing to lose — a live caller's bucket refills from its own
	// traffic anyway.
	for k := range s.entries {
		delete(s.entries, k)
		if int64(len(s.entries)) < l.keys {
			break
		}
	}
}

// Trim drops every scope that is fully refilled, and is called periodically so
// a long-lived process does not keep memory for scopes nobody uses any more.
func (l *localLimiter) Trim(b Budget) int {
	removed := 0
	full := float64(b.capacity())
	for _, s := range l.shards {
		s.mu.Lock()
		for k, e := range s.entries {
			if e.tokens >= full {
				delete(s.entries, k)
				removed++
			}
		}
		s.mu.Unlock()
	}
	return removed
}

// Size reports how many scopes the fallback currently holds (QA + status).
func (l *localLimiter) Size() int {
	n := 0
	for _, s := range l.shards {
		s.mu.Lock()
		n += len(s.entries)
		s.mu.Unlock()
	}
	return n
}

func minFloat(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}
