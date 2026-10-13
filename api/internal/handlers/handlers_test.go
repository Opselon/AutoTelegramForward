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
)

func TestWriteJSON_Encodes(t *testing.T) {
	rec := httptest.NewRecorder()
	writeJSON(rec, http.StatusOK, map[string]string{"a": "b"})
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("content type: %s", ct)
	}
	var got map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || got["a"] != "b" {
		t.Fatalf("body: %s", rec.Body.String())
	}
}

func TestBind_InvalidJSON(t *testing.T) {
	req := httptest.NewRequest("POST", "/", strings.NewReader("{invalid"))
	rec := httptest.NewRecorder()
	if bind(rec, req, &struct{}{}) {
		t.Fatal("bind should fail on invalid json")
	}
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
}

func TestBind_ValidJSON(t *testing.T) {
	req := httptest.NewRequest("POST", "/", strings.NewReader(`{"session_id":"abc"}`))
	rec := httptest.NewRecorder()
	var target backupReq
	if !bind(rec, req, &target) {
		t.Fatal("bind should succeed on valid json")
	}
	if target.SessionID != "abc" {
		t.Fatalf("parsed: %+v", target)
	}
}

func TestHealth(t *testing.T) {
	// Health handler doesn't touch gRPC, safe to test via nil clients.
	h := &Handlers{}
	req := httptest.NewRequest("GET", "/healthz", nil)
	rec := httptest.NewRecorder()
	h.Health(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var body map[string]string
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body["status"] != "ok" {
		t.Fatalf("body: %s", rec.Body.String())
	}
}

// ---------------------------------------------------------------------------
// Ownership guards: a tenant must never mutate another tenant's resources.
// ---------------------------------------------------------------------------

// fakeRulesClient answers ListRules from an in-memory set so the ownership
// check in UpdateRule/DeleteRule has something to scan.
type fakeRulesClient struct {
	pb.ForwardRuleControlServiceClient
	owned []*pb.ForwardRule
}

func (f *fakeRulesClient) ListRules(ctx context.Context, in *pb.ListRulesRequest, opts ...grpc.CallOption) (*pb.ListRulesResponse, error) {
	out := &pb.ListRulesResponse{}
	for _, r := range f.owned {
		if in.OwnerUserId == 0 || r.OwnerUserId == in.OwnerUserId {
			out.Rules = append(out.Rules, r)
		}
	}
	return out, nil
}

func (f *fakeRulesClient) UpdateRule(ctx context.Context, in *pb.UpdateRuleRequest, opts ...grpc.CallOption) (*pb.ForwardRule, error) {
	return in.Rule, nil
}

func (f *fakeRulesClient) DeleteRule(ctx context.Context, in *pb.DeleteRuleRequest, opts ...grpc.CallOption) (*pb.StatusResponse, error) {
	return &pb.StatusResponse{Success: true, Message: "deleted"}, nil
}

type fakeFilterClient struct {
	pb.FilterControlServiceClient
	owned []*pb.FilterRule
}

func (f *fakeFilterClient) ListFilters(ctx context.Context, in *pb.ListFiltersRequest, opts ...grpc.CallOption) (*pb.ListFiltersResponse, error) {
	out := &pb.ListFiltersResponse{}
	for _, fl := range f.owned {
		if in.OwnerUserId == 0 || fl.OwnerUserId == in.OwnerUserId {
			out.Filters = append(out.Filters, fl)
		}
	}
	return out, nil
}

type fakeAIClient struct {
	pb.AIControlServiceClient
	owned []*pb.AIConfig
}

func (f *fakeAIClient) ListAIConfigs(ctx context.Context, in *pb.ListAIConfigsRequest, opts ...grpc.CallOption) (*pb.ListAIConfigsResponse, error) {
	out := &pb.ListAIConfigsResponse{}
	for _, c := range f.owned {
		if in.OwnerUserId == 0 || c.OwnerUserId == in.OwnerUserId {
			out.Configs = append(out.Configs, c)
		}
	}
	return out, nil
}

func reqWithIdentity(uid int64) *http.Request {
	req := httptest.NewRequest("PUT", "/api/rules/x", strings.NewReader("{}"))
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid}))
}

func TestUpdateRule_RejectsForeignOwner(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{
		Rules: &fakeRulesClient{owned: []*pb.ForwardRule{
			{Id: "rule-a", OwnerUserId: 111},
		}},
	}}
	req := reqWithIdentity(222) // different tenant
	req.SetPathValue("id", "rule-a")
	rec := httptest.NewRecorder()
	h.UpdateRule(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("foreign rule update must 404, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestUpdateRule_AllowsOwner(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{
		Rules: &fakeRulesClient{owned: []*pb.ForwardRule{
			{Id: "rule-a", OwnerUserId: 111},
		}},
	}}
	req := reqWithIdentity(111)
	req.SetPathValue("id", "rule-a")
	rec := httptest.NewRecorder()
	h.UpdateRule(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("own rule update must 200, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestDeleteRule_RejectsForeignOwner(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{
		Rules: &fakeRulesClient{owned: []*pb.ForwardRule{
			{Id: "rule-a", OwnerUserId: 111},
		}},
	}}
	req := reqWithIdentity(222)
	req.SetPathValue("id", "rule-a")
	rec := httptest.NewRecorder()
	h.DeleteRule(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("foreign rule delete must 404, got %d", rec.Code)
	}
}

func TestUpdateFilter_RejectsForeignOwner(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{
		Filters: &fakeFilterClient{owned: []*pb.FilterRule{
			{Id: "fil-a", OwnerUserId: 111},
		}},
	}}
	req := reqWithIdentity(222)
	req.SetPathValue("id", "fil-a")
	rec := httptest.NewRecorder()
	h.UpdateFilter(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("foreign filter update must 404, got %d", rec.Code)
	}
}

func TestDeleteAIConfig_RejectsForeignOwner(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{
		AI: &fakeAIClient{owned: []*pb.AIConfig{
			{Id: "ai-a", OwnerUserId: 111},
		}},
	}}
	req := reqWithIdentity(222)
	req.SetPathValue("id", "ai-a")
	rec := httptest.NewRecorder()
	h.DeleteAIConfig(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("foreign AI config delete must 404, got %d", rec.Code)
	}
}

// ----------------------------------------------------------- v12.2 guards
// The API gateway must forward the authenticated identity on every by-id
// mutation so the core can enforce ownership server-side.

// recordingRulesClient captures the owner field the handler actually sent.
type recordingRulesClient struct {
	pb.ForwardRuleControlServiceClient
	owners []int64
	ids    []string
}

func (f *recordingRulesClient) ListRules(ctx context.Context, in *pb.ListRulesRequest, opts ...grpc.CallOption) (*pb.ListRulesResponse, error) {
	// Echo back one rule owned by whoever is asking so the handler-level
	// ownership guard passes and the call reaches the recording methods.
	return &pb.ListRulesResponse{Rules: []*pb.ForwardRule{
		{Id: "rule-a", OwnerUserId: in.OwnerUserId},
		{Id: "rule-x", OwnerUserId: in.OwnerUserId},
	}}, nil
}
func (f *recordingRulesClient) DeleteRule(ctx context.Context, in *pb.DeleteRuleRequest, opts ...grpc.CallOption) (*pb.StatusResponse, error) {
	f.owners = append(f.owners, in.OwnerUserId)
	f.ids = append(f.ids, in.Id)
	return &pb.StatusResponse{Success: true, Message: "deleted"}, nil
}
func (f *recordingRulesClient) PauseRule(ctx context.Context, in *pb.PauseRuleRequest, opts ...grpc.CallOption) (*pb.RuleActionResponse, error) {
	f.owners = append(f.owners, in.OwnerUserId)
	f.ids = append(f.ids, in.RuleId)
	return &pb.RuleActionResponse{Success: true, Message: "paused"}, nil
}
func (f *recordingRulesClient) ResumeRule(ctx context.Context, in *pb.PauseRuleRequest, opts ...grpc.CallOption) (*pb.RuleActionResponse, error) {
	f.owners = append(f.owners, in.OwnerUserId)
	f.ids = append(f.ids, in.RuleId)
	return &pb.RuleActionResponse{Success: true, Message: "resumed"}, nil
}
func (f *recordingRulesClient) TestRule(ctx context.Context, in *pb.TestRuleRequest, opts ...grpc.CallOption) (*pb.TestRuleResponse, error) {
	f.owners = append(f.owners, in.OwnerUserId)
	f.ids = append(f.ids, in.RuleId)
	return &pb.TestRuleResponse{}, nil
}

// TestDeleteRule_ForwardsOwnerIdentity proves the gateway does not strip the
// caller's identity before the core ownership check.
func TestDeleteRule_ForwardsOwnerIdentity(t *testing.T) {
	rc := &recordingRulesClient{}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}
	req := reqWithIdentity(111)
	req.SetPathValue("id", "rule-a")
	req.Body = io.NopCloser(strings.NewReader(""))
	rec := httptest.NewRecorder()
	h.DeleteRule(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(rc.ids) != 1 || rc.ids[0] != "rule-a" {
		t.Fatalf("core received ids=%v", rc.ids)
	}
	if len(rc.owners) != 1 || rc.owners[0] != 111 {
		t.Fatalf("core received owners=%v, want [111]", rc.owners)
	}
}

func TestPauseResume_ForwardOwnerIdentity(t *testing.T) {
	rc := &recordingRulesClient{}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}
	req := reqWithIdentity(222)
	req.SetPathValue("id", "rule-x")
	rec := httptest.NewRecorder()
	h.PauseRule(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("pause want 200, got %d", rec.Code)
	}
	req2 := reqWithIdentity(222)
	req2.SetPathValue("id", "rule-x")
	h.ResumeRule(httptest.NewRecorder(), req2)
	if len(rc.owners) != 2 || rc.owners[0] != 222 || rc.owners[1] != 222 {
		t.Fatalf("core received owners=%v, want [222 222]", rc.owners)
	}
}

func TestTestRule_ForwardsOwnerIdentity(t *testing.T) {
	rc := &recordingRulesClient{}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}
	req := reqWithIdentity(333)
	req.Body = io.NopCloser(strings.NewReader(`{}`))
	rec := httptest.NewRecorder()
	h.TestRule(rec, req)
	if len(rc.owners) != 1 || rc.owners[0] != 333 {
		t.Fatalf("TestRule must forward owner=333, got %v", rc.owners)
	}
}
