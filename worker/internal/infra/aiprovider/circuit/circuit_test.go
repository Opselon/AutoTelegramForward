package circuit

import (
	"testing"
	"time"
)

// circuit_test.go — the Closed → Open → Half-Open state machine, pinned
// against core/infrastructure/ai/circuit_breaker.py. A fake clock drives
// every transition so no test sleeps.

// fakeClock is the overridable now() the breakers consult.
type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time { return c.t }

func newFakeClock() *fakeClock { return &fakeClock{t: time.Unix(1000, 0)} }

func TestBreakerStartsClosedAndExecutes(t *testing.T) {
	b := NewBreaker("cfg-1")
	if b.State() != Closed {
		t.Errorf("state: got %s, want CLOSED", b.State())
	}
	if !b.CanExecute() {
		t.Error("a closed circuit must execute")
	}
}

func TestBreakerTripsAfterThreshold(t *testing.T) {
	b := NewBreaker("cfg-1")
	for i := 1; i < DefaultFailureThreshold; i++ {
		b.RecordFailure(true)
		if !b.CanExecute() {
			t.Errorf("failure %d: circuit must still execute (threshold %d not reached)", i, DefaultFailureThreshold)
		}
	}
	// The third consecutive failure trips it (threshold = 3).
	b.RecordFailure(true)
	if b.State() != Open {
		t.Errorf("state: got %s, want OPEN after %d consecutive failures", b.State(), DefaultFailureThreshold)
	}
	if b.CanExecute() {
		t.Error("an open circuit must not execute")
	}
}

func TestBreakerSuccessResetsConsecutiveFailures(t *testing.T) {
	b := NewBreaker("cfg-1")
	b.RecordFailure(true)
	b.RecordFailure(true)
	// One success zeroes the consecutive count, so another three
	// failures are needed to trip — not one.
	b.RecordSuccess()
	for i := 0; i < DefaultFailureThreshold-1; i++ {
		b.RecordFailure(true)
	}
	if b.State() != Closed {
		t.Errorf("state: got %s, want CLOSED: the success reset the consecutive count", b.State())
	}
	// The fourth failure (three after the reset) trips it.
	b.RecordFailure(true)
	if b.State() != Open {
		t.Errorf("state: got %s, want OPEN after three more consecutive failures", b.State())
	}
}

func TestBreakerRecoversToHalfOpenAfterTimeout(t *testing.T) {
	clock := newFakeClock()
	b := NewBreaker("cfg-1")
	b.now = clock.now

	for i := 0; i < DefaultFailureThreshold; i++ {
		b.RecordFailure(true)
	}
	if b.State() != Open {
		t.Fatalf("state: got %s, want OPEN", b.State())
	}

	// Before the recovery timeout: still open.
	clock.t = clock.t.Add(DefaultRecoveryTimeout - time.Second)
	if b.State() != Open {
		t.Errorf("state: got %s, want OPEN (recovery timeout not yet elapsed)", b.State())
	}
	// After: the read itself transitions it to HALF_OPEN
	// (circuit_breaker.py:44), which is what allows the single probe.
	clock.t = clock.t.Add(2 * time.Second)
	if b.State() != HalfOpen {
		t.Errorf("state: got %s, want HALF_OPEN", b.State())
	}
	if !b.CanExecute() {
		t.Error("a half-open circuit must allow the one probe")
	}
}

func TestBreakerHalfOpenProbeSuccessCloses(t *testing.T) {
	clock := newFakeClock()
	b := NewBreaker("cfg-1")
	b.now = clock.now
	for i := 0; i < DefaultFailureThreshold; i++ {
		b.RecordFailure(true)
	}
	clock.t = clock.t.Add(DefaultRecoveryTimeout + time.Second)
	if b.State() != HalfOpen {
		t.Fatalf("state: got %s, want HALF_OPEN", b.State())
	}
	// One successful probe closes the circuit again.
	b.RecordSuccess()
	if b.State() != Closed {
		t.Errorf("state: got %s, want CLOSED after the probe succeeded", b.State())
	}
}

func TestBreakerHalfOpenProbeFailureReopens(t *testing.T) {
	clock := newFakeClock()
	b := NewBreaker("cfg-1")
	b.now = clock.now
	for i := 0; i < DefaultFailureThreshold; i++ {
		b.RecordFailure(true)
	}
	clock.t = clock.t.Add(DefaultRecoveryTimeout + time.Second)
	if b.State() != HalfOpen {
		t.Fatalf("state: got %s, want HALF_OPEN", b.State())
	}
	// One failed probe trips it straight back OPEN — one bad probe is
	// enough to know the provider is not recovered.
	b.RecordFailure(true)
	if b.State() != Open {
		t.Errorf("state: got %s, want OPEN after the probe failed", b.State())
	}
	if b.trippedCount < 2 {
		t.Errorf("trippedCount: got %d, want at least 2", b.trippedCount)
	}
}

func TestBreakerPermanentFailureStillCounts(t *testing.T) {
	// A non-transient failure (401) is still booked
	// (circuit_breaker.py:83 books every failure): a key that went bad
	// trips the circuit instead of being retried forever.
	b := NewBreaker("cfg-1")
	for i := 0; i < DefaultFailureThreshold; i++ {
		b.RecordFailure(false)
	}
	if b.State() != Open {
		t.Errorf("state: got %s, want OPEN — permanent failures still trip", b.State())
	}
}

func TestBreakerResetAndTrip(t *testing.T) {
	b := NewBreaker("cfg-1")
	for i := 0; i < DefaultFailureThreshold; i++ {
		b.RecordFailure(true)
	}
	b.Reset()
	if b.State() != Closed {
		t.Errorf("Reset: state %s, want CLOSED", b.State())
	}
	b.Trip()
	if b.State() != Open {
		t.Errorf("Trip: state %s, want OPEN", b.State())
	}
	m := b.Metrics()
	if m.TrippedCount == 0 {
		t.Error("Metrics: TrippedCount should be non-zero after a trip")
	}
	if m.Name != "cfg-1" {
		t.Errorf("Metrics name: got %q", m.Name)
	}
}

func TestRegistrySelectHealthyProvider(t *testing.T) {
	r := NewRegistry()
	// A healthy primary is the primary, no failover.
	got, failover := r.SelectHealthyProvider("p", "s")
	if got != "p" || failover {
		t.Errorf("healthy primary: got %q failover=%v, want p false", got, failover)
	}
}

func TestRegistryFailoverWhenPrimaryOpen(t *testing.T) {
	r := NewRegistry()
	primary := r.GetCircuit("p")
	for i := 0; i < DefaultFailureThreshold; i++ {
		primary.RecordFailure(true)
	}
	got, failover := r.SelectHealthyProvider("p", "s")
	if got != "s" || !failover {
		t.Errorf("open primary: got %q failover=%v, want s true", got, failover)
	}
}

func TestRegistryBothOpenSelectsNothing(t *testing.T) {
	r := NewRegistry()
	for _, id := range []string{"p", "s"} {
		b := r.GetCircuit(id)
		for i := 0; i < DefaultFailureThreshold; i++ {
			b.RecordFailure(true)
		}
	}
	got, failover := r.SelectHealthyProvider("p", "s")
	if got != "" || failover {
		t.Errorf("both open: got %q failover=%v, want nothing", got, failover)
	}
}

func TestRegistryNoSecondaryWithOpenPrimary(t *testing.T) {
	r := NewRegistry()
	primary := r.GetCircuit("p")
	for i := 0; i < DefaultFailureThreshold; i++ {
		primary.RecordFailure(true)
	}
	got, failover := r.SelectHealthyProvider("p", "")
	if got != "" || failover {
		t.Errorf("no secondary: got %q failover=%v, want nothing", got, failover)
	}
}

func TestRegistryEmptyPrimarySelectsNothing(t *testing.T) {
	r := NewRegistry()
	if got, failover := r.SelectHealthyProvider("", "s"); got != "" || failover {
		t.Errorf("empty primary: got %q failover=%v", got, failover)
	}
	if r.GetCircuit("") != nil {
		t.Error("GetCircuit(\"\") should refuse to build a nameless circuit")
	}
}

func TestRegistryFailoverRecoversBackToPrimary(t *testing.T) {
	r := NewRegistry()
	primary := r.GetCircuit("p")
	for i := 0; i < DefaultFailureThreshold; i++ {
		primary.RecordFailure(true)
	}
	if _, ok := r.SelectHealthyProvider("p", "s"); !ok {
		t.Fatal("failover to the secondary was expected while the primary is open")
	}
	// Healing the primary makes it the answer again.
	primary.Reset()
	got, failover := r.SelectHealthyProvider("p", "s")
	if got != "p" || failover {
		t.Errorf("healed primary: got %q failover=%v, want p false", got, failover)
	}
}

func TestRegistryResetAllAndAllMetrics(t *testing.T) {
	r := NewRegistry()
	for _, id := range []string{"p", "s", "t"} {
		b := r.GetCircuit(id)
		for i := 0; i < DefaultFailureThreshold; i++ {
			b.RecordFailure(true)
		}
	}
	if len(r.AllMetrics()) != 3 {
		t.Errorf("AllMetrics: got %d circuits", len(r.AllMetrics()))
	}
	r.ResetAll()
	for id, m := range r.AllMetrics() {
		if m.State != Closed {
			t.Errorf("ResetAll: circuit %s is %s, want CLOSED", id, m.State)
		}
	}
}
