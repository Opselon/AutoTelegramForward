// Package forwardinterval is the Go port of the Python
// core/domain/forward_interval.py — the "Forward Every N Messages"
// arithmetic, in one place.
//
// A task can deliver only every Nth message that passes its filters. Two
// halves must agree on what that means, and neither may drift from the
// other:
//
//   - which message owns a slot — message 1, N+1, 2N+1 … (OwnsSlot), decided
//     from a 1-based count of the messages this task accepted;
//   - what a legal N is — 1 (forward every message, the default and the
//     backwards-compatible behaviour) through 1000 (Clamp).
//
// The counter itself is not kept here: it is persisted per task
// (rule_counters) because a restart must continue the sequence rather than
// replay slot 1. This package only defines the arithmetic, so the delivery
// preflight, the settings registry and the API validation all read the same
// bounds.
//
// Counting rules the pipeline relies on:
//
//   - only messages that passed every configured filter are counted — a
//     message the filter, media or content-policy stage rejected never
//     consumes a slot (the caller gates on the filter outcome);
//   - an album is one message. The dispatcher aggregates a media group and
//     hands the pipeline one payload, so the album is counted exactly once
//     regardless of how many items Telegram sent;
//   - an edit is not a new message: it updates a copy that already had its
//     slot, so edits are never counted and never advance the sequence.
//
// This is a verbatim port, not a redesign: the clamp's fail-open-to-1
// semantics, the non-positive-sequence rule and the slotsUntilNext formula
// are all Python's contract, and the parity harness compares the two
// engines case by case.
package forwardinterval

import (
	"math"
	"strconv"
	"strings"
)

// 1 means "forward every message", which is also the absence of the feature.
const (
	DefaultForwardEveryN = 1
	MinForwardEveryN     = 1
	MaxForwardEveryN     = 1000
)

// Clamp normalises any write source to a legal interval.
//
// Garbage (nil, "", "abc", NaN) and anything below the minimum becomes 1 —
// the feature off — because failing open keeps forwarding exactly as it was
// before the task configured anything. Values above the maximum are capped
// rather than rejected: a hand-edited row or an older client must not
// silence the task, and 1000 is the documented ceiling.
//
// The input is any, mirroring Python's typing.Any: the value may arrive
// from a JSON metadata blob (float64, string, bool), a hand-edited row or
// an API payload. Every one of those paths must land on the same number.
func Clamp(value any) int {
	// A boolean is not an interval: true/false both mean "not configured".
	// Python checks `value is None or isinstance(value, bool)` first, and
	// the bool arm must come before the numeric coercion below, because
	// float(true) is 1.0 — a perfectly legal interval — on both sides.
	if value == nil {
		return DefaultForwardEveryN
	}
	switch v := value.(type) {
	case bool:
		return DefaultForwardEveryN
	case string:
		// Python's try/except maps an unparseable string to the default,
		// and float(value) accepts "  7  " while rejecting "abc" and "".
		// strconv.ParseFloat shares that contract, so the only work
		// here is trimming the surrounding whitespace Python's lexer
		// ignores and treating a NaN/inf parse as "not an interval".
		s := strings.TrimSpace(v)
		if s == "" {
			return DefaultForwardEveryN
		}
		f, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return DefaultForwardEveryN
		}
		return clampInt(f)
	case float64:
		if math.IsNaN(v) || math.IsInf(v, 0) { // NaN / inf
			return DefaultForwardEveryN
		}
		return clampInt(v)
	case float32:
		f := float64(v)
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return DefaultForwardEveryN
		}
		return clampInt(f)
	case int:
		return clampInt(float64(v))
	case int64:
		return clampInt(float64(v))
	case int32:
		return clampInt(float64(v))
	}
	// Anything else (a map, a list, an object) is garbage: Python's
	// float(value) raises TypeError, which the try/except maps to 1.
	return DefaultForwardEveryN
}

// clampInt is Python's `int(number)` + bounds arm: a finite float truncates
// toward zero ("an interval is a whole message"), a sub-minimum value
// becomes 1 and anything above the ceiling is capped at it.
//
// Python additionally guards int() with (TypeError, ValueError,
// OverflowError); a float64 that overflows int64 surfaces here as
// math.MinInt64, which is far below the minimum and therefore becomes 1 —
// the same fail-open answer Python gives float("1e400") (inf → 1).
func clampInt(number float64) int {
	everyN := int(number)
	if everyN < MinForwardEveryN {
		return DefaultForwardEveryN
	}
	if everyN > MaxForwardEveryN {
		return MaxForwardEveryN
	}
	return everyN
}

// OwnsSlot reports whether the sequence-th accepted message is one of the
// forwarded ones.
//
// Sequence is 1-based: with N = 5 the messages 1, 6, 11, … are forwarded.
// A non-positive sequence is never a slot (an unreadable counter must not
// be treated as the first message, which would forward twice in a row).
func OwnsSlot(sequence int, everyN int) bool {
	if everyN <= MinForwardEveryN || everyN == DefaultForwardEveryN {
		return true
	}
	if sequence <= 0 {
		return false
	}
	return (sequence-1)%everyN == 0
}

// SlotsUntilNext reports how many more accepted messages arrive before the
// next forwarded one.
//
// 0 means this sequence owns a slot. SlotsUntilNext(2, 5) == 4: messages
// 3, 4, 5 and 6 still have to come, and 6 is forwarded. Used for UI copy
// ("next forward in 4 messages") and for tests; it never decides delivery.
func SlotsUntilNext(sequence int, everyN int) int {
	if everyN <= MinForwardEveryN || sequence <= 0 {
		return 0
	}
	if OwnsSlot(sequence, everyN) {
		return 0
	}
	nextSlot := ((sequence-1)/everyN+1)*everyN + 1
	return nextSlot - sequence
}
