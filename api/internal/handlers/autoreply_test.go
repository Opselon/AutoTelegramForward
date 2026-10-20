package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	grpcclient "autoforward/internal/grpcclient"
	pb "autoforward/proto"
)

// fakeAutoReplyClient is a tenant-aware stand-in for the core servicer. It
// refuses owner 0 the way the real one does, and answers a foreign id exactly
// like a missing one — so a handler that forgot to forward the identity, or
// that invented its own ownership rule, fails these tests.
type fakeAutoReplyClient struct {
	pb.AutoReplyControlServiceClient

	settings map[int64]*pb.AutoReplySettings
	prompts  map[int64][]*pb.AutoReplyPrompt
	targets  map[int64][]*pb.AutoReplyTarget
	rules    map[int64][]*pb.AutoReplyRule
	jobs     map[int64][]*pb.AutoReplyJob
	stats    *pb.AutoReplyStatsResponse
	preview  *pb.PreviewAutoReplyResponse
	resolved *pb.AutoReplyTarget

	calls              []string
	lastSettingsUpdate *pb.UpdateAutoReplySettingsRequest
	lastPreview        *pb.PreviewAutoReplyRequest
	lastRule           *pb.SaveAutoReplyRuleRequest
	lastImport         *pb.ImportAutoReplyPromptsRequest
	lastExport         *pb.ExportAutoReplyPromptsRequest
	lastRemove         *pb.RemoveAutoReplyTargetsRequest
	lastTargetUpdate   *pb.UpdateAutoReplyTargetRequest
	lastPromptList     *pb.ListAutoReplyPromptsRequest
	lastJobList        *pb.ListAutoReplyJobsRequest
}

func newFakeAutoReplyClient() *fakeAutoReplyClient {
	return &fakeAutoReplyClient{
		settings: map[int64]*pb.AutoReplySettings{},
		prompts:  map[int64][]*pb.AutoReplyPrompt{},
		targets:  map[int64][]*pb.AutoReplyTarget{},
		rules:    map[int64][]*pb.AutoReplyRule{},
		jobs:     map[int64][]*pb.AutoReplyJob{},
	}
}

func autoReplyHandler(f *fakeAutoReplyClient) *Handlers {
	return &Handlers{c: &grpcclient.Clients{AutoReply: f}}
}

func (f *fakeAutoReplyClient) method(name string) error {
	f.calls = append(f.calls, name)
	return nil
}

func (f *fakeAutoReplyClient) GetAutoReplySettings(_ context.Context, in *pb.GetAutoReplySettingsRequest, _ ...grpc.CallOption) (*pb.AutoReplySettings, error) {
	_ = f.method("GetAutoReplySettings")
	if in.OwnerUserId <= 0 {
		return &pb.AutoReplySettings{}, nil
	}
	if s, ok := f.settings[in.OwnerUserId]; ok {
		return s, nil
	}
	return &pb.AutoReplySettings{OwnerUserId: in.OwnerUserId, TargetMode: "EVERYONE"}, nil
}

func (f *fakeAutoReplyClient) UpdateAutoReplySettings(_ context.Context, in *pb.UpdateAutoReplySettingsRequest, _ ...grpc.CallOption) (*pb.AutoReplySettings, error) {
	_ = f.method("UpdateAutoReplySettings")
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.Unauthenticated, "owner_required")
	}
	f.lastSettingsUpdate = in
	return &pb.AutoReplySettings{
		OwnerUserId:     in.OwnerUserId,
		Enabled:         in.GetSettings().GetEnabled(),
		CooldownSeconds: in.GetSettings().GetCooldownSeconds(),
		TargetMode:      "EVERYONE",
	}, nil
}

func (f *fakeAutoReplyClient) ListAutoReplyPrompts(_ context.Context, in *pb.ListAutoReplyPromptsRequest, _ ...grpc.CallOption) (*pb.ListAutoReplyPromptsResponse, error) {
	_ = f.method("ListAutoReplyPrompts")
	if in.OwnerUserId <= 0 {
		return &pb.ListAutoReplyPromptsResponse{}, nil
	}
	f.lastPromptList = in
	return &pb.ListAutoReplyPromptsResponse{Prompts: f.prompts[in.OwnerUserId]}, nil
}

func (f *fakeAutoReplyClient) SaveAutoReplyPrompt(_ context.Context, in *pb.SaveAutoReplyPromptRequest, _ ...grpc.CallOption) (*pb.AutoReplyPrompt, error) {
	_ = f.method("SaveAutoReplyPrompt")
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.Unauthenticated, "owner_required")
	}
	prompt := in.GetPrompt()
	if prompt == nil {
		return nil, status.Error(codes.InvalidArgument, "prompt_invalid")
	}
	if prompt.Id == "" {
		prompt.Id = "p-new"
	}
	prompt.CreatedAt = 1700000000
	f.prompts[in.OwnerUserId] = append(f.prompts[in.OwnerUserId], prompt)
	return prompt, nil
}

func (f *fakeAutoReplyClient) DeleteAutoReplyPrompt(_ context.Context, in *pb.DeleteAutoReplyPromptRequest, _ ...grpc.CallOption) (*pb.StatusResponse, error) {
	_ = f.method("DeleteAutoReplyPrompt")
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.Unauthenticated, "owner_required")
	}
	list := f.prompts[in.OwnerUserId]
	for i, p := range list {
		if p.GetId() == in.PromptId {
			f.prompts[in.OwnerUserId] = append(list[:i], list[i+1:]...)
			return &pb.StatusResponse{Success: true, Message: "deleted"}, nil
		}
	}
	// A foreign prompt is indistinguishable from a missing one.
	return nil, status.Error(codes.NotFound, "prompt_not_found: that prompt does not exist for this account")
}

func (f *fakeAutoReplyClient) DuplicateAutoReplyPrompt(_ context.Context, in *pb.DuplicateAutoReplyPromptRequest, _ ...grpc.CallOption) (*pb.AutoReplyPrompt, error) {
	_ = f.method("DuplicateAutoReplyPrompt")
	for _, p := range f.prompts[in.OwnerUserId] {
		if p.GetId() == in.PromptId {
			name := in.Name
			if name == "" {
				name = p.GetName() + " (copy)"
			}
			return &pb.AutoReplyPrompt{Id: "p-copy", Name: name, SystemPrompt: p.GetSystemPrompt()}, nil
		}
	}
	return nil, status.Error(codes.NotFound, "prompt_not_found")
}

func (f *fakeAutoReplyClient) ExportAutoReplyPrompts(_ context.Context, in *pb.ExportAutoReplyPromptsRequest, _ ...grpc.CallOption) (*pb.ExportAutoReplyPromptsResponse, error) {
	_ = f.method("ExportAutoReplyPrompts")
	f.lastExport = in
	wanted := map[string]bool{}
	for _, id := range in.PromptIds {
		wanted[id] = true
	}
	type portable struct {
		Name         string `json:"name"`
		SystemPrompt string `json:"system_prompt"`
	}
	out := struct {
		SchemaVersion int        `json:"schema_version"`
		Kind          string     `json:"kind"`
		Prompts       []portable `json:"prompts"`
	}{SchemaVersion: 1, Kind: "autoreply_prompts", Prompts: []portable{}}
	for _, p := range f.prompts[in.OwnerUserId] {
		if len(wanted) == 0 || wanted[p.GetId()] {
			out.Prompts = append(out.Prompts, portable{Name: p.GetName(), SystemPrompt: p.GetSystemPrompt()})
		}
	}
	encoded, _ := json.Marshal(out)
	return &pb.ExportAutoReplyPromptsResponse{DocumentJson: string(encoded)}, nil
}

func (f *fakeAutoReplyClient) ImportAutoReplyPrompts(_ context.Context, in *pb.ImportAutoReplyPromptsRequest, _ ...grpc.CallOption) (*pb.ImportAutoReplyPromptsResponse, error) {
	_ = f.method("ImportAutoReplyPrompts")
	f.lastImport = in
	var doc struct {
		Prompts []struct {
			Name string `json:"name"`
		} `json:"prompts"`
	}
	if err := json.Unmarshal([]byte(in.DocumentJson), &doc); err != nil {
		return nil, status.Error(codes.InvalidArgument, "import_invalid: not json")
	}
	imported := make([]*pb.AutoReplyPrompt, 0, len(doc.Prompts))
	for i, p := range doc.Prompts {
		imported = append(imported, &pb.AutoReplyPrompt{Id: fmt.Sprintf("imp-%d", i), Name: p.Name})
	}
	return &pb.ImportAutoReplyPromptsResponse{Imported: int32(len(imported)), Prompts: imported}, nil
}

func (f *fakeAutoReplyClient) ListAutoReplyTargets(_ context.Context, in *pb.ListAutoReplyTargetsRequest, _ ...grpc.CallOption) (*pb.ListAutoReplyTargetsResponse, error) {
	_ = f.method("ListAutoReplyTargets")
	if in.OwnerUserId <= 0 {
		return &pb.ListAutoReplyTargetsResponse{}, nil
	}
	rows := f.targets[in.OwnerUserId]
	total, enabled := len(rows), 0
	for _, t := range rows {
		if t.GetIsEnabled() {
			enabled++
		}
	}
	return &pb.ListAutoReplyTargetsResponse{Targets: rows, Total: int32(total), Enabled: int32(enabled)}, nil
}

func (f *fakeAutoReplyClient) AddAutoReplyTargets(_ context.Context, in *pb.AddAutoReplyTargetsRequest, _ ...grpc.CallOption) (*pb.ListAutoReplyTargetsResponse, error) {
	_ = f.method("AddAutoReplyTargets")
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.Unauthenticated, "owner_required")
	}
	f.targets[in.OwnerUserId] = append(f.targets[in.OwnerUserId], in.Targets...)
	return f.ListAutoReplyTargets(context.Background(), &pb.ListAutoReplyTargetsRequest{OwnerUserId: in.OwnerUserId})
}

func (f *fakeAutoReplyClient) UpdateAutoReplyTarget(_ context.Context, in *pb.UpdateAutoReplyTargetRequest, _ ...grpc.CallOption) (*pb.AutoReplyTarget, error) {
	_ = f.method("UpdateAutoReplyTarget")
	f.lastTargetUpdate = in
	for _, t := range f.targets[in.OwnerUserId] {
		if t.GetChatId() == in.GetTarget().GetChatId() {
			return in.GetTarget(), nil
		}
	}
	return nil, status.Error(codes.NotFound, "target_not_found")
}

func (f *fakeAutoReplyClient) RemoveAutoReplyTargets(_ context.Context, in *pb.RemoveAutoReplyTargetsRequest, _ ...grpc.CallOption) (*pb.StatusResponse, error) {
	_ = f.method("RemoveAutoReplyTargets")
	f.lastRemove = in
	remove := map[string]bool{}
	for _, id := range in.ChatIds {
		remove[id] = true
	}
	kept := make([]*pb.AutoReplyTarget, 0, len(f.targets[in.OwnerUserId]))
	removed := 0
	for _, t := range f.targets[in.OwnerUserId] {
		if remove[t.GetChatId()] {
			removed++
			continue
		}
		kept = append(kept, t)
	}
	f.targets[in.OwnerUserId] = kept
	return &pb.StatusResponse{Success: true, Message: fmt.Sprintf("removed=%d", removed)}, nil
}

func (f *fakeAutoReplyClient) ResolveAutoReplyChat(_ context.Context, in *pb.ResolveAutoReplyChatRequest, _ ...grpc.CallOption) (*pb.ResolveAutoReplyChatResponse, error) {
	_ = f.method("ResolveAutoReplyChat")
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.Unauthenticated, "owner_required")
	}
	if f.resolved == nil {
		return &pb.ResolveAutoReplyChatResponse{Found: false, ErrorCode: "not_found"}, nil
	}
	return &pb.ResolveAutoReplyChatResponse{Found: true, Target: f.resolved}, nil
}

func (f *fakeAutoReplyClient) ListAutoReplyRules(_ context.Context, in *pb.ListAutoReplyRulesRequest, _ ...grpc.CallOption) (*pb.ListAutoReplyRulesResponse, error) {
	_ = f.method("ListAutoReplyRules")
	if in.OwnerUserId <= 0 {
		return &pb.ListAutoReplyRulesResponse{}, nil
	}
	return &pb.ListAutoReplyRulesResponse{Rules: f.rules[in.OwnerUserId]}, nil
}

func (f *fakeAutoReplyClient) SaveAutoReplyRule(_ context.Context, in *pb.SaveAutoReplyRuleRequest, _ ...grpc.CallOption) (*pb.AutoReplyRule, error) {
	_ = f.method("SaveAutoReplyRule")
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.Unauthenticated, "owner_required")
	}
	f.lastRule = in
	rule := in.GetRule()
	if rule.Id == "" {
		rule.Id = "r-new"
	}
	f.rules[in.OwnerUserId] = append(f.rules[in.OwnerUserId], rule)
	return rule, nil
}

func (f *fakeAutoReplyClient) DeleteAutoReplyRule(_ context.Context, in *pb.DeleteAutoReplyRuleRequest, _ ...grpc.CallOption) (*pb.StatusResponse, error) {
	_ = f.method("DeleteAutoReplyRule")
	for _, rule := range f.rules[in.OwnerUserId] {
		if rule.GetId() == in.RuleId {
			return &pb.StatusResponse{Success: true, Message: "deleted"}, nil
		}
	}
	return nil, status.Error(codes.NotFound, "rule_not_found")
}

func (f *fakeAutoReplyClient) ReorderAutoReplyRules(_ context.Context, in *pb.ReorderAutoReplyRulesRequest, _ ...grpc.CallOption) (*pb.ListAutoReplyRulesResponse, error) {
	_ = f.method("ReorderAutoReplyRules")
	byID := map[string]*pb.AutoReplyRule{}
	for _, rule := range f.rules[in.OwnerUserId] {
		byID[rule.GetId()] = rule
	}
	ordered := make([]*pb.AutoReplyRule, 0, len(in.RuleIds))
	for _, id := range in.RuleIds {
		if rule, ok := byID[id]; ok {
			ordered = append(ordered, rule)
		}
	}
	return &pb.ListAutoReplyRulesResponse{Rules: ordered}, nil
}

func (f *fakeAutoReplyClient) ListAutoReplyJobs(_ context.Context, in *pb.ListAutoReplyJobsRequest, _ ...grpc.CallOption) (*pb.ListAutoReplyJobsResponse, error) {
	_ = f.method("ListAutoReplyJobs")
	if in.OwnerUserId <= 0 {
		return &pb.ListAutoReplyJobsResponse{}, nil
	}
	f.lastJobList = in
	rows := f.jobs[in.OwnerUserId]
	return &pb.ListAutoReplyJobsResponse{Jobs: rows, Total: int32(len(rows))}, nil
}

func (f *fakeAutoReplyClient) jobAction(in *pb.AutoReplyJobRequest, status *pb.StatusResponse) (*pb.StatusResponse, error) {
	for _, job := range f.jobs[in.OwnerUserId] {
		if job.GetId() == in.JobId {
			return status, nil
		}
	}
	return nil, statusp(codes.NotFound, "job_not_found")
}

func (f *fakeAutoReplyClient) CancelAutoReplyJob(_ context.Context, in *pb.AutoReplyJobRequest, _ ...grpc.CallOption) (*pb.StatusResponse, error) {
	_ = f.method("CancelAutoReplyJob")
	return f.jobAction(in, &pb.StatusResponse{Success: true, Message: "cancelled"})
}

func (f *fakeAutoReplyClient) RetryAutoReplyJob(_ context.Context, in *pb.AutoReplyJobRequest, _ ...grpc.CallOption) (*pb.StatusResponse, error) {
	_ = f.method("RetryAutoReplyJob")
	return f.jobAction(in, &pb.StatusResponse{Success: true, Message: "requeued"})
}

func (f *fakeAutoReplyClient) GetAutoReplyStats(_ context.Context, in *pb.GetAutoReplyStatsRequest, _ ...grpc.CallOption) (*pb.AutoReplyStatsResponse, error) {
	_ = f.method("GetAutoReplyStats")
	if in.OwnerUserId <= 0 {
		return &pb.AutoReplyStatsResponse{StatusJson: "{}"}, nil
	}
	if f.stats != nil {
		return f.stats, nil
	}
	return &pb.AutoReplyStatsResponse{StatusJson: "{}"}, nil
}

func (f *fakeAutoReplyClient) PreviewAutoReply(_ context.Context, in *pb.PreviewAutoReplyRequest, _ ...grpc.CallOption) (*pb.PreviewAutoReplyResponse, error) {
	_ = f.method("PreviewAutoReply")
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.Unauthenticated, "owner_required")
	}
	f.lastPreview = in
	if f.preview != nil {
		return f.preview, nil
	}
	return &pb.PreviewAutoReplyResponse{Ok: true}, nil
}

// statusp keeps the fake's job lookup terse without importing a second name for
// the status package at the call sites.
func statusp(code codes.Code, msg string) error { return status.Error(code, msg) }

// autoReplyRequest builds a request with the verified identity attached the way
// RequireAuth attaches it. uid 0 means "no identity at all", which is the state
// the handler must survive without leaking rows.
func autoReplyRequest(uid int64, method, target, body string, pathValues map[string]string) *http.Request {
	var req *http.Request
	if body == "" {
		req = httptest.NewRequest(method, target, nil)
	} else {
		req = httptest.NewRequest(method, target, strings.NewReader(body))
	}
	if uid > 0 {
		req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid}))
	}
	for name, value := range pathValues {
		req.SetPathValue(name, value)
	}
	return req
}

func decodeAutoReplyBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("response is not a JSON object: %v (%s)", err, rec.Body.String())
	}
	return body
}

func autoReplyObjectField(t *testing.T, body map[string]any, key string) map[string]any {
	t.Helper()
	value, ok := body[key].(map[string]any)
	if !ok {
		t.Fatalf("%q must be a JSON object, got %T (%v)", key, body[key], body[key])
	}
	return value
}

func autoReplyListField(t *testing.T, body map[string]any, key string) []any {
	t.Helper()
	value, ok := body[key].([]any)
	if !ok {
		t.Fatalf("%q must be a JSON array, got %T (%v)", key, body[key], body[key])
	}
	return value
}

// ---------------------------------------------------------------------------
// No identity => no rows, and no core round trip.
// ---------------------------------------------------------------------------

func TestAutoReplyReads_WithoutIdentityLeakNothing(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.prompts[111] = []*pb.AutoReplyPrompt{{Id: "p1", Name: "tenant-one-secret"}}
	f.targets[111] = []*pb.AutoReplyTarget{{ChatId: "42", Title: "tenant-one-secret"}}
	f.rules[111] = []*pb.AutoReplyRule{{Id: "r1", Name: "tenant-one-secret"}}
	f.jobs[111] = []*pb.AutoReplyJob{{Id: "j1", ChatTitle: "tenant-one-secret"}}
	h := autoReplyHandler(f)

	cases := []struct {
		name    string
		path    string
		handler http.HandlerFunc
		key     string
	}{
		{"prompts", "/auto-reply/prompts", h.ListAutoReplyPrompts, "prompts"},
		{"targets", "/auto-reply/targets", h.ListAutoReplyTargets, "targets"},
		{"rules", "/auto-reply/rules", h.ListAutoReplyRules, "rules"},
		{"jobs", "/auto-reply/jobs", h.ListAutoReplyJobs, "jobs"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			tc.handler(rec, autoReplyRequest(0, http.MethodGet, tc.path, "", nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("want 200 with an empty page, got %d: %s", rec.Code, rec.Body.String())
			}
			body := decodeAutoReplyBody(t, rec)
			if items := autoReplyListField(t, body, tc.key); len(items) != 0 {
				t.Fatalf("unauthenticated read leaked %d rows: %s", len(items), rec.Body.String())
			}
			if strings.Contains(rec.Body.String(), "tenant-one-secret") {
				t.Fatalf("another tenant's row reached an unauthenticated response: %s", rec.Body.String())
			}
		})
	}

	t.Run("settings", func(t *testing.T) {
		rec := httptest.NewRecorder()
		h.GetAutoReplySettings(rec, autoReplyRequest(0, http.MethodGet, "/auto-reply/settings", "", nil))
		if rec.Code != http.StatusOK {
			t.Fatalf("want 200 defaults, got %d", rec.Code)
		}
		settings := autoReplyObjectField(t, decodeAutoReplyBody(t, rec), "settings")
		if settings["target_mode"] != "EVERYONE" {
			t.Fatalf("defaults must mirror the core: %v", settings)
		}
		if _, present := settings["owner_user_id"]; present {
			t.Fatalf("settings must not echo an owner id: %v", settings)
		}
	})

	t.Run("stats", func(t *testing.T) {
		rec := httptest.NewRecorder()
		h.GetAutoReplyStats(rec, autoReplyRequest(0, http.MethodGet, "/auto-reply/stats", "", nil))
		if rec.Code != http.StatusOK {
			t.Fatalf("want 200 zeroed stats, got %d", rec.Code)
		}
		stats := autoReplyObjectField(t, decodeAutoReplyBody(t, rec), "stats")
		if stats["sent_total"].(float64) != 0 {
			t.Fatalf("unauthenticated stats must be zero: %v", stats)
		}
		if _, ok := stats["statuses"].(map[string]any); !ok {
			t.Fatalf("statuses must be an object even when empty: %v", stats)
		}
	})

	if len(f.calls) != 0 {
		t.Fatalf("a read without identity must not reach the core, calls=%v", f.calls)
	}
}

func TestAutoReplyMutations_WithoutIdentityAreRefusedBeforeTheCore(t *testing.T) {
	f := newFakeAutoReplyClient()
	h := autoReplyHandler(f)

	cases := []struct {
		name    string
		method  string
		path    string
		body    string
		handler http.HandlerFunc
		paths   map[string]string
	}{
		{"settings", http.MethodPut, "/auto-reply/settings", `{"enabled":true}`, h.UpdateAutoReplySettings, nil},
		{"create prompt", http.MethodPost, "/auto-reply/prompts", `{"name":"x"}`, h.CreateAutoReplyPrompt, nil},
		{"delete prompt", http.MethodDelete, "/auto-reply/prompts/p1", "", h.DeleteAutoReplyPrompt, map[string]string{"id": "p1"}},
		{"duplicate prompt", http.MethodPost, "/auto-reply/prompts/p1/duplicate", `{}`, h.DuplicateAutoReplyPrompt, map[string]string{"id": "p1"}},
		{"export prompts", http.MethodGet, "/auto-reply/prompts/export", "", h.ExportAutoReplyPrompts, nil},
		{"import prompts", http.MethodPost, "/auto-reply/prompts/import", `{"prompts":[]}`, h.ImportAutoReplyPrompts, nil},
		{"add targets", http.MethodPost, "/auto-reply/targets", `{"targets":[{"chat_id":"42"}]}`, h.AddAutoReplyTargets, nil},
		{"remove targets", http.MethodDelete, "/auto-reply/targets", `{"chat_ids":["42"]}`, h.RemoveAutoReplyTargets, nil},
		{"update target", http.MethodPut, "/auto-reply/targets/42", `{"is_enabled":true}`, h.UpdateAutoReplyTarget, map[string]string{"chatId": "42"}},
		{"resolve chat", http.MethodPost, "/auto-reply/targets/resolve", `{"query":"bob"}`, h.ResolveAutoReplyChat, nil},
		{"create rule", http.MethodPost, "/auto-reply/rules", `{"name":"r"}`, h.CreateAutoReplyRule, nil},
		{"update rule", http.MethodPut, "/auto-reply/rules/r1", `{"name":"r"}`, h.UpdateAutoReplyRule, map[string]string{"id": "r1"}},
		{"delete rule", http.MethodDelete, "/auto-reply/rules/r1", "", h.DeleteAutoReplyRule, map[string]string{"id": "r1"}},
		{"reorder rules", http.MethodPost, "/auto-reply/rules/reorder", `{"rule_ids":["r1"]}`, h.ReorderAutoReplyRules, nil},
		{"retry job", http.MethodPost, "/auto-reply/jobs/j1/retry", "", h.RetryAutoReplyJob, map[string]string{"id": "j1"}},
		{"cancel job", http.MethodPost, "/auto-reply/jobs/j1/cancel", "", h.CancelAutoReplyJob, map[string]string{"id": "j1"}},
		{"preview", http.MethodPost, "/auto-reply/preview", `{"inbound_text":"hi"}`, h.PreviewAutoReply, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			tc.handler(rec, autoReplyRequest(0, tc.method, tc.path, tc.body, tc.paths))
			if rec.Code != http.StatusUnauthorized {
				t.Fatalf("want 401 without identity, got %d: %s", rec.Code, rec.Body.String())
			}
		})
	}
	if len(f.calls) != 0 {
		t.Fatalf("no mutation may reach the core without identity, calls=%v", f.calls)
	}
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

func TestAutoReplySettingsPut_ForwardsOnlyTheMaskAndTheVerifiedOwner(t *testing.T) {
	f := newFakeAutoReplyClient()
	h := autoReplyHandler(f)
	// The body names another owner and carries a field the mask does not
	// include: neither may be trusted or written.
	body := `{"enabled":true,"cooldown_seconds":30,"owner_user_id":999999,"update_mask":["cooldown_seconds"]}`
	rec := httptest.NewRecorder()
	h.UpdateAutoReplySettings(rec, autoReplyRequest(111, http.MethodPut, "/auto-reply/settings", body, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	req := f.lastSettingsUpdate
	if req == nil {
		t.Fatal("the settings update never reached the core")
	}
	if req.OwnerUserId != 111 {
		t.Fatalf("owner must come from the identity, got %d", req.OwnerUserId)
	}
	if req.GetSettings().GetOwnerUserId() != 111 {
		t.Fatalf("the settings payload must be stamped with the caller, got %d", req.GetSettings().GetOwnerUserId())
	}
	if len(req.UpdateMask) != 1 || req.UpdateMask[0] != "cooldown_seconds" {
		t.Fatalf("update_mask must be forwarded verbatim, got %v", req.UpdateMask)
	}
	if req.GetSettings().GetCooldownSeconds() != 30 {
		t.Fatalf("cooldown_seconds not forwarded: %+v", req.GetSettings())
	}
	settings := autoReplyObjectField(t, decodeAutoReplyBody(t, rec), "settings")
	if settings["cooldown_seconds"].(float64) != 30 {
		t.Fatalf("response must echo the effective settings: %v", settings)
	}
}

// A settings form that reads the object back and resubmits it must be accepted:
// the GET projection and the PUT decoder share one field vocabulary, so strict
// decoding cannot turn the documented round trip into a 400.
func TestAutoReplySettings_GetThenPutRoundTrips(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.settings[111] = &pb.AutoReplySettings{
		OwnerUserId: 111, Enabled: true, AiConfigId: "ai1", PromptId: "p1",
		TargetMode: "SELECTED_ONLY", Language: "fa", Tone: "friendly",
		ResponseLength: "short", Temperature: 0.7, MaxTokens: 300,
		ContextMessages: 6, MemoryEnabled: true, MemoryMaxFacts: 12,
		MaxReplyChars: 1200, TypingSimulation: true, TypingDelayMin: 1.5, TypingDelayMax: 4,
		CooldownSeconds: 15, RateLimitPerMinute: 20, MaxConcurrency: 3, MaxAttempts: 3,
		RetryBackoffSeconds: 20, QuietHoursStart: 23, QuietHoursEnd: 7,
		TimezoneOffsetMinutes: 210, ActiveDays: []int32{0, 1, 2, 3, 4},
		FallbackText: "later", IgnoreBots: true, Version: 4,
	}
	h := autoReplyHandler(f)

	rec := httptest.NewRecorder()
	h.GetAutoReplySettings(rec, autoReplyRequest(111, http.MethodGet, "/auto-reply/settings", "", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("get failed: %d %s", rec.Code, rec.Body.String())
	}
	echoed := autoReplyObjectField(t, decodeAutoReplyBody(t, rec), "settings")
	encoded, err := json.Marshal(echoed)
	if err != nil {
		t.Fatalf("re-encode: %v", err)
	}

	rec = httptest.NewRecorder()
	h.UpdateAutoReplySettings(rec, autoReplyRequest(111, http.MethodPut, "/auto-reply/settings", string(encoded), nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("a settings object read from this API must be writable again, got %d: %s", rec.Code, rec.Body.String())
	}
	got := f.lastSettingsUpdate.GetSettings()
	if got.GetAiConfigId() != "ai1" || got.GetQuietHoursEnd() != 7 || len(got.GetActiveDays()) != 5 {
		t.Fatalf("round trip lost settings: %+v", got)
	}
}

func TestAutoReplySettingsPut_RejectsATypoInsteadOfIgnoringIt(t *testing.T) {
	f := newFakeAutoReplyClient()
	h := autoReplyHandler(f)
	rec := httptest.NewRecorder()
	h.UpdateAutoReplySettings(rec, autoReplyRequest(111, http.MethodPut, "/auto-reply/settings", `{"cool_down_seconds":5}`, nil))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("an unknown settings key must be a 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if f.lastSettingsUpdate != nil {
		t.Fatal("a rejected body must not reach the core")
	}
}

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

func TestAutoReplyPrompts_ListForwardsFiltersAndProjectsSnakeCase(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.prompts[111] = []*pb.AutoReplyPrompt{{
		Id: "p1", Name: "Mine", SystemPrompt: "be kind", Tags: []string{"support"}, IsDefault: true,
	}}
	h := autoReplyHandler(f)
	rec := httptest.NewRecorder()
	h.ListAutoReplyPrompts(rec, autoReplyRequest(111, http.MethodGet, "/auto-reply/prompts?search=min&include_archived=1", "", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if f.lastPromptList == nil || f.lastPromptList.Search != "min" || !f.lastPromptList.IncludeArchived {
		t.Fatalf("search/include_archived not forwarded: %+v", f.lastPromptList)
	}
	prompt := autoReplyListField(t, decodeAutoReplyBody(t, rec), "prompts")[0].(map[string]any)
	if prompt["system_prompt"] != "be kind" || prompt["is_default"] != true {
		t.Fatalf("prompt not projected in snake_case: %v", prompt)
	}
	// An empty string must still be present: omitempty on the pb struct used to
	// drop exactly the values a form needs to render.
	if _, present := prompt["user_prompt_template"]; !present {
		t.Fatalf("optional fields must be present, not omitted: %v", prompt)
	}
	if _, leaked := prompt["SystemPrompt"]; leaked {
		t.Fatalf("a Go field name leaked into the response: %v", prompt)
	}
}

func TestAutoReplyDeletePrompt_ForeignIdIs404(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.prompts[111] = []*pb.AutoReplyPrompt{{Id: "p1", Name: "mine"}}
	h := autoReplyHandler(f)

	rec := httptest.NewRecorder()
	h.DeleteAutoReplyPrompt(rec, autoReplyRequest(222, http.MethodDelete, "/auto-reply/prompts/p1", "", map[string]string{"id": "p1"}))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("a foreign prompt must be 404, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(f.prompts[111]) != 1 {
		t.Fatal("a foreign delete must not remove the owner's row")
	}

	rec = httptest.NewRecorder()
	h.DeleteAutoReplyPrompt(rec, autoReplyRequest(111, http.MethodDelete, "/auto-reply/prompts/p1", "", map[string]string{"id": "p1"}))
	if rec.Code != http.StatusOK {
		t.Fatalf("the owner's delete must succeed, got %d: %s", rec.Code, rec.Body.String())
	}
	if body := decodeAutoReplyBody(t, rec); body["success"] != true {
		t.Fatalf("delete must answer {\"success\":true}: %s", rec.Body.String())
	}
}

func TestAutoReplyPrompts_ExportThenImportRoundTripsTheDocument(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.prompts[111] = []*pb.AutoReplyPrompt{{Id: "p1", Name: "Support", SystemPrompt: "be kind"}}
	h := autoReplyHandler(f)

	rec := httptest.NewRecorder()
	h.ExportAutoReplyPrompts(rec, autoReplyRequest(111, http.MethodGet, "/auto-reply/prompts/export?ids=p1", "", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("export failed: %d %s", rec.Code, rec.Body.String())
	}
	document := autoReplyObjectField(t, decodeAutoReplyBody(t, rec), "document")
	if document["schema_version"].(float64) != 1 {
		t.Fatalf("document must be the core's object, not a string: %v", document)
	}
	if f.lastExport == nil || len(f.lastExport.PromptIds) != 1 || f.lastExport.PromptIds[0] != "p1" {
		t.Fatalf("ids filter not forwarded: %+v", f.lastExport)
	}

	rec = httptest.NewRecorder()
	h.ImportAutoReplyPrompts(rec, autoReplyRequest(111, http.MethodPost, "/auto-reply/prompts/import",
		`{"schema_version":1,"prompts":[{"name":"Imported"}]}`, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("import failed: %d %s", rec.Code, rec.Body.String())
	}
	body := decodeAutoReplyBody(t, rec)
	if body["imported"].(float64) != 1 || body["skipped"].(float64) != 0 {
		t.Fatalf("import counts wrong: %v", body)
	}
	if f.lastImport.OwnerUserId != 111 || !strings.Contains(f.lastImport.DocumentJson, "Imported") {
		t.Fatalf("document not forwarded verbatim with the verified owner: %+v", f.lastImport)
	}

	rec = httptest.NewRecorder()
	h.ImportAutoReplyPrompts(rec, autoReplyRequest(111, http.MethodPost, "/auto-reply/prompts/import", `{"schema_version":1}`, nil))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("a document without prompts must be a 400, got %d", rec.Code)
	}
}

// ---------------------------------------------------------------------------
// Rules
// ---------------------------------------------------------------------------

func TestAutoReplyRules_ConditionsTravelAsObjectsAndPathIdWins(t *testing.T) {
	f := newFakeAutoReplyClient()
	h := autoReplyHandler(f)

	body := `{"name":"Pricing","is_enabled":true,"priority":10,"action":"REPLY",` +
		`"conditions":{"keywords_any":["price","هزینه"],"chat_ids":["42"]},"prompt_id":"p1","cooldown_seconds":0}`
	rec := httptest.NewRecorder()
	h.CreateAutoReplyRule(rec, autoReplyRequest(111, http.MethodPost, "/auto-reply/rules", body, nil))
	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	rule := f.lastRule.GetRule()
	if f.lastRule.OwnerUserId != 111 {
		t.Fatalf("owner must travel on the request, not on the rule message")
	}
	var conditions map[string]any
	if err := json.Unmarshal([]byte(rule.GetConditionsJson()), &conditions); err != nil {
		t.Fatalf("conditions_json is not JSON: %q (%v)", rule.GetConditionsJson(), err)
	}
	if _, ok := conditions["keywords_any"].([]any); !ok {
		t.Fatalf("conditions lost their object shape: %s", rule.GetConditionsJson())
	}
	if rule.GetExclusionsJson() != "{}" {
		t.Fatalf("absent exclusions must be an empty object, got %q", rule.GetExclusionsJson())
	}
	if rule.GetCooldownSeconds() != 0 {
		t.Fatalf("an explicit 0 cooldown must be kept, got %d", rule.GetCooldownSeconds())
	}
	responseRule := autoReplyObjectField(t, decodeAutoReplyBody(t, rec), "rule")
	if _, ok := responseRule["conditions"].(map[string]any); !ok {
		t.Fatalf("conditions must come back as an object: %v", responseRule)
	}

	// PUT: the body's id may not redirect the write.
	rec = httptest.NewRecorder()
	h.UpdateAutoReplyRule(rec, autoReplyRequest(111, http.MethodPut, "/auto-reply/rules/path-id",
		`{"id":"body-id","name":"Renamed","conditions":{}}`, map[string]string{"id": "path-id"}))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if got := f.lastRule.GetRule().GetId(); got != "path-id" {
		t.Fatalf("the path id must win, got %q", got)
	}
	if got := f.lastRule.GetRule().GetCooldownSeconds(); got != -1 {
		t.Fatalf("an absent cooldown must inherit (-1), got %d", got)
	}

	// A conditions array is not the documented shape and must not be silently
	// read as "match everything".
	before := len(f.calls)
	rec = httptest.NewRecorder()
	h.CreateAutoReplyRule(rec, autoReplyRequest(111, http.MethodPost, "/auto-reply/rules",
		`{"name":"Bad","conditions":["price"]}`, nil))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("non-object conditions must be 400, got %d", rec.Code)
	}
	if len(f.calls) != before {
		t.Fatal("a rejected rule body must not reach the core")
	}
}

// ---------------------------------------------------------------------------
// Targets
// ---------------------------------------------------------------------------

func TestAutoReplyTargets_RemoveReportsTheCoresCount(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.targets[111] = []*pb.AutoReplyTarget{
		{ChatId: "42", Title: "Bob", Username: "bob"},
		{ChatId: "43", Title: "Ann"},
	}
	h := autoReplyHandler(f)
	rec := httptest.NewRecorder()
	h.RemoveAutoReplyTargets(rec, autoReplyRequest(111, http.MethodDelete, "/auto-reply/targets",
		`{"chat_ids":["42","43","999"]}`, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	body := decodeAutoReplyBody(t, rec)
	if body["success"] != true || body["removed"].(float64) != 2 {
		t.Fatalf("removed must be the count the core confirmed: %v", body)
	}
	if f.lastRemove.OwnerUserId != 111 || len(f.lastRemove.ChatIds) != 3 {
		t.Fatalf("remove not forwarded: %+v", f.lastRemove)
	}
}

func TestAutoReplyTargets_UpdateKeepsTheResolvedUsername(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.targets[111] = []*pb.AutoReplyTarget{{ChatId: "42", Title: "Bob", Username: "bob_handle"}}
	h := autoReplyHandler(f)
	rec := httptest.NewRecorder()
	h.UpdateAutoReplyTarget(rec, autoReplyRequest(111, http.MethodPut, "/auto-reply/targets/42",
		`{"is_enabled":false,"note":"muted","priority":5}`, map[string]string{"chatId": "42"}))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	target := f.lastTargetUpdate.GetTarget()
	if target.GetUsername() != "bob_handle" {
		t.Fatalf("editing a note must not erase the resolved handle, got %q", target.GetUsername())
	}
	if target.GetIsEnabled() || target.GetNote() != "muted" || target.GetPriority() != 5 {
		t.Fatalf("editable fields not forwarded: %+v", target)
	}
	if f.lastTargetUpdate.OwnerUserId != 111 {
		t.Fatalf("owner not forwarded: %d", f.lastTargetUpdate.OwnerUserId)
	}
	if body := autoReplyObjectField(t, decodeAutoReplyBody(t, rec), "target"); body["chat_id"] != "42" {
		t.Fatalf("response target wrong: %v", body)
	}
}

func TestAutoReplyTargets_ResolveShapesFoundAndMissing(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.resolved = &pb.AutoReplyTarget{ChatId: "42", Title: "Bob", Username: "bob"}
	h := autoReplyHandler(f)

	rec := httptest.NewRecorder()
	h.ResolveAutoReplyChat(rec, autoReplyRequest(111, http.MethodPost, "/auto-reply/targets/resolve",
		`{"session_id":"s1","query":"@bob"}`, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	body := decodeAutoReplyBody(t, rec)
	if body["found"] != true {
		t.Fatalf("found flag wrong: %v", body)
	}
	if target := autoReplyObjectField(t, body, "target"); target["username"] != "bob" {
		t.Fatalf("target not projected: %v", target)
	}

	f.resolved = nil
	rec = httptest.NewRecorder()
	h.ResolveAutoReplyChat(rec, autoReplyRequest(111, http.MethodPost, "/auto-reply/targets/resolve",
		`{"query":"nobody"}`, nil))
	body = decodeAutoReplyBody(t, rec)
	if body["found"] != false {
		t.Fatalf("missing chat must be found=false: %v", body)
	}
	if target, present := body["target"]; !present || target != nil {
		t.Fatalf("missing chat must carry \"target\":null, got %v", body)
	}
}

// ---------------------------------------------------------------------------
// Jobs, stats, preview
// ---------------------------------------------------------------------------

func TestAutoReplyJobs_ListForwardsFiltersAndKeepsJobFields(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.jobs[111] = []*pb.AutoReplyJob{{Id: "j1", ChatId: "42", Status: "DEAD_LETTER", Attempt: 3, MaxAttempts: 3, ErrorCode: "provider_error"}}
	h := autoReplyHandler(f)
	rec := httptest.NewRecorder()
	h.ListAutoReplyJobs(rec, autoReplyRequest(111, http.MethodGet, "/auto-reply/jobs?status=DEAD_LETTER&chat_id=42&limit=9999", "", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if f.lastJobList.Status != "DEAD_LETTER" || f.lastJobList.ChatId != "42" {
		t.Fatalf("filters not forwarded: %+v", f.lastJobList)
	}
	if f.lastJobList.Limit != maxAutoReplyPage {
		t.Fatalf("page size must clamp to %d, got %d", maxAutoReplyPage, f.lastJobList.Limit)
	}
	job := autoReplyListField(t, decodeAutoReplyBody(t, rec), "jobs")[0].(map[string]any)
	if job["status"] != "DEAD_LETTER" || job["error_code"] != "provider_error" {
		t.Fatalf("job not projected: %v", job)
	}
	if _, present := job["reply_text"]; !present {
		t.Fatalf("empty job text must still be present: %v", job)
	}
}

func TestAutoReplyStats_ParsesStatusCounters(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.stats = &pb.AutoReplyStatsResponse{
		SentToday: 5, SentTotal: 40, Pending: 2, Retrying: 1, DeadLetter: 3,
		ChatsEnabled: 7, Prompts: 4, Rules: 6, AverageLatencyMs: 812.5,
		LastSentAt: 1700000123, StatusJson: `{"SENT":5,"PENDING":2,"DEAD_LETTER":3}`,
	}
	h := autoReplyHandler(f)
	rec := httptest.NewRecorder()
	h.GetAutoReplyStats(rec, autoReplyRequest(111, http.MethodGet, "/auto-reply/stats", "", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	stats := autoReplyObjectField(t, decodeAutoReplyBody(t, rec), "stats")
	if stats["sent_today"].(float64) != 5 || stats["dead_letter"].(float64) != 3 {
		t.Fatalf("counters wrong: %v", stats)
	}
	statuses := stats["statuses"].(map[string]any)
	if statuses["SENT"].(float64) != 5 || statuses["DEAD_LETTER"].(float64) != 3 {
		t.Fatalf("per-status counters not parsed from status_json: %v", statuses)
	}
}

func TestAutoReplyPreview_ReturnsTheCoreResult(t *testing.T) {
	f := newFakeAutoReplyClient()
	f.preview = &pb.PreviewAutoReplyResponse{
		Ok: true, ReplyText: "سلام، در خدمتم", Provider: "openai", Model: "gpt-4o-mini",
		LatencyMs: 412, Tokens: 88,
	}
	h := autoReplyHandler(f)
	rec := httptest.NewRecorder()
	h.PreviewAutoReply(rec, autoReplyRequest(111, http.MethodPost, "/auto-reply/preview",
		`{"inbound_text":"قیمت؟","prompt_id":"p1","ai_config_id":"ai1","owner_user_id":999}`, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	result := autoReplyObjectField(t, decodeAutoReplyBody(t, rec), "result")
	if result["ok"] != true || result["reply_text"] != "سلام، در خدمتم" {
		t.Fatalf("preview result not passed through: %v", result)
	}
	if result["provider"] != "openai" || result["model"] != "gpt-4o-mini" ||
		result["latency_ms"].(float64) != 412 || result["tokens"].(float64) != 88 {
		t.Fatalf("preview metadata wrong: %v", result)
	}
	if _, present := result["error"]; !present {
		t.Fatalf("an empty error must still be present: %v", result)
	}
	if len(result) != 8 {
		t.Fatalf("preview result must expose exactly the documented fields: %v", result)
	}
	if f.lastPreview.OwnerUserId != 111 {
		t.Fatalf("preview must use the verified owner, got %d", f.lastPreview.OwnerUserId)
	}
	if f.lastPreview.InboundText != "قیمت؟" || f.lastPreview.PromptId != "p1" || f.lastPreview.AiConfigId != "ai1" {
		t.Fatalf("preview body not forwarded: %+v", f.lastPreview)
	}
}

func TestAutoReplyPreview_RequiresText(t *testing.T) {
	f := newFakeAutoReplyClient()
	h := autoReplyHandler(f)
	rec := httptest.NewRecorder()
	h.PreviewAutoReply(rec, autoReplyRequest(111, http.MethodPost, "/auto-reply/preview", `{"inbound_text":"  "}`, nil))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("empty inbound_text must be 400, got %d", rec.Code)
	}
	if f.lastPreview != nil {
		t.Fatal("an empty preview must not reach the core")
	}
}
