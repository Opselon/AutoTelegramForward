// Package dedup is the Go Worker's event-level and content-level idempotency
// plane, ported from the Python Core's two distinct replay protections.
//
// There are two protections and they answer two different questions. Mixing
// them up is the classic bug this package exists to prevent, so they are
// stated plainly:
//
//   - Event-level dedup (processed_messages, WasProcessed/MarkProcessed)
//     answers "did this task already see this Telegram event?". It exists so
//     a reconnect replay — the client resurfacing a buffered update, or the
//     queue handing a job to a second worker after a lease expiry — cannot
//     deliver the same message id twice. The marker is keyed by
//     (rule_id, chat_id, message_id) and carries NO target dimension: the
//     Python pipeline only writes it once the whole fan-out was accepted
//     (pipeline.py:1435), so a partial fan-out stays replayable and the
//     replay repairs only the missing target.
//
//   - Content-level dedup (content_dedup, ClaimContentHash/ReleaseContentHash)
//     answers "did this task already forward this *content*, under a different
//     message id?". It backs the user-facing Duplicate Filters feature: a
//     channel re-posting its own announcement gets one copy, not two. The
//     fingerprint is scoped to the rule, so the same text under another task
//     is a different message and still forwards.
//
// The claim/release discipline is the load-bearing part. A content claim is
// EARNED on delivery acceptance and must be RELEASED when the message never
// reached a target (pipeline.py:781 _release_content_claim). A claim kept
// forever silently drops the next legitimate identical post — the exact
// silent-drop the Python side documents and the Go side must not reintroduce.
package dedup

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

// FingerprintInput is the message snapshot a content fingerprint is computed
// over. It mirrors the fields core/application/pipeline.py::_content_fingerprint
// actually reads: text, caption, the media kind string, and the album id.
//
// The Python fingerprint is computed over the ingest-side MessagePayload,
// whose text/caption the pipeline snapshotted into the job payload. The
// Worker therefore reads the same keys off job.Payload rather than
// re-deriving them (see PayloadFingerprint).
type FingerprintInput struct {
	Text         string
	Caption      string
	MediaType    string
	MediaGroupID string
}

// PayloadFingerprint is the content fingerprint a delivery job carries.
//
// It is a byte-for-byte port of pipeline.py:274 `_content_fingerprint`:
//
//	body := (text or "") + "||" + (caption or "")
//	normalized := " ".join(body.split()).casefold()
//	media_key := str(media_type) (+ ":album:" + media_group_id when set)
//	sha256(normalized + "|" + media_key)
//
// The media kind is lowercased to match Python's
// str(MediaType.value) — the enqueue path snapshots the enum's *value*
// ("photo", not "PHOTO") into the job payload (durable_queue.py:361), and
// the Python fingerprint reads that same value. A Go side that took the
// key verbatim would disagree with Python about every media message.
//
// Two properties of the Python original are deliberately preserved even
// though they look like bugs to a reader expecting Unicode-normalized keys:
//
//  1. The "||" seam between text and caption is load-bearing, not decorative.
//     `" ".join(body.split())` preserves it the way Python's str.split /
//     join always does: internal whitespace runs collapse to one space, and
//     the ends trim only when a whitespace run is actually consumed there.
//     A text with a trailing space ("ab " + "||" + "") keeps that space
//     before the seam; " " + " " collapses to one. Matching this exactly is
//     what keeps the two engines from disagreeing about a re-post whose
//     owner merely re-wrapped or re-padded it.
//
//  2. The fold is a plain casefold, NOT the content engine's fold_text. The
//     Persian/Arabic letter unification the branding dedup key applies
//     (branding.NormalizeText) is a DIFFERENT function over a DIFFERENT key:
//     _normalize_for_fingerprint lives in the branding hop, while
//     _content_fingerprint — the one ported here — is deliberately the
//     cheaper plain casefold. Reusing the fold here would make the Go Worker
//     treat posts as duplicates that the Python pipeline forwards, and vice
//     versa. (The fold IS used elsewhere in the Worker, at the branding hop,
//     where Python uses it.)
//
// Go's strings.ToLower stands in for Python's str.casefold. The two differ
// only on the Latin full-case edges (ß/ẞ/İ); the media-kind and album keys
// never contain them, and a text body that does is the same edge the branding
// fold already tolerates on both sides.
func PayloadFingerprint(in FingerprintInput) string {
	body := orEmpty(in.Text) + "||" + orEmpty(in.Caption)
	normalized := collapseWS(strings.ToLower(body))
	// str(MediaType.value) on the Python side is the lowercase wire form
	// ("photo"); the album suffix is the raw group id.
	mediaKey := strings.ToLower(orEmpty(in.MediaType))
	if orEmpty(in.MediaGroupID) != "" {
		mediaKey += ":album:" + in.MediaGroupID
	}
	raw := normalized + "|" + mediaKey
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

// orEmpty normalizes a Python "or ''" idiom: any missing/empty field is the
// empty string, never a nil-pointer or a "null" literal.
func orEmpty(s string) string { return s }

// collapseWS is Go's " ".join(str.split()): splits on every run of Unicode
// whitespace and rejoins with single spaces. strings.Fields implements the
// split half; the join is the Python join.
func collapseWS(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

// Store is the dedup port the delivery engine consults. Both backends
// implement it; the delivery engine never knows which.
//
// The methods are the Python repository interface
// (IProcessedMessageRepository + the content half of the message-map
// repository) one for one, so a divergence in any of them is a divergence
// from the reference, not from a Go-side redesign.
type Store interface {
	// WasProcessed reports whether the task already saw this Telegram event.
	// See SqliteProcessedMessageRepository.was_processed.
	WasProcessed(ctx context.Context, ruleID, chatID string, messageID int64) (bool, error)

	// MarkProcessed records that the task accepted this event, so a reconnect
	// replay cannot re-deliver it. INSERT OR IGNORE keeps replay idempotent;
	// the Python marker carries no target dimension.
	MarkProcessed(ctx context.Context, ruleID, chatID string, messageID int64, mediaGroupID string) error

	// ClaimContentHash atomically claims a content fingerprint for a rule.
	// True means THIS caller won the UNIQUE(rule_id, content_hash) race and
	// may forward; False means another worker (or an earlier delivery)
	// already owns it and the message is a duplicate.
	ClaimContentHash(ctx context.Context, ruleID, contentHash string) (bool, error)

	// WasContentForwarded reports whether the rule already forwarded this
	// fingerprint. The non-claiming read, for stores written before the
	// atomic claim existed — the Python pipeline falls back to it when
	// claim_content_hash is unavailable.
	WasContentForwarded(ctx context.Context, ruleID, contentHash string) (bool, error)

	// ReleaseContentHash gives a claim back when the message never reached a
	// target. A claim is earned on delivery acceptance; anything that drops
	// the message after the dedup stage must release it or the next
	// legitimate identical post is silently dropped.
	ReleaseContentHash(ctx context.Context, ruleID, contentHash string) error

	// Close releases the underlying connection.
	Close() error
}
