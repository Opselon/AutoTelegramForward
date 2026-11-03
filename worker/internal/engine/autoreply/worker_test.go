package autoreply_test

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"atf/worker/internal/domain/aiconfig"
	domain "atf/worker/internal/domain/autoreply"
	engine "atf/worker/internal/engine/autoreply"
	"atf/worker/internal/infra/aiprovider"
	"atf/worker/internal/infra/autoreply/store"
	"atf/worker/internal/telegraph"
)

type fakeSender struct {
	mu     sync.Mutex
	sentID int64
	fail   *telegraph.DeliveryFailure
	calls  int
}

func (f *fakeSender) SendMessage(ctx context.Context, sessionID string, targetChatID int64, text string, threadID int64) (int64, *telegraph.DeliveryFailure) {
	// Concurrency-safe: the worker dispatches jobs across a goroutine pool,
	// so this counter is genuinely written from multiple goroutines.
	f.mu.Lock()
	defer f.mu.Unlock()
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
		PollInterval: 100 * time.Millisecond,
		LeaseSeconds: 30,
	})
	return w, st, snd
}

func TestWorkerEndToEndHappyPath(t *testing.T) {
	ctx := context.Background()
	w, st, snd := setupTestWorker(t, "Hello from AI bot!", nil)

	st.SaveSettings(ctx, &domain.AutoReplySettings{
		OwnerUserID:     100,
		Enabled:         true,
		AIConfigID:      "cfg-user100",
		ContextMessages: 5,
	})

	st.Enqueue(ctx, &domain.AutoReplyJob{
		ID:              "j-1",
		OwnerUserID:     100,
		ChatID:          "-100555",
		SourceMessageID: 10,
		InboundText:     "Hi there!",
	})

	ok, err := w.Step(ctx, 100)
	if err != nil || !ok {
		t.Fatalf("Step failed: ok=%v, err=%v", ok, err)
	}

	snd.mu.Lock()
	calls := snd.calls
	snd.mu.Unlock()
	if calls != 1 {
		t.Errorf("expected 1 send call, got %d", calls)
	}

	job, err := st.GetJob(ctx, "j-1", 100)
	if err != nil || job.Status != domain.JobStatusSent {
		t.Errorf("job not sent: %+v", job)
	}
	if job.ReplyText != "Hello from AI bot!" {
		t.Errorf("unexpected reply text: %q", job.ReplyText)
	}

	msgs, err := st.ListMessages(ctx, 100, "-100555", 10)
	if err != nil || len(msgs) != 1 || msgs[0].Role != "assistant" {
		t.Errorf("messages history: %+v, err=%v", msgs, err)
	}
}

func TestWorkerTenantIsolationAIConfig(t *testing.T) {
	ctx := context.Background()
	w, st, snd := setupTestWorker(t, "Should not be sent", nil)

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

	ok, _ := w.Step(ctx, 100)
	snd.mu.Lock()
	calls := snd.calls
	snd.mu.Unlock()
	if ok || calls > 0 {
		t.Errorf("foreign AI config must not be executed, calls=%d", calls)
	}

	job, _ := st.GetJob(ctx, "j-foreign", 100)
	if job == nil || job.Status != domain.JobStatusDeadLetter {
		t.Errorf("expected dead-letter DEAD_LETTER, got %+v", job)
	}
}

func TestWorkerTransientSendFailureRetries(t *testing.T) {
	ctx := context.Background()
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
	snd.mu.Lock()
	calls := snd.calls
	snd.mu.Unlock()
	if ok || calls != 1 {
		t.Fatalf("send should fail once: ok=%v, calls=%d", ok, calls)
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
	snd.mu.Lock()
	calls := snd.calls
	snd.mu.Unlock()
	if ok || calls != 1 {
		t.Fatalf("send should fail once: ok=%v, calls=%d", ok, calls)
	}

	job, _ := st.GetJob(ctx, "j-permanent", 100)
	if job == nil || job.Status != domain.JobStatusDeadLetter {
		t.Errorf("expected DEAD_LETTER, got %+v", job)
	}
}

func TestWorkerRunShutdownDrains(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	w, st, snd := setupTestWorker(t, "Hello from AI bot!", nil)

	st.SaveSettings(ctx, &domain.AutoReplySettings{
		OwnerUserID: 100,
		Enabled:     true,
		AIConfigID:  "cfg-user100",
	})

	for i := 0; i < 3; i++ {
		st.Enqueue(ctx, &domain.AutoReplyJob{
			ID:              fmt.Sprintf("j-drain-%d", i),
			OwnerUserID:     100,
			ChatID:          "-1001",
			SourceMessageID: int64(100 + i),
			InboundText:     "ordered",
		})
	}

	runErr := make(chan error, 1)
	go func() { runErr <- w.Run(ctx, 0) }()

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		snd.mu.Lock()
		calls := snd.calls
		snd.mu.Unlock()
		if calls >= 3 {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	cancel()
	select {
	case err := <-runErr:
		if err != context.Canceled {
			t.Errorf("Run returned %v, want Canceled", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not exit after cancel")
	}

	snd.mu.Lock()
	calls := snd.calls
	snd.mu.Unlock()
	if calls != 3 {
		t.Errorf("expected 3 replies flushed before exit, got %d", calls)
	}
}

// blockingProvider measures real in-flight concurrency: each ChatComplete
// blocks until the test releases it, so the concurrency cap is observed at
// saturation rather than raced against fast fakes.
type blockingProvider struct {
	gate        chan struct{}
	inFlight    *int32
	maxInFlight *int32
}

func (b blockingProvider) Rewrite(ctx context.Context, cfg *aiconfig.AIConfig, text string) (string, error) {
	return "reply", nil
}

func (b blockingProvider) ChatComplete(ctx context.Context, cfg *aiconfig.AIConfig, msgs []aiprovider.Message, temp float64) (string, error) {
	cur := atomic.AddInt32(b.inFlight, 1)
	for {
		old := atomic.LoadInt32(b.maxInFlight)
		if cur <= old || atomic.CompareAndSwapInt32(b.maxInFlight, old, cur) {
			break
		}
	}
	<-b.gate
	atomic.AddInt32(b.inFlight, -1)
	return "reply", nil
}

func (b blockingProvider) HealthCheck(ctx context.Context, cfg *aiconfig.AIConfig) (bool, error) {
	return true, nil
}

func TestWorkerRunBoundedPoolNeverExceedsConcurrency(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	dir := t.TempDir()
	st, err := store.NewSQLiteStore(context.Background(), filepath.Join(dir, "pva.db"))
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	st.SaveSettings(ctx, &domain.AutoReplySettings{
		OwnerUserID: 100, Enabled: true, AIConfigID: "cfg-user100",
	})

	gate := make(chan struct{})
	var inFlight, maxInFlight int32
	provider := blockingProvider{gate: gate, inFlight: &inFlight, maxInFlight: &maxInFlight}

	factory := aiprovider.NewFactory()
	factory.Register("openai", func() aiprovider.Provider { return provider })
	aiSt := &fakeAIStore{configs: map[string]*aiconfig.AIConfig{
		"cfg-user100": {ID: "cfg-user100", OwnerUserID: 100, Provider: "openai", IsEnabled: true},
	}}
	snd := &fakeSender{}

	w := engine.NewWorker(st, aiSt, factory, snd, "w-1", engine.Options{
		Concurrency:  2,
		PollInterval: 5 * time.Millisecond,
	})

	for i := 0; i < 20; i++ {
		st.Enqueue(ctx, &domain.AutoReplyJob{
			ID:              fmt.Sprintf("j-bounded-%d", i),
			OwnerUserID:     100,
			ChatID:          fmt.Sprintf("-%d", 2000+i),
			SourceMessageID: int64(i),
			InboundText:     "x",
		})
	}

	runErr := make(chan error, 1)
	go func() { runErr <- w.Run(ctx, 0) }()

	// Wait until the pool has certainly saturated: both slots are full and
	// blocked, so the cap is measured at saturation.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if atomic.LoadInt32(&maxInFlight) >= 2 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Release slots one at a time so the pool stays saturated across the
	// whole drain, then let the rest flush.
	go func() {
		for i := 0; i < 20; i++ {
			gate <- struct{}{}
			time.Sleep(5 * time.Millisecond)
		}
		close(gate)
	}()

	// Wait for every reply (or fail on timeout).
	deadline = time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		snd.mu.Lock()
		calls := snd.calls
		snd.mu.Unlock()
		if calls >= 20 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}

	cancel()
	select {
	case <-runErr:
	case <-time.After(10 * time.Second):
		t.Fatal("Run did not exit")
	}

	if max := atomic.LoadInt32(&maxInFlight); max > 2 {
		t.Errorf("max in-flight ChatComplete = %d, want <= 2 (concurrency cap)", max)
	}
	if max := atomic.LoadInt32(&maxInFlight); max < 2 {
		t.Errorf("max in-flight = %d, want 2: the pool should actually saturate", max)
	}
	snd.mu.Lock()
	calls := snd.calls
	snd.mu.Unlock()
	if calls != 20 {
		t.Errorf("expected 20 replies, got %d", calls)
	}
}
