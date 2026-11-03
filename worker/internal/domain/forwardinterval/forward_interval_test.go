package forwardinterval

import (
	"math"
	"testing"
)

// clampCases is the parity table Python pins in
// core/tests/test_forward_every_n.py::TestTheIntervalArithmetic::test_clamp.
// The harness replays the same inputs against clamp_forward_every_n, so
// these cases are not a local fixture — they are the shared contract.
var clampCases = []struct {
	name string
	raw  any
	want int
}{
	{"int one", 1, 1},
	{"int five", 5, 5},
	{"int ceiling", 1000, 1000},
	{"zero means off", 0, 1},
	{"negative means off", -3, 1},
	{"over the ceiling is capped not rejected", 1001, 1000},
	{"absurd value is capped", 1e9, 1000},
	{"float whole", 5.0, 5},
	{"float fraction truncates toward zero", 5.9, 5},
	{"numeric string", "5", 5},
	{"padded numeric string", "  7  ", 7},
	{"empty string", "", 1},
	{"non-numeric string", "abc", 1},
	{"nil", nil, 1},
	{"true is not an interval", true, 1},
	{"false is not an interval", false, 1},
	{"nan float", math.NaN(), 1},
	{"positive infinity", math.Inf(1), 1},
	{"negative infinity", math.Inf(-1), 1},
	{"nan string", "nan", 1},
	{"inf string", "inf", 1},
	{"map is garbage", map[string]any{}, 1},
	{"slice is garbage", []any{}, 1},
	{"huge but finite float", 1e18, 1000},
	{"tiny positive float", 0.5, 1},
	{"negative float", -2.7, 1},
	{"float32", float32(7), 7},
	{"int64", int64(12), 12},
	{"int32", int32(9), 9},
	{"scientific notation string", "2e3", 1000},
	// Python's float() accepts underscore digit separators between digits;
	// "1_000" IS 1000 there, so the port must agree. A leading, trailing
	// or doubled underscore is a ValueError and stays garbage.
	{"underscore separators are digits", "1_000", 1000},
	{"doubled underscore is garbage", "1__000", 1},
	{"leading underscore is garbage", "_1000", 1},
	{"trailing underscore is garbage", "1000_", 1},
}

func TestClamp(t *testing.T) {
	for _, tc := range clampCases {
		t.Run(tc.name, func(t *testing.T) {
			got := Clamp(tc.raw)
			if got != tc.want {
				t.Errorf("Clamp(%#v) = %d, want %d", tc.raw, got, tc.want)
			}
			// Any accepted value is idempotent: clamping twice is a no-op,
			// because the output is always already a legal int.
			if Clamp(got) != tc.want {
				t.Errorf("Clamp(Clamp(%#v)) = %d, want %d (not idempotent)", tc.raw, Clamp(got), tc.want)
			}
		})
	}
}

func TestClampAcceptsEveryLegalInterval(t *testing.T) {
	for n := MinForwardEveryN; n <= MaxForwardEveryN; n++ {
		if got := Clamp(n); got != n {
			t.Fatalf("Clamp(%d) = %d, want %d", n, got, n)
		}
	}
}

func TestN5ForwardsTheFirstOfEveryCycle(t *testing.T) {
	slots := []int{}
	for c := 1; c <= 20; c++ {
		if OwnsSlot(c, 5) {
			slots = append(slots, c)
		}
	}
	want := []int{1, 6, 11, 16}
	if len(slots) != len(want) {
		t.Fatalf("slots = %v, want %v", slots, want)
	}
	for i := range want {
		if slots[i] != want[i] {
			t.Fatalf("slots = %v, want %v", slots, want)
		}
	}
}

func TestN1ForwardsEveryMessage(t *testing.T) {
	for c := 1; c < 50; c++ {
		if !OwnsSlot(c, 1) {
			t.Errorf("OwnsSlot(%d, 1) = false, want true", c)
		}
	}
}

func TestEverySettingInRangeOwnsSlotsEveryN(t *testing.T) {
	for _, n := range []int{1, 2, 3, 5, 7, 10, 100, 999, 1000} {
		hits := []int{}
		for c := 1; c <= 4*n; c++ {
			if OwnsSlot(c, n) {
				hits = append(hits, c)
			}
		}
		want := make([]int, 0, 4)
		for k := 0; k < 4; k++ {
			want = append(want, 1+k*n)
		}
		if len(hits) != len(want) {
			t.Fatalf("n=%d: hits = %v, want %v", n, hits, want)
		}
		for i := range want {
			if hits[i] != want[i] {
				t.Fatalf("n=%d: hits = %v, want %v", n, hits, want)
			}
		}
	}
}

func TestZeroAndNegativeSequencesOwnNothing(t *testing.T) {
	// An unreadable counter must not behave like the first message.
	for _, everyN := range []int{2, 5, 1000} {
		if OwnsSlot(0, everyN) {
			t.Errorf("OwnsSlot(0, %d) = true, want false", everyN)
		}
		if OwnsSlot(-17, everyN) {
			t.Errorf("OwnsSlot(-17, %d) = true, want false", everyN)
		}
	}
	// N = 1 never looks at the counter at all: every message goes.
	if !OwnsSlot(0, 1) {
		t.Error("OwnsSlot(0, 1) = false, want true")
	}
}

func TestSlotsUntilNext(t *testing.T) {
	cases := []struct {
		sequence int
		everyN   int
		want     int
	}{
		{1, 5, 0}, // this message goes
		{2, 5, 4}, // 3, 4, 5, then 6 goes
		{5, 5, 1}, // the very next one goes
		{6, 5, 0},
		{9, 1, 0}, // N = 1: always now
		{0, 5, 0}, // non-positive sequence: undefined cadence
	}
	for _, tc := range cases {
		if got := SlotsUntilNext(tc.sequence, tc.everyN); got != tc.want {
			t.Errorf("SlotsUntilNext(%d, %d) = %d, want %d", tc.sequence, tc.everyN, got, tc.want)
		}
	}
}

// TestSlotsUntilNextAgreesWithOwnsSlot pins the internal consistency the UI
// copy relies on: the count reaches zero exactly on a slot.
func TestSlotsUntilNextAgreesWithOwnsSlot(t *testing.T) {
	for _, everyN := range []int{2, 3, 5, 17, 1000} {
		for sequence := 1; sequence <= 3*everyN+2; sequence++ {
			owns := OwnsSlot(sequence, everyN)
			until := SlotsUntilNext(sequence, everyN)
			if owns != (until == 0) {
				t.Errorf("everyN=%d sequence=%d: owns=%v until=%d (must agree)", everyN, sequence, owns, until)
			}
			if !owns {
				// Stepping forward through the reported count must land on
				// the next slot, never past it.
				next := sequence + until
				if !OwnsSlot(next, everyN) {
					t.Errorf("everyN=%d sequence=%d: next=%d is not a slot", everyN, sequence, next)
				}
			}
		}
	}
}

func TestTheThousandMessageTask(t *testing.T) {
	// N = 1000: slots are 1, 1001, 2001 — the first message of every thousand.
	if !OwnsSlot(1001, 1000) || !OwnsSlot(2001, 1000) {
		t.Error("the first of every thousand must own a slot")
	}
	if OwnsSlot(1000, 1000) || OwnsSlot(999, 1000) {
		t.Error("the 999th/1000th message must not own a slot under N=1000")
	}
}
