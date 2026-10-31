// Reconciliation fields on the delivery-stats surface (v32).
//
// GetDeliveryStats gained additive health aggregates and the reconciler's
// findings. This test pins that they reach the JSON response exactly as Core
// produced them, and that a pre-v32 Core (nil fields) still renders an empty
// list instead of a null — a dashboard iterating over "findings" must not
// break on an older Core.

package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"autoforward/internal/grpcclient"
	pb "autoforward/proto"
	"google.golang.org/grpc"
)

// statsClient answers the stats call with a fixed response, so what is under
// test is the handler's own projection, not Core.
type statsClient struct {
	pb.DeliveryControlServiceClient
	resp *pb.DeliveryStatsResponse
	err  error
}

func (c *statsClient) GetDeliveryStats(_ context.Context, _ *pb.DeliveryStatsRequest, _ ...grpc.CallOption) (*pb.DeliveryStatsResponse, error) {
	return c.resp, c.err
}

func statsRequest() *http.Request {
	req := httptest.NewRequest(http.MethodGet, "/api/queue", nil)
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 5094837833}))
}

func TestDeliveryStatsForwardsTheReconciliationFields(t *testing.T) {
	resp := &pb.DeliveryStatsResponse{
		ProcessedTotal:           10,
		ForwardedTotal:           8,
		PendingTotal:             2,
		DeliveredTotal:           8,
		RetryingTotal:            1,
		SuccessRate:              0.8,
		FlaggedFindings:          1,
		OldestPendingAgeSeconds:  45,
		FailureRateByDestination: map[string]float64{"-100T": 0.5},
		FailureRateBySource:      map[string]float64{"-100S": 0.25},
		Findings: []*pb.DeliveryFinding{{
			Kind:              "expired_lease",
			RuleId:            "rule-1",
			SourceChatId:      "-100S",
			SourceMessageId:   500,
			DestinationChatId: "-100T",
			DeliveryJobId:     "job-1",
			WorkerId:          "w-dead",
			AttemptNumber:     2,
			ErrorCategory:     "expired_lease",
			ErrorDetail:       "lease expired 3600s ago",
			Retryable:         true,
			Timestamp:         1700000000,
		}},
	}
	h := &Handlers{c: &grpcclient.Clients{Delivery: &statsClient{resp: resp}}}
	w := httptest.NewRecorder()
	h.DeliveryStats(w, statsRequest())

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body: %s)", w.Code, w.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatalf("response is not valid JSON: %v", err)
	}

	// The aggregates arrive as numbers, not strings.
	assertStatsNum(t, out, "pending_total", 2)
	assertStatsNum(t, out, "delivered_total", 8)
	assertStatsNum(t, out, "retrying_total", 1)
	assertStatsNum(t, out, "success_rate", 0.8)
	assertStatsNum(t, out, "oldest_pending_age_seconds", 45)
	assertStatsNum(t, out, "flagged_findings", 1)

	// The per-rate maps survive the round trip.
	dest, _ := out["failure_rate_by_destination"].(map[string]any)
	if dest["-100T"] != 0.5 {
		t.Errorf("failure_rate_by_destination[-100T] = %v, want 0.5", dest["-100T"])
	}
	src, _ := out["failure_rate_by_source"].(map[string]any)
	if src["-100S"] != 0.25 {
		t.Errorf("failure_rate_by_source[-100S] = %v, want 0.25", src["-100S"])
	}

	// The finding keeps every identifier an operator needs.
	findings, _ := out["findings"].([]any)
	if len(findings) != 1 {
		t.Fatalf("findings = %d items, want 1", len(findings))
	}
	f, _ := findings[0].(map[string]any)
	for key, want := range map[string]any{
		"kind":                "expired_lease",
		"rule_id":             "rule-1",
		"source_chat_id":      "-100S",
		"destination_chat_id": "-100T",
		"delivery_job_id":     "job-1",
		"worker_id":           "w-dead",
		"retryable":           true,
	} {
		if f[key] != want {
			t.Errorf("finding.%s = %v, want %v", key, f[key], want)
		}
	}
	// Numeric identifiers survive as numbers too.
	if n, _ := f["source_message_id"].(float64); n != 500 {
		t.Errorf("finding.source_message_id = %v, want 500", f["source_message_id"])
	}
	if n, _ := f["attempt_number"].(float64); n != 2 {
		t.Errorf("finding.attempt_number = %v, want 2", f["attempt_number"])
	}
}

// A pre-v32 Core returns the message with the additive fields unset. The
// handler must render an empty findings list (never null).
func TestDeliveryStatsWithoutReconciliationFieldsRendersEmptyLists(t *testing.T) {
	resp := &pb.DeliveryStatsResponse{
		ProcessedTotal: 10,
		ForwardedTotal: 10,
		InQueue:        0,
	}
	h := &Handlers{c: &grpcclient.Clients{Delivery: &statsClient{resp: resp}}}
	w := httptest.NewRecorder()
	h.DeliveryStats(w, statsRequest())

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatalf("response is not valid JSON: %v", err)
	}
	findings, ok := out["findings"].([]any)
	if !ok {
		t.Fatalf("findings is %T, want a JSON array (never null)", out["findings"])
	}
	if len(findings) != 0 {
		t.Errorf("findings = %d items, want 0 on a pre-v32 Core", len(findings))
	}
	// The legacy fields still answer.
	if out["processed_total"] == nil {
		t.Error("processed_total is missing on a legacy response")
	}
}

func assertStatsNum(t *testing.T, out map[string]any, key string, want float64) {
	t.Helper()
	got, ok := out[key].(float64)
	if !ok {
		t.Errorf("%s is %T, want a number", key, out[key])
		return
	}
	if got != want {
		t.Errorf("%s = %v, want %v", key, got, want)
	}
}
