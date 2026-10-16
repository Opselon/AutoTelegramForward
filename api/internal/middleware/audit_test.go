package middleware

import (
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"autoforward/internal/logpolicy"
)

// The administrative audit trail must be exempt from the policy it administers.
// An operator who narrows collection to CRITICAL, or switches diagnostics off,
// must still be able to find *who* did it and *when*: an audit trail that can be
// disabled by the thing it audits is not an audit trail.

func TestAdminAuditSurvivesAMaximallyRestrictivePolicy(t *testing.T) {
	client := newRecordingLogsClient()
	// Collect nothing at all ordinary: a policy narrower than anything the
	// control plane can configure through the API.
	policy := logpolicy.New([]string{"CRITICAL"}, 0)
	emit := AuditEmitter(&ErrorLogSink{Client: client, Policy: policy})

	emit(5094837833, "logging.diagnostics.start", "ADMIN_REQUIRED", map[string]any{
		"action":         "logging.diagnostics.start",
		"seconds":        600,
		"correlation_id": "corr-admin-1",
	})

	entry := client.wait(t)
	if entry.Category != "audit" {
		t.Fatalf("the record must be identifiable as audit evidence, got %q", entry.Category)
	}
	if entry.UserId != 5094837833 {
		t.Fatalf("the record must name the actor, got %d", entry.UserId)
	}
	if entry.CorrelationId != "corr-admin-1" {
		t.Fatalf("the record must be joinable to the request, got %q", entry.CorrelationId)
	}
	var detail map[string]any
	if err := json.Unmarshal([]byte(entry.Detail), &detail); err != nil {
		t.Fatalf("the detail must be structured JSON: %v", err)
	}
	if detail["action"] != "logging.diagnostics.start" {
		t.Fatalf("the detail must name the change: %v", detail)
	}
	if policy.Snapshot().EmittedSecurity != 1 {
		t.Fatal("the exempt emission must be attributed as security-relevant, not as ordinary traffic")
	}
}

func TestAdminAuditIsNotSuppressedWhenCollectionIsOff(t *testing.T) {
	client := newRecordingLogsClient()
	// Levels that exclude everything: an empty collected set.
	policy := logpolicy.New(nil, 0)
	policy.DisableDiagnostics()
	emit := AuditEmitter(&ErrorLogSink{Client: client, Policy: policy})

	emit(7, "logging.diagnostics.stop", "ADMIN_REQUIRED", map[string]any{"action": "logging.diagnostics.stop"})
	if got := client.wait(t).Level; got != "INFO" {
		t.Fatalf("an admin change is INFO-level evidence, got %q", got)
	}
}

func TestAuditEmitterWithoutATransportDoesNotPanic(t *testing.T) {
	// The action still happened; the API's stderr trail is what remains, and the
	// call must return rather than take the request down.
	emit := AuditEmitter(nil)
	emit(1, "logging.diagnostics.start", "ADMIN_REQUIRED", map[string]any{})

	emitClosed := AuditEmitter(&ErrorLogSink{})
	emitClosed(1, "logging.diagnostics.start", "ADMIN_REQUIRED", map[string]any{})
}

func TestAuditEmissionDoesNotBlockTheCaller(t *testing.T) {
	client := &blockingLogsClient{recordingLogsClient: newRecordingLogsClient(), release: make(chan struct{})}
	defer close(client.release)
	emit := AuditEmitter(&ErrorLogSink{Client: client, Policy: logpolicy.New(nil, 0)})

	done := make(chan struct{})
	go func() {
		emit(1, "logging.diagnostics.start", "ADMIN_REQUIRED", map[string]any{})
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("the admin response must not wait on the log transport")
	}
}

var _ = http.StatusOK
