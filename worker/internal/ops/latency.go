package ops

import (
	"sort"
	"sync"
)

// latencyRing is a bounded ring buffer of latency observations in
// MILLISECONDS (the units the contract's /ops/queue reports).
//
// Mutex-guarded rather than lock-free on purpose: the append path is one
// short critical section, and the reader (a percentile query) needs a
// sorted copy anyway. A lock-free ring would add correctness risk (a reader
// racing a wraparound can observe a torn window) for no measurable gain at
// the append rate the delivery engine produces.
type latencyRing struct {
	mu     sync.Mutex
	buf    []float64
	next   int
	filled bool
	sum    float64
}

// newLatencyRing builds a ring of the given capacity.
func newLatencyRing(capacity int) *latencyRing {
	if capacity < 1 {
		capacity = 1
	}
	return &latencyRing{buf: make([]float64, capacity)}
}

// observe appends one observation, in milliseconds.
func (r *latencyRing) observe(ms float64) {
	r.mu.Lock()
	// The first wraparound subtracts the value being evicted; before that
	// the slot holds a zero that must not pollute the running mean.
	if r.filled {
		r.sum -= r.buf[r.next]
	}
	r.buf[r.next] = ms
	r.sum += ms
	r.next++
	if r.next >= len(r.buf) {
		r.next = 0
		r.filled = true
	}
	r.mu.Unlock()
}

// sorted returns a sorted copy of the windowed observations.
func (r *latencyRing) sorted() []float64 {
	r.mu.Lock()
	n := r.next
	if r.filled {
		n = len(r.buf)
	}
	out := make([]float64, 0, n)
	if r.filled {
		out = append(out, r.buf...)
	} else {
		out = append(out, r.buf[:r.next]...)
	}
	r.mu.Unlock()
	sort.Float64s(out)
	return out
}

// mean returns the average of the windowed observations, or 0 when empty.
func (r *latencyRing) mean() float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := r.next
	if r.filled {
		n = len(r.buf)
	}
	if n <= 0 {
		return 0
	}
	return r.sum / float64(n)
}

// countInWindow is the number of observations currently held.
func (r *latencyRing) countInWindow() int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.filled {
		return int64(len(r.buf))
	}
	return int64(r.next)
}

// percentile returns the p-th percentile (0..100) of the windowed
// observations by nearest-rank. p50 of one observation is that observation;
// an empty window is 0 — the surface's "no data yet", and the panel's job
// to render as such rather than as a fabricated value.
func (r *latencyRing) percentile(p float64) float64 {
	s := r.sorted()
	n := len(s)
	if n == 0 {
		return 0
	}
	if p <= 0 {
		return s[0]
	}
	if p >= 100 {
		return s[n-1]
	}
	// Nearest rank: rank = ceil(p/100 * N), clamped to the window.
	rank := int((p/100.0)*float64(n) + 0.999999)
	if rank < 1 {
		rank = 1
	}
	if rank > n {
		rank = n
	}
	return s[rank-1]
}
