package delivery

import (
	"context"
	"database/sql"
	"strings"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph"
	"atf/worker/internal/telegraph/fakegateway"
)

// --- test doubles ---------------------------------------------------------- //

type testLogger struct{ lines []string }

func (l *testLogger) Info(msg string, args ...any)  { l.lines = append(l.lines, "INFO "+msg) }
func (l *testLogger) Warn(msg string, args ...any)  { l.lines = append(l.lines, "WARN "+msg) }
func (l *testLogger) Error(msg string, args ...any) { l.lines = append(l.lines, "ERROR "+msg) }

type testMetrics struct{ sent, retried, failed, unknown int }

func (m *testMetrics) Outcome(kind string) {
	switch kind {
	case "sent":
		m.sent++
	case "retried":
		m.retried++
	case "failed":
		m.failed++
	case "unknown":
		m.unknown++
	}
}
func (m *testMetrics) ObserveRPC(string, float64)  {}
func (m *testMetrics) ObserveWait(float64)         {}

// staticRules returns a live, unpaused rule and no persisted filter.
type staticRules struct{}

func (staticRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	return &rule.ForwardRule{ID: ruleID, IsActive: true}, nil, nil
}

// deletedRules simulates the task being deleted while its job sat in the queue.
type deletedRules struct{}

func (deletedRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	return nil, nil, nil
}

type harness struct {
	store   queue.Store
	gateway *fakegateway.Fake
	engine  *Engine
	metrics *testMetrics
	log     *testLogger
}

func newHarness(t *testing.T, opts ...Options) *harness {
	t.Helper()
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	o := Options{NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second}
	if len(opts) > 0 {
		o = opts[0]
	}
	dir := t.TempDir()
	engine := New(store, staticRules{}, gw, log, metrics, "w-test", dir, o)
	return &harness{store: store, gateway: gw, engine: engine, metrics: metrics, log: log}
}

// enqueue inserts a job directly into delivery_jobs so the fence and the
// queue agree about its existence.
func (h *harness) enqueue(t *testing.T, j *job.DeliveryJob) {
	t.Helper()
	insertJobRow(t, h.store, j)
}

func jobPayload(extra map[string]any) *job.Payload {
	raw := map[string]any{"session_id": "s1"}
	for k, v := range extra {
		raw[k] = v
	}
	return job.NewPayload(raw)
}

// --- the scenarios --------------------------------------------------------- //

// TestNormalTextForward: a plain text job is dispatched as a copy and lands
// SENT, with the mapping row carrying the confirmed target id.
func TestNormalTextForward(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:1:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{"text": "hello world"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Errorf("sent = %d, want 1", h.metrics.sent)
	}
	calls := h.gateway.Recorded()
	if len(calls) == 0 {
		t.Fatal("no gateway call was made")
	}
	gotText := ""
	for _, c := range calls {
		if c.Op == "SendMessage" {
			gotText = c.Text
		}
	}
	if gotText != "hello world" {
		t.Errorf("sent text = %q, want the job's transformed text", gotText)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
}

// TestMediaForwardCopyThenFallback: a media job is copied, and when the
// source forbids forwarding the copy path falls through to the re-upload
// bypass (only because the task opted in via fallback_enabled).
func TestMediaForwardCopyThenFallback(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:2:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 2,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "caption", "has_media": true, "media_type": "photo",
			"fallback_enabled": true,
		}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))

	h.gateway.Reset()
	h.gateway.RestrictedSource = true
	j2 := &job.DeliveryJob{
		ID: "r1:c:3:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 3,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "caption", "has_media": true, "media_type": "photo",
			"fallback_enabled": true,
		}),
	}
	h.enqueue(t, j2)
	if err := h.engine.Process(context.Background(), j2); err != nil {
		t.Fatalf("process restricted: %v", err)
	}
	// The restricted source must NOT fail the job: the fallback delivers it.
	assertJobStatus(t, h.store, j2.ID, string(job.StatusSent))
	downs := 0
	for _, c := range h.gateway.Recorded() {
		if c.Op == "DownloadMedia" {
			downs++
		}
	}
	if downs == 0 {
		t.Error("restricted source with fallback_enabled did not stage a re-upload download")
	}
}

// TestFallbackOptOutIsRespected: with fallback_enabled false, a restricted
// source must FAIL the job instead of being rescued by a bypass the owner
// did not ask for.
func TestFallbackOptOutIsRespected(t *testing.T) {
	h := newHarness(t)
	h.gateway.RestrictedSource = true
	j := &job.DeliveryJob{
		ID: "r1:c:4:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 4,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "caption", "has_media": true, "media_type": "photo",
			"fallback_enabled": false,
		}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusFailed))
	if h.metrics.failed != 1 {
		t.Errorf("failed = %d, want 1", h.metrics.failed)
	}
}

// TestFloodWaitDefersWithoutSpendingAttempt: Telegram demanded a wait; the
// job is deferred for exactly that window and its attempt budget is intact.
// Spending an attempt on a rate limit is what turns a short suspension into
// a dead-lettered, "permanently failed" healthy forward.
func TestFloodWaitDefersWithoutSpendingAttempt(t *testing.T) {
	h := newHarness(t)
	h.gateway.ForwardFailsWith = &telegraph.DeliveryFailure{
		Code: "FLOOD_WAIT", ErrorType: "FLOOD_WAIT_X", RetryAfterSeconds: 17,
		Message: "too many requests",
	}
	j := &job.DeliveryJob{
		ID: "r1:c:5:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 5,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "hi", "forward_mode": "DIRECT_FORWARD",
		}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusRetryWait))
	assertAttempts(t, h.store, j.ID, 0)
	if h.metrics.retried != 1 {
		t.Errorf("retried = %d, want 1", h.metrics.retried)
	}
}

// TestTimeoutIsUnknownNeverRetriedBlindly: a response that never came back
// means Telegram may have accepted the send. The job is marked UNKNOWN —
// never FAILED, never automatically re-dispatched. Blindly retrying here is
// exactly how duplicate messages are made.
func TestTimeoutIsUnknownNeverRetriedBlindly(t *testing.T) {
	h := newHarness(t)
	h.gateway.ForwardFailsWith = &telegraph.DeliveryFailure{
		Code: "NETWORK_TIMEOUT", Message: "deadline exceeded",
	}
	j := &job.DeliveryJob{
		ID: "r1:c:6:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 6,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "hi", "forward_mode": "DIRECT_FORWARD"}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusUnknown))
	if h.metrics.unknown != 1 {
		t.Errorf("unknown = %d, want 1", h.metrics.unknown)
	}
	// And the fence must say UNKNOWN too: a later claim resolves to
	// "unknown", not "dispatch", so no recovery loop can re-send it.
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 6, TargetChatID: "t"}
	outcome, _, err := h.store.FenceClaim(context.Background(), key, "other", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("fence claim: %v", err)
	}
	if outcome != job.FenceUnknown {
		t.Errorf("fence outcome after timeout = %s, want unknown", outcome)
	}
}

// TestDuplicateJobSubmissionIsNotDuplicateDelivery: re-processing the same
// logical job after a crash must not re-send. The fence's already_sent
// outcome closes the job with the recorded target id and nothing is
// dispatched.
func TestDuplicateJobSubmissionIsNotDuplicateDelivery(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:7:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 7,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "once"}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("first process: %v", err)
	}
	sends := len(h.gateway.Recorded())

	// A replay: the job row says PENDING again (a crash between Telegram's
	// accept and the commit), but the fence remembers the confirmed send.
	j2 := &job.DeliveryJob{
		ID: "r1:c:7:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 7,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "once"}),
	}
	h.enqueue(t, j2)
	if err := h.engine.Process(context.Background(), j2); err != nil {
		t.Fatalf("replay process: %v", err)
	}
	if got := len(h.gateway.Recorded()); got != sends {
		t.Errorf("replay performed %d gateway call(s), want %d (no re-send)", got, sends)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
}

// TestDeletedRuleDropsJob: a job whose task was deleted while it sat in the
// queue must not forward a message the owner has already removed.
func TestDeletedRuleDropsJob(t *testing.T) {
	h := newHarness(t)
	h.engine.rules = deletedRules{}
	j := &job.DeliveryJob{
		ID: "r1:c:8:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 8,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "hi"}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusFailed))
	if len(h.gateway.Recorded()) != 0 {
		t.Error("a deleted rule's job was dispatched to Telegram")
	}
}

// TestKeywordReplyIsTextOnly: a keyword-reply deliverable never re-copies or
// re-forwards the source message's media, even when the payload carries it.
func TestKeywordReplyIsTextOnly(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:10:t:keyword_reply", RuleID: "r1", SourceChatID: "c",
		SourceMessageID: 10, TargetChatID: "t", Status: job.StatusPending,
		MaxAttempts: 5, JobKind: "keyword_reply",
		Payload: *jobPayload(map[string]any{
			"text": "matched!", "is_reply": true, "has_media": true,
			"media_type": "photo", "forward_mode": "COPY_MESSAGE",
		}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
	calls := h.gateway.Recorded()
	if len(calls) != 1 || calls[0].Op != "SendMessage" {
		t.Errorf("reply dispatched as %v, want exactly one SendMessage", calls)
	}
}

// TestForumTopicRouting: a configured topic_id must reach the transport — a
// message silently landing in the General topic is the bug this prevents.
func TestForumTopicRouting(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:11:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 11,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "topic", "topic_id": float64(77)}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	for _, c := range h.gateway.Recorded() {
		if c.Op == "SendMessage" && c.ThreadID != 77 {
			t.Errorf("thread id = %d, want 77", c.ThreadID)
		}
	}
}

// TestBrandingHopForwardsFromC: the A->C->B hop must deliver a FORWARD of
// C's post (so the header names C), never a forward of the original source.
func TestBrandingHopForwardsFromC(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:12:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 12,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "branded",
			"branding": map[string]any{
				"chat_id":      "-100C",
				"message_id":   float64(55),
				"force_forward": true,
			},
		}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
	var sawForward bool
	for _, c := range h.gateway.Recorded() {
		if c.Op == "ForwardMessages" {
			sawForward = true
			if c.ChatID == 12 && c.MessageID == 12 { // from the original source
				t.Error("hop forwarded from the ORIGINAL source, not from C")
			}
		}
	}
	if !sawForward {
		t.Error("branding hop did not perform a forward of C's post")
	}
}

// TestSplitAlbumResumesNotDuplicates: a split album records which items
// Telegram confirmed; a retry re-sends only the missing ones (audit finding
// E2E-5: the retry used to duplicate every item that had already landed).
func TestSplitAlbumResumesNotDuplicates(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:13:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 13,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "album", "media_group_id": "g1",
			"album_message_ids": []any{float64(1), float64(2), float64(3)},
			"album_mode": "split",
		}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
	firstRound := 0
	for _, c := range h.gateway.Recorded() {
		if c.Op == "CopyMessage" {
			firstRound++
		}
	}
	if firstRound != 3 {
		t.Fatalf("split album sent %d items, want 3", firstRound)
	}

	// The real E2E-5 shape, on a DISTINCT logical delivery (a different
	// source message, so the first job's confirmed fence does not resolve
	// this one): the second item fails partway through the split. The engine
	// records item 1's confirmation, the job is retried, and the retry must
	// send ONLY the missing items — never re-send what Telegram already
	// accepted.
	h.gateway.Reset()
	h.gateway.CopyFailOnNth = 2
	partial := &job.DeliveryJob{
		ID: "r1:c:14:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 14,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "album", "media_group_id": "g2",
			"album_message_ids": []any{float64(11), float64(12), float64(13)},
			"album_mode":       "split",
		}),
	}
	h.enqueue(t, partial)
	if err := h.engine.Process(context.Background(), partial); err != nil {
		t.Fatalf("partial process: %v", err)
	}
	// The failed split must be retried (budget-bounded), not dead-lettered.
	assertJobStatus(t, h.store, partial.ID, string(job.StatusRetryWait))
	// And item 1's confirmation must be persisted into the job payload.
	assertPayloadHas(t, h.store, partial.ID, `"11":`)

	// The retry re-reads the job from the store (the run loop always
	// re-claims), so it carries item 11's confirmation in its payload: only
	// items 12 and 13 may be dispatched — item 11 never goes to Telegram a
	// second time.
	h.gateway.Reset()
	h.gateway.CopyFailOnNth = 0
	db := queue.StoreDB(t, h.store)
	if _, err := db.Exec(`UPDATE delivery_jobs SET status='PENDING', next_retry_at=0 WHERE id=?`, partial.ID); err != nil {
		t.Fatalf("re-arm retry: %v", err)
	}
	claimed, err := h.store.ClaimBatch(context.Background(), "w-resume", 5, time.Hour)
	if err != nil {
		t.Fatalf("re-claim: %v", err)
	}
	var resumed *job.DeliveryJob
	for _, c := range claimed {
		if c.ID == partial.ID {
			resumed = c
		}
	}
	if resumed == nil {
		t.Fatal("the partial split job was not re-claimable")
	}
	if err := h.engine.Process(context.Background(), resumed); err != nil {
		t.Fatalf("resume process: %v", err)
	}
	reDispatched := []int64{}
	for _, c := range h.gateway.Recorded() {
		if c.Op == "CopyMessage" {
			reDispatched = append(reDispatched, c.MessageID)
		}
	}
	if len(reDispatched) != 2 {
		t.Errorf("resume re-sent %d item(s) %v, want exactly the 2 missing items", len(reDispatched), reDispatched)
	}
	for _, id := range reDispatched {
		if id == 11 {
			t.Error("resume re-sent item 11, which Telegram had already accepted")
		}
	}
	assertJobStatus(t, h.store, partial.ID, string(job.StatusSent))
}

// --- helpers shared with the queue tests ------------------------------------ //

func assertJobStatus(t *testing.T, s queue.Store, jobID, want string) {
	t.Helper()
	got := jobStatus(t, s, jobID)
	if got != want {
		t.Errorf("job %s status = %s, want %s", jobID, got, want)
	}
}

// assertPayloadHas checks the job's persisted payload contains a marker.
func assertPayloadHas(t *testing.T, s queue.Store, jobID, marker string) {
	t.Helper()
	db := queue.StoreDB(t, s)
	var payload string
	if err := db.QueryRow(`SELECT payload_json FROM delivery_jobs WHERE id=?`, jobID).Scan(&payload); err != nil {
		t.Fatalf("read payload of %s: %v", jobID, err)
	}
	if !strings.Contains(payload, marker) {
		t.Errorf("payload of %s = %q, want it to contain %s", jobID, payload, marker)
	}
}

func assertAttempts(t *testing.T, s queue.Store, jobID string, want int) {
	t.Helper()
	got := attempts(t, s, jobID)
	if got != want {
		t.Errorf("job %s attempts = %d, want %d", jobID, got, want)
	}
}

func jobStatus(t *testing.T, s queue.Store, jobID string) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	claimed, err := s.ClaimBatch(ctx, "probe", 50, time.Hour)
	if err != nil {
		t.Fatalf("probe claim: %v", err)
	}
	for _, c := range claimed {
		if c.ID == jobID {
			return string(c.Status)
		}
	}
	return directStatus(t, s, jobID)
}

// directStatus reads the row without going through the claim path.
func directStatus(t *testing.T, s queue.Store, jobID string) string {
	t.Helper()
	db := storeDBOf(t, s)
	var status string
	err := db.QueryRow(`SELECT status FROM delivery_jobs WHERE id=?`, jobID).Scan(&status)
	if err != nil {
		t.Fatalf("read status of %s: %v", jobID, err)
	}
	return status
}

func attempts(t *testing.T, s queue.Store, jobID string) int {
	t.Helper()
	db := storeDBOf(t, s)
	var n int
	if err := db.QueryRow(`SELECT attempts FROM delivery_jobs WHERE id=?`, jobID).Scan(&n); err != nil {
		t.Fatalf("read attempts of %s: %v", jobID, err)
	}
	return n
}

// openTestStore builds a fresh SQLite store carrying the Core's queue
// schema, identical to the queue package's own fixture so the delivery
// engine is exercised against the real table shapes.
func openTestStore(t *testing.T) queue.Store {
	t.Helper()
	return queue.NewTestStore(t)
}

// insertJobRow writes a delivery_jobs row for the test.
func insertJobRow(t *testing.T, s queue.Store, j *job.DeliveryJob) {
	t.Helper()
	queue.InsertJob(t, s, j)
}

func storeDBOf(t *testing.T, s queue.Store) *sql.DB {
	t.Helper()
	return queue.StoreDB(t, s)
}

// TestMediaScratchIsPerJob: two concurrent re-uploads must never collide on
// the same staged file — the scratch path is unique per job by construction.
func TestMediaScratchIsPerJob(t *testing.T) {
	h := newHarness(t)
	h.gateway.RestrictedSource = true
	for i := 0; i < 2; i++ {
		j := &job.DeliveryJob{
			ID: "r1:c:" + string(rune('A'+i)) + ":t", RuleID: "r1",
			SourceChatID: "c", SourceMessageID: int64(30 + i),
			TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
			Payload: *jobPayload(map[string]any{
				"text": "m", "has_media": true, "media_type": "photo",
				"fallback_enabled": true,
			}),
		}
		h.enqueue(t, j)
		if err := h.engine.Process(context.Background(), j); err != nil {
			t.Fatalf("process %d: %v", i, err)
		}
	}
	paths := map[string]bool{}
	for _, c := range h.gateway.Recorded() {
		if c.Op == "DownloadMedia" && c.MediaPath != "" {
			paths[c.MediaPath] = true
		}
	}
	if len(paths) != 2 {
		t.Errorf("distinct scratch paths = %d, want 2 (per-job uniqueness)", len(paths))
	}
}


