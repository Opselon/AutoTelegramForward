package queue

// Regression tests for the fence-claim lease semantics (audit round S4/S3).
//
// S4: every goroutine in a worker process shares one worker id, so a
// CLAIMED fence whose worker_id matches the caller used to be handed back
// as FenceDispatch with no lease check — a dispatch that outlived the 30s
// job lease (a large album re-upload) was handed to a second claim and the
// job was delivered twice. The lease now decides, for same-worker claims
// exactly as for cross-worker ones.
//
// S3: a SENT fence is terminal. The SQLite store used to accept both
// FenceRecordSent and FenceRecordOutcome unconditionally, so a late sibling
// failure could stamp FAILED over a confirmed send and turn it back into a
// re-dispatchable job.

import (
	"context"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
)

func TestFenceClaimSameWorkerHonorsLease(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{
		RuleID: "r1", SourceChatID: "c", SourceMessageID: 41,
		TargetChatID: "t", JobKind: "forward",
	}
	const id = "r1:c:41:t"

	// Claim with a LIVE lease: the same worker re-claiming must see it in
	// flight, not get a second dispatch. The first dispatch is still
	// running — that is what the claim means.
	outcome, _, err := s.FenceClaim(ctx, key, "w1", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("claim 1: %v", err)
	}
	if outcome != job.FenceDispatch {
		t.Fatalf("first claim = %s, want dispatch", outcome)
	}
	outcome, _, err = s.FenceClaim(ctx, key, "w1", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("same-worker claim: %v", err)
	}
	if outcome != job.FenceInFlight {
		t.Fatalf("same-worker live-lease claim = %s, want in_flight (a second dispatch here delivers the job twice)", outcome)
	}

	// Once the fence's lease has expired the claim is stale: the stale
	// sweep + reconciler own it, never a direct re-dispatch.
	freshKey := job.AttemptKey{
		RuleID: "r1", SourceChatID: "c", SourceMessageID: 42,
		TargetChatID: "t", JobKind: "forward",
	}
	outcome, _, err = s.FenceClaim(ctx, freshKey, "w1", float64(time.Now().Add(-time.Minute).Unix()))
	if err != nil {
		t.Fatalf("expired claim: %v", err)
	}
	if outcome != job.FenceDispatch {
		t.Fatalf("first claim of expired-lease fence = %s, want dispatch (the claim IS the lease)", outcome)
	}
	outcome, _, err = s.FenceClaim(ctx, freshKey, "w1", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("second claim on expired fence: %v", err)
	}
	if outcome != job.FenceStale {
		t.Fatalf("same-worker expired-lease claim = %s, want stale", outcome)
	}

	// A FAILED fence must still be re-claimable by the same worker — the
	// replay path depends on it (the pre-read restructure must not have
	// closed it).
	retryKey := job.AttemptKey{
		RuleID: "r1", SourceChatID: "c", SourceMessageID: 43,
		TargetChatID: "t", JobKind: "forward",
	}
	if _, _, err := s.FenceClaim(ctx, retryKey, "w1", float64(time.Now().Add(time.Minute).Unix())); err != nil {
		t.Fatalf("claim: %v", err)
	}
	if err := s.FenceRecordOutcome(ctx, retryKey, "w1", "FAILED", "transient"); err != nil {
		t.Fatalf("record outcome: %v", err)
	}
	outcome, row, err := s.FenceClaim(ctx, retryKey, "w1", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("reclaim after FAILED: %v", err)
	}
	if outcome != job.FenceDispatch {
		t.Fatalf("FAILED reclaim = %s, want dispatch", outcome)
	}
	if row == nil || row.Attempts != 1 {
		t.Fatalf("FAILED reclaim attempts = %+v, want 1 (the counter must survive the restructure)", row)
	}
}

func TestFenceSentIsTerminalOnSQLite(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{
		RuleID: "r1", SourceChatID: "c", SourceMessageID: 44,
		TargetChatID: "t", JobKind: "forward",
	}

	if _, _, err := s.FenceClaim(ctx, key, "w1", float64(time.Now().Add(time.Minute).Unix())); err != nil {
		t.Fatalf("claim: %v", err)
	}
	if err := s.FenceRecordSent(ctx, key, "w1", 4400); err != nil {
		t.Fatalf("record sent: %v", err)
	}

	// A sibling's late failure must NOT turn a confirmed send back into a
	// re-dispatchable failure (S3 — the message is physically in the
	// target; FAILED here would eventually duplicate it).
	if err := s.FenceRecordOutcome(ctx, key, "w1", "FAILED", "late sibling failure"); err != nil {
		t.Fatalf("record outcome over SENT: %v", err)
	}
	outcome, _, err := s.FenceClaim(ctx, key, "w2", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("claim after outcome-over-sent: %v", err)
	}
	if outcome != job.FenceAlreadySent {
		t.Fatalf("claim after FAILED-over-SENT = %s, want already_sent (the fence must stay terminal)", outcome)
	}

	// FenceRecordSent is likewise never a downgrade path once SENT.
	if err := s.FenceRecordSent(ctx, key, "w2", 4401); err != nil {
		t.Fatalf("second record sent: %v", err)
	}
	_, row, err := s.FenceClaim(ctx, key, "w3", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("final claim: %v", err)
	}
	if row.TargetMessageID != 4400 {
		t.Fatalf("target id after second record-sent = %d, want 4400 (first confirmed send wins)", row.TargetMessageID)
	}
}

func TestPurgeAgedFencesKeepsUnknownAndFresh(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	db := StoreDB(t, s)

	mk := func(msg int, status string, ageDays int) job.AttemptKey {
		key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: int64(msg), TargetChatID: "t", JobKind: "forward"}
		_, err := db.Exec(`INSERT INTO delivery_attempts (
			id, rule_id, source_chat_id, source_message_id, target_chat_id, job_kind,
			status, worker_id, lease_until, attempts, last_error, created_at, updated_at)
			VALUES (?,?,?,?,?,?,?,?,0,0,'',?,?)`,
			key.String(), key.RuleID, key.SourceChatID, key.SourceMessageID,
			key.TargetChatID, key.JobKind, status, "w1",
			time.Now().Unix()-int64(ageDays*24*3600), time.Now().Unix()-int64(ageDays*24*3600))
		if err != nil {
			t.Fatalf("seed fence %d: %v", msg, err)
		}
		return key
	}
	oldSent := mk(61, "SENT", 10)
	oldFailed := mk(62, "FAILED", 10)
	oldUnknown := mk(63, "UNKNOWN", 10)
	freshSent := mk(64, "SENT", 1)

	// Jobs-side guard rail: nothing here may delete unresolved evidence.
	purged, err := s.PurgeAgedFences(ctx, time.Now().Add(-7*24*time.Hour), 500)
	if err != nil {
		t.Fatalf("purge: %v", err)
	}
	if purged != 2 {
		t.Fatalf("purged = %d, want exactly the 2 aged terminal rows", purged)
	}
	for _, tc := range []struct {
		key   job.AttemptKey
		want  bool
		label string
	}{
		{oldSent, false, "aged SENT fence"},
		{oldFailed, false, "aged FAILED fence"},
		{oldUnknown, true, "aged UNKNOWN fence (evidence — never swept)"},
		{freshSent, true, "fresh SENT fence (inside retention)"},
	} {
		out, _, err := s.FenceClaim(ctx, tc.key, "probe", float64(time.Now().Add(time.Minute).Unix()))
		if err != nil {
			t.Fatalf("%s probe: %v", tc.label, err)
		}
		gone := out == job.FenceDispatch // absent row → fresh claim → dispatch
		if gone == tc.want {
			t.Errorf("%s: gone=%v, want row-present=%v (outcome %s)", tc.label, gone, tc.want, out)
		}
	}
}
