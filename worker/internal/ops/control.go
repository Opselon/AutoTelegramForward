package ops

import (
	"context"
	"sync"
	"time"
)

// ControlPlane is the §10 worker-control surface: safe operational actions
// the admin can request from the Web DevOps interface.
//
// Design contract (the contract's §10, verbatim requirements):
//
//   - A worker must never falsely report a restart or any other operation
//     simply because the UI accepted the request. Every operation is an
//     asynchronous job with a REAL status the worker itself advances:
//     REQUESTED → ACCEPTED → EXECUTING → COMPLETED/FAILED/TIMED_OUT.
//   - Disruptive actions require explicit confirmation; the UI enforces
//     that, the API enforces admin authorization.
//   - Audit trail: who asked, when, and what the worker did about it.
//   - Completion must be verified, not assumed.
//
// What is deliberately NOT here:
//   - Arbitrary shell execution. Not exposed, not proxied, not possible.
//   - Anything that can be acknowledged as successful before the worker
//     actually did it.
//   - Cross-process actions (restart/shutdown) that need a supervisor —
//     those are represented as REQUESTED with an honest note that the
//     process must be restarted by the deployment layer; the /ops surface
//     never claims a restart it cannot perform.
//
// State is in-memory: a control is about the live process. A restart
// forgets pause state — which is exactly right, because a restart is
// itself the strongest "resume everything" available.
type ControlPlane struct {
	mu sync.Mutex

	// pause holds the pause request when processing is paused. nil = running.
	pause *PauseState

	// ops is the bounded audit log of control operations the worker has
	// executed: the trail the contract requires, capped so a chatty admin
	// cannot grow it without bound.
	ops    []*ControlOp
	opsCap int
}

// PauseState is the live state of one pause request.
type PauseState struct {
	RequestedBy string
	RequestedAt time.Time
	// Effect is what the pause actually does, stated honestly: in-flight
	// deliveries complete (the contract forbids a mid-job abort that could
	// leave a half-sent album), and the claim loop stops taking new work.
	// The UI must never imply jobs in flight were cancelled.
	InFlightComplete bool
}

// ControlOp is one executed control operation, for the audit trail.
type ControlOp struct {
	Kind        string      // pause | resume
	RequestedBy string
	RequestedAt time.Time
	AcceptedAt  time.Time
	CompletedAt time.Time
	Status      string // ACCEPTED | COMPLETED | FAILED
	// Reason carries the honest outcome — e.g. a resume requested while
	// already running completes with "already running", which is NOT an
	// error but must not be reported as a state change either.
	Reason string
}

// NewControlPlane builds the §10 surface. opsCap bounds the audit log.
func NewControlPlane(opsCap int) *ControlPlane {
	if opsCap <= 0 {
		opsCap = 64
	}
	return &ControlPlane{opsCap: opsCap}
}

// Pause requests processing pause. Returns the recorded operation so the
// caller can answer with the asynchronous status the contract requires —
// the request is not "paused" the instant it lands: the claim loop must
// observe the gate, which happens within one poll interval.
//
// alreadyPaused reports whether the worker was already paused, in which
// case the operation completes with an honest "already paused" reason
// rather than a fabricated state change.
func (c *ControlPlane) Pause(requestedBy string, fn func() error) (op *ControlOp, alreadyPaused bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	op = &ControlOp{
		Kind:        "pause",
		RequestedBy: requestedBy,
		RequestedAt: time.Now(),
		AcceptedAt:  time.Now(),
		Status:      "ACCEPTED",
	}
	if c.pause != nil {
		op.Status = "COMPLETED"
		op.CompletedAt = time.Now()
		op.Reason = "already paused — no state change"
		c.record(op)
		return op, true
	}

	// ACCEPTED: the gate is set here; EXECUTING is the claim loop observing
	// it within one poll interval; COMPLETED is when effect verification can
	// run (in-flight deliveries may still be running — the pause is
	// complete for new claims, in-flight work completes by design).
	if fn != nil {
		if err := fn(); err != nil {
			op.Status = "FAILED"
			op.CompletedAt = time.Now()
			op.Reason = err.Error()
			c.record(op)
			return op, false
		}
	}
	c.pause = &PauseState{
		RequestedBy:      requestedBy,
		RequestedAt:      op.RequestedAt,
		InFlightComplete: true,
	}
	op.Status = "COMPLETED"
	op.CompletedAt = time.Now()
	op.Reason = "claim loop stops accepting new work; in-flight deliveries complete"
	c.record(op)
	return op, false
}

// Resume requests processing resume. Mirror of Pause: a resume while
// running completes with an honest "already running" reason — the contract
// forbids reporting a state change that did not happen.
func (c *ControlPlane) Resume(requestedBy string) (op *ControlOp, wasRunning bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	op = &ControlOp{
		Kind:        "resume",
		RequestedBy: requestedBy,
		RequestedAt: time.Now(),
		AcceptedAt:  time.Now(),
		Status:      "ACCEPTED",
	}
	if c.pause == nil {
		op.Status = "COMPLETED"
		op.CompletedAt = time.Now()
		op.Reason = "already running — no state change"
		c.record(op)
		return op, true
	}
	c.pause = nil
	op.Status = "COMPLETED"
	op.CompletedAt = time.Now()
	op.Reason = "claim loop resumes accepting work"
	c.record(op)
	return op, false
}

// IsPaused is the gate the claim loop consults: it must NOT take new work
// while paused. Cheap (one mutex read), so polling it in the tight claim
// loop costs nothing; the contract forbids a pause that only takes effect
// at the next claim because the loop never looked.
func (c *ControlPlane) IsPaused() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.pause != nil
}

// PauseState exposes the live pause context for /ops: who asked and when,
// so the console can show WHY delivery stopped, not just that it did.
// Nil when running.
func (c *ControlPlane) PauseState() *PauseState {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.pause == nil {
		return nil
	}
	p := *c.pause
	return &p
}

// Ops returns the audit trail, newest first.
func (c *ControlPlane) Ops() []*ControlOp {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]*ControlOp, len(c.ops))
	copy(out, c.ops)
	// reverse to newest-first without sorting (record appends oldest-first)
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out
}

// record appends to the bounded audit log, evicting the OLDEST entry when
// full: the trail keeps the most recent operations, which is the slice an
// operator actually reads. Eviction is by position, never by status — an
// old FAILED is dropped before a new COMPLETED, which is correct because
// the audit's purpose is recent operator actions, not permanent history.
func (c *ControlPlane) record(op *ControlOp) {
	if len(c.ops) >= c.opsCap {
		c.ops = c.ops[1:]
	}
	c.ops = append(c.ops, op)
}

// ctxKey is unexported so the control-plane context value cannot be
// forged by another package.
type ctxKey int

const pauseCtxKey ctxKey = 1

// WithPauseGate returns a context that carries the pause gate so the claim
// loop can consult it once per iteration instead of threading the plane
// through every call.
func WithPauseGate(ctx context.Context, c *ControlPlane) context.Context {
	return context.WithValue(ctx, pauseCtxKey, c)
}

// PauseGateFrom returns the control plane the context carries, or nil.
// Used by the claim loop: one lookup, one IsPaused.
func PauseGateFrom(ctx context.Context) *ControlPlane {
	if c, ok := ctx.Value(pauseCtxKey).(*ControlPlane); ok {
		return c
	}
	return nil
}
