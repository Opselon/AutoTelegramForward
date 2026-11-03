// Package replyenqueue is the delivery engine's keyword-reply enqueue port:
// it shapes a primary forward job into the secondary keyword-reply job the
// Python pipeline enqueues when classify_keyword_trigger fires
// (pipeline.py:1381-1411, durable_queue.enqueue_delivery with
// job_kind="keyword_reply").
//
// It lives in the delivery package's own subtree rather than in
// internal/domain/reply because every field it carries is a property of
// the JOB pair, not of the classifier's decision: the job id derivation,
// the payload's is_reply marker, and the mapping/message_map semantics
// that make a reply a distinct deliverable rather than a re-delivery of
// the forward.
package replyenqueue

import (
	"fmt"

	"atf/worker/internal/domain/job"
)

// Kind is the durable deliverable kind (database.py:1177, durable_queue's
// job_kind): the column is part of the delivery_jobs UNIQUE key, which is
// what keeps a reply from being deduplicated away against the forward for
// the same target.
const Kind = "keyword_reply"

// JobID derives the reply's job id from the primary job's id. Python
// builds it as f"{rule.id}:{chat}:{msg}:{target}{suffix}" with
// suffix=":keyword_reply" (durable_queue.py:476-478); the primary job's
// id IS that string without the suffix, so appending the kind is exact.
//
// The clone the delivery engine already carries (j.ID, j.RuleID, source
// and target ids, attempts bookkeeping) is unchanged; only the kind and
// the payload differ.
func JobID(primaryID string) string { return primaryID + ":" + Kind }

// Reply clones a primary forward job into its keyword-reply companion.
//
// What is deliberately NOT carried over is everything that would make the
// reply a re-delivery of the source message:
//
//   - the payload's media shape (album ids, media type, has_media) is
//     dropped: a keyword reply is a pure-text follow-up;
//   - the branding hop is dropped (pipeline.py:1387-1391: replies go to
//     the same targets minus C, because a reply carries no header to
//     brand);
//   - the message_map row is not written: keyword-reply jobs never
//     populate the source->target mapping (sqlite.go NoteMapping's own
//     comment records the Python worker's rule), so edit/delete sync has
//     nothing to reconcile against a reply.
//
// The fence key's JobKind field is what keeps the two deliverables'
// idempotency claims separate: the reply claims its own send, never the
// forward's.
func Reply(primary *job.DeliveryJob, text string) *job.DeliveryJob {
	if primary == nil {
		return nil
	}
	r := *primary
	r.ID = JobID(primary.ID)
	r.JobKind = Kind
	r.Status = job.StatusPending
	r.Attempts = 0
	r.ErrorDetail = ""
	r.IntermediateChatID = ""
	r.IntermediateMsgID = 0
	r.DeliveryStage = "DIRECT"
	payload := cloneReplyPayload(primary.Payload, text)
	r.Payload = payload
	return &r
}

// ReplyText describes the reply the classifier produced. It is the
// delivery engine's reply to "did the trigger fire and what does it say",
// kept separate from job.DeliveryJob so the classifier port stays free of
// queue types.
type ReplyText struct {
	Text string
	// Fired reports whether the configured reply is non-empty.
	Fired bool
}

// Stringify reports the reply text for a fired trigger, or "" for one
// that did not fire — the shape the enqueue decision consumes.
func (r ReplyText) Stringify() string {
	if !r.Fired {
		return ""
	}
	return r.Text
}

// cloneReplyPayload builds the reply job's payload from the primary
// forward's: the same identity and routing keys, the reply's text, and
// the is_reply marker the dispatch path reads to send text-only
// (durable_queue.py:391-393). Media and branding keys are removed, so a
// reply never re-copies or re-forwards the source message.
func cloneReplyPayload(p job.Payload, text string) job.Payload {
	raw := p.Raw()
	if raw == nil {
		raw = map[string]any{}
	}
	clone := make(map[string]any, len(raw)+2)
	for k, v := range raw {
		clone[k] = v
	}
	clone["text"] = text
	clone["is_reply"] = true
	// The media shape must not survive: a reply is text-only, and the
	// dispatch path decides copy vs forward from these keys.
	for _, key := range []string{
		"media_group_id", "album_message_ids", "album_mode", "album_sent",
		"has_media", "media_type", "branding",
	} {
		delete(clone, key)
	}
	return *job.NewPayload(clone)
}

// EnsureJobIDNotEmpty is a guard the enqueue path uses to refuse to build
// a reply for a job whose id cannot name one. It exists so the failure is
// an explicit, logged refusal rather than a silently malformed row.
func EnsureJobIDNotEmpty(primaryID string) error {
	if primaryID == "" {
		return fmt.Errorf("keyword reply: primary job id is empty")
	}
	return nil
}
