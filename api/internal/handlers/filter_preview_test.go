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

func TestFilterPreviewSeparationWithRegexBlockAndRequire(t *testing.T) {
	f := &fakeFilterPreviewClient{}
	h := filterPreviewHandler(f)
	rec := httptest.NewRecorder()
	req := reqReplace("POST", "/api/filters/test",
		`{"rule_id":"abc","sample":"buy signal","whitelist_words":["signal"],"blacklist_words":["scam"],"regex_require":["^buy"],"regex_block":["promo"]}`,
		5094837833)
	h.TestFilterRule(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if f.got == nil {
		t.Fatal("core never received the preview call")
	}
	wl := f.got.GetFilter().GetWhitelistKeywords()
	bl := f.got.GetFilter().GetBlacklistKeywords()
	rxBl := f.got.GetRegexBlock()
	rxReq := f.got.GetRegexRequire()
	filterRxBl := f.got.GetFilter().GetRegexBlock()
	filterRxReq := f.got.GetFilter().GetRegexRequire()

	if len(wl) != 1 || wl[0] != "signal" {
		t.Errorf("whitelist words not mapped: %v", wl)
	}
	if len(bl) != 1 || bl[0] != "scam" {
		t.Errorf("blacklist words not mapped: %v", bl)
	}
	if len(rxReq) != 1 || rxReq[0] != "^buy" || len(filterRxReq) != 1 || filterRxReq[0] != "^buy" {
		t.Errorf("regex require not preserved: req=%v, filter=%v", rxReq, filterRxReq)
	}
	if len(rxBl) != 1 || rxBl[0] != "promo" || len(filterRxBl) != 1 || filterRxBl[0] != "promo" {
		t.Errorf("regex block not preserved: req=%v, filter=%v", rxBl, filterRxBl)
	}
}

func TestEnrichCustomMetadataAndFormatRuleJSONParity(t *testing.T) {
	rule := &pb.ForwardRule{
		Id: "r123",
	}
	payload := `{"metadata":{"whitelist_words":["wl1"],"blacklist_words":["bl1"],"regex_block":["^rx_bl"],"regex_require":["^rx_wl"],"whitelist_mode":"all"}}`
	enrichCustomMetadata([]byte(payload), rule)

	if rule.CustomMetadataJson == "" {
		t.Fatal("custom metadata JSON was not populated")
	}

	formatted := formatRuleJSON(rule)
	wl, ok := formatted["whitelist_words"].([]string)
	if !ok || len(wl) != 1 || wl[0] != "wl1" {
		t.Errorf("expected whitelist_words ['wl1'], got %v", formatted["whitelist_words"])
	}
	bl, ok := formatted["blacklist_words"].([]string)
	if !ok || len(bl) != 1 || bl[0] != "bl1" {
		t.Errorf("expected blacklist_words ['bl1'], got %v", formatted["blacklist_words"])
	}
	rxBl, ok := formatted["regex_block"].([]string)
	if !ok || len(rxBl) != 1 || rxBl[0] != "^rx_bl" {
		t.Errorf("expected regex_block ['^rx_bl'], got %v", formatted["regex_block"])
	}
	rxWl, ok := formatted["regex_require"].([]string)
	if !ok || len(rxWl) != 1 || rxWl[0] != "^rx_wl" {
		t.Errorf("expected regex_require ['^rx_wl'], got %v", formatted["regex_require"])
	}
	if formatted["whitelist_mode"] != "all" {
		t.Errorf("expected whitelist_mode 'all', got %v", formatted["whitelist_mode"])
	}
}
