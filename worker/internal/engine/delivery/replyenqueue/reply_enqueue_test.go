package replyenqueue

import (
	"testing"

	"atf/worker/internal/domain/job"
)

// primaryFixture is a representative forward job: the keys the dispatch
// path reads, an album shape, and a branding hop — everything a reply
// must NOT inherit.
func primaryFixture() *job.DeliveryJob {
	raw := map[string]any{
		"session_id":         "sess-1",
		"source_chat_id":     "-1001",
		"source_message_id":  42,
		"target_chat_id":     "-1002",
		"text":               "big sale today",
		"effective_text":     "big sale today",
		"has_media":          true,
		"media_type":         "photo",
		"media_group_id":     "alb_A",
		"album_message_ids":  []any{41, 42, 43},
		"album_mode":         "album",
		"forward_mode":       "COPY_MESSAGE",
		"content_mode":       "AUTO",
		"delivery_transport": "USER",
		"branding":           map[string]any{"chat_id": "-1009", "message_id": 7},
		"correlation_id":     "corr-1",
		"owner_user_id":      77,
		"topic_id":           3,
	}
	return &job.DeliveryJob{
		ID:              "r1:-1001:42:-1002",
		RuleID:          "r1",
		SourceChatID:    "-1001",
		SourceMessageID: 42,
		TargetChatID:    "-1002",
		Status:          job.StatusClaimed,
		Attempts:        1,
		MaxAttempts:     5,
		JobKind:         "forward",
		DeliveryStage:   "BRANDED",
		Payload:         *job.NewPayload(raw),
	}
}

// TestReplyIsADistinctDeliverable pins the job-level differences: the
// id and kind change, the identity fields are preserved, and the
// attempts/state of the primary never leak into the reply.
func TestReplyIsADistinctDeliverable(t *testing.T) {
	primary := primaryFixture()
	reply := Reply(primary, "thanks for reading")

	if reply == nil {
		t.Fatal("Reply returned nil for a non-nil primary")
	}
	if reply.ID != "r1:-1001:42:-1002:keyword_reply" {
		t.Fatalf("reply id = %q, want the kind-suffixed primary id", reply.ID)
	}
	if reply.JobKind != Kind {
		t.Fatalf("reply kind = %q, want %q", reply.JobKind, Kind)
	}
	if reply.Status != job.StatusPending {
		t.Fatalf("reply status = %v, want PENDING", reply.Status)
	}
	if reply.Attempts != 0 {
		t.Fatalf("reply attempts = %d, want 0", reply.Attempts)
	}
	if reply.ErrorDetail != "" {
		t.Fatalf("reply error detail = %q, want empty", reply.ErrorDetail)
	}
	// The primary is untouched: the enqueue does not mutate the job it
	// is derived from.
	if primary.ID != "r1:-1001:42:-1002" || primary.JobKind != "forward" {
		t.Fatalf("primary was mutated: id=%q kind=%q", primary.ID, primary.JobKind)
	}
}

// TestReplyDropsTheMediaAndBrandingShape pins what a reply must never
// re-deliver: the album and media keys that would make the dispatch path
// copy or forward the source message's media, and the branding hop that
// would route it through channel C.
func TestReplyDropsTheMediaAndBrandingShape(t *testing.T) {
	reply := Reply(primaryFixture(), "thanks")

	if reply.Payload.MediaGroupID() != "" {
		t.Fatalf("reply carries media_group_id %q — a reply is text-only", reply.Payload.MediaGroupID())
	}
	if len(reply.Payload.AlbumMessageIDs()) != 0 {
		t.Fatalf("reply carries album_message_ids %v — a reply is text-only", reply.Payload.AlbumMessageIDs())
	}
	if reply.Payload.HasMedia() {
		t.Fatal("reply claims has_media — a reply is text-only")
	}
	if reply.Payload.MediaType() != "" {
		t.Fatalf("reply carries media_type %q — a reply is text-only", reply.Payload.MediaType())
	}
	if reply.Payload.Branding() != nil {
		t.Fatalf("reply carries a branding hop — pipeline.py:1387 excludes C from replies")
	}
	if !reply.Payload.IsReply() {
		t.Fatal("reply payload lacks the is_reply marker the dispatch path reads")
	}
}

// TestReplyKeepsTheRoutingIdentity pins what a reply MUST keep: the same
// target, session, correlation id, topic and owner the forward carried,
// and the same reply text the classifier produced.
func TestReplyKeepsTheRoutingIdentity(t *testing.T) {
	primary := primaryFixture()
	reply := Reply(primary, "thanks for reading")

	if reply.TargetChatID != primary.TargetChatID {
		t.Fatalf("target = %q, want the primary's %q", reply.TargetChatID, primary.TargetChatID)
	}
	if reply.SourceChatID != primary.SourceChatID || reply.SourceMessageID != primary.SourceMessageID {
		t.Fatalf("source = %q:%d, want the primary's %q:%d",
			reply.SourceChatID, reply.SourceMessageID, primary.SourceChatID, primary.SourceMessageID)
	}
	if reply.RuleID != primary.RuleID {
		t.Fatalf("rule id = %q, want %q", reply.RuleID, primary.RuleID)
	}
	if reply.Payload.SessionID() != primary.Payload.SessionID() {
		t.Fatalf("session id = %q, want %q", reply.Payload.SessionID(), primary.Payload.SessionID())
	}
	if reply.Payload.CorrelationID() != primary.Payload.CorrelationID() {
		t.Fatalf("correlation id = %q, want %q", reply.Payload.CorrelationID(), primary.Payload.CorrelationID())
	}
	if reply.Payload.TopicID() != primary.Payload.TopicID() {
		t.Fatalf("topic id = %d, want %d", reply.Payload.TopicID(), primary.Payload.TopicID())
	}
	if reply.Payload.OwnerUserID() != primary.Payload.OwnerUserID() {
		t.Fatalf("owner id = %d, want %d", reply.Payload.OwnerUserID(), primary.Payload.OwnerUserID())
	}
	if reply.Payload.Text() != "thanks for reading" {
		t.Fatalf("text = %q, want the reply text", reply.Payload.Text())
	}
}

// TestReplyTextStringify pins the fired/not-fired vocabulary the enqueue
// decision consumes.
func TestReplyTextStringify(t *testing.T) {
	if (ReplyText{}).Stringify() != "" {
		t.Fatal("an unfired reply must stringify to empty")
	}
	if got := (ReplyText{Text: "thanks", Fired: true}).Stringify(); got != "thanks" {
		t.Fatalf("fired reply stringified to %q, want the text", got)
	}
}

// TestNilPrimaryIsSafe: the port never panics on a missing primary — the
// enqueue path treats it as "nothing to schedule".
func TestNilPrimaryIsSafe(t *testing.T) {
	if got := Reply(nil, "thanks"); got != nil {
		t.Fatalf("Reply(nil) = %v, want nil", got)
	}
}

// TestEnsureJobIDNotEmpty guards the one malformed input the enqueue path
// can refuse explicitly.
func TestEnsureJobIDNotEmpty(t *testing.T) {
	if err := EnsureJobIDNotEmpty(""); err == nil {
		t.Fatal("empty primary id must be refused")
	}
	if err := EnsureJobIDNotEmpty("r1:c:1:t"); err != nil {
		t.Fatalf("non-empty primary id refused: %v", err)
	}
}

// TestJobIDIsDeterministic: the same primary names the same reply, so a
// replay never enqueues a second one — the UNIQUE key on
// (rule, source, message, target, job_kind) enforces what this function
// computes.
func TestJobIDIsDeterministic(t *testing.T) {
	a := JobID("r1:-1001:42:-1002")
	b := JobID("r1:-1001:42:-1002")
	if a != b || a != "r1:-1001:42:-1002:keyword_reply" {
		t.Fatalf("JobID not deterministic: %q vs %q", a, b)
	}
	if a == "r1:-1001:42:-1002" {
		t.Fatal("the reply id must differ from the primary id")
	}
}
