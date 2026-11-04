package ops

import "sync"

// windowCounter is a bounded sliding-window count: how many of the last N
// events matched. It is the "recent failure history" the stage-health
// derivation reads, without keeping the events themselves.
//
// A single ring of 0/1 flags is enough here because the only question is
// "how many of the last N were failures" (the total is the ring size).
type windowCounter struct {
	mu     sync.Mutex
	slots  []bool
	next   int
	filled bool
	count  int // matched slots currently in the window
	cap    int
}

// newWindowCounter builds a window of the given capacity.
func newWindowCounter(capacity int) *windowCounter {
	if capacity < 1 {
		capacity = 1
	}
	return &windowCounter{slots: make([]bool, capacity), cap: capacity}
}

// add records one event; matched reports whether it counts toward the total.
func (w *windowCounter) add(matched bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.filled && w.slots[w.next] {
		w.count--
	}
	w.slots[w.next] = matched
	if matched {
		w.count++
	}
	w.next++
	if w.next >= w.cap {
		w.next = 0
		w.filled = true
	}
}

// snapshot returns how many matched events are in the current window.
func (w *windowCounter) snapshot() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.count
}
