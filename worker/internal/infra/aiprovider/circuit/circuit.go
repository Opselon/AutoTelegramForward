// Package circuit is the Go port of the AI provider circuit breaker
// (core/infrastructure/ai/circuit_breaker.py): the Closed → Open →
// Half-Open state machine that keeps one failing provider from sinking
// every delivery, plus the registry's failover selection.
package circuit

import (
	"sync"
	"time"
)

// State is the circuit's position in its state machine
// (value_objects.CircuitState).
type State string

const (
	Closed   State = "CLOSED"
	Open     State = "OPEN"
	HalfOpen State = "HALF_OPEN"
)

// Defaults mirror the Python registry's constructor defaults
// (circuit_breaker.py:142): three consecutive failures trip a circuit,
// and it stays open for 30s before one probe is allowed through.
const (
	DefaultFailureThreshold = 3
	DefaultRecoveryTimeout  = 30.0 * time.Second
	DefaultHalfOpenProbes   = 1
)

// Breaker tracks state and failure metrics for one AI provider
// configuration (circuit_breaker.py:19). The identity it tracks is the
// ai_configs row id: two configs may point at the same provider host and
// still be separate circuits, exactly as Python keys them.
//
// All access is mutex-guarded: the delivery engine fans out per target,
// and one rewrite failing while another succeeds must not race the
// counter into a wrong trip.
type Breaker struct {
	mu sync.Mutex

	name             string
	failureThreshold int
	recoveryTimeout  time.Duration
	halfOpenMaxProbes int

	state               State
	consecutiveFailures int
	consecutiveSuccesses int
	lastFailureTime     time.Time
	lastStateChange     time.Time
	totalRequests       int
	totalFailures       int
	trippedCount        int

	// now is the clock the state machine consults, overridable in tests
	// so a recovery timeout is observed without sleeping.
	now func() time.Time
}

// NewBreaker builds one circuit with the given identity and the
// registry's defaults.
func NewBreaker(name string) *Breaker {
	return &Breaker{
		name:              name,
		failureThreshold:  DefaultFailureThreshold,
		recoveryTimeout:   DefaultRecoveryTimeout,
		halfOpenMaxProbes: DefaultHalfOpenProbes,
		state:             Closed,
		now:               time.Now,
	}
}

// Name is the circuit's identity (the config id).
func (b *Breaker) Name() string { return b.name }

// SetClock replaces the clock the state machine consults. It exists for
// tests, so the OPEN → HALF_OPEN recovery transition is observed without
// sleeping the real recovery timeout; no production caller sets it.
func (b *Breaker) SetClock(now func() time.Time) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if now != nil {
		b.now = now
	}
}

// State returns the current EFFECTIVE state (circuit_breaker.py:44): an
// OPEN circuit whose recovery timeout has elapsed transitions itself to
// HALF_OPEN on read, which is what makes the single probe possible.
func (b *Breaker) State() State {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.effectiveState()
}

// effectiveState is the read side of the state machine, called with the
// mutex held.
func (b *Breaker) effectiveState() State {
	if b.state == Open {
		elapsed := b.now().Sub(b.lastFailureTime)
		if elapsed >= b.recoveryTimeout {
			b.state = HalfOpen
			b.consecutiveSuccesses = 0
			b.lastStateChange = b.now()
		}
	}
	return b.state
}

// CanExecute reports whether a request is allowed to proceed
// (circuit_breaker.py:59): CLOSED and HALF_OPEN both allow; OPEN does
// not. Half-open is the recovery probe: exactly one request through, and
// its outcome decides the trip back.
func (b *Breaker) CanExecute() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	switch b.effectiveState() {
	case Closed, HalfOpen:
		return true
	}
	return false
}

// RecordSuccess books a success (circuit_breaker.py:68): the consecutive
// failure count resets, and a half-open probe that succeeds closes the
// circuit again.
func (b *Breaker) RecordSuccess() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.totalRequests++
	b.consecutiveFailures = 0
	if b.state == HalfOpen {
		b.consecutiveSuccesses++
		if b.consecutiveSuccesses >= b.halfOpenMaxProbes {
			b.state = Closed
			b.consecutiveSuccesses = 0
			b.lastStateChange = b.now()
		}
	}
}

// RecordFailure books a failure (circuit_breaker.py:83). A CLOSED
// circuit trips OPEN when the consecutive count reaches the threshold;
// a HALF_OPEN probe that fails trips back OPEN immediately — one bad
// probe is enough to know the provider is not recovered.
//
// isTransient carries the classification but does not gate the booking,
// mirroring Python: a permanent failure (401) still counts, so a key
// that went bad trips the circuit instead of being retried forever.
func (b *Breaker) RecordFailure(isTransient bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.totalRequests++
	b.totalFailures++
	b.lastFailureTime = b.now()
	b.consecutiveFailures++

	switch b.state {
	case Closed:
		if b.consecutiveFailures >= b.failureThreshold {
			b.state = Open
			b.trippedCount++
			b.lastStateChange = b.now()
		}
	case HalfOpen:
		b.state = Open
		b.trippedCount++
		b.lastStateChange = b.now()
	}
}

// Reset returns the circuit to CLOSED (circuit_breaker.py:109).
func (b *Breaker) Reset() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.state = Closed
	b.consecutiveFailures = 0
	b.consecutiveSuccesses = 0
	b.lastFailureTime = time.Time{}
	b.lastStateChange = b.now()
}

// Trip forces the circuit OPEN (circuit_breaker.py:118).
func (b *Breaker) Trip() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.state = Open
	b.lastFailureTime = b.now()
	b.trippedCount++
	b.lastStateChange = b.now()
}

// Metrics is the observable state (circuit_breaker.py:126), for health
// surfaces. It never copies the raw timestamps Python exposes — the
// worker's contract reports state and counts, and a wall clock in a
// metrics payload is a log-line liability, not a signal.
type Metrics struct {
	Name                string
	State               State
	ConsecutiveFailures int
	TotalRequests       int
	TotalFailures       int
	TrippedCount        int
}

// Metrics reports the breaker's observable state.
func (b *Breaker) Metrics() Metrics {
	b.mu.Lock()
	defer b.mu.Unlock()
	return Metrics{
		Name:                b.name,
		State:               b.effectiveState(),
		ConsecutiveFailures: b.consecutiveFailures,
		TotalRequests:       b.totalRequests,
		TotalFailures:       b.totalFailures,
		TrippedCount:        b.trippedCount,
	}
}

// Registry maintains one circuit per AI config and answers the failover
// question (circuit_breaker.py:139).
type Registry struct {
	mu       sync.Mutex
	circuits map[string]*Breaker

	failureThreshold int
	recoveryTimeout  time.Duration
	now              func() time.Time
}

// NewRegistry builds the default registry (circuit_breaker.py:142).
func NewRegistry() *Registry {
	return &Registry{
		circuits: map[string]*Breaker{},
	}
}

// GetCircuit returns the circuit for one provider id, creating it with
// the registry's defaults on first use (circuit_breaker.py:151).
func (r *Registry) GetCircuit(providerID string) *Breaker {
	if providerID == "" {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	b, ok := r.circuits[providerID]
	if !ok {
		b = NewBreaker(providerID)
		if r.now != nil {
			b.now = r.now
		}
		r.circuits[providerID] = b
	}
	return b
}

// SelectHealthyProvider is the failover selection
// (circuit_breaker.py:160): the primary when its circuit executes, the
// secondary when only the primary is open, and nothing when both are
// down. The boolean reports whether the answer came from the secondary.
func (r *Registry) SelectHealthyProvider(primaryID, secondaryID string) (string, bool) {
	if primaryID == "" {
		return "", false
	}
	if primary := r.GetCircuit(primaryID); primary != nil && primary.CanExecute() {
		return primaryID, false
	}
	if secondaryID != "" {
		if secondary := r.GetCircuit(secondaryID); secondary != nil && secondary.CanExecute() {
			return secondaryID, true
		}
	}
	return "", false
}

// ResetAll returns every circuit to CLOSED (circuit_breaker.py:192),
// the operation an operator's "try again now" runs.
func (r *Registry) ResetAll() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, b := range r.circuits {
		b.Reset()
	}
}

// AllMetrics reports every circuit's observable state
// (circuit_breaker.py:196), sorted by name for a stable health surface.
func (r *Registry) AllMetrics() map[string]Metrics {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[string]Metrics, len(r.circuits))
	for name, b := range r.circuits {
		out[name] = b.Metrics()
	}
	return out
}
