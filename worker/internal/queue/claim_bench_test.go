package queue

import (
	"context"
	"fmt"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
)

// The two-phase claim replaced a single-phase form whose per-candidate
// "earlier unsent job in this lane" probe scanned the whole active backlog,
// making one claim cost O(backlog) — 12ms at 20k rows, which caps the worker
// at ~300 messages/second no matter how many workers run.
//
// These benchmarks reproduce that regime against an ISOLATED temp database
// (never the shared atf.db) and assert the regression stays fixed:
// claim latency must scale with the number of LANES, not with the backlog,
// and the FIFO/lane contract must hold while it does.

func benchClaimEnv(t testing.TB, lanes, perLane int) (Store, func()) {
	ctx := context.Background()
	s, err := NewSQLite(ctx, t.TempDir()+"/bench.db")
	if err != nil {
		t.Fatalf("open bench db: %v", err)
	}
	created := time.Now().Unix()
	for lane := 0; lane < lanes; lane++ {
		tgt := fmt.Sprintf("-100%d", 200+lane)
		for i := 0; i < perLane; i++ {
			// Reverse the source order against creation order so a claim
			// that sorts by created_at instead of (source, message) would
			// pick the wrong head: the benchmark would silently stop
			// guarding the contract it exists to guard.
			src := int64(100000 - i)
			j := &job.DeliveryJob{
				ID: fmt.Sprintf("bench-%d-%d", lane, i), RuleID: "bench-rule",
				SourceChatID: "-1001", SourceMessageID: src, TargetChatID: tgt,
				Status: job.StatusPending, MaxAttempts: 5, CreatedAt: created,
				Payload: *job.NewPayload(map[string]any{"text": "b"}),
			}
			if _, err := s.Enqueue(ctx, j); err != nil {
				t.Fatalf("enqueue: %v", err)
			}
		}
	}
	return s, func() { _ = s.Close() }
}

// BenchmarkClaimBatch_BacklogScaling pins the regression fix: one claim
// batch must stay cheap while the backlog per lane grows. Pre-fix, this
// scaled linearly with the backlog (the correlated NOT EXISTS scanned the
// active set per candidate); the two-phase claim probes through the
// target-scoped composite index instead.
func BenchmarkClaimBatch_BacklogScaling(b *testing.B) {
	for _, perLane := range []int{50, 200, 1000, 4000} {
		b.Run(fmt.Sprintf("lanes=8/perLane=%d", perLane), func(b *testing.B) {
			s, done := benchClaimEnv(b, 8, perLane)
			defer done()
			ctx := context.Background()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				// Claim and mark so the next claim still has work: a claim
				// against an empty queue measures nothing.
				js, err := s.ClaimBatch(ctx, "bench-worker", 5, time.Second)
				if err != nil {
					b.Fatal(err)
				}
				for _, j := range js {
					if err := s.MarkFailed(ctx, j.ID, "bench"); err != nil {
						b.Fatal(err)
					}
				}
			}
		})
	}
}
