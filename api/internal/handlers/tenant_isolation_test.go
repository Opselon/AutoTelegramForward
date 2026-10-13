package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	grpcclient "autoforward/internal/grpcclient"
	pb "autoforward/proto"

	"google.golang.org/grpc"
)

// ---------------------------------------------------------------------------
// Tenant isolation regression tests: user A must never read/write user B's
// rule, filter, or AI config. Handlers enforce this via the owns* guards
// (scoped List* by OwnerUserId) and every list passes OwnerUserId so the
// core only returns the caller's own rows.
// ---------------------------------------------------------------------------

type isoRulesClient struct {
	pb.ForwardRuleControlServiceClient
	rules []*pb.ForwardRule
	got   []int64 // OwnerUserId seen on ListRules
}

func (f *isoRulesClient) ListRules(ctx context.Context, in *pb.ListRulesRequest, opts ...grpc.CallOption) (*pb.ListRulesResponse, error) {
	f.got = append(f.got, in.OwnerUserId)
	// Deliberately return mixed-tenant rows: the handler must enforce the
	// boundary even if a downstream service accidentally ignores OwnerUserId.
	return &pb.ListRulesResponse{Rules: f.rules}, nil
}

func (f *isoRulesClient) UpdateRule(ctx context.Context, in *pb.UpdateRuleRequest, opts ...grpc.CallOption) (*pb.ForwardRule, error) {
	return in.Rule, nil
}

func (f *isoRulesClient) DeleteRule(ctx context.Context, in *pb.DeleteRuleRequest, opts ...grpc.CallOption) (*pb.StatusResponse, error) {
	return &pb.StatusResponse{Success: true, Message: "deleted"}, nil
}

type isoFilterClient struct {
	pb.FilterControlServiceClient
	filters []*pb.FilterRule
	got     []int64
}

func (f *isoFilterClient) ListFilters(ctx context.Context, in *pb.ListFiltersRequest, opts ...grpc.CallOption) (*pb.ListFiltersResponse, error) {
	f.got = append(f.got, in.OwnerUserId)
	return &pb.ListFiltersResponse{Filters: f.filters}, nil
}

func (f *isoFilterClient) UpdateFilter(ctx context.Context, in *pb.UpdateFilterRequest, opts ...grpc.CallOption) (*pb.FilterRule, error) {
	return in.Filter, nil
}

func (f *isoFilterClient) DeleteFilter(ctx context.Context, in *pb.DeleteFilterRequest, opts ...grpc.CallOption) (*pb.StatusResponse, error) {
	return &pb.StatusResponse{Success: true, Message: "deleted"}, nil
}

type isoAIClient struct {
	pb.AIControlServiceClient
	configs []*pb.AIConfig
	got     []int64
}

func (f *isoAIClient) ListAIConfigs(ctx context.Context, in *pb.ListAIConfigsRequest, opts ...grpc.CallOption) (*pb.ListAIConfigsResponse, error) {
	f.got = append(f.got, in.OwnerUserId)
	return &pb.ListAIConfigsResponse{Configs: f.configs}, nil
}

func (f *isoAIClient) UpdateAIConfig(ctx context.Context, in *pb.UpdateAIConfigRequest, opts ...grpc.CallOption) (*pb.AIConfig, error) {
	return in.Config, nil
}

func (f *isoAIClient) DeleteAIConfig(ctx context.Context, in *pb.DeleteAIConfigRequest, opts ...grpc.CallOption) (*pb.StatusResponse, error) {
	return &pb.StatusResponse{Success: true, Message: "deleted"}, nil
}

func isoReq(method string, uid int64, body string) *http.Request {
	req := httptest.NewRequest(method, "/api/x", strings.NewReader(body))
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid}))
}

func isoHandlers() (*Handlers, *isoRulesClient, *isoFilterClient, *isoAIClient) {
	rc := &isoRulesClient{rules: []*pb.ForwardRule{
		{Id: "rule-a", OwnerUserId: 111},
		{Id: "rule-b", OwnerUserId: 222},
	}}
	fc := &isoFilterClient{filters: []*pb.FilterRule{
		{Id: "fil-a", OwnerUserId: 111},
		{Id: "fil-b", OwnerUserId: 222},
	}}
	ac := &isoAIClient{configs: []*pb.AIConfig{
		{Id: "ai-a", OwnerUserId: 111},
		{Id: "ai-b", OwnerUserId: 222},
	}}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc, Filters: fc, AI: ac}}
	return h, rc, fc, ac
}

func TestIsolation_GetRule_ForeignIs404(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("GET", 111, "")
	req.SetPathValue("id", "rule-b") // belongs to tenant 222
	rec := httptest.NewRecorder()
	h.GetRule(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant GetRule must 404, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestIsolation_GetRule_OwnerSeesOwn(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("GET", 111, "")
	req.SetPathValue("id", "rule-a")
	rec := httptest.NewRecorder()
	h.GetRule(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("own GetRule must 200, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestIsolation_UpdateRule_ForeignIs404(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("PUT", 111, "{}")
	req.SetPathValue("id", "rule-b")
	rec := httptest.NewRecorder()
	h.UpdateRule(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant UpdateRule must 404, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestIsolation_DeleteRule_ForeignIs404(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("DELETE", 111, "")
	req.SetPathValue("id", "rule-b")
	rec := httptest.NewRecorder()
	h.DeleteRule(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant DeleteRule must 404, got %d", rec.Code)
	}
}

func TestIsolation_UpdateFilter_ForeignIs404(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("PUT", 111, "{}")
	req.SetPathValue("id", "fil-b")
	rec := httptest.NewRecorder()
	h.UpdateFilter(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant UpdateFilter must 404, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestIsolation_DeleteFilter_ForeignIs404(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("DELETE", 111, "")
	req.SetPathValue("id", "fil-b")
	rec := httptest.NewRecorder()
	h.DeleteFilter(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant DeleteFilter must 404, got %d", rec.Code)
	}
}

func TestIsolation_UpdateFilter_OwnerAllowed(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("PUT", 111, "{}")
	req.SetPathValue("id", "fil-a")
	rec := httptest.NewRecorder()
	h.UpdateFilter(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("own UpdateFilter must 200, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestIsolation_UpdateAIConfig_ForeignIs404(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("PUT", 111, "{}")
	req.SetPathValue("id", "ai-b")
	rec := httptest.NewRecorder()
	h.UpdateAIConfig(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant UpdateAIConfig must 404, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestIsolation_DeleteAIConfig_ForeignIs404(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("DELETE", 111, "")
	req.SetPathValue("id", "ai-b")
	rec := httptest.NewRecorder()
	h.DeleteAIConfig(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant DeleteAIConfig must 404, got %d", rec.Code)
	}
}

func TestIsolation_UpdateAIConfig_OwnerAllowed(t *testing.T) {
	h, _, _, _ := isoHandlers()
	req := isoReq("PUT", 111, "{}")
	req.SetPathValue("id", "ai-a")
	rec := httptest.NewRecorder()
	h.UpdateAIConfig(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("own UpdateAIConfig must 200, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestIsolation_ListRules_NeverLeaksCrossTenant(t *testing.T) {
	h, rc, _, _ := isoHandlers()
	req := isoReq("GET", 111, "")
	rec := httptest.NewRecorder()
	h.ListRules(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("ListRules must 200, got %d", rec.Code)
	}
	if len(rc.got) != 1 || rc.got[0] != 111 {
		t.Fatalf("ListRules must scope core query to caller 111, saw %v", rc.got)
	}
	var body struct {
		Total int              `json:"total"`
		Rules []map[string]any `json:"rules"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Total != 1 {
		t.Fatalf("tenant 111 must see exactly 1 rule, saw total=%d: %s", body.Total, rec.Body.String())
	}
	if body.Rules[0]["id"] != "rule-a" {
		t.Fatalf("leaked wrong rule: %v", body.Rules[0])
	}
}

func TestIsolation_ListFilters_NeverLeaksCrossTenant(t *testing.T) {
	h, _, fc, _ := isoHandlers()
	req := isoReq("GET", 222, "")
	rec := httptest.NewRecorder()
	h.ListFilters(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("ListFilters must 200, got %d", rec.Code)
	}
	if len(fc.got) != 1 || fc.got[0] != 222 {
		t.Fatalf("ListFilters must scope core query to caller 222, saw %v", fc.got)
	}
	var body struct {
		Total   int              `json:"total"`
		Filters []map[string]any `json:"filters"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Total != 1 {
		t.Fatalf("tenant 222 must see exactly 1 filter, saw total=%d: %s", body.Total, rec.Body.String())
	}
}

func TestIsolation_ListAIConfigs_NeverLeaksCrossTenant(t *testing.T) {
	h, _, _, ac := isoHandlers()
	req := isoReq("GET", 111, "")
	rec := httptest.NewRecorder()
	h.ListAIConfigs(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("ListAIConfigs must 200, got %d", rec.Code)
	}
	if len(ac.got) != 1 || ac.got[0] != 111 {
		t.Fatalf("ListAIConfigs must scope core query to caller 111, saw %v", ac.got)
	}
	var body struct {
		Total   int              `json:"total"`
		Configs []map[string]any `json:"configs"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Total != 1 {
		t.Fatalf("tenant 111 must see exactly 1 AI config, saw total=%d: %s", body.Total, rec.Body.String())
	}
}
