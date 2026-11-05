package queue

// The reconciliation surface's store contract. Production stranded a day of
// UNKNOWN deliveries because the decision step never existed; these tests
// pin the listing and the atomic fence+job resolution it must perform.

import (
	"context"
	"strings"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
)


// seedUnknownFence creates a fence row in the state the fence sweep leaves
// after an ambiguous outcome: UNKNOWN with the recorded failure evidence.
func seedUnknownFence(t *testing.T, s Store, key job.AttemptKey, lastError string) {
	t.Helper()
	ctx := context.Background()
	if _, _, ferr := s.FenceClaim(ctx, key, "w-test", float64(time.Now().Add(time.Minute).Unix())); ferr != nil {
		t.Fatalf("fence claim: %v", ferr)
	}
	if ferr := s.FenceRecordOutcome(ctx, key, "w-test", "UNKNOWN", lastError); ferr != nil {
		t.Fatalf("fence outcome: %v", ferr)
	}
}

// ageFence backdates a fence row so it is older than any grace window.
func ageFence(t *testing.T, s Store, key job.AttemptKey, age time.Duration) {
	t.Helper()
	db := StoreDB(t, s)
	if _, err := db.Exec(`UPDATE delivery_attempts SET updated_at=? WHERE id=?`,
		time.Now().Add(-age).Unix(), key.String()); err != nil {
		t.Fatalf("age fence: %v", err)
	}
}

func TestUnknownFencesListsOnlyAgedUnknownRows(t *testing.T) {
	s := NewTestStore(t)
	ctx := context.Background()

	old := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 1, TargetChatID: "t"}
	fresh := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 2, TargetChatID: "t"}
	failed := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 3, TargetChatID: "t"}

	seedUnknownFence(t, s, old, "rpc error: code = Unimplemented desc = boom")
	seedUnknownFence(t, s, fresh, "rpc error: code = Unimplemented desc = boom")
	seedUnknownFence(t, s, failed, "rpc error: code = Unimplemented desc = boom")
	ageFence(t, s, old, time.Hour)
	if ferr := s.FenceRecordOutcome(ctx, failed, "w-test", "FAILED", "permanent"); ferr != nil {
		t.Fatalf("fail fence: %v", ferr)
	}
	ageFence(t, s, failed, time.Hour)

	got, err := s.UnknownFences(ctx, time.Now().Add(-30*time.Minute), 100)
	if err != nil {
		t.Fatalf("unknown fences: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("UnknownFences returned %d rows, want exactly the aged UNKNOWN one: %+v", len(got), got)
	}
	if got[0].Key.String() != old.String() {
		t.Errorf("listed %s, want %s", got[0].Key.String(), old.String())
	}
	if got[0].LastError == "" {
		t.Errorf("listed fence carries no recorded failure evidence — the reconciler would have nothing to classify")
	}
}

func TestUnknownFencesRespectsTheLimit(t *testing.T) {
	s := NewTestStore(t)
	ctx := context.Background()
	for i := int64(0); i < 3; i++ {
		key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: i, TargetChatID: "t"}
		seedUnknownFence(t, s, key, "ambiguous")
		ageFence(t, s, key, time.Hour)
	}
	got, err := s.UnknownFences(ctx, time.Now().Add(-30*time.Minute), 2)
	if err != nil {
		t.Fatalf("unknown fences: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("limit not honored: got %d rows, want 2", len(got))
	}
	// Oldest first, always.
	if got[0].Key.SourceMessageID > got[1].Key.SourceMessageID {
		t.Errorf("ordering broken: %+v", got)
	}
}

func TestReconcileUnknownClosesFenceAndJobAtomically(t *testing.T) {
	s := NewTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 5, TargetChatID: "t"}
	seedUnknownFence(t, s, key, "rpc error: code = Unimplemented desc = method not implemented")
	ageFence(t, s, key, time.Hour)
	InsertJob(t, s, &job.DeliveryJob{
		ID: key.String(), RuleID: key.RuleID, SourceChatID: key.SourceChatID,
		SourceMessageID: key.SourceMessageID, TargetChatID: key.TargetChatID,
		Status: job.StatusUnknown, MaxAttempts: 5,
	})

	ok, err := s.ReconcileUnknown(ctx, key, "w-reconciler", "reconciled: provably unexecuted")
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if !ok {
		t.Fatal("reconcile reported no change — the row was fully UNKNOWN")
	}

	db := StoreDB(t, s)
	var fStatus, fErr string
	if err := db.QueryRow(`SELECT status, last_error FROM delivery_attempts WHERE id=?`, key.String()).
		Scan(&fStatus, &fErr); err != nil {
		t.Fatalf("fence read: %v", err)
	}
	if fStatus != "FAILED" {
		t.Errorf("fence status = %s, want FAILED", fStatus)
	}
	if fErr == "" || !strings.Contains(fErr, "provably unexecuted") {
		t.Errorf("fence last_error lost the reconciliation reason: %q", fErr)
	}
	var jStatus, jErr string
	if err := db.QueryRow(`SELECT status, error_detail FROM delivery_jobs WHERE id=?`, key.String()).
		Scan(&jStatus, &jErr); err != nil {
		t.Fatalf("job read: %v", err)
	}
	if jStatus != "FAILED" {
		t.Errorf("job status = %s, want FAILED — an unresolved job state beside a resolved fence is the inconsistency this exists to prevent", jStatus)
	}
	if jErr == "" || !strings.Contains(jErr, "provably unexecuted") {
		t.Errorf("job error_detail lost the reconciliation reason: %q", jErr)
	}

	// The resolved job is re-dispatchable through the normal path: the fence
	// re-claims a FAILED row (FenceDispatch), never blocks a retry.
	outcome, _, ferr := s.FenceClaim(ctx, key, "w-retry", float64(time.Now().Add(time.Minute).Unix()))
	if ferr != nil {
		t.Fatalf("re-claim after reconcile: %v", ferr)
	}
	if outcome != job.FenceDispatch {
		t.Errorf("post-reconcile fence claim = %s, want dispatch (a reconciled delivery must be recoverable)", outcome)
	}
}

func TestReconcileUnknownLeavesMovedFenceAlone(t *testing.T) {
	s := NewTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 6, TargetChatID: "t"}
	seedUnknownFence(t, s, key, "ambiguous")
	ageFence(t, s, key, time.Hour)
	if ferr := s.FenceRecordSent(ctx, key, "w-other", 777); ferr != nil {
		t.Fatalf("record sent: %v", ferr)
	}

	ok, err := s.ReconcileUnknown(ctx, key, "w-reconciler", "reconciled")
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if ok {
		t.Fatal("reconcile overrode a fence that already recorded a confirmed send")
	}
	var status string
	if err := StoreDB(t, s).QueryRow(`SELECT status FROM delivery_attempts WHERE id=?`, key.String()).
		Scan(&status); err != nil {
		t.Fatalf("fence read: %v", err)
	}
	if status != "SENT" {
		t.Errorf("fence status = %s, want untouched SENT", status)
	}
}

func TestReconcileUnknownRollsBackWhenJobAlreadyMoved(t *testing.T) {
	s := NewTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 7, TargetChatID: "t"}
	seedUnknownFence(t, s, key, "ambiguous")
	ageFence(t, s, key, time.Hour)
	InsertJob(t, s, &job.DeliveryJob{
		ID: key.String(), RuleID: key.RuleID, SourceChatID: key.SourceChatID,
		SourceMessageID: key.SourceMessageID, TargetChatID: key.TargetChatID,
		Status: job.StatusSent, MaxAttempts: 5,
	})

	// Inconsistent pre-state (UNKNOWN fence beside a SENT job — possible only
	// through an out-of-band write). The reconcile must change NOTHING: a
	// half-reconciliation is the exact defect the transaction guards against.
	ok, err := s.ReconcileUnknown(ctx, key, "w-reconciler", "reconciled")
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if ok {
		t.Fatal("reconcile reported success against an inconsistent pair")
	}
	var fStatus string
	if err := StoreDB(t, s).QueryRow(`SELECT status FROM delivery_attempts WHERE id=?`, key.String()).
		Scan(&fStatus); err != nil {
		t.Fatalf("fence read: %v", err)
	}
	if fStatus != "UNKNOWN" {
		t.Errorf("fence status = %s, want UNKNOWN (rolled back) — a FAILED fence beside a SENT job would let a duplicate dispatch through", fStatus)
	}
}
