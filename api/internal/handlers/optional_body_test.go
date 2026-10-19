package handlers

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	grpcclient "autoforward/internal/grpcclient"
	pb "autoforward/proto"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Four endpoints treat their body as optional, and all four used to write a
// refusal they then ignored:
//
//	_ = bind(w, r, &body)
//
// bind had ALREADY written `400 {"error":"invalid JSON: EOF"}` for the empty
// body before the handler discarded the result and wrote its own answer into
// the same response. The client therefore received a 400 whose body held two
// concatenated JSON documents, and the documented "send no body, take the
// defaults" form — which is exactly what the dashboard's Purge-DLQ, Pause and
// Backup-export buttons send — never worked at all.
//
// These tests pin the response INTEGRITY, not just the status: a second JSON
// document appended to the first is the tell that two writers fought over one
// response.

// singleJSONDocument fails when the body is not exactly one JSON value, which is
// what a double write produces.
func singleJSONDocument(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	dec := json.NewDecoder(rec.Body)
	var first map[string]any
	if err := dec.Decode(&first); err != nil {
		t.Fatalf("response is not JSON: %v (%s)", err, rec.Body.String())
	}
	var second any
	if err := dec.Decode(&second); err != io.EOF {
		t.Fatalf("response carries more than one JSON document (two writers): %s", rec.Body.String())
	}
	return first
}

// --- fakes -----------------------------------------------------------------

type fakePauseClient struct {
	pb.ForwardRuleControlServiceClient
	owned     []*pb.ForwardRule
	lastPause *pb.PauseRuleRequest
}

func (f *fakePauseClient) ListRules(_ context.Context, in *pb.ListRulesRequest, _ ...grpc.CallOption) (*pb.ListRulesResponse, error) {
	out := &pb.ListRulesResponse{}
	for _, r := range f.owned {
		if in.OwnerUserId == 0 || r.OwnerUserId == in.OwnerUserId {
			out.Rules = append(out.Rules, r)
		}
	}
	return out, nil
}

func (f *fakePauseClient) GetRule(_ context.Context, in *pb.GetRuleRequest, _ ...grpc.CallOption) (*pb.ForwardRule, error) {
	for _, r := range f.owned {
		if r.Id == in.RuleId && r.OwnerUserId == in.OwnerUserId {
			return r, nil
		}
	}
	return nil, status.Error(codes.NotFound, "rule not found")
}

func (f *fakePauseClient) PauseRule(_ context.Context, in *pb.PauseRuleRequest, _ ...grpc.CallOption) (*pb.RuleActionResponse, error) {
	f.lastPause = in
	return &pb.RuleActionResponse{Success: true, Message: "paused"}, nil
}

// --- the endpoints ---------------------------------------------------------

func TestPurgeDeadLetter_EmptyBodyIsTheDocumentedForm(t *testing.T) {
	delivery := &fakeDeliveryClient{}
	h := &Handlers{c: &grpcclient.Clients{Delivery: delivery}}
	req := httptest.NewRequest("DELETE", "/api/dlq", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	rec := httptest.NewRecorder()

	h.PurgeDeadLetter(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	body := singleJSONDocument(t, rec)
	if ok, _ := body["success"].(bool); !ok {
		t.Fatalf("body: %v", body)
	}
	// The purge must still be the caller's: an empty body means "all of mine".
	if delivery.lastPurge.GetOwnerUserId() != 111 {
		t.Fatalf("owner scope lost: %+v", delivery.lastPurge)
	}
}

func TestPauseRule_EmptyBodyIsTheDocumentedForm(t *testing.T) {
	rules := &fakePauseClient{owned: []*pb.ForwardRule{{Id: "rule-1", OwnerUserId: 111}}}
	h := &Handlers{c: &grpcclient.Clients{Rules: rules}}
	req := httptest.NewRequest("POST", "/api/rules/rule-1/pause", nil)
	req.SetPathValue("id", "rule-1")
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	rec := httptest.NewRecorder()

	h.PauseRule(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	singleJSONDocument(t, rec)
	if rules.lastPause == nil || rules.lastPause.GetOwnerUserId() != 111 {
		t.Fatalf("pause must carry the owner: %+v", rules.lastPause)
	}
}

func TestBackupExport_EmptyBodyIsTheDocumentedForm(t *testing.T) {
	backup := &fakeBackupClient{}
	h := &Handlers{c: &grpcclient.Clients{Backup: backup}}
	req := httptest.NewRequest("POST", "/api/backup/export", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	rec := httptest.NewRecorder()

	h.BackupExport(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	singleJSONDocument(t, rec)
	// A non-admin with no body exports their own tenant, never the installation.
	if backup.lastExport.GetOwnerUserId() != 111 {
		t.Fatalf("owner scope lost: %+v", backup.lastExport)
	}
}

func TestPurgeLogs_EmptyBodyIsReadAsDefaultsNotAsMalformedJSON(t *testing.T) {
	h, fake := purgeHandlerWithRule(222)
	req := httptest.NewRequest("POST", "/api/logs/purge", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{},
		&Identity{UserID: 5094837833, IsAdmin: true}))
	rec := httptest.NewRecorder()

	h.PurgeLogs(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	singleJSONDocument(t, rec)
	// An absent body must still apply the 24h safety window.
	if fake.lastPurge.GetOlderThanHours() != 24 {
		t.Fatalf("safety window lost: %+v", fake.lastPurge)
	}
}

// A malformed body on an optional-body endpoint is still a client error: the
// fix must not turn "optional" into "ignored".
func TestOptionalBody_MalformedBodyIsStillRefused(t *testing.T) {
	h, fake := purgeHandlerWithRule(222)
	req := httptest.NewRequest("POST", "/api/logs/purge", strings.NewReader("{not json"))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{},
		&Identity{UserID: 5094837833, IsAdmin: true}))
	rec := httptest.NewRecorder()

	h.PurgeLogs(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
	singleJSONDocument(t, rec)
	if fake.lastPurge != nil {
		t.Fatalf("a malformed body must never reach the logger: %+v", fake.lastPurge)
	}
}
