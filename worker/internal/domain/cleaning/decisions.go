package cleaning

import (
	"context"
	"encoding/json"
	"fmt"
)

// DecisionRecorder is the narrow seam between the cleaning pipeline and the
// central Rust Log Monitor. The pipeline knows nothing about gRPC, protobuf
// or log levels — it hands over one structured decision record per
// significant event, and the recorder decides where it goes.
//
// The contract (migration brief §6): every important business decision must
// produce an observable decision event, but the pipeline must never block on
// log delivery. Implementations are expected to be non-blocking and
// bounded; a nil recorder makes every call a no-op, which is what the
// delivery engine uses when the Logger is unreachable.
type DecisionRecorder interface {
	// RecordDecision emits one decision event. The implementation owns
	// severity, redaction and backpressure; the caller owns the fields.
	RecordDecision(ctx context.Context, event DecisionEvent)
}

// DecisionEvent is one observable business decision from the transform
// pipeline. Field names match the central logger's structured vocabulary so
// the Log Center's correlation views work across the Python Core, the API
// and the Worker from day one.
type DecisionEvent struct {
	// Category groups the event for the Log Center's filters:
	// "transform", "filter", "forward", ...
	Category string `json:"category"`
	// Level is DEBUG | INFO | WARN | ERROR. The pipeline emits WARN for
	// recoverable anomalies (a stage failed and delivered untransformed
	// text), INFO for meaningful lifecycle events (stage applied), never
	// ERROR — a transform failure is not a delivery failure.
	Level string `json:"level"`
	// Message is the human-readable one-liner, the same vocabulary the
	// Python [PIPELINE:...] markers use.
	Message string `json:"message"`
	// RuleID, CorrelationID, OwnerUserID are the correlation keys that make
	// a message's whole lifecycle reconstructable from the log store.
	RuleID        string `json:"rule_id,omitempty"`
	CorrelationID string `json:"correlation_id,omitempty"`
	OwnerUserID   int64  `json:"owner_user_id,omitempty"`
	// SourceID / TargetID identify the chats involved.
	SourceID string `json:"source_id,omitempty"`
	TargetID string `json:"target_id,omitempty"`
	// Decision is the outcome token: "APPLY", "DROP", "SKIP", ...
	Decision string `json:"decision,omitempty"`
	// ReasonCode is the stable machine token: "EMPTY_AFTER_CLEAN",
	// "CLEANER_FAILED", ...
	ReasonCode string `json:"reason_code,omitempty"`
	// StageNames lists the pipeline stages that changed the text — the
	// observable answer to "which transformation changed the content?".
	StageNames []string `json:"stage_names,omitempty"`
	// Detail carries the free-form diagnostic context (never message text
	// or secrets).
	Detail map[string]any `json:"detail,omitempty"`
}

// jsonSnapshot renders one event for a logging implementation that prefers a
// plain string payload. Kept here so the event shape is serialized in exactly
// one place.
func (e DecisionEvent) jsonSnapshot() string {
	body, err := json.Marshal(e)
	if err != nil {
		return fmt.Sprintf(`{"category":%q,"message":%q,"reason_code":"serialization_failed"}`, e.Category, e.Message)
	}
	return string(body)
}

// String is the single-line form a stdout/slog emitter uses.
func (e DecisionEvent) String() string { return e.jsonSnapshot() }

// nilSafeRecorder lets a nil DecisionRecorder be called without a guard at
// every call site.
type nilSafeRecorder struct{}

func (nilSafeRecorder) RecordDecision(context.Context, DecisionEvent) {}

// RecorderOr returns the given recorder, or a no-op when it is nil. Call it
// once when constructing an engine, not at every event.
func RecorderOr(r DecisionRecorder) DecisionRecorder {
	if r == nil {
		return nilSafeRecorder{}
	}
	return r
}
