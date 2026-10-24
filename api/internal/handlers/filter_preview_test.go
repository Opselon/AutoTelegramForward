package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc"

	grpcclient "autoforward/internal/grpcclient"
	pb "autoforward/proto"
)

// fakeFilterPreviewClient records the preview request the Core receives.
type fakeFilterPreviewClient struct {
	pb.FilterControlServiceClient
	got *pb.TestFilterRuleRequest
}

func (f *fakeFilterPreviewClient) ListFilters(ctx context.Context, in *pb.ListFiltersRequest, opts ...grpc.CallOption) (*pb.ListFiltersResponse, error) {
	return &pb.ListFiltersResponse{}, nil
}

func (f *fakeFilterPreviewClient) TestFilterRule(ctx context.Context, in *pb.TestFilterRuleRequest, opts ...grpc.CallOption) (*pb.TestFilterRuleResponse, error) {
	f.got = in
	return &pb.TestFilterRuleResponse{
		Allowed: true, Reason: "whitelist:gold",
		Matched: []string{"gold"}, Mode: "any",
	}, nil
}

func filterPreviewHandler(f *fakeFilterPreviewClient) *Handlers {
	return &Handlers{c: &grpcclient.Clients{Filters: f}}
}

// The preview must be owner-scoped and read-only: the caller's identity is the
// only owner the Core ever sees, and the draft fields travel alongside the
// stored rule id.
func TestFilterPreviewForwardsOwnerAndDraft(t *testing.T) {
	f := &fakeFilterPreviewClient{}
	h := filterPreviewHandler(f)
	rec := httptest.NewRecorder()
	req := reqReplace("POST", "/api/filters/test",
		`{"rule_id":"abc","sample":"gold is rising","blacklist_keywords":["spam"],"regex_patterns":["^BUY"]}`,
		5094837833)
	h.TestFilterRule(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if f.got == nil {
		t.Fatal("core never received the preview call")
	}
	if f.got.GetOwnerUserId() != 5094837833 {
		t.Errorf("owner scope not forwarded: %d", f.got.GetOwnerUserId())
	}
	if f.got.GetSample() != "gold is rising" {
		t.Errorf("sample not forwarded: %q", f.got.GetSample())
	}
	if len(f.got.GetFilter().GetBlacklistKeywords()) != 1 || f.got.GetFilter().GetBlacklistKeywords()[0] != "spam" {
		t.Errorf("draft blacklist not forwarded: %v", f.got.GetFilter().GetBlacklistKeywords())
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out["allowed"] != true || out["reason"] != "whitelist:gold" {
		t.Errorf("unexpected response: %v", out)
	}
}

// An unauthenticated call never reaches the Core.
func TestFilterPreviewRequiresIdentity(t *testing.T) {
	h := filterPreviewHandler(&fakeFilterPreviewClient{})
	rec := httptest.NewRecorder()
	h.TestFilterRule(rec, reqReplace("POST", "/api/filters/test", `{}`, 0))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
}

// A sample longer than the preview bound is refused before the Core is called.
func TestFilterPreviewBoundsSampleLength(t *testing.T) {
	f := &fakeFilterPreviewClient{}
	h := filterPreviewHandler(f)
	rec := httptest.NewRecorder()
	long := strings.Repeat("a", maxSampleTextLen+1)
	h.TestFilterRule(rec, reqReplace("POST", "/api/filters/test",
		`{"sample":"`+long+`"}`, 5094837833))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
	if f.got != nil {
		t.Fatal("oversized sample reached the Core")
	}
}
