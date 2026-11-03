package store_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"atf/worker/internal/domain/autoreply"
	"atf/worker/internal/infra/autoreply/store"
)

func newTestStore(t *testing.T) store.Store {
	t.Helper()
	dir := t.TempDir()
	dsn := filepath.Join(dir, "autoreply_test.db")
	s, err := store.NewSQLiteStore(context.Background(), dsn)
	if err != nil {
		t.Fatalf("NewSQLiteStore: %v", err)
	}
	return s
}

func TestSettingsAndTenantIsolation(t *testing.T) {
	ctx := context.Background()
	s := newTestStore(t)

	// User 1
	cfg1 := &autoreply.AutoReplySettings{
		OwnerUserID: 100,
		Enabled:     true,
		Tone:        autoreply.ToneProfessional,
	}
	saved1, err := s.SaveSettings(ctx, cfg1)
	if err != nil {
		t.Fatalf("save user 1: %v", err)
	}
	if !saved1.Enabled || saved1.Tone != autoreply.ToneProfessional {
		t.Errorf("saved1 mismatch: %+v", saved1)
	}

	// User 2
	cfg2 := &autoreply.AutoReplySettings{
		OwnerUserID: 200,
		Enabled:     false,
		Tone:        autoreply.ToneCasual,
	}
	if _, err := s.SaveSettings(ctx, cfg2); err != nil {
		t.Fatalf("save user 2: %v", err)
	}

	// Verify isolation
	got1, err := s.GetSettings(ctx, 100)
	if err != nil || got1 == nil || got1.Tone != autoreply.ToneProfessional {
		t.Errorf("got1: %+v, err=%v", got1, err)
	}

	got2, err := s.GetSettings(ctx, 200)
	if err != nil || got2 == nil || got2.Tone != autoreply.ToneCasual {
		t.Errorf("got2: %+v, err=%v", got2, err)
	}

	// Non-existent user gets defaults
	got3, err := s.GetSettings(ctx, 300)
	if err != nil || got3 == nil || got3.Enabled {
		t.Errorf("got3 expected disabled default, got %+v", got3)
	}
}

func TestJobLifecycle(t *testing.T) {
	ctx := context.Background()
	s := newTestStore(t)
	now := time.Now().Unix()

	j := &autoreply.AutoReplyJob{
		ID:              "job-1",
		OwnerUserID:     100,
		ChatID:          "-100123",
		SourceMessageID: 55,
		InboundText:     "hello bot",
		PromptID:        "p1",
		AIConfigID:      "cfg1",
		MaxAttempts:     3,
	}

	enqueued, stored, err := s.Enqueue(ctx, j)
	if err != nil || !enqueued {
		t.Fatalf("enqueue failed: enqueued=%v, err=%v", enqueued, err)
	}
	if stored.Status != autoreply.JobStatusPending {
		t.Errorf("status: %v", stored.Status)
	}

	// Duplicate enqueue is no-op
	enq2, _, err := s.Enqueue(ctx, j)
	if err != nil || enq2 {
		t.Errorf("duplicate enqueue should be false, got %v", enq2)
	}

	// Claim
	claimed, err := s.ClaimNext(ctx, 100, "w1", now, 30)
	if err != nil || claimed == nil {
		t.Fatalf("claim failed: %+v, err=%v", claimed, err)
	}
	if claimed.ID != "job-1" || claimed.Status != autoreply.JobStatusRunning {
		t.Errorf("claimed job mismatch: %+v", claimed)
	}

	// Complete sent
	ok, err := s.CompleteSent(ctx, claimed.ID, claimed.LeaseToken, 999, "hi human", 250)
	if err != nil || !ok {
		t.Fatalf("complete failed: ok=%v, err=%v", ok, err)
	}

	// Verify terminal status
	final, err := s.GetJob(ctx, "job-1", 100)
	if err != nil || final == nil || final.Status != autoreply.JobStatusSent {
		t.Errorf("final job status: %+v", final)
	}
	if final.ReplyText != "hi human" {
		t.Errorf("reply text: %q", final.ReplyText)
	}
}

func TestPromptsAndIsolation(t *testing.T) {
	ctx := context.Background()
	s := newTestStore(t)

	p1 := &autoreply.AutoReplyPrompt{
		ID:                 "p-1",
		OwnerUserID:        100,
		Name:               "Support Prompt",
		SystemPrompt:       "You are support",
		UserPromptTemplate: "{text}",
	}
	if _, err := s.SavePrompt(ctx, p1); err != nil {
		t.Fatalf("save prompt: %v", err)
	}

	// Owner 100 sees it
	got1, err := s.GetPrompt(ctx, "p-1", 100)
	if err != nil || got1 == nil || got1.Name != "Support Prompt" {
		t.Errorf("got1: %+v, err=%v", got1, err)
	}

	// Owner 200 CANNOT see it
	got2, err := s.GetPrompt(ctx, "p-1", 200)
	if err != nil || got2 != nil {
		t.Errorf("owner 200 should not see prompt, got %+v, err=%v", got2, err)
	}
}

func TestMemoryFactsAndDedupe(t *testing.T) {
	ctx := context.Background()
	s := newTestStore(t)

	id1, err := s.Remember(ctx, 100, "chat-1", "User prefers English", autoreply.MemoryKindFact, 3600)
	if err != nil || id1 == "" {
		t.Fatalf("remember 1: id=%s, err=%v", id1, err)
	}

	// Duplicate content hash in same chat is idempotent
	id2, err := s.Remember(ctx, 100, "chat-1", "User prefers English", autoreply.MemoryKindFact, 3600)
	if err != nil || id2 == "" {
		t.Fatalf("remember 2 duplicate: id=%s, err=%v", id2, err)
	}

	facts, err := s.ListMemory(ctx, 100, "chat-1", 10)
	if err != nil || len(facts) != 1 {
		t.Errorf("expected 1 fact after dedupe, got %d, err=%v", len(facts), err)
	}
}

func TestLeaseRecovery(t *testing.T) {
	ctx := context.Background()
	s := newTestStore(t)
	now := time.Now().Unix()

	j := &autoreply.AutoReplyJob{
		ID:              "job-expire",
		OwnerUserID:     100,
		ChatID:          "-100999",
		SourceMessageID: 12,
		InboundText:     "test expire",
	}
	s.Enqueue(ctx, j)

	// Claim with short lease (10s)
	claimed, err := s.ClaimNext(ctx, 100, "w1", now, 10)
	if err != nil || claimed == nil {
		t.Fatalf("claim: %v", err)
	}

	// Not expired yet
	recovered, err := s.RecoverExpiredLeases(ctx, now+5)
	if err != nil || recovered != 0 {
		t.Errorf("recovered too early: %d", recovered)
	}

	// Now expired (15s later)
	recovered, err = s.RecoverExpiredLeases(ctx, now+15)
	if err != nil || recovered != 1 {
		t.Errorf("expected 1 recovered lease, got %d, err=%v", recovered, err)
	}

	// Can be claimed again
	reclaimed, err := s.ClaimNext(ctx, 100, "w2", now+20, 30)
	if err != nil || reclaimed == nil || reclaimed.ID != "job-expire" {
		t.Errorf("reclaim failed: %+v", reclaimed)
	}
}
