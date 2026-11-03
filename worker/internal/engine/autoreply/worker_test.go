package autoreply_test

import (
	"context"
	"path/filepath"
	"testing"

	"atf/worker/internal/domain/aiconfig"
	domain "atf/worker/internal/domain/autoreply"
	engine "atf/worker/internal/engine/autoreply"
	"atf/worker/internal/infra/aiprovider"
	"atf/worker/internal/infra/autoreply/store"
	"atf/worker/internal/telegraph"
)

type fakeSender struct {
	sentID int64
	fail   *telegraph.DeliveryFailure
	calls  int
}

func (f *fakeSender) SendMessage(ctx context.Context, sessionID string, targetChatID int64, text string, threadID int64) (int64, *telegraph.DeliveryFailure) {
	f.calls++
	if f.fail != nil {
		return 0, f.fail
	}
	f.sentID++
	return f.sentID, nil
}

type fakeAIStore struct {
	configs map[string]*aiconfig.AIConfig
}

func (s *fakeAIStore) GetOwned(ctx context.Context, id string, owner int64) (*aiconfig.AIConfig, error) {
	c := s.configs[id]
	if c == nil || c.OwnerUserID != owner {
		return nil, nil
	}
	return c, nil
}

func (s *fakeAIStore) GetByID(ctx context.Context, id string) (*aiconfig.AIConfig, error) {
	return s.configs[id], nil
}

type fakeProvider struct {
	reply string
	err   error
}

func (p *fakeProvider) Rewrite(ctx context.Context, cfg *aiconfig.AIConfig, text string) (string, error) {
	return p.reply, p.err
}

func (p *fakeProvider) ChatComplete(ctx context.Context, cfg *aiconfig.AIConfig, messages []aiprovider.Message, temp float64) (string, error) {
	return p.reply, p.err
}

func (p *fakeProvider) HealthCheck(ctx context.Context, cfg *aiconfig.AIConfig) (bool, error) {
	return true, nil
}

func setupTestWorker(t *testing.T, reply string, fail *telegraph.DeliveryFailure) (*engine.Worker, store.Store, *fakeSender) {
	t.Helper()
	dir := t.TempDir()
	st, err := store.NewSQLiteStore(context.Background(), filepath.Join(dir, "pva.db"))
	if err != nil {
		t.Fatalf("store: %v", err)
	}

	aiSt := &fakeAIStore{
		configs: map[string]*aiconfig.AIConfig{
			"cfg-user100": {
				ID:          "cfg-user100",
				OwnerUserID: 100,
				Provider:    "openai",
				Model:       "gpt-4o",
				IsEnabled:   true,
			},
			"cfg-user200": {
				ID:          "cfg-user200",
				OwnerUserID: 200,
				Provider:    "openai",
				Model:       "gpt-4o",
				IsEnabled:   true,
			},
		},
	}

	factory := aiprovider.NewFactory()
	factory.Register("openai", func() aiprovider.Provider {
		return &fakeProvider{reply: reply}
	})

	snd := &fakeSender{fail: fail}

	w := engine.NewWorker(st, aiSt, factory, snd, "worker-1", engine.Options{
		Concurrency:  2,
		PollInterval: 100,
		LeaseSeconds: 30,
	})
	return w, st, snd
}

func TestWorkerEndToEndHappyPath(t *testing.T) {
	ctx := context.Background()
	w, st, snd := setupTestWorker(t, "Hello from AI bot!", nil)

	// Configure user 100
	st.SaveSettings(ctx, &domain.AutoReplySettings{
		OwnerUserID:     100,
		Enabled:         true,
		AIConfigID:      "cfg-user100",
		ContextMessages: 5,
	})

	// Enqueue job
	st.Enqueue(ctx, &domain.AutoReplyJob{
		ID:              "j-1",
		OwnerUserID:     100,
		ChatID:          "-100555",
		SourceMessageID: 10,
		InboundText:     "Hi there!",
	})

	// Process
	ok, err := w.Step(ctx, 100)
	if err != nil || !ok {
		t.Fatalf("Step failed: ok=%v, err=%v", ok, err)
	}

	if snd.calls != 1 {
		t.Errorf("expected 1 send call, got %d", snd.calls)
	}

	job, err := st.GetJob(ctx, "j-1", 100)
	if err != nil || job.Status != domain.JobStatusSent {
		t.Errorf("job not sent: %+v", job)
	}
	if job.ReplyText != "Hello from AI bot!" {
		t.Errorf("unexpected reply text: %q", job.ReplyText)
	}

	// Message history should contain assistant response
	msgs, err := st.ListMessages(ctx, 100, "-100555", 10)
	if err != nil || len(msgs) != 1 || msgs[0].Role != "assistant" {
		t.Errorf("messages history: %+v, err=%v", msgs, err)
	}
}

func TestWorkerTenantIsolationAIConfig(t *testing.T) {
	ctx := context.Background()
	w, st, snd := setupTestWorker(t, "Should not be sent", nil)

	// User 100 tries to configure User 200's AI config
	st.SaveSettings(ctx, &domain.AutoReplySettings{
		OwnerUserID: 100,
		Enabled:     true,
		AIConfigID:  "cfg-user200", // foreign!
	})

	st.Enqueue(ctx, &domain.AutoReplyJob{
		ID:              "j-foreign",
		OwnerUserID:     100,
		ChatID:          "-100555",
		SourceMessageID: 11,
		InboundText:     "Attempt exploit",
	})

	// Process should fail and dead-letter the job
	ok, _ := w.Step(ctx, 100)
	if ok || snd.calls > 0 {
		t.Errorf("foreign AI config must not be executed, calls=%d", snd.calls)
	}

	job, _ := st.GetJob(ctx, "j-foreign", 100)
	if job == nil || job.Status != domain.JobStatusDeadLetter {
		t.Errorf("expected dead-letter DEAD_LETTER, got %+v", job)
	}
}

func TestWorkerTransientSendFailureRetries(t *testing.T) {
	ctx := context.Background()
	// FLOOD_WAIT is transient -> retry
	fail := &telegraph.DeliveryFailure{
		Code:      "FLOOD_WAIT",
		ErrorType: "FLOOD_WAIT_10",
	}
	w, st, snd := setupTestWorker(t, "Hello", fail)

	st.SaveSettings(ctx, &domain.AutoReplySettings{
		OwnerUserID: 100,
		Enabled:     true,
		AIConfigID:  "cfg-user100",
	})

	st.Enqueue(ctx, &domain.AutoReplyJob{
		ID:              "j-transient",
		OwnerUserID:     100,
		ChatID:          "-1001",
		SourceMessageID: 20,
		InboundText:     "test transient",
		MaxAttempts:     3,
	})

	ok, _ := w.Step(ctx, 100)
	if ok || snd.calls != 1 {
		t.Fatalf("send should fail once: ok=%v, calls=%d", ok, snd.calls)
	}

	job, _ := st.GetJob(ctx, "j-transient", 100)
	if job == nil || job.Status != domain.JobStatusRetryWait {
		t.Errorf("expected RETRY_WAIT, got %+v", job)
	}
	if job.Attempt != 1 {
		t.Errorf("expected attempt=1, got %d", job.Attempt)
	}
}

func TestWorkerPermanentSendFailureDeadLetters(t *testing.T) {
	ctx := context.Background()
	// USER_DEACTIVATED is permanent -> dead letter
	fail := &telegraph.DeliveryFailure{
		Code:      "USER_DEACTIVATED",
		ErrorType: "USER_DEACTIVATED",
	}
	w, st, snd := setupTestWorker(t, "Hello", fail)

	st.SaveSettings(ctx, &domain.AutoReplySettings{
		OwnerUserID: 100,
		Enabled:     true,
		AIConfigID:  "cfg-user100",
	})

	st.Enqueue(ctx, &domain.AutoReplyJob{
		ID:              "j-permanent",
		OwnerUserID:     100,
		ChatID:          "-1001",
		SourceMessageID: 21,
		InboundText:     "test permanent",
		MaxAttempts:     3,
	})

	ok, _ := w.Step(ctx, 100)
	if ok || snd.calls != 1 {
		t.Fatalf("send should fail once: ok=%v, calls=%d", ok, snd.calls)
	}

	job, _ := st.GetJob(ctx, "j-permanent", 100)
	if job == nil || job.Status != domain.JobStatusDeadLetter {
		t.Errorf("expected DEAD_LETTER, got %+v", job)
	}
}
