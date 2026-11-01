package delivery

import (
	"context"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph/fakegateway"
)

// The fence-ordering regression suite.
//
// Rule (see Engine.Process): the send-side fence is claimed as the LAST step
// before the first Telegram side effect. Anything that can decide "do not
// send" without touching Telegram — a paused rule, a rule-store outage, a
// filter drop, a rate-limited target — must run first, so a job only ever
// holds a CLAIMED fence row when it is genuinely about to dispatch.
//
// The failure this pins: the fence used to be claimed before those checks, so
// a deferred job waited in RETRY_WAIT with a CLAIMED fence that the sweep
// then turned UNKNOWN — blocking a delivery that never touched Telegram until
// a human reconciled it.

// --- controllable rule store ------------------------------------------------ //

type fixedRules struct {
	rule   *rule.ForwardRule
	filter *rule.FilterRule
	err    error
}

func (f *fixedRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	if f.err != nil {
		return nil, nil, f.err
	}
	if f.rule == nil {
		return nil, nil, nil
	}
	return f.rule, f.filter, nil
}

// fenceProbeStore decorodes the real store and records whether the fence was
// touched at all, so a test can prove a preflight rejection never claimed it.
type fenceProbeStore struct {
	queue.Store
	fenceClaims   int
	fenceOutcomes []string
}

func (f *fenceProbeStore) FenceClaim(ctx context.Context, key job.AttemptKey, workerID string, leaseUntil float64) (job.FenceOutcome, *job.DeliveryAttempt, error) {
	f.fenceClaims++
	out, row, err := f.Store.FenceClaim(ctx, key, workerID, leaseUntil)
	f.fenceOutcomes = append(f.fenceOutcomes, string(out))
	return out, row, err
}

func newFenceHarness(t *testing.T, rs rule.Store) (*harness, *fenceProbeStore) {
	t.Helper()
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, rs, gw, nil, log, metrics, "w-fence", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})
	probe := &fenceProbeStore{Store: store}
	engine.store = probe
	return &harness{store: store, gateway: gw, engine: engine, metrics: metrics, log: log}, probe
}

// --- the regression cases --------------------------------------------------- //

// TestPausedRuleDoesNotClaimFence: a paused rule defers the job, and no fence
// row may exist afterwards — the job must stay retryable forever, not be
// blocked by an unresolved CLAIMED send.
func TestPausedRuleDoesNotClaimFence(t *testing.T) {
	h, probe := newFenceHarness(t, &fixedRules{rule: &rule.ForwardRule{
		ID: "r1", IsActive: true, IsPaused: true, PausedUntil: time.Now().Add(time.Hour).Unix(),
	}})
	j := &job.DeliveryJob{
		ID: "r1:c:1:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "hi"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}

	if probe.fenceClaims != 0 {
		t.Fatalf("a paused rule claimed the send fence %d time(s); the fence must only be claimed when a send is imminent", probe.fenceClaims)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusRetryWait))
	// And the fence row for the logical send must not exist at all.
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 1, TargetChatID: "t"}
	if row := fenceRowFor(t, h.store, key); row != "" {
		t.Errorf("paused rule left a fence row behind: %s", row)
	}
	// The attempt budget is untouched: a pause is never a delivery failure.
	assertAttempts(t, h.store, j.ID, 0)
}

// TestRuleStoreOutageDefersWithoutClaimingFence: a database error on the rule
// read must defer the job safely. Poisoning the fence here would mean every
// transient rule-store hiccup permanently blocks the delivery.
func TestRuleStoreOutageDefersWithoutClaimingFence(t *testing.T) {
	h, probe := newFenceHarness(t, &fixedRules{err: context.DeadlineExceeded})
	j := &job.DeliveryJob{
		ID: "r1:c:2:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 2,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "hi"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if probe.fenceClaims != 0 {
		t.Fatalf("a rule-store outage claimed the send fence %d time(s)", probe.fenceClaims)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusRetryWait))
	for _, c := range h.gateway.Recorded() {
		if c.Op == "SendMessage" {
			t.Error("a rule-store outage dispatched a Telegram send")
		}
	}
}

// TestFilterRejectionDoesNotClaimFence: a filter drop is a permanent decision
// about the content; it must fail the job cleanly, never leaving an
// unresolved CLAIMED fence behind.
func TestFilterRejectionDoesNotClaimFence(t *testing.T) {
	h, probe := newFenceHarness(t, &fixedRules{
		rule:   &rule.ForwardRule{ID: "r1", IsActive: true},
		filter: &rule.FilterRule{IsEnabled: true, BlacklistKeywords: []string{"blocked"}},
	})
	j := &job.DeliveryJob{
		ID: "r1:c:3:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 3,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "this is blocked text"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if probe.fenceClaims != 0 {
		t.Fatalf("a filter-rejected job claimed the send fence %d time(s)", probe.fenceClaims)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusFailed))
}

// TestRateLimitedTargetDoesNotClaimFence: a throttled target is a defer; no
// send was imminent, so no fence may be held while the window elapses.
func TestRateLimitedTargetDoesNotClaimFence(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, &fixedRules{rule: &rule.ForwardRule{ID: "r1", IsActive: true}}, gw, nil, log, metrics, "w-fence", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second,
		MaxPerMinutePerTarget: 1, RPCTimeout: 5 * time.Second,
	})
	// Burn the one allowed send for this target so the next job is throttled.
	engine.recordSend("t")

	j := &job.DeliveryJob{
		ID: "r1:c:4:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 4,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "hi"}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, store, j.ID, string(job.StatusRetryWait))
	if got := fenceRowCount(t, store, "r1:c:4:t"); got != 0 {
		t.Errorf("rate-limited job left %d fence row(s) behind", got)
	}
}

// TestExpiredPauseStillDelivers: once a pause's deadline has elapsed the rule
// is no longer paused, so the job proceeds to a normal dispatch (and the
// fence IS claimed, because a send is genuinely imminent).
func TestExpiredPauseStillDelivers(t *testing.T) {
	h, probe := newFenceHarness(t, &fixedRules{rule: &rule.ForwardRule{
		ID: "r1", IsActive: true, IsPaused: true, PausedUntil: time.Now().Add(-time.Minute).Unix(),
	}})
	j := &job.DeliveryJob{
		ID: "r1:c:5:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 5,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "hi"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if probe.fenceClaims != 1 {
		t.Errorf("an expired pause must let the job through to the fence: %d claim(s)", probe.fenceClaims)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
}

// TestGenuineSendStillRequiresFenceClaim: the reordering must not weaken
// duplicate protection — a dispatch still happens under a claimed fence, and
// the confirmed outcome is recorded on the fence row.
func TestGenuineSendStillRequiresFenceClaim(t *testing.T) {
	h, probe := newFenceHarness(t, &fixedRules{rule: &rule.ForwardRule{ID: "r1", IsActive: true}})
	j := &job.DeliveryJob{
		ID: "r1:c:6:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 6,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "hi"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if probe.fenceClaims != 1 {
		t.Errorf("a genuine send must claim the fence exactly once, got %d", probe.fenceClaims)
	}
	if len(probe.fenceOutcomes) != 1 || probe.fenceOutcomes[0] != string(job.FenceDispatch) {
		t.Errorf("fence outcomes = %v, want [dispatch]", probe.fenceOutcomes)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
}

// TestFenceUnavailableDefersWithoutSending: when the fence cannot be reached,
// the send is refused (not dispatched unprotected) and the job defers.
func TestFenceUnavailableDefersWithoutSending(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	engine := New(store, &fixedRules{rule: &rule.ForwardRule{ID: "r1", IsActive: true}}, gw, nil, &testLogger{}, &testMetrics{}, "w-fence", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})
	engine.store = &brokenFenceStore{Store: store}

	j := &job.DeliveryJob{
		ID: "r1:c:7:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 7,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "hi"}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, store, j.ID, string(job.StatusRetryWait))
	for _, c := range gw.Recorded() {
		if c.Op == "SendMessage" {
			t.Error("the worker dispatched a Telegram send without a valid fence claim")
		}
	}
}

// --- helpers ---------------------------------------------------------------- //

func fenceRowFor(t *testing.T, s queue.Store, key job.AttemptKey) string {
	t.Helper()
	db := queue.StoreDB(t, s)
	var status string
	err := db.QueryRow(`SELECT status FROM delivery_attempts WHERE id=?`, key.String()).Scan(&status)
	if err != nil {
		return ""
	}
	return status
}

func fenceRowCount(t *testing.T, s queue.Store, jobID string) int {
	t.Helper()
	db := queue.StoreDB(t, s)
	var n int
	if err := db.QueryRow(`SELECT COUNT(*) FROM delivery_attempts WHERE id=?`, jobID).Scan(&n); err != nil {
		t.Fatalf("count fence rows for %s: %v", jobID, err)
	}
	return n
}

// brokenFenceStore makes FenceClaim fail, simulating a fence-table outage.
type brokenFenceStore struct {
	queue.Store
}

func (b *brokenFenceStore) FenceClaim(ctx context.Context, key job.AttemptKey, workerID string, leaseUntil float64) (job.FenceOutcome, *job.DeliveryAttempt, error) {
	return "", nil, context.DeadlineExceeded
}
