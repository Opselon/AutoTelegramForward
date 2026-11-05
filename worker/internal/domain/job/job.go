// Package job holds the durable delivery-job domain the Go Worker owns.
//
// The types here mirror the *existing* `delivery_jobs` / `delivery_attempts`
// tables one-for-one: this migration takes ownership of the queue the Python
// Core already created, it does not invent a second one. Keeping the column
// names and status vocabulary identical is what lets one authoritative store
// serve both engines during the strangler-fig transition.
package job

import "fmt"

// Status is the persisted delivery_jobs.status vocabulary.
//
// Only the values the existing store writes are defined; adding states "for
// appearance" would break the claim query's NOT-EXISTS lane predicate, which
// treats exactly PENDING/RETRY_WAIT/CLAIMED as "unsent".
type Status string

const (
	StatusPending    Status = "PENDING"
	StatusClaimed    Status = "CLAIMED"
	StatusRetryWait  Status = "RETRY_WAIT"
	StatusSent       Status = "SENT"
	StatusFailed     Status = "FAILED"
	StatusDeadLetter Status = "DEAD_LETTER"
	StatusUnknown    Status = "UNKNOWN"
)

// IsUnsent reports whether a row in this state still blocks its target lane.
// It must agree with the claim query's `status IN (...)` predicate exactly,
// otherwise two workers could share one target lane.
func (s Status) IsUnsent() bool {
	switch s {
	case StatusPending, StatusClaimed, StatusRetryWait:
		return true
	}
	return false
}

// DeliveryJob is one row of delivery_jobs, with the payload the Python
// pipeline enqueued carried verbatim in Payload.
//
// Payload keeps the Python-side keys (`forward_mode`, `content_mode`,
// `text`, `has_media`, `album_message_ids`, `branding`, ...) so the Go worker
// makes the same delivery-shape decisions the Python worker made, from the
// same data, without a second representation of the same business state.
type DeliveryJob struct {
	ID                 string
	RuleID             string
	SourceChatID       string
	SourceMessageID    int64
	TargetChatID       string
	Status             Status
	Attempts           int
	MaxAttempts        int
	NextRetryAt        float64
	LeaseUntil         float64
	WorkerID           string
	ErrorDetail        string
	CreatedAt          int64
	UpdatedAt          int64
	IntermediateChatID string
	IntermediateMsgID  int64
	DeliveryStage      string
	JobKind            string
	Payload            Payload

	// ClaimedFrom is the status the delivery_jobs row carried when the
	// claim that produced this job read it. It is NOT persisted: only the
	// claim sets it, and only the in-memory job a worker is processing
	// ever sees it.
	//
	// Its one reader is the forward-every-N cadence gate. A row that was
	// PENDING at claim time is a first dispatch; a row that was RETRY_WAIT
	// or CLAIMED (an expired lease) is a RE-DISPATCH of a job the engine
	// already processed once. Python's cadence is evaluated once per
	// message at ingest — a delivery retry never re-enters it — so the Go
	// gate must not consume a second cadence slot for a re-dispatch.
	// Nothing else reads this field on purpose: it is a claim-time fact,
	// not a property of the job.
	ClaimedFrom Status
}

// AttemptKey is the deterministic business identity of one logical delivery —
// the `delivery_attempts` id. It is deliberately identical to the job id
// (modulo the job_kind suffix) so a fence row joins onto its job with no
// lookup table, exactly as the Python fence does.
type AttemptKey struct {
	RuleID          string
	SourceChatID    string
	SourceMessageID int64
	TargetChatID    string
	JobKind         string
}

// String is the storage id: rule:source_chat:source_message:target[:kind].
//
// Keep this byte-for-byte identical to Python's DeliveryAttemptKey.id, or the
// Go fence and the Python fence are two different fences.
func (k AttemptKey) String() string {
	suffix := ""
	if k.JobKind != "" && k.JobKind != "forward" {
		suffix = ":" + k.JobKind
	}
	return fmt.Sprintf("%s:%s:%d:%s%s", k.RuleID, k.SourceChatID,
		k.SourceMessageID, k.TargetChatID, suffix)
}

// UnknownFence is an ambiguous delivery attempt awaiting reconciliation: a
// send whose outcome Telegram never reported, deliberately left UNKNOWN so
// nothing dispatches it twice. LastError carries the evidence a reconciler
// needs to decide whether the operation provably never executed.
type UnknownFence struct {
	Key       AttemptKey
	LastError string
	UpdatedAt int64
}

// fenceOutcome mirrors what delivery_attempts.claim_or_check returns.
// The vocabulary is shared with Python on purpose: only "dispatch" allows a
// send, and SENT/UNKNOWN are never re-dispatched.
type FenceOutcome string

const (
	FenceDispatch    FenceOutcome = "dispatch"
	FenceAlreadySent FenceOutcome = "already_sent"
	FenceInFlight    FenceOutcome = "in_flight"
	FenceStale       FenceOutcome = "stale"
	FenceUnknown     FenceOutcome = "unknown"
)

// DeliveryAttempt is the read-back of one delivery_attempts row.
type DeliveryAttempt struct {
	Status          string
	WorkerID        string
	LeaseUntil      float64
	TargetMessageID int64
	LastError       string
	Attempts        int
}
