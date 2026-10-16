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

// --------------------------------------------------------------------------- //
// targets are first-class: resolution, mirroring and route validation
// --------------------------------------------------------------------------- //

// routeCaptureClient records the rule the handler hands to the core (and the
// mask it forwarded), so a test can assert on the canonical shape that is
// actually written — not on the handler's internal variables.
type routeCaptureClient struct {
	pb.ForwardRuleControlServiceClient
	stored  []*pb.ForwardRule
	created *pb.ForwardRule
	updated *pb.ForwardRule
	gotMask []string
}

func (f *routeCaptureClient) ListRules(ctx context.Context, in *pb.ListRulesRequest, opts ...grpc.CallOption) (*pb.ListRulesResponse, error) {
	if f.stored != nil {
		out := &pb.ListRulesResponse{}
		for _, r := range f.stored {
			if in.OwnerUserId == 0 || r.OwnerUserId == in.OwnerUserId {
				out.Rules = append(out.Rules, r)
			}
		}
		return out, nil
	}
	return &pb.ListRulesResponse{Rules: []*pb.ForwardRule{{Id: "rule-a", OwnerUserId: in.OwnerUserId}}}, nil
}

func (f *routeCaptureClient) CreateRule(ctx context.Context, in *pb.CreateRuleRequest, opts ...grpc.CallOption) (*pb.ForwardRule, error) {
	f.created = in.Rule
	return in.Rule, nil
}

func (f *routeCaptureClient) UpdateRule(ctx context.Context, in *pb.UpdateRuleRequest, opts ...grpc.CallOption) (*pb.ForwardRule, error) {
	f.updated = in.Rule
	f.gotMask = append([]string(nil), in.UpdateMask...)
	return in.Rule, nil
}

// fakeSessionsClient resolves the handles a test declares and reports every
// other handle as not found, so the resolve_failed path is reachable too.
type fakeSessionsClient struct {
	pb.SessionControlServiceClient
	handles map[string]string
	calls   []string
}

func (f *fakeSessionsClient) ResolveChat(ctx context.Context, in *pb.ResolveChatRequest, opts ...grpc.CallOption) (*pb.ResolveChatResponse, error) {
	f.calls = append(f.calls, in.Handle)
	if id, ok := f.handles[normalizeChatID(in.Handle)]; ok {
		return &pb.ResolveChatResponse{Found: true, ChatId: id}, nil
	}
	return &pb.ResolveChatResponse{Found: false, Error: "chat not found"}, nil
}

func postRule(t *testing.T, h *Handlers, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/rules", strings.NewReader(body))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	rec := httptest.NewRecorder()
	h.CreateRule(rec, req)
	return rec
}

func responseCode(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("response is not JSON: %s", rec.Body.String())
	}
	code, _ := body["code"].(string)
	return code
}

func wantStrings(t *testing.T, got, want []string, label string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s = %v, want %v", label, got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("%s = %v, want %v", label, got, want)
		}
	}
}

func storedRule() []*pb.ForwardRule {
	return []*pb.ForwardRule{{
		Id: "rule-a", OwnerUserId: 111,
		SourceChatId: "-1001", SourceChatIds: []string{"-1001"},
		TargetChatId: "-2002", TargetChatIds: []string{"-2002"},
	}}
}

// -------------------------------------------------------------------- create

func TestCreateRule_TwoSourcesOneTarget(t *testing.T) {
	rc := &routeCaptureClient{}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := postRule(t, h, `{"name":"two-into-one","source_chat_ids":["-1001","-2002"],"target_chat_id":"-9999"}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	got := rc.created
	wantStrings(t, got.SourceChatIds, []string{"-1001", "-2002"}, "source_chat_ids")
	wantStrings(t, got.TargetChatIds, []string{"-9999"}, "target_chat_ids")
	if got.SourceChatId != "-1001" {
		t.Fatalf("source_chat_id must mirror the first source, got %q", got.SourceChatId)
	}
	if got.TargetChatId != "-9999" {
		t.Fatalf("target_chat_id must mirror the first target, got %q", got.TargetChatId)
	}
}

func TestCreateRule_OneSourceTwoTargets(t *testing.T) {
	rc := &routeCaptureClient{}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := postRule(t, h, `{"source_chat_id":"-1001","target_chat_ids":["-2002","-3003"]}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	got := rc.created
	wantStrings(t, got.SourceChatIds, []string{"-1001"}, "source_chat_ids")
	if got.SourceChatId != "-1001" {
		t.Fatalf("source_chat_id = %q, want -1001", got.SourceChatId)
	}
	wantStrings(t, got.TargetChatIds, []string{"-2002", "-3003"}, "target_chat_ids")
	if got.TargetChatId != "-2002" {
		t.Fatalf("target_chat_id must mirror the first target, got %q", got.TargetChatId)
	}
}

func TestCreateRule_TwoSourcesTwoTargets(t *testing.T) {
	rc := &routeCaptureClient{}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := postRule(t, h, `{"source_chat_ids":["-1001","-2002"],"target_chat_ids":["-3003","-4004"]}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	got := rc.created
	wantStrings(t, got.SourceChatIds, []string{"-1001", "-2002"}, "source_chat_ids")
	wantStrings(t, got.TargetChatIds, []string{"-3003", "-4004"}, "target_chat_ids")
	if got.SourceChatId != "-1001" || got.TargetChatId != "-3003" {
		t.Fatalf("mirrors wrong: source=%q target=%q", got.SourceChatId, got.TargetChatId)
	}
}

// Every entry of both lists is resolved, not just the primary, and the mirrors
// end up holding the resolved numeric ids.
func TestCreateRule_ResolvesEveryHandleOnBothSides(t *testing.T) {
	rc := &routeCaptureClient{}
	sc := &fakeSessionsClient{handles: map[string]string{
		"alpha": "-1000000001", "beta": "-2000000002", "gamma": "-3000000003",
	}}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc, Sessions: sc}}

	rec := postRule(t, h, `{"source_chat_ids":["@alpha","@beta"],"target_chat_ids":["@gamma","-4004"]}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	got := rc.created
	wantStrings(t, got.SourceChatIds, []string{"-1000000001", "-2000000002"}, "source_chat_ids")
	wantStrings(t, got.TargetChatIds, []string{"-3000000003", "-4004"}, "target_chat_ids")
	if got.SourceChatId != "-1000000001" {
		t.Fatalf("source_chat_id = %q, want the first resolved source", got.SourceChatId)
	}
	if got.TargetChatId != "-3000000003" {
		t.Fatalf("target_chat_id = %q, want the first resolved target", got.TargetChatId)
	}
	wantStrings(t, sc.calls, []string{"@alpha", "@beta", "@gamma"}, "resolver calls")
}

func TestCreateRule_RejectsDuplicateInsideAList(t *testing.T) {
	cases := []struct {
		name string
		body string
		code string
	}{
		{"source", `{"source_chat_ids":["-1001","-1001"],"target_chat_id":"-9999"}`, "duplicate_source:-1001"},
		{"source after trim", `{"source_chat_ids":[" -1001 ","-1001"],"target_chat_id":"-9999"}`, "duplicate_source:-1001"},
		{"target", `{"source_chat_id":"-1001","target_chat_ids":["-9999","-9999"]}`, "duplicate_target:-9999"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rc := &routeCaptureClient{}
			h := &Handlers{c: &grpcclient.Clients{Rules: rc}}
			rec := postRule(t, h, tc.body)
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
			}
			if got := responseCode(t, rec); got != tc.code {
				t.Fatalf("code = %q, want %q (%s)", got, tc.code, rec.Body.String())
			}
			if rc.created != nil {
				t.Fatal("a rejected rule must never reach the core")
			}
		})
	}
}

func TestCreateRule_RejectsSourceThatIsAlsoATarget(t *testing.T) {
	rc := &routeCaptureClient{}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := postRule(t, h, `{"source_chat_ids":["-1001","-2002"],"target_chat_ids":["-2002","-3003"]}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if got := responseCode(t, rec); got != "source_target_overlap:-2002" {
		t.Fatalf("code = %q, want source_target_overlap:-2002 (%s)", got, rec.Body.String())
	}
	if rc.created != nil {
		t.Fatal("a self-looping rule must never reach the core")
	}
}

func TestCreateRule_RejectsEmptyTargetList(t *testing.T) {
	for _, body := range []string{
		`{"source_chat_ids":["-1001"]}`,
		`{"source_chat_id":"-1001","target_chat_ids":["  ",""]}`,
	} {
		rc := &routeCaptureClient{}
		h := &Handlers{c: &grpcclient.Clients{Rules: rc}}
		rec := postRule(t, h, body)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: want 400, got %d: %s", body, rec.Code, rec.Body.String())
		}
		if got := responseCode(t, rec); got != "target_empty" {
			t.Fatalf("%s: code = %q, want target_empty", body, got)
		}
		if rc.created != nil {
			t.Fatal("a rule without targets must never reach the core")
		}
	}
}

func TestResolveTargetChats_EmptyListIsRejected(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{}}
	for _, rule := range []*pb.ForwardRule{
		{},
		{TargetChatId: "  ", TargetChatIds: []string{"", "  "}},
	} {
		got, errStr := h.resolveTargetChats(context.Background(), "", rule, 111)
		if got != nil || errStr != "target_empty" {
			t.Fatalf("resolveTargetChats(%v) = %v, %q; want nil, target_empty", rule, got, errStr)
		}
	}
}

// A handle the core cannot resolve is a bad request, and the core's own message
// stays visible in the body.
func TestCreateRule_PropagatesResolveFailureUnchanged(t *testing.T) {
	rc := &routeCaptureClient{}
	sc := &fakeSessionsClient{handles: map[string]string{}}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc, Sessions: sc}}

	rec := postRule(t, h, `{"source_chat_id":"-1001","target_chat_id":"@missing"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if got := responseCode(t, rec); got != "target_resolve_failed" {
		t.Fatalf("code = %q, want target_resolve_failed", got)
	}
	if !strings.Contains(rec.Body.String(), "resolve_failed") {
		t.Fatalf("the core's reason was swallowed: %s", rec.Body.String())
	}
	if rc.created != nil {
		t.Fatal("an unresolvable target must never reach the core")
	}
}

// -------------------------------------------------------------------- update

func TestUpdateRule_ResolvesEveryTargetAndMirrorsTheFirst(t *testing.T) {
	rc := &routeCaptureClient{stored: storedRule()}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := patchRule(t, h, `{"target_chat_ids":["-3003","-4004"],"source_chat_ids_text":"-1001"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	wantStrings(t, rc.updated.TargetChatIds, []string{"-3003", "-4004"}, "target_chat_ids")
	if rc.updated.TargetChatId != "-3003" {
		t.Fatalf("target_chat_id must mirror the first target, got %q", rc.updated.TargetChatId)
	}
	if rc.updated.SourceChatId != "-1001" {
		t.Fatalf("source_chat_id = %q, want -1001", rc.updated.SourceChatId)
	}
}

func TestUpdateRule_RejectsDuplicateInsideAList(t *testing.T) {
	rc := &routeCaptureClient{stored: storedRule()}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := patchRule(t, h, `{"source_chat_id":"-1001","target_chat_ids":["-3003","-3003"]}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if got := responseCode(t, rec); got != "duplicate_target:-3003" {
		t.Fatalf("code = %q, want duplicate_target:-3003", got)
	}
	if rc.updated != nil {
		t.Fatal("a rejected update must never reach the core")
	}
}

// A masked update that rewrites only the targets is still checked against the
// stored sources, so a rule cannot be pointed at its own source.
func TestUpdateRule_RejectsOverlapWithTheStoredSource(t *testing.T) {
	rc := &routeCaptureClient{stored: storedRule()}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := patchRule(t, h, `{"target_chat_ids":["-1001","-3003"],"update_mask":["target_chat_ids"]}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if got := responseCode(t, rec); got != "source_target_overlap:-1001" {
		t.Fatalf("code = %q, want source_target_overlap:-1001", got)
	}
	if rc.updated != nil {
		t.Fatal("a self-looping update must never reach the core")
	}
}

// Naming only the list in the mask would leave the single-value field the
// dispatcher reads first at its stale value, so the mirror is named too.
func TestUpdateRule_MaskNamesTheMirrorAlongWithTheList(t *testing.T) {
	rc := &routeCaptureClient{stored: storedRule()}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := patchRule(t, h, `{"target_chat_ids":["-3003","-4004"],"update_mask":["target_chat_ids"]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if !containsString(rc.gotMask, "target_chat_id") {
		t.Fatalf("mask %v must also name target_chat_id", rc.gotMask)
	}
	if rc.updated.TargetChatId != "-3003" {
		t.Fatalf("target_chat_id = %q, want -3003", rc.updated.TargetChatId)
	}
}

// The comma-separated convenience form is merged into the list it belongs to,
// and an id already present in the array is not repeated.
func TestUpdateRule_AcceptsCommaSeparatedEndpointText(t *testing.T) {
	rc := &routeCaptureClient{stored: storedRule()}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := patchRule(t, h, `{"source_chat_ids":["-1001"],"source_chat_ids_text":"-1001, -2002،-3003","target_chat_ids_text":"-9999"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	wantStrings(t, rc.updated.SourceChatIds, []string{"-1001", "-2002", "-3003"}, "source_chat_ids")
	wantStrings(t, rc.updated.TargetChatIds, []string{"-9999"}, "target_chat_ids")
	if rc.updated.SourceChatId != "-1001" || rc.updated.TargetChatId != "-9999" {
		t.Fatalf("mirrors wrong: source=%q target=%q", rc.updated.SourceChatId, rc.updated.TargetChatId)
	}
}

// A round trip (GET -> PATCH the whole rule back) sends both the list and its
// mirror with the same first id: that is the documented shape, not a duplicate.
func TestUpdateRule_AcceptsTheMirroredLegacyFieldsOnARoundTrip(t *testing.T) {
	rc := &routeCaptureClient{stored: storedRule()}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := patchRule(t, h, `{"source_chat_ids":["-1001","-2002"],"target_chat_ids":["-3003","-4004"],"sourceChatId":"-1001","targetChatId":"-3003"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	wantStrings(t, rc.updated.SourceChatIds, []string{"-1001", "-2002"}, "source_chat_ids")
	wantStrings(t, rc.updated.TargetChatIds, []string{"-3003", "-4004"}, "target_chat_ids")
}
