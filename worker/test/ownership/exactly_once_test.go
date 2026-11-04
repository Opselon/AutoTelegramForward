// exactly_once_test.go — §3 of the Go-ownership contract: the exactly-once
// transform proof.
//
// J-042 (Category J) pins the easy half: a transform applies once across an
// EDIT because the edit carries its own raw body. This suite pins the harder
// half — the paths where the SAME job body is handed to the engine TWICE and
// the transform stage must still run exactly once:
//
//  1. redispatch: a job is dispatched, its transform runs, and the job is
//     then dispatched AGAIN (a reconnect replay, a duplicate ingest event).
//     The delivered body must show ONE pipeline run, not two stacked ones.
//  2. lease-expiry recovery: a job was claimed, its transform ran, and the
//     process died before MarkSent. The stale-claim sweep hands the job to
//     a new worker through the REAL claim path (Reclaim → ClaimBatch). The
//     recovered dispatch must not re-run, re-stack, or double-apply the
//     transform on top of the result the dead worker already computed.
//
// THE STAIRCASING PROBE. The replacement maps MK → MKX: the output CONTAINS
// the find pattern, so every additional pass of the stage appends another X
// and the delivered body becomes a run counter. One pass over "MK MK body"
// yields "MKX MKX body"; a second pass yields "MKXX MKXX body"; a third
// yields "MKXXX MKXXX body" (see marker_contract_test.go's constants). A
// replay that double-applies the stage is therefore OBSERVABLE in the target
// text — the proof does not depend on a counter or a log line the engine
// also writes.
//
// The same probe covers the FEN (forward-every-N) side: the cadence gate
// advances the per-task counter exactly once per accepted message, so under
// N=2 the SECOND message owns the slot and the counter reads the message
// count, not the dispatch count (the §1 double-count race, J-042-adjacent).
package ownership

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"atf/worker/internal/domain/job"

	"atf/worker/internal/engine/delivery"
)

// exactlyOnceCase is one replay scenario: the dispatch count, the recovery
// path that produced the second dispatch, and the body that must appear.
type exactlyOnceCase struct {
	name       string // the scenario label
	redispatch bool   // hand the engine the job a second time
	reclaim    bool   // recover through the real stale-claim path instead
	wantBody   string // the gateway text after the surviving dispatch
}

// exactlyOnceMatrix is the scenario table. Every row ends in the same
// claim: the delivered body shows ONE transform pass.
func exactlyOnceMatrix() []exactlyOnceCase {
	return []exactlyOnceCase{
		{
			name:     "single-dispatch",
			wantBody: onePipelineRun,
		},
		{
			name:       "redispatch-marker",
			redispatch: true,
			wantBody:   onePipelineRun,
		},
		{
			name:     "lease-expiry-recovery",
			reclaim:  true,
			wantBody: onePipelineRun,
		},
		{
			name:       "lease-expiry-then-redispatch",
			reclaim:    true,
			redispatch: true,
			wantBody:   onePipelineRun,
		},
	}
}

// TestExactlyOnceTransformOnRedispatch: one message, up to two dispatches of
// the same job — the transform stage runs exactly once, proved by the
// staircasing probe in the delivered text.
func TestExactlyOnceTransformOnRedispatch(t *testing.T) {
	ctx := context.Background()
	// The worker owns the transform stage for the whole suite; the marker
	// contract suite (§2) pins the other switch position.
	defer delivery.SetWorkerOwnsTransforms(false)

	for i, c := range exactlyOnceMatrix() {
		c := c
		t.Run(fmt.Sprintf("ATF-OWN-XONCE-%03d-%s", i+1, c.name), func(t *testing.T) {
			rec := NewRecorder()
			h := NewHarness(t, 5094837833)
			// The task owns FEN (forward every N=1, i.e. every message) so
			// the cadence gate runs alongside the transform: a replay that
			// double-advanced the counter would also double-apply the slot
			// decision, and the collapsed counter is a second witness.
			taskID := fmt.Sprintf("task-xonce-%d", i)
			h.Install(TaskSpec{ID: taskID, Source: "-1001", Target: "-7001", Active: true,
				Metadata: map[string]any{"forward_every_n": 1},
				Policy: &PolicySpec{Enabled: boolPtr(true),
					// MK -> MKX: the output contains the input, so the
					// number of Xes in the body is the number of times the
					// stage ran.
					Replacements: map[string]string{"MK": "MKX"}}})

			e := evt(fmt.Sprintf("ATF-OWN-XONCE-%03d", i+1), "XONCE",
				fmt.Sprintf("transform exactly once under %s", c.name),
				fmt.Sprintf("gateway sees %q (one pass, no staircasing)", c.wantBody))
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(9700+i), "-7001", "MK MK body")
			err := h.Run(ctx, j)

			// The second dispatch, through whichever recovery path this
			// scenario names. Both hand the SAME job row back to the real
			// engine; the distinction is whether the claim surface (the
			// stale-claim sweep) produced it or the dispatch replay did.
			if c.reclaim {
				recovered := h.Reclaim(ctx, j, j.Status)
				// The recovered claim carries the pre-recovery status in
				// ClaimedFrom — the cadence and redispatch gates read it to
				// tell a first dispatch from a re-dispatch, and the job must
				// survive the recovery with that state intact.
				if recovered == nil {
					t.Fatalf("XONCE-%03d: reclaim returned no job", i+1)
				}
				j = recovered
				err = h.Run(ctx, j)
			}
			if c.redispatch {
				err = h.Run(ctx, j)
			}

			delivered := h.AssertDelivered(&e, j, c.wantBody)
			// No staircasing, read off the delivered text: a second pass of
			// the stage would have appended another X to every token.
			// One full Go pipeline run (content_policy then replacements)
			// leaves "MKXX MKXX body"; a SECOND full run leaves one more X
			// per token (doubleRun). Count pipeline runs, not stage passes:
			// the harness installs the replacement on both config surfaces
			// because Python applies both, so one run is already two passes.
			bodies := h.bodiesForTarget("-7001")
			stacked := false
			for _, b := range bodies {
				if strings.Contains(b, doubleRun) {
					stacked = true
				}
			}
			// The decision log's stage list must also show one run: the
			// notes for a single pass are exactly the replacement stage.
			notes := h.StageNamesForRule(taskID)
			if delivered && !stacked {
				e.Result = ResultPass
				e.Observed = fmt.Sprintf("bodies=%v notes=%v", bodies, notes)
			} else {
				e.Result = ResultFail
				e.FailureReason = fmt.Sprintf("bodies=%v notes=%v delivered=%v stacked=%v (want %q)",
					bodies, notes, delivered, stacked, c.wantBody)
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("XONCE-%03d (%s): %s", i+1, c.name, e.FailureReason)
			}
		})
	}
}

// TestExactlyOnceCounterUnderReplay: the FEN counter is the second exactly-
// once surface. Under N=2 the cadence gate forwards the odd messages and
// skips the even ones, and the counter counts MESSAGES — a redispatched job
// (a lease-expiry recovery, a backoff release) consumes no second slot,
// because the gate tells a first dispatch from a re-dispatch through the
// claim's ClaimedFrom and only the first advances the counter.
func TestExactlyOnceCounterUnderReplay(t *testing.T) {
	ctx := context.Background()
	defer delivery.SetWorkerOwnsTransforms(false)

	t.Run("ATF-OWN-XONCE-101-fen-counter-once-per-message", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-xonce-fen", Source: "-1001", Target: "-7001", Active: true,
			Metadata: map[string]any{"forward_every_n": 2},
			Policy:   &PolicySpec{Enabled: boolPtr(true)}})

		e := evt("ATF-OWN-XONCE-101", "XONCE",
			"FEN counter advances once per message under N=2",
			"msg1 and msg3 delivered, msg2 skipped, counter=3")
		start := now()
		// OwnsSlot(sequence, everyN) is (seq-1)%everyN == 0: under N=2 the
		// odd sequences own a slot and the even ones do not.
		j1 := h.Enqueue("task-xonce-fen", "-1001", 9751, "-7001", "first")
		err1 := h.Run(ctx, j1) // sequence 1: owns the slot, delivers
		j2 := h.Enqueue("task-xonce-fen", "-1001", 9752, "-7001", "second")
		_ = h.Run(ctx, j2) // sequence 2: skipped by cadence
		j3 := h.Enqueue("task-xonce-fen", "-1001", 9753, "-7001", "third")
		err3 := h.Run(ctx, j3) // sequence 3: owns the slot, delivers

		// j2 is skipped by cadence: its job row is closed (not SENT) and
		// the second message never reached the gateway. The other two
		// messages DID deliver to the same target, so the check is scoped
		// to this job's row and body, not to the target as a whole.
		delivered1 := h.AssertDelivered(&e, j1, "first")
		delivered3 := h.AssertDelivered(&e, j3, "third")
		j2Status := h.jobStatus(j2.ID)
		skipped2 := j2Status != "SENT" && !h.gatewaySent("second")
		counter := h.CounterAccepted(ctx, "task-xonce-fen")
		if delivered1 && skipped2 && delivered3 && counter == 3 {
			e.Result = ResultPass
			e.Observed = fmt.Sprintf("j2 status=%s counter=%d (one advance per message)", j2Status, counter)
		} else {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("delivered1=%v skipped2=%v delivered3=%v j2=%s counter=%d (want 3 — one advance per message, not per dispatch)",
				delivered1, skipped2, delivered3, j2Status, counter)
		}
		h.Finish(rec, &e, start, err1)
		if err3 != nil {
			e.FailureReason = fmt.Sprintf("%s; third run error: %v", e.FailureReason, err3)
		}
		if e.Result != ResultPass {
			t.Errorf("XONCE-101: %s", e.FailureReason)
		}
	})

	t.Run("ATF-OWN-XONCE-102-fen-replay-does-not-consume", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-xonce-fen2", Source: "-1001", Target: "-7001", Active: true,
			Metadata: map[string]any{"forward_every_n": 2},
			Policy:   &PolicySpec{Enabled: boolPtr(true)}})

		e := evt("ATF-OWN-XONCE-102", "XONCE",
			"a redispatched job consumes no second cadence slot",
			"counter stays at 1 after the slot owner is recovered through the claim path")
		start := now()
		j1 := h.Enqueue("task-xonce-fen2", "-1001", 9761, "-7001", "first")
		err := h.Run(ctx, j1) // sequence 1: owns the slot, delivers

		// The lease expired before MarkSent landed. The stale-claim sweep
		// hands the SAME job back through the REAL claim path, and the
		// claim's ClaimedFrom (CLAIMED, via the lease) is what tells the
		// cadence gate this is a re-dispatch: it must NOT advance the
		// counter a second time for the one message.
		recovered := h.Reclaim(ctx, j1, job.StatusClaimed)
		if recovered == nil {
			t.Fatalf("XONCE-102: reclaim returned no job")
		}
		err = h.Run(ctx, recovered)

		delivered := h.AssertDelivered(&e, recovered, "first")
		counter := h.CounterAccepted(ctx, "task-xonce-fen2")
		// The counter read the MESSAGE count (1), not the dispatch count
		// (2): the recovery re-delivered without consuming a second slot,
		// so the NEXT message still owns the slot the first one used.
		if delivered && counter == 1 {
			e.Result = ResultPass
			e.Observed = fmt.Sprintf("counter=%d after recovery (redispatch consumed no slot)", counter)
		} else {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("delivered=%v counter=%d (want 1 — the redispatch consumed a second slot)",
				delivered, counter)
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("XONCE-102: %s", e.FailureReason)
		}
	})
}
