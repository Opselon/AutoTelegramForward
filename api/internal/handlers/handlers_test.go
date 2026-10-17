package handlers

import (
	"bytes"
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

// GetRule mirrors the core servicer's owner-scoped single-row fetch:
// returns the rule only when owner_user_id matches (fail-closed).
func (f *fakeRulesClient) GetRule(ctx context.Context, in *pb.GetRuleRequest, opts ...grpc.CallOption) (*pb.ForwardRule, error) {
	if in.OwnerUserId == 0 {
		return nil, status.Error(codes.NotFound, "owner required")
	}
	for _, r := range f.owned {
		if r.Id == in.RuleId && r.OwnerUserId == in.OwnerUserId {
			return r, nil
		}
	}
	return nil, status.Error(codes.NotFound, "rule not found")
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

// countingRulesClient records whether toggle used the owner-scoped single
// fetch (GetRule) or the full list scan (ListRules).
type countingRulesClient struct {
	pb.ForwardRuleControlServiceClient
	owned []*pb.ForwardRule
	lists int
	gets  int
}

func (f *countingRulesClient) ListRules(ctx context.Context, in *pb.ListRulesRequest, opts ...grpc.CallOption) (*pb.ListRulesResponse, error) {
	f.lists++
	out := &pb.ListRulesResponse{}
	for _, r := range f.owned {
		if in.OwnerUserId == 0 || r.OwnerUserId == in.OwnerUserId {
			out.Rules = append(out.Rules, r)
		}
	}
	return out, nil
}

func (f *countingRulesClient) GetRule(ctx context.Context, in *pb.GetRuleRequest, opts ...grpc.CallOption) (*pb.ForwardRule, error) {
	f.gets++
	if in.OwnerUserId == 0 {
		return nil, status.Error(codes.NotFound, "owner required")
	}
	for _, r := range f.owned {
		if r.Id == in.RuleId && r.OwnerUserId == in.OwnerUserId {
			return r, nil
		}
	}
	return nil, status.Error(codes.NotFound, "rule not found")
}

func (f *countingRulesClient) UpdateRule(ctx context.Context, in *pb.UpdateRuleRequest, opts ...grpc.CallOption) (*pb.ForwardRule, error) {
	for i, r := range f.owned {
		if r.Id == in.Rule.Id {
			f.owned[i] = in.Rule
		}
	}
	return in.Rule, nil
}

func (f *countingRulesClient) DeleteRule(ctx context.Context, in *pb.DeleteRuleRequest, opts ...grpc.CallOption) (*pb.StatusResponse, error) {
	return &pb.StatusResponse{Success: true, Message: "deleted"}, nil
}

// ---------------------------------------------------------------------------
// Per-task configuration guards.
// ---------------------------------------------------------------------------

// taskConfigKeys lists every per-task config key the bot writes into
// rule metadata. Keep in sync with core/tests/test_task_config_guard.py
// (ALL_KEYS) and with applyRuleMetadata's parityKeys in handlers.go.
var taskConfigKeys = []string{
	// Replace rules / content modification.
	"replacements", "header", "footer", "watermark", "watermark_text",
	"link_replacement", "clean_words",
	// Processing messages.
	"process_raw_text", "button_to_text", "remove_emojis",
	"block_voice", "block_stickers", "album_mode",
	// AI features.
	"ocr_image_ai", "smart_ai_rewrite", "news_digest",
	"ai_creativity", "ai_max_tokens",
	// Paid add-on catalogue + toggles.
	"paid_addons", "keyword_confirmation", "keyword_reply",
	"auto_delete_seconds", "link_buttons", "affiliate_kit", "crypto_mode",
	"duplicate_filters", "auto_posting", "power_control",
	// Cleaner / sync.
	"cleaner_enabled", "cleaner_config",
	"sync_replies", "sync_edits", "sync_deletes",
	// Language preference.
	"language", "show_forward_header",
}

// TestApplyRuleMetadata_PreservesAllTaskConfigKeys guards the Go write
// path: updating a rule through the API must not drop any per-task
// configuration the bot (or the mobile/web editor) had set.
func TestApplyRuleMetadata_PreservesAllTaskConfigKeys(t *testing.T) {
	raw := map[string]json.RawMessage{}
	for _, k := range taskConfigKeys {
		switch k {
		case "replacements", "paid_addons", "cleaner_config", "duplicate_filters",
			"clean_words", "link_buttons", "affiliate_kit":
			raw[k] = json.RawMessage(`{"k":"v"}`)
		case "ai_creativity":
			raw[k] = json.RawMessage(`0.7`)
		case "ai_max_tokens":
			raw[k] = json.RawMessage(`600`)
		case "process_raw_text", "ocr_image_ai", "smart_ai_rewrite",
			"news_digest", "block_voice", "block_stickers", "remove_emojis",
			"sync_replies", "sync_edits", "sync_deletes", "show_forward_header":
			raw[k] = json.RawMessage(`true`)
		default:
			raw[k] = json.RawMessage(`"value"`)
		}
	}
	meta, _ := json.Marshal(raw)
	body := map[string]any{
		"name":         "guard-rule",
		"source_chat":  -1001,
		"target_chat":  -1002,
		"metadata":     raw,
		"is_active":    true,
		"routing_type": int32(0),
		"forward_mode": int32(1),
	}
	bodyJSON, _ := json.Marshal(body)

	h := &Handlers{c: &grpcclient.Clients{
		Rules: &fakeRulesClient{owned: []*pb.ForwardRule{
			{Id: "guard", OwnerUserId: 111, CustomMetadataJson: string(meta)},
		}},
	}}
	req := reqWithIdentity(111)
	req.SetPathValue("id", "guard")
	req.Body = io.NopCloser(bytes.NewReader(bodyJSON))
	rec := httptest.NewRecorder()
	h.UpdateRule(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("update must succeed, got %d: %s", rec.Code, rec.Body.String())
	}

	// The applied rule must retain every task-config key.
	applied := appliedRuleMetadata(t, h)
	for _, k := range taskConfigKeys {
		if _, ok := applied[k]; !ok {
			t.Errorf("task-config key %q was dropped by applyRuleMetadata", k)
		}
	}
}

// TestToggleRule_UsesOwnerScopedFetch guards the scale fix: toggle must
// fetch exactly the caller's rule and never another tenant's.
func TestToggleRule_UsesOwnerScopedFetch(t *testing.T) {
	rc := &countingRulesClient{owned: []*pb.ForwardRule{
		{Id: "r1", OwnerUserId: 111, IsActive: true},
		{Id: "r2", OwnerUserId: 222, IsActive: true},
	}}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}
	req := reqWithIdentity(222)
	req.SetPathValue("id", "r2")
	rec := httptest.NewRecorder()
	h.ToggleRule(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("own rule toggle must 200, got %d: %s", rec.Code, rec.Body.String())
	}

	// r2 must now be inactive; r1 (another tenant) must be untouched.
	for _, r := range rc.owned {
		if r.Id == "r2" && r.IsActive {
			t.Errorf("toggle did not flip the caller's rule")
		}
		if r.Id == "r1" && !r.IsActive {
			t.Errorf("toggle crossed tenants: flipped r1 belonging to 111")
		}
	}
	// The single-row fetch must have been used, not the list scan.
	if rc.gets != 1 {
		t.Errorf("expected exactly one owner-scoped GetRule call, got %d", rc.gets)
	}
	if rc.lists != 0 {
		t.Errorf("toggle must not scan the owner's rule list, made %d ListRules calls", rc.lists)
	}
}

// appliedRuleMetadata extracts the metadata the API would write back.
func appliedRuleMetadata(t *testing.T, h *Handlers) map[string]any {
	t.Helper()
	resp, err := h.c.Rules.GetRule(context.Background(), &pb.GetRuleRequest{RuleId: "guard", OwnerUserId: 111})
	if err != nil || resp == nil {
		t.Fatalf("could not read back guard rule: %v", err)
	}
	out := map[string]any{}
	if err := json.Unmarshal([]byte(resp.CustomMetadataJson), &out); err != nil {
		t.Fatalf("metadata is not valid JSON: %v", err)
	}
	return out
}
