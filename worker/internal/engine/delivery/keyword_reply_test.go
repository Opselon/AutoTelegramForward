package delivery

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph/fakegateway"
)

// keywordRules serves a task with the keyword-reply add-on fully
// configured: confirmation on, a reply text, the entitlement recorded,
// and the word list living in the content policy like every current
// client writes it.
type keywordRules struct {
	rule *rule.ForwardRule
}

func (k keywordRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	if k.rule != nil && k.rule.ID == ruleID {
		return k.rule, nil, nil
	}
	return nil, nil, nil
}

func keywordRuleFixture() *rule.ForwardRule {
	return &rule.ForwardRule{
		ID:       "r-kw",
		IsActive: true,
		Metadata: map[string]any{
			"keyword_confirmation": true,
			"keyword_reply":        "thanks for reading",
			"paid_addons":          map[string]any{"keyword_reply": true},
			"content_policy":       map[string]any{"whitelist_words": []any{"sale"}},
		},
	}
}

// newKeywordHarness builds an engine around a keyword-configured rule,
// the same way newHarness builds one around staticRules{}.
func newKeywordHarness(t *testing.T, r *rule.ForwardRule) (*harness, *keywordRules) {
	t.Helper()
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	rules := keywordRules{rule: r}
	o := Options{NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second}
	engine := New(store, rules, gw, nil, log, metrics, "w-kw", t.TempDir(), o)
	return &harness{store: store, gateway: gw, engine: engine, metrics: metrics, log: log}, &rules
}

// findReplyJob reads the reply row the W4 block enqueued, or nil when no
// such row exists. The reply's job id is the primary's plus the kind.
func findReplyJob(t *testing.T, h *harness, primaryID string) *job.DeliveryJob {
	t.Helper()
	row := storeDBOf(t, h.store).QueryRow(
		`SELECT id, rule_id, source_chat_id, source_message_id,
		        target_chat_id, status, job_kind
		 FROM delivery_jobs WHERE id = ?`, primaryID+":keyword_reply")
	var (
		id, ruleID, sourceChat, target, status, kind string
		sourceMsg                                    int64
	)
	if err := row.Scan(&id, &ruleID, &sourceChat, &sourceMsg, &target, &status, &kind); err != nil {
		return nil
	}
	return &job.DeliveryJob{
		ID: id, RuleID: ruleID, SourceChatID: sourceChat,
		SourceMessageID: sourceMsg, TargetChatID: target,
		Status: job.Status(status), JobKind: kind,
	}
}

// replyPayloadText carries the row's payload fields the W4 contract
// pins: the is_reply marker and the reply text.
func replyPayloadText(t *testing.T, h *harness, replyID string) (string, bool) {
	t.Helper()
	var payload string
	err := storeDBOf(t, h.store).QueryRow(
		`SELECT payload_json FROM delivery_jobs WHERE id = ?`, replyID).Scan(&payload)
	if err != nil {
		return "", false
	}
	var raw map[string]any
	if err := json.Unmarshal([]byte(payload), &raw); err != nil {
		t.Fatalf("decode reply payload %s: %v", replyID, err)
	}
	text, _ := raw["text"].(string)
	isReply, _ := raw["is_reply"].(bool)
	return text, isReply
}

// TestKeywordReplyEnqueuedOnTrigger: a forward whose text matches the
// task's whitelist gets a keyword_reply job enqueued to the SAME target,
// and the forward itself still lands SENT.
//
// This is the W4 contract: the trigger never delays or drops the forward
// (pipeline.py:1381-1411); it only schedules the secondary reply.
func TestKeywordReplyEnqueuedOnTrigger(t *testing.T) {
	h, _ := newKeywordHarness(t, keywordRuleFixture())
	j := &job.DeliveryJob{
		ID: "r-kw:c:1:t", RuleID: "r-kw", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		JobKind: "forward",
		Payload: *jobPayload(map[string]any{"text": "big sale today", "effective_text": "big sale today"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Fatalf("sent = %d, want 1 (the forward must still deliver)", h.metrics.sent)
	}

	reply := findReplyJob(t, h, j.ID)
	if reply == nil {
		t.Fatal("no keyword_reply job was enqueued for a matching text")
	}
	if reply.JobKind != "keyword_reply" {
		t.Fatalf("kind = %q, want keyword_reply", reply.JobKind)
	}
	if reply.TargetChatID != "t" {
		t.Fatalf("target = %q, want the forward's target", reply.TargetChatID)
	}
	if reply.Status != job.StatusPending {
		t.Fatalf("status = %v, want PENDING", reply.Status)
	}
	if text, isReply := replyPayloadText(t, h, reply.ID); text != "thanks for reading" || !isReply {
		t.Fatalf("reply payload = (%q, is_reply=%v), want the reply text and the marker", text, isReply)
	}
}

// TestKeywordReplyNotEnqueuedWithoutTrigger: a text that matches nothing
// enqueues nothing — the add-on never invents a reply.
func TestKeywordReplyNotEnqueuedWithoutTrigger(t *testing.T) {
	h, _ := newKeywordHarness(t, keywordRuleFixture())
	j := &job.DeliveryJob{
		ID: "r-kw:c:2:t", RuleID: "r-kw", SourceChatID: "c", SourceMessageID: 2,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		JobKind: "forward",
		Payload: *jobPayload(map[string]any{"text": "nothing to see", "effective_text": "nothing to see"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Fatalf("sent = %d, want 1", h.metrics.sent)
	}
	if got := findReplyJob(t, h, j.ID); got != nil {
		t.Fatalf("a reply was enqueued without a trigger: %+v", got)
	}
}

// TestKeywordReplyRespectsTheAddonGate: the entitlement gate is part of
// the W4 decision, and the harness's rule carries the metadata DIRECTLY —
// no hand-built reply.Settings — so a projection bypass would pass here
// while production diverges (the trap the parity rules warn about).
func TestKeywordReplyRespectsTheAddonGate(t *testing.T) {
	r := keywordRuleFixture()
	r.Metadata["paid_addons"] = map[string]any{"keyword_reply": false}
	h, _ := newKeywordHarness(t, r)
	j := &job.DeliveryJob{
		ID: "r-kw:c:3:t", RuleID: "r-kw", SourceChatID: "c", SourceMessageID: 3,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		JobKind: "forward",
		Payload: *jobPayload(map[string]any{"text": "big sale today", "effective_text": "big sale today"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Fatalf("sent = %d, want 1 (the forward delivers regardless)", h.metrics.sent)
	}
	if got := findReplyJob(t, h, j.ID); got != nil {
		t.Fatalf("a reply was enqueued without the paid-addon entitlement: %+v", got)
	}
}

// TestKeywordReplyDoesNotChain: a keyword_reply job never enqueues
// another reply. Without this the trigger would loop on its own
// deliverable.
func TestKeywordReplyDoesNotChain(t *testing.T) {
	h, _ := newKeywordHarness(t, keywordRuleFixture())
	// A reply job whose OWN text happens to contain the keyword: the
	// trigger must not fire on it.
	j := &job.DeliveryJob{
		ID: "r-kw:c:4:t:keyword_reply", RuleID: "r-kw", SourceChatID: "c", SourceMessageID: 4,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		JobKind: "keyword_reply",
		Payload: *jobPayload(map[string]any{"text": "sale!", "is_reply": true, "effective_text": "sale!"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Fatalf("sent = %d, want 1 (the reply itself must deliver)", h.metrics.sent)
	}
	if got := findReplyJob(t, h, j.ID); got != nil {
		t.Fatalf("a reply chained out of a reply: %+v", got)
	}
}

// TestKeywordReplyIsIdempotent: running the same forward twice (a replay
// of the decision after a crash) enqueues the reply exactly once — the
// UNIQUE (rule, source, message, target, job_kind) key makes the second
// enqueue a no-op, which is the Python enqueue's own contract.
func TestKeywordReplyIsIdempotent(t *testing.T) {
	h, _ := newKeywordHarness(t, keywordRuleFixture())
	j := &job.DeliveryJob{
		ID: "r-kw:c:5:t", RuleID: "r-kw", SourceChatID: "c", SourceMessageID: 5,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		JobKind: "forward",
		Payload: *jobPayload(map[string]any{"text": "big sale today", "effective_text": "big sale today"}),
	}
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}

	// Re-run the enqueue decision on the same primary job: a replay
	// after a crash between the send and the commit, or a re-claimed
	// job, must not produce a second reply row.
	h.engine.enqueueKeywordReply(context.Background(), j, keywordRuleFixture(), h.log)
	h.engine.enqueueKeywordReply(context.Background(), j, keywordRuleFixture(), h.log)

	count := 0
	if err := storeDBOf(t, h.store).QueryRow(
		`SELECT COUNT(*) FROM delivery_jobs WHERE job_kind = 'keyword_reply'`).Scan(&count); err != nil {
		t.Fatalf("count replies: %v", err)
	}
	if count != 1 {
		t.Fatalf("keyword_reply rows = %d, want 1 (the enqueue must be idempotent)", count)
	}
}
