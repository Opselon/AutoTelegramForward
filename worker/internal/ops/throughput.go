package ops

import (
	"sync"
	"time"
)

// throughputHistory answers the contract's §4 aggregation windows:
// delivery throughput for the last 1m / 5m / 15m / 1h.
//
// Design constraints (the bounded-telemetry rule the whole ops surface is
// built on):
//
//   - NO per-message writes. The history is fed from SampleRates, the ticker
//     the collector already runs, as a DELTA of the monotonic delivered
//     counter. One int per 15s tick.
//   - Bounded storage by construction: 60 minute-buckets = one hour of
//     history, regardless of message rate. A worker delivering 10k messages
//     a minute stores exactly the same 60 ints as an idle one.
//   - The windows are wall-clock minutes, so "last 5 minutes" means the five
//     minute buckets whose boundaries have fully elapsed — the current,
//     partially-filled minute is excluded from every window (its count is
//     still growing and would understate the rate).
//
// What this is NOT: percentile latency windows (that is the latency ring's
// job) or backlog-curve history (the backlog trend's). One signal, one
// bounded structure.
type throughputHistory struct {
	mu sync.Mutex
	// buckets: one per minute, ring-indexed. bucket i covers the minute
	// (now - (60-i)min, now - (59-i)min]; index 0 is the OLDEST.
	buckets [60]int64
	// lastTick is the timestamp of the last SampleRates tick we consumed,
	// so a restart of the ticker (not the worker) cannot be read as a
	// gap in history.
	lastTick time.Time
	// lastDelivered is the monotonic counter value at the last tick; the
	// delta between ticks lands in the minute bucket the tick belongs to.
	lastDelivered int64
	// currentHead is the minute bucket currently accumulating (the
	// partially-filled minute excluded from the windows).
	currentHead int
	// minutesObserved: how many distinct wall-clock minutes have received
	// at least one tick, capped at the ring size. This IS the count of
	// complete minutes of history: a minute that got a tick has 60s of
	// elapsed delivery behind it (the delta came from the previous tick),
	// so it is not a partial minute. Capped so the number cannot lie
	// about having more than an hour of history.
	minutesObserved int
}

// newThroughputHistory builds the 60-minute ring. deliv is the current
// monotonic delivered count, so the first delta is measured from the real
// baseline rather than zero (zero would record the worker's entire history
// as if it happened in the first minute).
func newThroughputHistory(deliv int64) *throughputHistory {
	return &throughputHistory{lastDelivered: deliv}
}

// observe consumes one ticker tick: the delivered count at that tick and
// the tick's timestamp. The DELTA lands in the minute bucket the tick
// belongs to; on a minute boundary the ring advances, aging the oldest
// bucket out (overwrite, not grow).
func (h *throughputHistory) observe(now time.Time, delivered int64) {
	h.mu.Lock()
	defer h.mu.Unlock()

	minute := now.Unix() / 60
	delta := delivered - h.lastDelivered
	if delta < 0 {
		// Monotonic counter went backwards — a restart resets the count.
		// The pre-restart history is still valid; only this delta is
		// meaningless, so drop it and re-baseline.
		h.lastDelivered = delivered
		h.lastTick = now
		return
	}

	var advanced int
	switch {
	case h.lastTick.IsZero():
		// First tick after construction: the delta is measured from the
		// construction baseline, and this minute becomes the first
		// observed minute of history.
		advanced = 1
	default:
		prevMinute := h.lastTick.Unix() / 60
		if minute > prevMinute {
			advanced = int(minute - prevMinute)
		}
	}
	if advanced > 0 {
		if advanced > 60 {
			advanced = 60 // the whole history aged out at once
		}
		for i := 0; i < advanced; i++ {
			h.currentHead = (h.currentHead + 1) % 60
			h.buckets[h.currentHead] = 0
		}
		if h.minutesObserved < 60 {
			if h.minutesObserved+advanced > 60 {
				h.minutesObserved = 60
			} else {
				h.minutesObserved += advanced
			}
		}
	}
	h.buckets[h.currentHead] += delta
	h.lastDelivered = delivered
	h.lastTick = now
}

// windows returns the delivered-message count in each aggregation window.
// The CURRENT minute is excluded from every window: it is partially elapsed
// and its count would understate the rate. A window older than the ring's
// full history reports -1 = "not enough history yet", so the console can
// render 'insufficient history' instead of a misleading small number.
func (h *throughputHistory) windows(now time.Time) map[string]int64 {
	h.mu.Lock()
	defer h.mu.Unlock()

	// fullyElapsed: how many minutes of history are complete. A minute that
	// received a tick is complete by construction (its delta spans the full
	// 60s since the previous tick), so this is exactly minutesObserved.
	// Minutes between the last tick and now are NOT history — no ticker ran
	// — and do not count.
	fullyElapsed := int64(h.minutesObserved)

	sum := func(bucketCount int64) int64 {
		if fullyElapsed < bucketCount {
			return -1 // not enough history
		}
		// Walk backwards from the head, the head bucket being the
		// minute BEFORE the current partially-elapsed one.
		var total int64
		idx := h.currentHead
		for i := int64(0); i < bucketCount; i++ {
			total += h.buckets[idx]
			idx--
			if idx < 0 {
				idx = 59
			}
		}
		return total
	}

	return map[string]int64{
		"last_1m":  sum(1),
		"last_5m":  sum(5),
		"last_15m": sum(15),
		"last_1h":  sum(60),
	}
}

// historyAge reports how many minutes of history the ring actually holds,
// so /ops can say "history since X" rather than implying a full hour exists
// right after a start.
func (h *throughputHistory) historyAgeMinutes(now time.Time) int64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.lastTick.IsZero() {
		return 0
	}
	return now.Unix()/60 - h.lastTick.Unix()/60
}
