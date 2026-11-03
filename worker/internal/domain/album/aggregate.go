// Package album ports the media-group aggregation DECISION the Python
// MessageDispatcher's AlbumAggregator makes (core/infrastructure/telegram/
// album_aggregator.py), as a pure function of the configuration and the
// in-flight group state.
//
// What is ported is the decision, not the asyncio plumbing. The Python
// aggregator owns a coroutine-based TTL timer per group; the Worker
// instead derives the same answers — is this group complete, how long may
// it accumulate, when must it flush — from a clock the caller supplies,
// which is what makes the decision testable and replay-safe.
//
// The one F-02 fact this package exists to encode: the Python dispatcher
// (dispatcher.py:96) constructs its aggregator with a HARD-CODED
// ttl_seconds=2.0, so ForwardRule.album_aggregation_window_seconds
// (entities.py:410, default 0.8) is never read on that path today. The
// decision below takes the per-task window as an INPUT and applies the
// authoritative semantics — the value the task configured — so the port
// is correct with respect to the field's contract while the wiring (the
// dispatcher's constructor) stays a Python-side defect the parity
// harness documents, not something this package silently re-decides.
package album

import (
	"math"
	"time"
)

// MaxGroupItems is Telegram's album limit (album_aggregator.py:83): a
// media group that reaches this many pieces is complete immediately, no
// matter how much window is left.
const MaxGroupItems = 10

// DefaultWindow is album_aggregation_window_seconds' default
// (entities.py:410): the value a task carries when it never configured
// the field explicitly.
const DefaultWindow = 0.8

// DefaultMaxActiveAlbums is the dispatcher's LRU capacity
// (dispatcher.py:96, max_active_albums=500): the bound on in-flight
// groups before the oldest is evicted and flushed early.
const DefaultMaxActiveAlbums = 500

// Settings is the per-task aggregation configuration. Every field is an
// input to the decision, never a store handle or a timer: the caller
// resolves the task's configured window out of the rule and hands it here.
type Settings struct {
	// Window is album_aggregation_window_seconds for the task, in
	// seconds. A non-positive value means "no window", which is the one
	// case a group is flushed as soon as it is seen — it can never grow.
	Window float64
	// MaxActiveAlbums bounds how many in-flight groups may accumulate
	// before the oldest is evicted. <= 0 means unbounded (the decision
	// never forces an eviction flush).
	MaxActiveAlbums int
}

// Group is the state of one in-flight media group: what has been
// collected, and since when. It mirrors AlbumGroup's fields, minus the
// asyncio task handle, which is an implementation detail of the Python
// timer and not part of the decision.
type Group struct {
	// Items is the number of distinct pieces collected so far.
	Items int
	// CreatedAt is when the group opened (time.monotonic() on the Python
	// side — a monotonically increasing clock, never a wall time).
	CreatedAt float64
}

// Decision is the aggregation outcome for one group at one instant.
//
// It is a value, not an error: every state has an answer, and none of
// them is exceptional.
type Decision struct {
	// Complete reports whether the group must be flushed now: it hit the
	// item limit, its window elapsed, or the caller evicted it.
	Complete bool
	// Reason is why the group flushed, in the vocabulary the Python
	// aggregator's logs and the parity harness use:
	//
	//   "album_limit"  — 10 items reached (album_aggregator.py:83)
	//   "window"       — the TTL window elapsed
	//   "lru_eviction" — the caller's capacity bound forced it
	//   ""             — not complete, keep collecting
	Reason string
	// Deadline is the instant the group becomes complete by window
	// elapsed, in the same monotonic clock units CreatedAt uses. It is
	// informative only: a group complete by item limit has one anyway,
	// and the caller may use it to schedule the next check.
	Deadline float64
}

// NewSettings resolves a task's configured window into Settings,
// applying the same defaulting the Python entity applies
// (entities.py:410): a field read as 0 or negative on the Go side is
// treated as the documented default, never as "aggregation disabled",
// because no such mode exists on the Python path — a task that turns
// albums off does it with album_mode, not with the window.
func NewSettings(window float64, maxActiveAlbums int) Settings {
	if maxActiveAlbums <= 0 {
		maxActiveAlbums = DefaultMaxActiveAlbums
	}
	if math.IsNaN(window) {
		window = DefaultWindow
	}
	return Settings{Window: window, MaxActiveAlbums: maxActiveAlbums}
}

// FlushAtWindow reports the instant a group that opened at createdAt
// becomes complete by window elapsed. It is Deadline without the group,
// exposed so a caller can answer "when should I look again?" with no
// state of its own.
func (s Settings) FlushAtWindow(createdAt float64) float64 {
	return createdAt + s.windowOrZero()
}

// windowOrZero is the effective window: the configured value, or 0 when
// the task disabled the window — the one case where a group is flushed on
// arrival because it can never grow.
func (s Settings) windowOrZero() float64 {
	if !(s.Window > 0) || math.IsInf(s.Window, 1) {
		return 0
	}
	return s.Window
}

// Evaluate answers "what happens to this group at now?".
//
// The precedence mirrors the Python aggregator's ingest path exactly:
//
//  1. A group at or past the album limit is complete NOW, by item limit,
//     regardless of the window. (Python flushes immediately at 10 items
//     and cancels the pending timer — the timer's remaining time is
//     irrelevant.)
//  2. Otherwise the group is complete when its window elapsed, by
//     window. A zero-length window makes every group complete on
//     arrival, which is the semantics of "no collection window".
//  3. Otherwise the group keeps collecting.
//
// The LRU eviction is not decided here: it depends on how many OTHER
// groups are in flight, which the caller owns. EvaluateEvicted answers
// that question separately so the two inputs never fold into one
// predicate.
func (s Settings) Evaluate(g Group, now float64) Decision {
	d := Decision{Deadline: s.FlushAtWindow(g.CreatedAt)}
	switch {
	case g.Items >= MaxGroupItems:
		d.Complete = true
		d.Reason = "album_limit"
	case now >= d.Deadline:
		d.Complete = true
		d.Reason = "window"
	}
	return d
}

// EvaluateEvicted answers the LRU question: given inFlight in-flight
// groups, must the caller evict (and therefore flush early) before
// opening another one?
//
// It mirrors the Python aggregator's capacity check
// (album_aggregator.py:90-98): eviction happens when the bound is
// already reached, and it takes the OLDEST group — the caller decides
// which one that is.
func (s Settings) EvaluateEvicted(inFlight int) bool {
	if s.MaxActiveAlbums <= 0 {
		return false
	}
	return inFlight >= s.MaxActiveAlbums
}

// ShouldCollect reports whether a piece belongs to an open group at all.
// A message with no media group id is not an album piece: Python's
// ingest returns True (the caller treats it as a standalone message and
// never opens a group for it).
func ShouldCollect(mediaGroupID string) bool { return mediaGroupID != "" }

// monotonicSeconds is the clock the decision expects: monotonic seconds,
// matching time.monotonic() on the Python side. Exposed so tests can
// supply a fake clock; production uses MonotonicNow().
var monotonicSeconds = func() float64 {
	return float64(time.Now().UnixNano()) / float64(time.Second)
}

// MonotonicNow returns the current monotonic clock in seconds, the unit
// every Group.CreatedAt and Decision.Deadline is expressed in.
func MonotonicNow() float64 { return monotonicSeconds() }
