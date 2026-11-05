package ops

import (
	"time"

	"atf/worker/internal/health"
)

// TrackerBridge adapts the Collector to health.Tracker.
//
// WHY THIS EXISTS. /healthz already has a contract monitors depend on, and
// health.Tracker is its shape. Replacing the tracker with the collector
// outright would either break that contract or drag health-package state
// into ops. Instead the bridge IS a health.Tracker whose every method
// forwards to the collector — so:
//
//   - the state machine (starting/ready/degraded/stopping) is written once,
//     by the run loop, through the same calls it already makes;
//   - the /healthz counters (delivered/retried/failed/unknown, active,
//     uptime) are read from the collector, i.e. from the counters the
//     delivery engine actually increments;
//   - there is no second counter set anywhere in the process.
//
// THE DEFECT THIS CLOSES: /healthz used to report a health.Tracker's own
// delivered=0 while the engine had delivered 41 — two owners, two numbers,
// one lie. With the bridge there is exactly one number, and the /ops
// surface's "delivered" and /healthz's "delivered" are the same field read
// from the same atomic.
type TrackerBridge struct {
	collector *Collector
}

// NewTrackerBridge wraps a collector as a health.Tracker. The bridge holds
// no state of its own, so the collector remains the only owner.
func NewTrackerBridge(c *Collector) *TrackerBridge { return &TrackerBridge{collector: c} }

// SetState forwards to the collector's state machine.
func (b *TrackerBridge) SetState(s health.State) {
	switch s {
	case health.StateReady:
		b.collector.SetState(StateReady)
	case health.StateDegraded:
		b.collector.SetState(StateDegraded)
	case health.StateStopping:
		b.collector.SetState(StateStopping)
	default:
		b.collector.SetState(StateStarting)
	}
}

// State forwards from the collector's state machine.
func (b *TrackerBridge) State() health.State {
	switch b.collector.State() {
	case StateReady:
		return health.StateReady
	case StateDegraded:
		return health.StateDegraded
	case StateStopping:
		return health.StateStopping
	}
	return health.StateStarting
}

// Active forwards the in-flight count (NOT goroutines).
func (b *TrackerBridge) Active() int64 { return b.collector.Active() }

// Enter marks one delivery as in-flight.
func (b *TrackerBridge) Enter() int64 { return b.collector.Enter() }

// Leave marks one delivery as complete.
func (b *TrackerBridge) Leave() int64 { return b.collector.Leave() }

// Outcome records a delivery outcome on the one counter set.
func (b *TrackerBridge) Outcome(kind string) { b.collector.Outcome(kind) }

// Snapshot returns the collector's operational counters in the /healthz
// shape (state/uptime/active/delivered/retried/failed/unknown). Monitors
// that read /healthz today keep reading the same keys — but the numbers
// are now the real ones.
func (b *TrackerBridge) Snapshot() map[string]any {
	c := b.collector
	return map[string]any{
		"state":          health.State(b.collector.State()).String(),
		"uptime_seconds": int(time.Since(c.StartedAt()).Seconds()),
		"active":         c.Active(),
		"delivered":      c.Delivered(),
		"retried":        c.Retried(),
		"failed":         c.Failed(),
		"rejected":       c.Rejected(),
		"unknown":        c.Unknown(),
	}
}

// Compile-time guarantee: the bridge satisfies the health surface's
// tracker contract, so /healthz never learns that its tracker changed.
var _ health.Tracker = (*TrackerBridge)(nil)
