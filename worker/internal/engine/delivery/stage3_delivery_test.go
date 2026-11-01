package delivery

import (
	"context"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/engine/scheduler"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph"
	"atf/worker/internal/telegraph/fakegateway"
)

// --- BOT Transport Tests ---------------------------------------------------- //

// TestBOTDeliveryDispatchesThroughBot verifies that a job requesting BOT
// transport uses the Bot Api methods (not pyrogram MTProto sends) and carries
// the attached bot's token id and tenant identity without leaking secrets.
func TestBOTDeliveryDispatchesThroughBot(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	log := &testLogger{}
	metrics := &testMetrics{}
	r := &rule.ForwardRule{
		ID: "r-bot", SessionID: "sess-1", IsActive: true,
		DeliveryTransport: "BOT", BotTokenID: "tok-123", OwnerUserID: 77,
	}
	engine := New(store, &fixedRules{rule: r}, gw, nil, log, metrics, "w1", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})

	j := &job.DeliveryJob{
		ID: "r-bot:c:1:t", RuleID: "r-bot", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "bot message", "delivery_transport": "BOT",
			"bot_token_id": "tok-123", "owner_user_id": 77,
			"content_mode": "TEXT_ONLY",
		}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}

	botCalls := gw.BotRecorded()
	if len(botCalls) == 0 {
		t.Fatal("no bot call recorded — the delivery dispatched as USER")
	}
	if botCalls[0].Op != "BotSendText" {
		t.Errorf("bot op = %q, want BotSendText", botCalls[0].Op)
	}
	if botCalls[0].TokenID != "tok-123" {
		t.Errorf("token id = %q, want tok-123 (never the raw token)", botCalls[0].TokenID)
	}
	if botCalls[0].OwnerUserID != 77 {
		t.Errorf("owner = %d, want 77 (tenant isolation)", botCalls[0].OwnerUserID)
	}
}

// TestBOTDeliveryFallsBackToUSERWhenUnsupported verifies that an unsupported
// Bot Api operation (e.g. media group delivery) falls back to USER transport
// when the feature permits it, logging the reason explicitly.
func TestBOTDeliveryFallsBackToUSERWhenUnsupported(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	// Script the fake to report the typed BOT_METHOD_INVALID refusal
	gw.BotSendUnsupported = true

	log := &testLogger{}
	metrics := &testMetrics{}
	r := &rule.ForwardRule{
		ID: "r-bot-fallback", SessionID: "sess-user", IsActive: true,
		DeliveryTransport: "BOT", BotTokenID: "tok-123", OwnerUserID: 77,
	}
	engine := New(store, &fixedRules{rule: r}, gw, nil, log, metrics, "w1", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})

	j := &job.DeliveryJob{
		ID: "r-bot-fallback:c:1:t", RuleID: "r-bot-fallback", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "fallback text", "delivery_transport": "BOT",
			"bot_token_id": "tok-123", "owner_user_id": 77,
			"session_id": "sess-user", "content_mode": "TEXT_ONLY",
		}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}

	// Bot call was attempted...
	if len(gw.BotRecorded()) == 0 {
		t.Fatal("no bot call attempted before fallback")
	}
	// ...and USER call delivered the message.
	userCalls := gw.Recorded()
	if len(userCalls) == 0 {
		t.Fatal("no USER call recorded — fallback failed to deliver")
	}
	if userCalls[0].Op != "SendMessage" {
		t.Errorf("fallback op = %q, want SendMessage", userCalls[0].Op)
	}
	if userCalls[0].SessionID != "sess-user" {
		t.Errorf("fallback session = %q, want sess-user", userCalls[0].SessionID)
	}
}

// --- Branding Stage 1 Tests ------------------------------------------------ //

// TestBrandingStage1PostsIntoCBeforeStage2 verifies the Stage-3 ordering
// rule: the Go Worker performs stage 1 (post into C) FIRST, checkpoints the
// intermediate id, then forwards from C to target B.
func TestBrandingStage1PostsIntoCBeforeStage2(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	log := &testLogger{}
	metrics := &testMetrics{}
	r := &rule.ForwardRule{
		ID: "r-brand", SessionID: "sess-1", IsActive: true,
		IntermediateChannelID: "-100999", UseIntermediate: true,
	}
	engine := New(store, &fixedRules{rule: r}, gw, nil, log, metrics, "w1", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})

	// Job carries a Stage-3 branding intent (message_id=0, stage=INTENT)
	j := &job.DeliveryJob{
		ID: "r-brand:c:1:t", RuleID: "r-brand", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "branded content", "session_id": "sess-1",
			"content_mode": "TEXT_ONLY",
			"branding": map[string]any{
				"chat_id": "-100999", "message_id": 0, "message_ids": []any{},
				"force_forward": true, "content_mode": "TEXT_ONLY",
				"stage": "INTENT", "dedup_key": "fingerprint-1",
			},
		}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}

	// 1. Stage 1 posted into C (-100999).
	botCalls := gw.BotRecorded()
	var stage1Call *fakegateway.BotCall
	for _, c := range botCalls {
		if c.Op == "BrandingStage1" {
			cCopy := c
			stage1Call = &cCopy
			break
		}
	}
	if stage1Call == nil {
		t.Fatal("no BrandingStage1 call was recorded")
	}
	if stage1Call.TargetChatID != -100999 {
		t.Errorf("stage 1 posted to chat %d, want -100999 (channel C)", stage1Call.TargetChatID)
	}

	// 2. Stage 2 forwarded from C (-100999) to target B ("t").
	userCalls := gw.Recorded()
	if len(userCalls) == 0 {
		t.Fatal("no stage 2 call recorded")
	}
	// Target was forwarded from C: ChatID is fromChatID on ForwardMessages.
	if userCalls[0].ChatID != -100999 {
		t.Errorf("stage 2 forwarded from chat %d, want -100999 (C's post)", userCalls[0].ChatID)
	}

	// 3. The job's intermediate checkpoint is populated.
	if j.IntermediateChatID != "-100999" || j.IntermediateMsgID == 0 {
		t.Errorf("checkpoint not populated on job: chat=%q msg=%d",
			j.IntermediateChatID, j.IntermediateMsgID)
	}
	if j.DeliveryStage != "BRANDED" {
		t.Errorf("delivery_stage = %q, want BRANDED", j.DeliveryStage)
	}
}

// TestBrandingStage1ReusesConfirmedCheckpoint verifies restart recovery: a
// job whose intermediate checkpoint is already confirmed does NOT post into C
// a second time — it reuses the recorded intermediate id for stage 2.
func TestBrandingStage1ReusesConfirmedCheckpoint(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	log := &testLogger{}
	metrics := &testMetrics{}
	r := &rule.ForwardRule{
		ID: "r-brand-resume", SessionID: "sess-1", IsActive: true,
		IntermediateChannelID: "-100999", UseIntermediate: true,
	}
	engine := New(store, &fixedRules{rule: r}, gw, nil, log, metrics, "w1", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})

	// A job whose stage 1 already confirmed (IntermediateMsgID=555), but
	// crashed before stage 2 completed.
	j := &job.DeliveryJob{
		ID: "r-brand-resume:c:1:t", RuleID: "r-brand-resume", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		IntermediateChatID: "-100999", IntermediateMsgID: 555, DeliveryStage: "BRANDED",
		Payload: *jobPayload(map[string]any{
			"text": "branded content", "session_id": "sess-1",
			"content_mode": "TEXT_ONLY",
			"branding": map[string]any{
				"chat_id": "-100999", "message_id": 0, "message_ids": []any{},
				"force_forward": true, "stage": "INTENT",
			},
		}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}

	// No stage 1 post into C may happen on resume:
	for _, c := range gw.BotRecorded() {
		if c.Op == "BrandingStage1" {
			t.Error("a second BrandingStage1 call occurred on resume — the checkpoint was not reused")
		}
	}

	// Stage 2 must forward from the checkpointed id (555):
	calls := gw.Recorded()
	if len(calls) == 0 {
		t.Fatal("stage 2 was not executed")
	}
	if len(calls[0].MessageIDs) == 0 || calls[0].MessageIDs[0] != 555 {
		t.Errorf("stage 2 forwarded ids %v, want checkpointed id [555]", calls[0].MessageIDs)
	}
}

// --- Auto-Delete Scheduling Tests ------------------------------------------ //

type testSchedStore struct {
	scheduled []scheduler.Action
}

func (s *testSchedStore) Schedule(ctx context.Context, a scheduler.Action) error {
	s.scheduled = append(s.scheduled, a)
	return nil
}
func (s *testSchedStore) ClaimDue(ctx context.Context, workerID string, limit int, now float64) ([]scheduler.Action, error) {
	return nil, nil
}
func (s *testSchedStore) Complete(ctx context.Context, id string) error     { return nil }
func (s *testSchedStore) Fail(ctx context.Context, id, detail string) error { return nil }
func (s *testSchedStore) Retry(ctx context.Context, id string, next float64, d string) error {
	return nil
}
func (s *testSchedStore) PendingCount(ctx context.Context) (int, error) { return len(s.scheduled), nil }
func (s *testSchedStore) Close() error                                  { return nil }

// TestConfirmedDeliverySchedulesAutoDelete proves that a confirmed delivery
// schedules cleanup when auto_delete_seconds is configured.
func TestConfirmedDeliverySchedulesAutoDelete(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	sched := &testSchedStore{}
	log := &testLogger{}
	metrics := &testMetrics{}
	r := &rule.ForwardRule{ID: "r-autodel", SessionID: "sess-1", IsActive: true}
	engine := New(store, &fixedRules{rule: r}, gw, sched, log, metrics, "w1", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})

	j := &job.DeliveryJob{
		ID: "r-autodel:c:1:t", RuleID: "r-autodel", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "-100222", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "ephemeral message", "session_id": "sess-1",
			"auto_delete_seconds": 3600, // 1-hour TTL
			"owner_user_id":       77,
		}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}

	if len(sched.scheduled) != 1 {
		t.Fatalf("scheduled %d actions, want exactly 1", len(sched.scheduled))
	}
	action := sched.scheduled[0]
	if action.JobID != j.ID {
		t.Errorf("scheduled action job_id = %q, want %q", action.JobID, j.ID)
	}
	if action.ChatID != "-100222" {
		t.Errorf("scheduled chat = %q, want -100222", action.ChatID)
	}
	if action.OwnerUserID != 77 {
		t.Errorf("scheduled owner = %d, want 77", action.OwnerUserID)
	}
	now := float64(time.Now().Unix())
	if action.DueAt < now+3500 || action.DueAt > now+3700 {
		t.Errorf("due_at = %f, want ~%f (now + 3600)", action.DueAt, now+3600)
	}
}

// TestUnconfirmedDeliveryDoesNotScheduleAutoDelete proves that a delivery that
// failed or was deferred never schedules deletion.
func TestUnconfirmedDeliveryDoesNotScheduleAutoDelete(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	// Script send to fail permanently:
	gw.SendFailsWith = &telegraph.DeliveryFailure{Code: "PERMISSION_DENIED", Message: "cannot send"}
	sched := &testSchedStore{}
	log := &testLogger{}
	metrics := &testMetrics{}
	r := &rule.ForwardRule{ID: "r-fail", SessionID: "sess-1", IsActive: true}
	engine := New(store, &fixedRules{rule: r}, gw, sched, log, metrics, "w1", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})

	j := &job.DeliveryJob{
		ID: "r-fail:c:1:t", RuleID: "r-fail", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "-100222", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "should fail", "session_id": "sess-1",
			"auto_delete_seconds": 3600,
		}),
	}
	queue.InsertJob(t, store, j)

	_ = engine.Process(context.Background(), j)

	if len(sched.scheduled) != 0 {
		t.Errorf("an action was scheduled for an unconfirmed delivery (%+v)", sched.scheduled)
	}
}

// --- Keyword Reply BOT Compatibility Tests --------------------------------- //

// TestKeywordReplyBOTCompatibility proves that a keyword-reply job executes
// through the BOT transport when requested, with buttons and topic thread.
func TestKeywordReplyBOTCompatibility(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	log := &testLogger{}
	metrics := &testMetrics{}
	r := &rule.ForwardRule{
		ID: "r-reply", SessionID: "sess-1", IsActive: true,
		DeliveryTransport: "BOT", BotTokenID: "tok-reply", OwnerUserID: 77,
	}
	engine := New(store, &fixedRules{rule: r}, gw, nil, log, metrics, "w1", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})

	j := &job.DeliveryJob{
		ID: "r-reply:c:1:t:keyword_reply", RuleID: "r-reply", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", JobKind: "keyword_reply", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text":     "Auto-Reply: Thanks for reaching out!",
			"is_reply": true, "delivery_transport": "BOT",
			"bot_token_id": "tok-reply", "owner_user_id": 77,
			"topic_id": 99,
		}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}

	botCalls := gw.BotRecorded()
	if len(botCalls) == 0 {
		t.Fatal("keyword reply did not dispatch through BOT transport")
	}
	if botCalls[0].Op != "BotSendText" {
		t.Errorf("reply op = %q, want BotSendText", botCalls[0].Op)
	}
	if botCalls[0].Text != "Auto-Reply: Thanks for reaching out!" {
		t.Errorf("reply text = %q", botCalls[0].Text)
	}
	if botCalls[0].ThreadID != 99 {
		t.Errorf("reply topic = %d, want 99", botCalls[0].ThreadID)
	}
}
