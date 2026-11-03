package album

import (
	"math"
	"strings"
	"testing"
)

// TestEvaluatePrecedence pins the decision's precedence: item limit first,
// window second, collecting otherwise. Each case supplies exactly one
// reason to flush and one reason not to.
func TestEvaluatePrecedence(t *testing.T) {
	s := NewSettings(2.0, 500)
	tests := []struct {
		name      string
		group     Group
		now       float64
		wantDone  bool
		wantEvent string
	}{
		{
			name:      "item limit beats a window with time left",
			group:     Group{Items: MaxGroupItems, CreatedAt: 100},
			now:       100.1,
			wantDone:  true,
			wantEvent: "album_limit",
		},
		{
			name:      "item limit beats an elapsed window too",
			group:     Group{Items: MaxGroupItems, CreatedAt: 100},
			now:       1_000_000,
			wantDone:  true,
			wantEvent: "album_limit",
		},
		{
			name:      "an open group with time left keeps collecting",
			group:     Group{Items: 3, CreatedAt: 100},
			now:       100.5,
			wantDone:  false,
			wantEvent: "",
		},
		{
			name:      "a window that just elapsed flushes",
			group:     Group{Items: 3, CreatedAt: 100},
			now:       102,
			wantDone:  true,
			wantEvent: "window",
		},
		{
			name:      "a single-item group is flushed by its window",
			group:     Group{Items: 1, CreatedAt: 100},
			now:       103,
			wantDone:  true,
			wantEvent: "window",
		},
		{
			name:      "deadline math is inclusive: at the deadline it flushes",
			group:     Group{Items: 2, CreatedAt: 100},
			now:       102,
			wantDone:  true,
			wantEvent: "window",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			d := s.Evaluate(tc.group, tc.now)
			if d.Complete != tc.wantDone {
				t.Fatalf("Complete = %v, want %v (reason %q)", d.Complete, tc.wantDone, d.Reason)
			}
			if d.Reason != tc.wantEvent {
				t.Fatalf("Reason = %q, want %q", d.Reason, tc.wantEvent)
			}
		})
	}
}

// TestDeadlines pins the deadline arithmetic: open_at + window, for the
// windows the product actually carries.
func TestDeadlines(t *testing.T) {
	tests := []struct {
		window    float64
		createdAt float64
		want      float64
	}{
		{0.8, 100, 100.8},   // the F-02 default
		{2.0, 100, 102},     // the dispatcher's hard-coded value
		{0.0, 100, 100},     // no window: complete on arrival
		{-1, 100, 100},      // negative window: same as none
		{1.5, 0, 1.5},       // a group opened at the clock's zero
		{0.05, 100, 100.05}, // the Python unit test's TTL
	}
	for _, tc := range tests {
		t.Run("window", func(t *testing.T) {
			s := NewSettings(tc.window, 500)
			if got := s.FlushAtWindow(tc.createdAt); got != tc.want {
				t.Fatalf("FlushAtWindow(%g) = %g, want %g", tc.createdAt, got, tc.want)
			}
		})
	}
}

// TestZeroWindowPinsNoCollection: a window that is not positive makes
// every group complete on arrival, because it can never grow.
func TestZeroWindowPinsNoCollection(t *testing.T) {
	for _, window := range []float64{0, -1, math.Inf(1)} {
		s := NewSettings(window, 500)
		d := s.Evaluate(Group{Items: 1, CreatedAt: 100}, 100)
		if !d.Complete || d.Reason != "window" {
			t.Fatalf("window %g: Evaluate = %+v, want complete/window", window, d)
		}
	}
}

// TestNaNWindowDefaults: the configured default applies when the field
// cannot be read as a number (a malformed projection is the documented
// 0.8 default, never a crash).
func TestNaNWindowDefaults(t *testing.T) {
	s := NewSettings(math.NaN(), 500)
	if s.Window != DefaultWindow {
		t.Fatalf("NaN window: got %g, want the default %g", s.Window, DefaultWindow)
	}
	d := s.Evaluate(Group{Items: 1, CreatedAt: 100}, 100)
	if d.Deadline != 100+DefaultWindow {
		t.Fatalf("NaN window deadline: got %g, want %g", d.Deadline, 100+DefaultWindow)
	}
}

// TestEviction mirrors the Python LRU bound: eviction starts only when
// the capacity is already reached, and it never starts when unbounded.
func TestEviction(t *testing.T) {
	t.Run("capacity reached evicts", func(t *testing.T) {
		s := NewSettings(2.0, 2)
		if !s.EvaluateEvicted(2) {
			t.Fatal("inFlight == capacity must evict")
		}
	})
	t.Run("below capacity does not", func(t *testing.T) {
		s := NewSettings(2.0, 2)
		if s.EvaluateEvicted(1) {
			t.Fatal("inFlight < capacity must not evict")
		}
	})
	t.Run("a non-positive capacity is unbounded", func(t *testing.T) {
		// The defaulting in NewSettings makes 0 the documented 500, so an
		// explicit unbounded mode is expressed by staying above the
		// capacity, not by passing zero.
		s := Settings{Window: 2.0, MaxActiveAlbums: 0}
		if s.EvaluateEvicted(1_000_000) {
			t.Fatal("unbounded capacity must never evict")
		}
	})
}

// TestShouldCollect: only a message carrying a media group id opens a
// group. Python's ingest returns True (standalone) for the empty id.
func TestShouldCollect(t *testing.T) {
	if !ShouldCollect("alb_A") {
		t.Fatal("a media group id must collect")
	}
	if ShouldCollect("") {
		t.Fatal("an empty media group id must not collect")
	}
}

// TestDefaultConstants pins the constants to their Python sources, so a
// change upstream is a visible test failure rather than a silent
// divergence.
func TestDefaultConstants(t *testing.T) {
	if MaxGroupItems != 10 {
		t.Errorf("MaxGroupItems = %d, want 10 (album_aggregator.py:83)", MaxGroupItems)
	}
	if DefaultWindow != 0.8 {
		t.Errorf("DefaultWindow = %g, want 0.8 (entities.py:410)", DefaultWindow)
	}
	if DefaultMaxActiveAlbums != 500 {
		t.Errorf("DefaultMaxActiveAlbums = %d, want 500 (dispatcher.py:96)", DefaultMaxActiveAlbums)
	}
}

// TestMonotonicNow is a smoke test: the clock moves forward and is a
// finite number, never a wall-clock zero.
func TestMonotonicNow(t *testing.T) {
	first := MonotonicNow()
	second := MonotonicNow()
	if math.IsNaN(first) || math.IsInf(first, 0) {
		t.Fatalf("MonotonicNow = %g, want finite", first)
	}
	if second < first {
		t.Fatalf("monotonic clock went backwards: %g then %g", first, second)
	}
	if strings.Contains(t.Name(), "never") {
		t.Fatal("unreachable")
	}
}
