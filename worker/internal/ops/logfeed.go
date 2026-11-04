package ops

import (
	"fmt"
	"strings"
	"time"
)

// LogEntry is one engine log line's projection into the /ops error ring.
// The engine's Logger interface (Info/Warn/Error with key/value args) is
// the existing emit path the contract requires the ring be fed from, so
// this is the adapter: it reads the ids the line already carries and drops
// everything that must never reach an ops field (message content,
// credentials, multi-line detail).
type LogEntry struct {
	Severity      string
	Component     string
	Message       string
	RuleID        string
	JobID         string
	Code          string
	Reason        string
	CorrelationID string
	// Detail is the underlying error string, when the line carried one.
	Detail string
}

// RecordLogEntry feeds one logger.Warn/Error line into the error ring.
//
// args are the structured key/value pairs the engine already pairs with
// the message — "rule_id", "job_id", "code", "reason_code",
// "correlation_id", "error", ... — exactly the fields the ring's §1.5
// shape wants. Values are coerced to strings and sanitized inside the
// ring; anything unrecognized is ignored rather than surfacing.
//
// The message itself is the log line's summary ("transient failure —
// retrying with backoff"): an operator-readable classification, never the
// message content or a secret. The args may carry "error" whose value is
// an engine error string; that value is truncated to one line and 160
// chars by the ring's sanitize().
func (c *Collector) RecordLogEntry(severity, component, msg string, args []any) {
	if c == nil || c.errors == nil {
		return
	}
	entry := LogEntry{
		Severity:  severity,
		Component: component,
		Message:   msg,
	}
	// The engine emits fields as key, value, key, value... (slog's
	// convention); the field set is fixed, so a linear scan is both the
	// simplest and the cheapest read.
	for i := 0; i+1 < len(args); i += 2 {
		key, ok := args[i].(string)
		if !ok {
			continue
		}
		val := stringOf(args[i+1])
		switch strings.ToLower(strings.TrimSpace(key)) {
		case "rule_id":
			entry.RuleID = val
		case "job_id":
			entry.JobID = val
		case "code", "error_type":
			entry.Code = val
		case "reason_code":
			entry.Reason = val
		case "correlation_id":
			entry.CorrelationID = val
		case "error":
			// The error value is the line's detail; it becomes the ring's
			// message when the summary alone would say nothing (e.g.
			// "claim failed — retrying" without the underlying cause).
			if entry.Detail == "" {
				entry.Detail = val
			}
		}
	}
	if entry.Reason != "" {
		// The reason code IS the category the ring dedups on; an empty one
		// would dedup the whole component into one bucket.
		if entry.Code == "" {
			entry.Code = entry.Reason
		}
	}
	c.RecordError(entry)
}

// RecordError is the typed entry point: one classified failure into the
// ring. It is the single funnel every error path (log lines, delivery
// failures, stage failures) pours through, so the dedup and sanitize rules
// live in exactly one place.
func (c *Collector) RecordError(e LogEntry) {
	if c == nil || c.errors == nil {
		return
	}
	if e.Severity != "WARN" && e.Severity != "ERROR" && e.Severity != "CRITICAL" {
		return // info/debug lines never enter the error ring
	}
	if e.Component == "" {
		e.Component = "worker"
	}
	message := e.Message
	if e.Detail != "" {
		message = e.Message + ": " + e.Detail
	}
	c.errors.Record(ErrorItem{
		Ts:            time.Now().UTC().Format(time.RFC3339),
		Severity:      e.Severity,
		Component:     e.Component,
		WorkerID:      c.WorkerID(),
		RuleID:        e.RuleID,
		JobID:         e.JobID,
		Category:      e.Reason,
		Code:          e.Code,
		Retryable:     classifyRetryable(e.Code),
		CorrelationID: e.CorrelationID,
		Message:       message,
	})
}

// classifyRetryable maps the engine's existing failure vocabulary onto the
// ring's retryable flag. The codes are DeliveryFailure.Code /
// reason_code values the engine already emits; an unknown code is
// retryable=false (the conservative read: an operator must not assume a
// retry is coming).
func classifyRetryable(code string) bool {
	switch strings.ToUpper(strings.TrimSpace(code)) {
	case "FLOOD_WAIT", "RATE_LIMITED", "NETWORK_TIMEOUT", "NETWORK_DISCONNECTED",
		"SESSION_UNAVAILABLE", "TELEGRAM_TEMPORARY_ERROR", "SOURCE_UNAVAILABLE",
		"TARGET_UNAVAILABLE", "INTERNAL_ERROR":
		return true
	}
	return false
}

// stringOf coerces a structured arg value to its operator-readable string.
// Errors arrive as error values (their .Error()), ids as strings, numbers
// through fmt — the same rendering the log line itself uses.
func stringOf(v any) string {
	switch s := v.(type) {
	case string:
		return s
	case error:
		return s.Error()
	case nil:
		return ""
	}
	return fmt.Sprint(v)
}
