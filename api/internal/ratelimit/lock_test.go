package ratelimit

import (
	"context"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Leases: worker coordination.
//
// The properties that matter, in order:
//
//   1. At most one holder at a time.
//   2. A holder that loses its lease (TTL expiry, failover) must be able to
//      FIND OUT — a lease nobody notices losing is a correctness bug waiting
//      for a slow GC pause.
//   3. An expired lease must be acquirable again, or a crashed worker stops the
//      job forever.
//   4. A process that lost its lease must not be able to release the new
//      holder's (the token check).
//
// And the property that is deliberately NOT claimed: a lease does not make
// delivery idempotent. That is the database's job (delivery_jobs' unique
// constraint and claim/lease_until columns), and the tests in
// core/tests/test_p1_durable_pipeline.py are the ones that pin it.
// ---------------------------------------------------------------------------

func TestLeaseExcludesASecondHolder(t *testing.T) {
	everyBackend(t, func(t *testing.T, h *harness) {
		ctx := context.Background()
		first, ok, err := h.lim.AcquireLease(ctx, "watchdog", LeaseOptions{TTL: time.Minute})
		if err != nil || !ok {
			t.Fatalf("first acquisition failed: ok=%v err=%v", ok, err)
		}
		defer func() { _ = first.Release(ctx) }()

		second, ok, err := h.lim.AcquireLease(ctx, "watchdog", LeaseOptions{TTL: time.Minute})
		if err != nil {
			t.Fatalf("second acquisition errored: %v", err)
		}
		if ok {
			t.Fatalf("two replicas hold the same lease (second key %s)", second.Key())
		}
	})
}

func TestLeaseIsReleasedByItsOwnerAndReacquirable(t *testing.T) {
	everyBackend(t, func(t *testing.T, h *harness) {
		ctx := context.Background()
		lease, ok, err := h.lim.AcquireLease(ctx, "cleanup", LeaseOptions{TTL: time.Minute})
		if err != nil || !ok {
			t.Fatalf("acquire: ok=%v err=%v", ok, err)
		}
		if err := lease.Release(ctx); err != nil {
			t.Fatalf("release: %v", err)
		}
		if err := lease.Release(ctx); err != nil {
			t.Fatalf("release must be idempotent: %v", err)
		}
		_, ok, err = h.lim.AcquireLease(ctx, "cleanup", LeaseOptions{TTL: time.Minute})
		if err != nil || !ok {
			t.Fatalf("a released lease must be acquirable: ok=%v err=%v", ok, err)
		}
	})
}

// A crashed holder must not wedge the job forever: the lease expires and the
// next replica takes over.
func TestExpiredLeaseIsAcquirableByAnotherReplica(t *testing.T) {
	real := os.Getenv("ATF_TEST_REDIS_ADDR")
	if real == "" {
		t.Skip("lease expiry needs a real Redis clock (miniredis would need its own time control) — set ATF_TEST_REDIS_ADDR")
	}
	h := newRealHarness(t, AlgoBucket)
	ctx := context.Background()

	// A very short lease, held by a replica that then "crashes" (never
	// renews, never releases).
	crash, ok, err := h.lim.AcquireLease(ctx, "epoch", LeaseOptions{TTL: 300 * time.Millisecond})
	if err != nil || !ok {
		t.Fatalf("acquire: ok=%v err=%v", ok, err)
	}
	_ = crash // deliberately leaked, as a crashed process would leave it

	deadline := time.Now().Add(3 * time.Second)
	for {
		takeover, ok, err := h.lim.AcquireLease(ctx, "epoch", LeaseOptions{TTL: time.Minute})
		if err != nil {
			t.Fatalf("takeover errored: %v", err)
		}
		if ok {
			defer func() { _ = takeover.Release(ctx) }()
			t.Logf("the next replica took over after the lease expired (waited %.0fms)",
				time.Since(deadline.Add(-3*time.Second)).Seconds()*1000)
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the lease never expired: a crashed worker would block the job forever")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// Renewal is token-checked: the loser of a takeover must not be able to renew
// (or release) a lease it no longer holds.
func TestOnlyTheHolderCanRenewOrRelease(t *testing.T) {
	everyBackend(t, func(t *testing.T, h *harness) {
		ctx := context.Background()
		holder, ok, err := h.lim.AcquireLease(ctx, "token-check", LeaseOptions{TTL: time.Minute})
		if err != nil || !ok {
			t.Fatalf("acquire: ok=%v err=%v", ok, err)
		}
		// An impostor: same key, a different token — exactly what a process
		// that stalled past its TTL and came back would look like.
		impostor := &Lease{
			rdb:     h.lim.rdb,
			renew:   h.lim.scripts[luaScripts.LeaseRenew.Name],
			release: h.lim.scripts[luaScripts.Lease.Name],
			key:     holder.Key(),
			token:   "not-the-owner",
			ttl:     time.Minute,
			stop:    make(chan struct{}),
			done:    make(chan struct{}),
		}
		close(impostor.done)
		if err := impostor.Renew(ctx); err != ErrLeaseNotHeld {
			t.Fatalf("an impostor renewed the lease: %v", err)
		}
		if err := impostor.Release(ctx); err != nil {
			t.Fatalf("impostor release errored (it must be a no-op, not an error): %v", err)
		}
		// The real holder still owns it.
		if err := holder.Renew(ctx); err != nil {
			t.Fatalf("the owner lost its lease to a no-op: %v", err)
		}
		if err := holder.Release(ctx); err != nil {
			t.Fatalf("owner release: %v", err)
		}
	})
}

// Auto-renew keeps a lease across work that outlives the TTL, and Lost()
// reports a lease that could not be renewed.
func TestAutoRenewKeepsALeaseAlive(t *testing.T) {
	real := os.Getenv("ATF_TEST_REDIS_ADDR")
	if real == "" {
		t.Skip("auto-renew timing needs a real Redis clock — set ATF_TEST_REDIS_ADDR")
	}
	h := newRealHarness(t, AlgoBucket)
	ctx := context.Background()
	lease, ok, err := h.lim.AcquireLease(ctx, "long-job", LeaseOptions{TTL: 400 * time.Millisecond, AutoRenew: true})
	if err != nil || !ok {
		t.Fatalf("acquire: ok=%v err=%v", ok, err)
	}
	defer func() { _ = lease.Release(ctx) }()

	// Sleep past two TTLs: without auto-renew the lease would be gone.
	time.Sleep(1100 * time.Millisecond)
	_, taken, err := h.lim.AcquireLease(ctx, "long-job", LeaseOptions{TTL: time.Minute})
	if err != nil {
		t.Fatalf("probe acquisition errored: %v", err)
	}
	if taken {
		t.Fatal("the lease was not renewed, so a second replica took it while the first was still working")
	}
	if lease.Lost() {
		t.Fatal("auto-renew reported the lease lost while it was being renewed")
	}
}

// RunWithLease is the shape callers actually use. What it guarantees is
// MUTUAL EXCLUSION, not "runs exactly once ever": a body that finishes releases
// the lease, and the next replica may then legitimately run it (that is how a
// periodic loop works). The claim being tested is therefore "never two at the
// same time", plus "released even when the body fails".
func TestRunWithLeaseSerialisesReplicasAndReleasesOnError(t *testing.T) {
	everyBackend(t, func(t *testing.T, h *harness) {
		ctx := context.Background()
		var inFlight, maxInFlight, ran atomic.Int64
		var wg sync.WaitGroup
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				_, _ = h.lim.RunWithLease(ctx, "singleton", LeaseOptions{TTL: time.Minute}, func(context.Context) error {
					now := inFlight.Add(1)
					for {
						peak := maxInFlight.Load()
						if now <= peak || maxInFlight.CompareAndSwap(peak, now) {
							break
						}
					}
					ran.Add(1)
					time.Sleep(5 * time.Millisecond) // hold the critical section briefly
					inFlight.Add(-1)
					return context.DeadlineExceeded // a failing body must still release
				})
			}()
		}
		wg.Wait()
		if got := maxInFlight.Load(); got != 1 {
			t.Fatalf("%d replicas ran the singleton body at the same time, want 1", got)
		}
		if ran.Load() == 0 {
			t.Fatal("nobody ran the singleton body")
		}
		t.Logf("%d sequential runs, never more than one at a time", ran.Load())
		// Released despite the error, so the next attempt can run.
		again, ok, err := h.lim.AcquireLease(ctx, "singleton", LeaseOptions{TTL: time.Minute})
		if err != nil || !ok {
			t.Fatalf("the lease was not released after a failed body: ok=%v err=%v", ok, err)
		}
		_ = again.Release(ctx)
	})
}

// Leases of different names are independent, and the deployment prefix keeps
// two deployments on one Redis apart.
func TestLeasesAreNamespaced(t *testing.T) {
	h := newHarness(t, AlgoBucket)
	ctx := context.Background()
	a, okA, _ := h.lim.AcquireLease(ctx, "a", LeaseOptions{TTL: time.Minute})
	b, okB, _ := h.lim.AcquireLease(ctx, "b", LeaseOptions{TTL: time.Minute})
	if !okA || !okB {
		t.Fatal("leases with different names must not contend")
	}
	defer func() { _ = a.Release(ctx); _ = b.Release(ctx) }()
	if a.Key() == b.Key() {
		t.Fatalf("two names produced one key: %s", a.Key())
	}
	if want := h.lim.opts.Prefix + "lock:a"; a.Key() != want {
		t.Fatalf("lease key %q, want %q", a.Key(), want)
	}
}
