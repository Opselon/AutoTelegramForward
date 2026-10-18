package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc"

	pb "autoforward/proto"

	"autoforward/internal/grpcclient"
)

// A control-plane action that says "no" has to say it in JSON.
//
// `pb.RuleActionResponse` marks every scalar `omitempty`, so the generated
// struct serialises a refusal as `{"message":"not_found"}` — the `success`
// field is simply absent. Clients that ask `resp["success"] == false` never see
// the refusal (the key is missing, not false), and clients that test the body's
// truthiness read the non-empty object as a success. The result was live: a
// refused dead-letter replay was narrated as a replay by the CLI, and both the
// dashboard and the Android app removed the row they had failed to re-queue.
//
// These tests assert on the raw response bytes, because that is what a client
// decodes — a struct-level assertion on `resp.Success` passes while the field is
// still missing from the wire.

// refusalClient answers every delivery action with the given refusal, the way
// the core does (success=false plus a documented result code).
type refusalClient struct {
	pb.DeliveryControlServiceClient
	success bool
	message string
}

func (f *refusalClient) ListDeadLetter(_ context.Context, _ *pb.ListDeadLetterRequest, _ ...grpc.CallOption) (*pb.ListDeadLetterResponse, error) {
	return &pb.ListDeadLetterResponse{}, nil
}

func (f *refusalClient) ReplayDeadLetter(_ context.Context, _ *pb.ReplayDeadLetterRequest, _ ...grpc.CallOption) (*pb.RuleActionResponse, error) {
	return &pb.RuleActionResponse{Success: f.success, Message: f.message, ErrorCode: f.message}, nil
}

func (f *refusalClient) PurgeDeadLetter(_ context.Context, _ *pb.PurgeDeadLetterRequest, _ ...grpc.CallOption) (*pb.RuleActionResponse, error) {
	return &pb.RuleActionResponse{Success: f.success, Message: f.message, ErrorCode: f.message}, nil
}

func actionRequest(method, target, body string) *http.Request {
	req := httptest.NewRequest(method, target, strings.NewReader(body))
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 5094837833}))
}

func decodeAction(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		t.Fatalf("action response is not a JSON object: %v (%s)", err, body)
	}
	return out
}

func TestReplayDeadLetter_StatesTheRefusalOnTheWire(t *testing.T) {
	for _, code := range []string{"not_found", "already_pending", "already_sent", "replay_failed"} {
		fc := &refusalClient{success: false, message: code}
		h := &Handlers{c: &grpcclient.Clients{Delivery: fc}}
		rec := httptest.NewRecorder()

		h.ReplayDeadLetter(rec, actionRequest(http.MethodPost, "/dlq/x/retry", ""))

		if rec.Code != http.StatusOK {
			t.Fatalf("%s: expected 200, got %d", code, rec.Code)
		}
		body := decodeAction(t, rec.Body.Bytes())
		got, ok := body["success"]
		if !ok {
			t.Fatalf("%s: the refusal carried no `success` field: %s", code, rec.Body.String())
		}
		if got != false {
			t.Fatalf("%s: success = %v, want false", code, got)
		}
		if body["error_code"] != code {
			t.Fatalf("%s: error_code = %v, want %q", code, body["error_code"], code)
		}
		// And the message stays readable for clients that only surface a string.
		if body["message"] != code {
			t.Fatalf("%s: message = %v, want %q", code, body["message"], code)
		}
	}
}

func TestReplayDeadLetter_StatesSuccessOnTheWire(t *testing.T) {
	fc := &refusalClient{success: true, message: "requeued"}
	h := &Handlers{c: &grpcclient.Clients{Delivery: fc}}
	rec := httptest.NewRecorder()

	h.ReplayDeadLetter(rec, actionRequest(http.MethodPost, "/dlq/x/retry", ""))

	body := decodeAction(t, rec.Body.Bytes())
	if body["success"] != true {
		t.Fatalf("success = %v, want true (%s)", body["success"], rec.Body.String())
	}
}

func TestPurgeDeadLetter_StatesTheRefusalOnTheWire(t *testing.T) {
	fc := &refusalClient{success: false, message: "purge_failed"}
	h := &Handlers{c: &grpcclient.Clients{Delivery: fc}}
	rec := httptest.NewRecorder()

	h.PurgeDeadLetter(rec, actionRequest(http.MethodDelete, "/dlq", `{}`))

	body := decodeAction(t, rec.Body.Bytes())
	if v, ok := body["success"]; !ok || v != false {
		t.Fatalf("a failed purge must state success=false: %s", rec.Body.String())
	}
}

func TestPurgeDeadLetter_ReportsTheCountItRemoved(t *testing.T) {
	fc := &refusalClient{success: true, message: "purged 3"}
	h := &Handlers{c: &grpcclient.Clients{Delivery: fc}}
	rec := httptest.NewRecorder()

	h.PurgeDeadLetter(rec, actionRequest(http.MethodDelete, "/dlq", `{}`))

	body := decodeAction(t, rec.Body.Bytes())
	if body["success"] != true || body["message"] != "purged 3" {
		t.Fatalf("a purge must answer with the number removed: %s", rec.Body.String())
	}
}
