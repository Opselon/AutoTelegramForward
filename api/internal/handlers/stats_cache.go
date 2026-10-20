package handlers

// A short-lived cache for the log-store aggregates.
//
// `/api/logs/stats` is the second-slowest endpoint in the stack (measured p50
// 42.55ms, p95 44.69ms): it fans a request out to the logger, which aggregates
// the whole archive (a COUNT, three GROUP BYs and the whole-archive by-rule
// roll-up). The dashboard polls it, so the same answer is recomputed dozens of
// times a minute for a number that moves by a row or two in that window.
//
// Two properties follow from that measurement:
//
//   * the answer is a *summary*, so a one-second-old copy is indistinguishable
//     from a live one on screen;
//   * concurrent pollers must not multiply the fan-out, so one key has at most
//     one in-flight computation (single-flight).
//
// Hence: a TTL cache with stale-while-revalidate, keyed by the exact scope the
// RPC takes (rule_id + user_id). A warm request is a mutex, a map lookup and a
// struct copy — no goroutine, no allocation beyond the response the handler
// marshals anyway.
//
// The freshness contract is visible to the client (`cached` + `age_ms`), so a
// stale answer is never presented as a live one, and `?fresh=1` bypasses the
// cache for an operator who needs the number right now (verifying that a purge
// took effect, for example).

import (
	"sync"
	"time"
)

// defaultStatsTTL is deliberately short: long enough to absorb a dashboard's
// polling, short enough that an operator watching a live purge sees it move.
const defaultStatsTTL = 1500 * time.Millisecond

// maxStatsScopes bounds the cache. The key space is tenants × tasks, so an
// unbounded map would be a slow leak in a long-lived process; the oldest entry
// is evicted when a new scope arrives at the cap.
const maxStatsScopes = 256

// cacheEntry is one cached value plus its provenance.
//
// `call` is non-nil while a fill is running: it is both the single-flight lock
// and the completion signal. `err` is cached too — a logger that is down should
// not be hammered once per dashboard poll — but an error is never served in
// place of a value, so a recovery is visible immediately when the TTL expires.
type cacheEntry[T any] struct {
	value T
	err   error
	at    time.Time
	call  chan struct{}
}

type ttlCache[T any] struct {
	mu      sync.Mutex
	ttl     time.Duration
	now     func() time.Time
	entries map[string]*cacheEntry[T]
	limit   int
}

func newTTLCache[T any](ttl time.Duration) *ttlCache[T] {
	return &ttlCache[T]{
		ttl:     ttl,
		now:     time.Now,
		entries: make(map[string]*cacheEntry[T]),
		limit:   maxStatsScopes,
	}
}

// get returns the value for key, computing it with fill when there is nothing
// fresh to serve.
//
// cached reports that the value came from the cache instead of this call's own
// computation, and age is how old it is. A stale value is returned immediately
// while fill runs in the background, so a slow downstream cannot make a polling
// client slow; a cold key blocks once and every concurrent caller for that key
// waits on the same computation.
func (c *ttlCache[T]) get(key string, fill func() (T, error)) (value T, cached bool, age time.Duration, err error) {
	c.mu.Lock()
	entry := c.entries[key]
	if entry == nil {
		entry = &cacheEntry[T]{}
		c.entries[key] = entry
		c.evictLocked(key)
	}

	haveValue := !entry.at.IsZero() && entry.err == nil
	fresh := haveValue && c.now().Sub(entry.at) < c.ttl

	// Fresh and settled: the plain cache hit.
	if fresh && entry.call == nil {
		value, age = entry.value, c.now().Sub(entry.at)
		c.mu.Unlock()
		return value, true, age, nil
	}
	// Fresh, a refresh is running, and we have a good value: serve it.
	if fresh && entry.call != nil {
		value, age = entry.value, c.now().Sub(entry.at)
		c.mu.Unlock()
		return value, true, age, nil
	}

	// Stale or cold: start a fill unless one is already running.
	wait := entry.call
	if wait == nil {
		wait = make(chan struct{})
		entry.call = wait
		go c.fill(entry, wait, fill)
	}
	// Stale with a usable value: serve it now, let the fill land behind us.
	if haveValue {
		value, age = entry.value, c.now().Sub(entry.at)
		c.mu.Unlock()
		return value, true, age, nil
	}
	c.mu.Unlock()

	// Cold (or the last attempt failed): wait for the fill we joined.
	<-wait

	c.mu.Lock()
	value, err, at := entry.value, entry.err, entry.at
	c.mu.Unlock()
	if !at.IsZero() {
		age = c.now().Sub(at)
	}
	return value, false, age, err
}

// fill runs one computation and publishes its result. Errors are cached for the
// TTL as well, so an outage produces one attempt per TTL per scope instead of
// one per request.
func (c *ttlCache[T]) fill(entry *cacheEntry[T], done chan struct{}, fn func() (T, error)) {
	value, err := fn()
	c.mu.Lock()
	// The entry may have been evicted while the fill ran; writing through the
	// pointer is still safe (the map no longer references it, so the result is
	// simply dropped).
	entry.value, entry.err, entry.at = value, err, c.now()
	entry.call = nil
	c.mu.Unlock()
	close(done)
}

// evictLocked drops the oldest settled entry when the map is over its cap. The
// incoming key is never the victim: it was just inserted and may be filling.
func (c *ttlCache[T]) evictLocked(keep string) {
	if c.limit <= 0 || len(c.entries) <= c.limit {
		return
	}
	oldestKey := ""
	var oldest time.Time
	for key, entry := range c.entries {
		if key == keep || entry.call != nil {
			continue
		}
		if oldestKey == "" || entry.at.Before(oldest) {
			oldestKey, oldest = key, entry.at
		}
	}
	if oldestKey != "" {
		delete(c.entries, oldestKey)
	}
}

// invalidate drops one key, used when a write makes the aggregate wrong now (a
// purge) rather than merely older.
func (c *ttlCache[T]) invalidate(key string) {
	c.mu.Lock()
	delete(c.entries, key)
	c.mu.Unlock()
}

// invalidateAll drops every scope. A purge is rarely scoped to the exact key a
// dashboard polls (one purge empties a level, a service or a whole window), so
// the safe answer is to forget everything.
func (c *ttlCache[T]) invalidateAll() {
	c.mu.Lock()
	c.entries = make(map[string]*cacheEntry[T])
	c.mu.Unlock()
}

// invalidateStatsCache is the handler-side wrapper: it must not create the
// cache just to empty it.
func (h *Handlers) invalidateStatsCache() {
	h.statsMu.Lock()
	cache := h.logStats
	h.statsMu.Unlock()
	if cache != nil {
		cache.invalidateAll()
	}
}
