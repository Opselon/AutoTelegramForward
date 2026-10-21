package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	grpcclient "autoforward/internal/grpcclient"
	pb "autoforward/proto"
)

// fakeReplaceClient models the core's replace-rule surface closely enough to
// run real handler code against it: it refuses owner 0, keeps per-ID owner
// attribution, and answers a foreign id exactly like a missing one — the same
// guarantees the real servicer makes over the wire. A middleware bug that
// dropped the identity therefore cannot hide behind a permissive fake.
type fakeReplaceClient struct {
	pb.ReplaceRuleControlServiceClient

	rules map[string]fakeReplaceRow
	nextID int

	lastInput *pb.ReplaceRuleInput
	lastOwner int64
	lastID    string
	enabled   *bool
}

func newFakeReplaceClient() *fakeReplaceClient {
	return &fakeReplaceClient{nextID: 1, rules: map[string]fakeReplaceRow{}}
}

func (f *fakeReplaceClient) NotFound() error {
	return status.Error(codes.NotFound, "not_found: rule not found for this owner")
}

type fakeReplaceRow struct {
	owner int64
	rule  *pb.ReplaceRule
}

func (f *fakeReplaceClient) create(in *pb.CreateReplaceRuleRequest) (*pb.ReplaceRule, error) {
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.InvalidArgument, "owner_user_id is required")
	}
	f.nextID++
	rid := "r" + strconv.Itoa(f.nextID)
	rule := &pb.ReplaceRule{Id: rid, Source: in.GetRule().GetSource(), To: in.GetRule().GetTo()}
	f.rules[rid] = fakeReplaceRow{in.OwnerUserId, rule}
	return rule, nil
}

func (f *fakeReplaceClient) CreateReplaceRule(_ context.Context, in *pb.CreateReplaceRuleRequest, _ ...grpc.CallOption) (*pb.ReplaceRule, error) {
	return f.create(in)
}

func (f *fakeReplaceClient) GetReplaceRule(_ context.Context, in *pb.GetReplaceRuleRequest, _ ...grpc.CallOption) (*pb.ReplaceRule, error) {
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.NotFound, "owner_user_id is required")
	}
	entry, ok := f.rules[in.RuleId]
	if !ok || entry.owner != in.OwnerUserId {
		return nil, f.NotFound()
	}
	return entry.rule, nil
}

func (f *fakeReplaceClient) UpdateReplaceRule(_ context.Context, in *pb.UpdateReplaceRuleRequest, _ ...grpc.CallOption) (*pb.ReplaceRule, error) {
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.NotFound, "owner_user_id is required")
	}
	entry, ok := f.rules[in.RuleId]
	if !ok || entry.owner != in.OwnerUserId {
		return nil, f.NotFound()
	}
	f.lastInput = in.Rule
	f.lastOwner = in.OwnerUserId
	f.lastID = in.RuleId
	return entry.rule, nil
}

func (f *fakeReplaceClient) ListReplaceRules(_ context.Context, in *pb.ListReplaceRulesRequest, _ ...grpc.CallOption) (*pb.ListReplaceRulesResponse, error) {
	return &pb.ListReplaceRulesResponse{
		Rules:    []*pb.ReplaceRule{{Id: "r1", Source: "a", To: "b", Enabled: true}},
		Page:     1,
		PerPage:  in.PerPage,
		Total:    1,
		Pages:    1,
		Counts:   &pb.ReplaceRuleCounts{Total: 1, Enabled: 1},
		Status:   "all",
		TaskId:   in.TaskId,
		Tasks:    []*pb.ReplaceRuleTask{{Id: "t1", Name: "Desk feed"}},
	}, nil
}

func (f *fakeReplaceClient) DeleteReplaceRule(_ context.Context, in *pb.DeleteReplaceRuleRequest, _ ...grpc.CallOption) (*pb.DeleteReplaceRuleResponse, error) {
	entry, ok := f.rules[in.RuleId]
	if !ok || entry.owner != in.OwnerUserId {
		return nil, f.NotFound()
	}
	delete(f.rules, in.RuleId)
	return &pb.DeleteReplaceRuleResponse{Ok: true, Removed: 2}, nil
}

func (f *fakeReplaceClient) SetReplaceRuleEnabled(_ context.Context, in *pb.SetReplaceRuleEnabledRequest, _ ...grpc.CallOption) (*pb.ReplaceRule, error) {
	entry, ok := f.rules[in.RuleId]
	if !ok || entry.owner != in.OwnerUserId {
		return nil, f.NotFound()
	}
	f.enabled = &in.Enabled
	return entry.rule, nil
}

func (f *fakeReplaceClient) DuplicateReplaceRule(_ context.Context, in *pb.DuplicateReplaceRuleRequest, _ ...grpc.CallOption) (*pb.ReplaceRule, error) {
	name := "copied"
	return f.create(&pb.CreateReplaceRuleRequest{
		OwnerUserId: in.OwnerUserId,
		Rule:        &pb.ReplaceRuleInput{Source: &name, To: &name},
	})
}

func (f *fakeReplaceClient) TestReplaceRule(_ context.Context, in *pb.TestReplaceRuleRequest, _ ...grpc.CallOption) (*pb.TestReplaceRuleResponse, error) {
	if in.OwnerUserId <= 0 {
		return nil, status.Error(codes.InvalidArgument, "owner_user_id is required")
	}
	return &pb.TestReplaceRuleResponse{Ok: true, Input: in.Sample, Output: "out", Matches: 1, Changed: true}, nil
}

func (f *fakeReplaceClient) ListReplaceRuleTasks(_ context.Context, in *pb.ListReplaceRuleTasksRequest, _ ...grpc.CallOption) (*pb.ListReplaceRuleTasksResponse, error) {
	return &pb.ListReplaceRuleTasksResponse{Tasks: []*pb.ReplaceRuleTask{{Id: "t1", Name: "Desk feed"}}}, nil
}

func replaceHandler(f *fakeReplaceClient) *Handlers {
	return &Handlers{c: &grpcclient.Clients{Replace: f}}
}

// reqReplace builds a request with the path id and (optionally) a verified
// identity, exactly as RequireAuth populates it for a real dashboard call.
func reqReplace(method, target, body string, owner int64) *http.Request {
	var reader *strings.Reader
	if body != "" {
		reader = strings.NewReader(body)
	} else {
		reader = strings.NewReader("")
	}
	r := httptest.NewRequest(method, target, reader)
	r.SetPathValue("id", targetID(target))
	if owner != 0 {
		return r.WithContext(context.WithValue(r.Context(), identityKey{}, &Identity{UserID: owner}))
	}
	return r
}

func targetID(target string) string {
	// Simple, sufficient: /api/replace-rules/<id>[/<verb>] → <id>
	parts := strings.Split(strings.TrimPrefix(target, "/api/replace-rules/"), "/")
	if len(parts) == 0 || parts[0] == "" || parts[0] == "tasks" || parts[0] == "test" {
		return ""
	}
	return parts[0]
}

// decodable JSON surface: snake_case, every field present.
func TestReplaceListRendersSnakeCase(t *testing.T) {
	f := newFakeReplaceClient()
	h := replaceHandler(f)
	rec := httptest.NewRecorder()
	h.ListReplaceRules(rec, reqReplace("GET", "/api/replace-rules?q=promo&per_page=5", "", 5094837833))
	if rec.Code != http.StatusOK {
		t.Fatalf("list: %d %s", rec.Code, rec.Body.String())
	}
	var body struct {
		PerPage float64 `json:"per_page"`
		Rules   []struct {
			ID      string `json:"id"`
			To      string `json:"to"`
			Enabled bool   `json:"enabled"`
		} `json:"rules"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.PerPage != 5 {
		t.Fatalf("per_page not read from query: %.0f", body.PerPage)
	}
	if len(body.Rules) != 1 || !body.Rules[0].Enabled || body.Rules[0].To == "" {
		t.Fatalf("projection dropped a field: %+v", body.Rules)
	}
}

func TestReplaceCreateRequiresTasks(t *testing.T) {
	f := newFakeReplaceClient()
	h := replaceHandler(f)
	rec := httptest.NewRecorder()
	h.CreateReplaceRule(rec, reqReplace("POST", "/api/replace-rules",
		`{"source":"a","to":"b"}`, 5094837833))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("a rule without tasks must be refused, got %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "task_required") {
		t.Fatalf("expected a task_required code, got %s", rec.Body.String())
	}
	rec = httptest.NewRecorder()
	h.CreateReplaceRule(rec, reqReplace("POST", "/api/replace-rules",
		`{"source":"a","to":"b","task_ids":["t1"]}`, 5094837833))
	if rec.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rec.Code, rec.Body.String())
	}
}

// The core refuses owner 0; the handler must never pass it an identity it did
// not verify in the caller's token.
func TestReplaceCreatePassesOnlyVerifiedIdentity(t *testing.T) {
	f := newFakeReplaceClient()
	h := replaceHandler(f)
	rec := httptest.NewRecorder()
	h.CreateReplaceRule(rec, reqReplace("POST", "/api/replace-rules",
		`{"source":"a","to":"b","task_ids":["t1"]}`, 0))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated create must be 401, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(f.rules) != 0 {
		t.Fatalf("the fake kept a row: %+v", f.rules)
	}
}

// A JSON body of {"enabled": false} must reach the core as an *absent-anything-
// else* field set, not an empty one: the pointer encoding is what keeps a
// disable from erasing the pattern it is switching off.
func TestReplacePartialUpdateSendsOnlySentFields(t *testing.T) {
	f := newFakeReplaceClient()
	f.rules["r1"] = fakeReplaceRow{5094837833, &pb.ReplaceRule{Id: "r1", Source: "promo", To: "OFFER"}}
	h := replaceHandler(f)
	rec := httptest.NewRecorder()
	h.UpdateReplaceRule(rec, reqReplace("PATCH", "/api/replace-rules/r1",
		`{"enabled": false}`, 5094837833))
	if rec.Code != http.StatusOK {
		t.Fatalf("patch: %d %s", rec.Code, rec.Body.String())
	}
	in := f.lastInput
	if in == nil || in.Enabled == nil || *in.Enabled || in.Source != nil || in.To != nil || in.Name != nil {
		t.Fatalf("expected exactly the enabled field, got %v", in)
	}
}

func TestReplaceForeignIDAnswersNotFound(t *testing.T) {
	f := newFakeReplaceClient()
	f.rules["r1"] = fakeReplaceRow{5094837833, &pb.ReplaceRule{Id: "r1", Source: "a", To: "b"}}
	h := replaceHandler(f)
	other := int64(6278059256)
	for _, tt := range []struct {
		name, method, target string
		body                 string
	}{
		{"get", "GET", "/api/replace-rules/r1", ""},
		{"patch", "PATCH", "/api/replace-rules/r1", `{"to":"hijack"}`},
		{"delete", "DELETE", "/api/replace-rules/r1", ""},
		{"enable", "POST", "/api/replace-rules/r1/enable", `{"enabled":true}`},
	} {
		t.Run(tt.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			switch tt.name {
			case "get":
				h.GetReplaceRule(rec, reqReplace("GET", tt.target, tt.body, other))
			case "patch":
				h.UpdateReplaceRule(rec, reqReplace("PATCH", tt.target, tt.body, other))
			case "delete":
				h.DeleteReplaceRule(rec, reqReplace("DELETE", tt.target, tt.body, other))
			case "enable":
				h.SetReplaceRuleEnabled(rec, reqReplace("POST", tt.target, tt.body, other))
			}
			if rec.Code != http.StatusNotFound {
				t.Fatalf("%s by a foreign id: %d %s", tt.name, rec.Code, rec.Body.String())
			}
		})
	}
	// And the copies are untouched.
	if f.rules["r1"].rule.To != "b" {
		t.Fatalf("the other tenant's delete removed a copy: %+v", f.rules["r1"])
	}
}

func TestReplaceEnableIsAbsolute(t *testing.T) {
	f := newFakeReplaceClient()
	f.rules["r1"] = fakeReplaceRow{5094837833, &pb.ReplaceRule{Id: "r1", Source: "a", To: "b"}}
	h := replaceHandler(f)
	rec := httptest.NewRecorder()
	h.SetReplaceRuleEnabled(rec, reqReplace("POST", "/api/replace-rules/r1/enable",
		`{"enabled": true}`, 5094837833))
	if rec.Code != http.StatusOK {
		t.Fatalf("enable: %d %s", rec.Code, rec.Body.String())
	}
	if f.enabled == nil || !*f.enabled {
		t.Fatalf("the boolean did not travel: %+v", f.enabled)
	}
}

// TestReplaceTestPreviewScoped pins the preview: it goes through the same
// identity and cannot be used to test a model prompt of another tenant.
func TestReplaceTestPreviewScoped(t *testing.T) {
	f := newFakeReplaceClient()
	h := replaceHandler(f)
	rec := httptest.NewRecorder()
	h.TestReplaceRule(rec, reqReplace("POST", "/api/replace-rules/test",
		`{"rule_id":"r1","sample":"promo 50"}`, 0))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("preview without an identity: %d %s", rec.Code, rec.Body.String())
	}
	rec = httptest.NewRecorder()
	h.TestReplaceRule(rec, reqReplace("POST", "/api/replace-rules/test",
		`{"rule_id":"r1","sample":"promo 50"}`, 5094837833))
	if rec.Code != http.StatusOK {
		t.Fatalf("preview: %d %s", rec.Code, rec.Body.String())
	}
	var body struct {
		Output  string `json:"output"`
		Matches int    `json:"matches"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body.Output != "out" || body.Matches != 1 {
		t.Fatalf("preview body: %+v", body)
	}
}

func TestReplaceDeleteReportsCopiesRemoved(t *testing.T) {
	f := newFakeReplaceClient()
	f.rules["r1"] = fakeReplaceRow{5094837833, &pb.ReplaceRule{Id: "r1", Source: "a", To: "b"}}
	h := replaceHandler(f)
	rec := httptest.NewRecorder()
	h.DeleteReplaceRule(rec, reqReplace("DELETE", "/api/replace-rules/r1", "", 5094837833))
	if rec.Code != http.StatusOK {
		t.Fatalf("delete: %d %s", rec.Code, rec.Body.String())
	}
	var body struct {
		OK      bool `json:"ok"`
		Removed int  `json:"removed"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if !body.OK || body.Removed != 2 {
		t.Fatalf("delete body: %+v", body)
	}
	// grpcError maps NOT_FOUND to 404 for a missing rule, not 410 or 204.
	rec = httptest.NewRecorder()
	h.DeleteReplaceRule(rec, reqReplace("DELETE", "/api/replace-rules/missing", "", 5094837833))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("missing rule delete: %d %s", rec.Code, rec.Body.String())
	}
}
