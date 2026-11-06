package reconciler

// The reconciliation loop's contract, against the real store: production
// stranded a day of UNKNOWN deliveries (13 rows, oldest 20+ hours) because
// the fence documented "reconciliation decides" and nothing ever decided.
// These tests pin the decision: provable server-side rejections close; every
// genuine ambiguity stays visible.

import (
	"context"
	"strings"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/queue"
)

type discardLogger struct{}

func (discardLogger) Info(string, ...any)  {}
func (discardLogger) Warn(string, ...any)  {}
func (discardLogger) Error(string, ...any) {}

// seedAmbiguous drives a fence through the exact production shape: claimed,
// outcome lost by the runtime, recorded UNKNOWN with the failure evidence.
func seedAmbiguous(t *testing.T, s queue.Store, key job.AttemptKey, evidence string, age time.Duration) {
	t.Helper()
	ctx := context.Background()
	if _, _, ferr := s.FenceClaim(ctx, key, "w-test", float64(time.Now().Add(time.Minute).Unix())); ferr != nil {
		t.Fatalf("fence claim: %v", ferr)
	}
	if ferr := s.FenceRecordOutcome(ctx, key, "w-test", "UNKNOWN", evidence); ferr != nil {
		t.Fatalf("fence outcome: %v", ferr)
	}
	if age > 0 {
		if _, err := queue.StoreDB(t, s).Exec(
			`UPDATE delivery_attempts SET updated_at=? WHERE id=?`,
			time.Now().Add(-age).Unix(), key.String()); err != nil {
			t.Fatalf("age fence: %v", err)
		}
	}
}

func newSweep(t *testing.T, s queue.Store) *Reconciler {
	t.Helper()
	return New(s, "w-reconciler", 10*time.Minute, time.Hour, discardLogger{})
}

func TestRunOnceClosesProvableServerRejections(t *testing.T) {
	s := queue.NewTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 1, TargetChatID: "t"}
	seedAmbiguous(t, s, key,
		"Uncertain outcome: rpc error: code = Unimplemented desc = Unexpected <class 'NotImplementedError'>: Method not implemented!",
		time.Hour)
	queue.InsertJob(t, s, &job.DeliveryJob{
		ID: key.String(), RuleID: key.RuleID, SourceChatID: key.SourceChatID,
		SourceMessageID: key.SourceMessageID, TargetChatID: key.TargetChatID,
		Status: job.StatusUnknown, MaxAttempts: 5,
	})

	resolved, remaining, err := newSweep(t, s).RunOnce(ctx)
	if err != nil {
		t.Fatalf("run once: %v", err)
	}
	if resolved != 1 || remaining != 0 {
		t.Fatalf("resolved=%d remaining=%d, want 1/0 — the production backlog shape must close", resolved, remaining)
	}

	db := queue.StoreDB(t, s)
	var jStatus, jErr string
	if err := db.QueryRow(`SELECT status, error_detail FROM delivery_jobs WHERE id=?`, key.String()).
		Scan(&jStatus, &jErr); err != nil {
		t.Fatalf("job read: %v", err)
	}
	if jStatus != "FAILED" {
		t.Errorf("job status = %s, want FAILED", jStatus)
	}
	if !strings.Contains(jErr, "Unimplemented") {
		t.Errorf("reconciliation reason lost the evidence: %q", jErr)
	}

	// And the closed job is recoverable through the normal path.
	outcome, _, ferr := s.FenceClaim(ctx, key, "w-retry", float64(time.Now().Add(time.Minute).Unix()))
	if ferr != nil {
		t.Fatalf("re-claim: %v", ferr)
	}
	if outcome != job.FenceDispatch {
		t.Errorf("post-reconcile claim = %s, want dispatch", outcome)
	}
}

func TestRunOnceLeavesGenuineAmbiguityUnknown(t *testing.T) {
	s := queue.NewTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 2, TargetChatID: "t"}
	seedAmbiguous(t, s, key,
		"Uncertain outcome: rpc error: code = DeadlineExceeded desc = context deadline exceeded",
		time.Hour)
	queue.InsertJob(t, s, &job.DeliveryJob{
		ID: key.String(), RuleID: key.RuleID, SourceChatID: key.SourceChatID,
		SourceMessageID: key.SourceMessageID, TargetChatID: key.TargetChatID,
		Status: job.StatusUnknown, MaxAttempts: 5,
	})

	resolved, remaining, err := newSweep(t, s).RunOnce(ctx)
	if err != nil {
		t.Fatalf("run once: %v", err)
	}
	if resolved != 0 || remaining != 1 {
		t.Fatalf("resolved=%d remaining=%d, want 0/1 — a timeout proves nothing about the send", resolved, remaining)
	}
	var status string
	if err := queue.StoreDB(t, s).QueryRow(
		`SELECT status FROM delivery_jobs WHERE id=?`, key.String()).Scan(&status); err != nil {
		t.Fatalf("job read: %v", err)
	}
	if status != "UNKNOWN" {
		t.Errorf("job status = %s, want untouched UNKNOWN — guessing here is the duplicate-delivery path", status)
	}
}

func TestRunOnceHonorsTheGraceWindow(t *testing.T) {
	s := queue.NewTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 3, TargetChatID: "t"}
	// Provable rejection but still inside the grace window: a live dispatch
	// could still be settling. Not listed, not touched.
	seedAmbiguous(t, s, key, "rpc error: code = Unimplemented desc = x", 0)

	resolved, remaining, err := newSweep(t, s).RunOnce(ctx)
	if err != nil {
		t.Fatalf("run once: %v", err)
	}
	if resolved != 0 || remaining != 0 {
		t.Fatalf("resolved=%d remaining=%d, want 0/0 — fresh rows must be left for the next pass", resolved, remaining)
	}
}

func TestProvablyUnexecutedTable(t *testing.T) {
	cases := []struct {
		evidence string
		want     bool
	}{
		{"rpc error: code = Unimplemented desc = Unexpected <class 'NotImplementedError'>: Method not implemented!", true},
		{"rpc error: code = Unimplemented desc = unknown method SendMedia", true},
		{"rpc error: code = DeadlineExceeded", false},
		{"rpc error: code = Unavailable desc = connection error", false},
		// Dial-phase Unavailable proves no stream existed: nothing reached a
		// handler, so no Telegram operation can have been attempted.
		{"rpc error: code = Unavailable desc = connection error: desc = \"transport: Error while dialing: dial tcp 10.0.0.5:50053: connect: connection rejection\"", true},
		// A bare Unavailable says nothing about WHERE the transport died: a
		// runtime killed mid-upload reports the same code after Telegram
		// accepted the send. Classifying it would duplicate the delivery.
		{"rpc error: code = Unavailable desc = transport is closing", false},
		{"Uncertain outcome: transport dropped mid-send", false},
		{"", false},
	}
	for _, tc := range cases {
		if got := provablyUnexecuted(tc.evidence); got != tc.want {
			t.Errorf("provablyUnexecuted(%q) = %v, want %v", tc.evidence, got, tc.want)
		}
	}
}
