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
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// fakeKeyCheckAI records what the provider-key path asked the core, and answers
// with whatever the test wants the core to have answered.
type fakeKeyCheckAI struct {
	pb.AIControlServiceClient

	checkResp *pb.CheckProviderKeyResponse
	lastCheck *pb.CheckProviderKeyRequest
	checks    int

	providers []*pb.AIProviderInfo
	owned     []*pb.AIConfig
}

func (f *fakeKeyCheckAI) CheckProviderKey(_ context.Context, req *pb.CheckProviderKeyRequest,
	_ ...grpc.CallOption) (*pb.CheckProviderKeyResponse, error) {
	f.checks++
	f.lastCheck = req
	if f.checkResp != nil {
		return f.checkResp, nil
	}
	return &pb.CheckProviderKeyResponse{Success: true, Healthy: true, LatencyMs: 41.5}, nil
}

func (f *fakeKeyCheckAI) ListAIProviders(_ context.Context, _ *pb.ListAIProvidersRequest,
	_ ...grpc.CallOption) (*pb.ListAIProvidersResponse, error) {
	return &pb.ListAIProvidersResponse{Providers: f.providers}, nil
}

func (f *fakeKeyCheckAI) ListAIConfigs(_ context.Context, _ *pb.ListAIConfigsRequest,
	_ ...grpc.CallOption) (*pb.ListAIConfigsResponse, error) {
	return &pb.ListAIConfigsResponse{Configs: f.owned}, nil
}

func asOwner(req *http.Request, uid int64) *http.Request {
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid}))
}

func checkKeyRequest(t *testing.T, body string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/ai-configs/check-key", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	return req
}

// A refusal must carry `healthy:false` on the wire.  Marshalling the protobuf
// struct would drop the zero value and hand the client an answer with the
// decision missing — the same defect #149 fixed for rule actions.
func TestCheckProviderKeyRefusalKeepsBothFlags(t *testing.T) {
	fake := &fakeKeyCheckAI{checkResp: &pb.CheckProviderKeyResponse{
		Success: false, Healthy: false, Detail: "Incorrect API key provided: [REDACTED]",
		ErrorCode: "invalid_key",
	}}
	h := &Handlers{c: &grpcclient.Clients{AI: fake}}

	rec := httptest.NewRecorder()
	h.CheckProviderKey(rec, asOwner(checkKeyRequest(t, `{"provider":"openai","model":"gpt-4o-mini","base_url":"https://api.openai.com/v1","api_key":"sk-secret"}`), 111))

	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %s", rec.Body.String())
	}
	for _, key := range []string{"success", "healthy", "latency_ms", "detail", "error_code"} {
		if _, ok := body[key]; !ok {
			t.Fatalf("refusal dropped %q: %s", key, rec.Body.String())
		}
	}
	if body["healthy"] != false || body["success"] != false {
		t.Fatalf("refusal must say false explicitly: %v", body)
	}
	if body["error_code"] != "invalid_key" {
		t.Fatalf("error_code lost: %v", body)
	}
	// The key the tenant typed is an input, never an echo.
	if strings.Contains(rec.Body.String(), "sk-secret") {
		t.Fatalf("the API key came back in the answer: %s", rec.Body.String())
	}
}

func TestCheckProviderKeyHealthyAnswerCarriesLatency(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{AI: &fakeKeyCheckAI{}}}
	rec := httptest.NewRecorder()
	h.CheckProviderKey(rec, asOwner(checkKeyRequest(t,
		`{"provider":"custom","model":"coding","base_url":"https://gw.example.com/v1","api_key":"k"}`), 111))

	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %s", rec.Body.String())
	}
	if body["success"] != true || body["healthy"] != true || body["latency_ms"] != 41.5 {
		t.Fatalf("unexpected answer: %s", rec.Body.String())
	}
}

// The tenant is the token, not the body: a probe spends the caller's own key.
func TestCheckProviderKeyUsesTheCallersIdentity(t *testing.T) {
	fake := &fakeKeyCheckAI{}
	h := &Handlers{c: &grpcclient.Clients{AI: fake}}
	rec := httptest.NewRecorder()
	h.CheckProviderKey(rec, asOwner(checkKeyRequest(t,
		`{"provider":"openai","model":"m","base_url":"https://api.openai.com/v1","api_key":"k","owner_user_id":999}`), 111))

	if fake.lastCheck == nil {
		t.Fatal("the core was never asked")
	}
	if fake.lastCheck.GetOwnerUserId() != 111 {
		t.Fatalf("body supplied the tenant: %d", fake.lastCheck.GetOwnerUserId())
	}
	if fake.lastCheck.GetApiKey() != "k" || fake.lastCheck.GetBaseUrl() != "https://api.openai.com/v1" {
		t.Fatalf("endpoint/key did not reach the core: %v", fake.lastCheck)
	}
}

// A config that is not the caller's is refused before any key is spent.
func TestCheckProviderKeyRefusesAForeignConfigWithoutProbing(t *testing.T) {
	fake := &fakeKeyCheckAI{owned: []*pb.AIConfig{{Id: "mine", OwnerUserId: 111}}}
	h := &Handlers{c: &grpcclient.Clients{AI: fake}}
	rec := httptest.NewRecorder()
	h.CheckProviderKey(rec, asOwner(checkKeyRequest(t,
		`{"ai_config_id":"theirs","api_key":"***cret"}`), 111))

	if fake.checks != 0 {
		t.Fatal("another tenant's config was probed")
	}
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body["success"] != false || body["error_code"] != "config_not_found" {
		t.Fatalf("expected a refusal, got %s", rec.Body.String())
	}
}

func TestCheckProviderKeyAllowsTheCallersOwnConfig(t *testing.T) {
	fake := &fakeKeyCheckAI{owned: []*pb.AIConfig{{Id: "mine", OwnerUserId: 111}}}
	h := &Handlers{c: &grpcclient.Clients{AI: fake}}
	rec := httptest.NewRecorder()
	h.CheckProviderKey(rec, asOwner(checkKeyRequest(t, `{"ai_config_id":"mine"}`), 111))

	if fake.checks != 1 {
		t.Fatalf("own config was not probed (%d calls)", fake.checks)
	}
}

func TestListAIProvidersProjectsTheCatalogue(t *testing.T) {
	fake := &fakeKeyCheckAI{providers: []*pb.AIProviderInfo{
		{Id: "openai", Label: "OpenAI", DefaultBaseUrl: "https://api.openai.com/v1",
			Models: []string{"gpt-5-mini"}},
		{Id: "custom", Label: "Custom (OpenAI-compatible)", DefaultBaseUrl: ""},
	}}
	h := &Handlers{c: &grpcclient.Clients{AI: fake}}
	rec := httptest.NewRecorder()
	h.ListAIProviders(rec, asOwner(httptest.NewRequest(http.MethodGet, "/ai-configs/providers", nil), 111))

	var body struct {
		Providers []struct {
			ID             string   `json:"id"`
			Label          string   `json:"label"`
			DefaultBaseURL string   `json:"default_base_url"`
			Models         []string `json:"models"`
		} `json:"providers"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %s", rec.Body.String())
	}
	if len(body.Providers) != 2 {
		t.Fatalf("expected both providers: %s", rec.Body.String())
	}
	if body.Providers[0].DefaultBaseURL != "https://api.openai.com/v1" || body.Providers[0].Models[0] != "gpt-5-mini" {
		t.Fatalf("provider fields lost: %s", rec.Body.String())
	}
	// A provider with no curated models is an empty list, not a missing field:
	// the client renders the endpoint box either way.
	if !strings.Contains(rec.Body.String(), `"models":[]`) {
		t.Fatalf("empty models must still be present: %s", rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"default_base_url":""`) {
		t.Fatalf("empty endpoint must still be present: %s", rec.Body.String())
	}
}

// A refused endpoint is the operator's message to the tenant, so it must arrive
// as 400 with the core's own code — not as a 500 named "Unexpected ValueError".
func TestGrpcErrorNamesAnEndpointRefusal(t *testing.T) {
	rec := httptest.NewRecorder()
	grpcError(rec, status.Error(codes.InvalidArgument, "endpoint_blocked"))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}
	if body["code"] != "endpoint_blocked" {
		t.Fatalf("code = %q, want endpoint_blocked", body["code"])
	}
	if body["error"] != "endpoint_blocked" {
		t.Fatalf("error = %q, want the core's message", body["error"])
	}
}
