package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"

	grpcclient "autoforward/internal/grpcclient"
	pb "autoforward/proto"
)

// fakeFilterStoreClient is the v30 filter surface the Core serves: List, one
// filter by id, Update. Every management action on any client reaches the
// Core through exactly these three calls, so recording them is enough to
// prove the API forwards the caller's intent faithfully.
type fakeFilterStoreClient struct {
	pb.FilterControlServiceClient
	stored map[string]*pb.FilterRule
	owner  int64
	// updated captures the last UpdateFilter the Core was asked to apply, so
	// a test can assert what was sent, not only what came back.
	updated *pb.FilterRule
}

func (f *fakeFilterStoreClient) ListFilters(ctx context.Context, in *pb.ListFiltersRequest, opts ...grpc.CallOption) (*pb.ListFiltersResponse, error) {
	out := &pb.ListFiltersResponse{}
	for _, fl := range f.stored {
		if fl.OwnerUserId == in.OwnerUserId {
			// Hand back a deep copy: the handler mutates the filter it read
			// for a toggle, and that must never rewrite the fake's store.
			out.Filters = append(out.Filters, proto.Clone(fl).(*pb.FilterRule))
		}
	}
	return out, nil
}

func (f *fakeFilterStoreClient) UpdateFilter(ctx context.Context, in *pb.UpdateFilterRequest, opts ...grpc.CallOption) (*pb.FilterRule, error) {
	f.updated = in.Filter
	saved := proto.Clone(in.Filter).(*pb.FilterRule)
	f.stored[in.Filter.Id] = saved
	return saved, nil
}

func filterStoreHandler(store *fakeFilterStoreClient) *Handlers {
	return &Handlers{c: &grpcclient.Clients{Filters: store}}
}

func enabled(v bool) *bool { return &v }

// reqFilter builds a request for the /api/filters surface with the filter id
// set as the :id path value. The shared reqReplace helper only understands
// the replace-rules path shape, so the filter routes need their own.
func reqFilter(method, target string, body string, owner int64) *http.Request {
	r := httptest.NewRequest(method, target, strings.NewReader(body))
	id := target
	for _, prefix := range []string{"/api/filters/"} {
		if strings.HasPrefix(id, prefix) {
			id = strings.TrimPrefix(id, prefix)
		}
	}
	// Drop any trailing verb: /api/filters/<id>/toggle → <id>.
	if i := strings.Index(id, "/"); i >= 0 {
		id = id[:i]
	}
	r.SetPathValue("id", id)
	if owner != 0 {
		return r.WithContext(context.WithValue(r.Context(), identityKey{}, &Identity{UserID: owner}))
	}
	return r
}



// list → one filter → toggle → re-read: the round trip every client makes,
// and the state must survive it intact.
func TestFilterListIncludesEnabledState(t *testing.T) {
	store := &fakeFilterStoreClient{
		owner: 5094837833,
		stored: map[string]*pb.FilterRule{
			"f1": {Id: "f1", OwnerUserId: 5094837833, BlacklistKeywords: []string{"spam"}, IsEnabled: enabled(true)},
			"f2": {Id: "f2", OwnerUserId: 5094837833, WhitelistKeywords: []string{"gold"}, IsEnabled: enabled(false)},
			"f3": {Id: "f3", OwnerUserId: 999, BlacklistKeywords: []string{"other"}, IsEnabled: enabled(true)},
		},
	}
	h := filterStoreHandler(store)
	rec := httptest.NewRecorder()
	h.ListFilters(rec, reqFilter("GET", "/api/filters", "", 5094837833))
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var out struct {
		Filters []map[string]any `json:"filters"`
		Total   int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out.Total != 2 {
		t.Fatalf("owner must see only their own filters, got total=%d", out.Total)
	}
	byID := map[string]map[string]any{}
	for _, f := range out.Filters {
		byID[f["id"].(string)] = f
	}
	if byID["f1"]["is_enabled"] != true {
		t.Errorf("f1 must render as enabled, got %v", byID["f1"]["is_enabled"])
	}
	if byID["f2"]["is_enabled"] != false {
		t.Errorf("f2 must render its stored disabled state, got %v", byID["f2"]["is_enabled"])
	}
	// The other owner's filter must not even reach the JSON.
	if _, leaked := byID["f3"]; leaked {
		t.Errorf("cross-owner leak: f3 visible in the caller's list")
	}
}

// A filter with no is_enabled at all (a pre-v30 row, or an older client) must
// read as enabled — absence is never a silent disable.
func TestFilterNilEnabledReadsAsEnabled(t *testing.T) {
	store := &fakeFilterStoreClient{
		owner: 5094837833,
		stored: map[string]*pb.FilterRule{
			"f1": {Id: "f1", OwnerUserId: 5094837833, BlacklistKeywords: []string{"spam"}},
		},
	}
	h := filterStoreHandler(store)
	rec := httptest.NewRecorder()
	h.GetFilter(rec, reqFilter("GET", "/api/filters/f1", "", 5094837833))
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out["is_enabled"] != true {
		t.Errorf("nil is_enabled must render enabled, got %v", out["is_enabled"])
	}
}

// The toggle flips the value the SERVER stores, not the value a possibly
// stale client sent: the lists can never be clobbered by a stale read.
func TestToggleFilterFlipsStoredValueOnly(t *testing.T) {
	store := &fakeFilterStoreClient{
		owner: 5094837833,
		stored: map[string]*pb.FilterRule{
			"f1": {
				Id: "f1", OwnerUserId: 5094837833,
				BlacklistKeywords: []string{"spam", "scam"},
				WhitelistKeywords: []string{"gold"},
				IsEnabled:         enabled(true),
			},
		},
	}
	h := filterStoreHandler(store)

	rec := httptest.NewRecorder()
	h.ToggleFilter(rec, reqFilter("POST", "/api/filters/f1/toggle", "", 5094837833))
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out["is_enabled"] != false {
		t.Errorf("toggle must flip enabled→disabled, got %v", out["is_enabled"])
	}
	// The words the user typed must survive the round trip untouched.
	if store.updated == nil {
		t.Fatal("core never received the update")
	}
	got := store.updated.GetBlacklistKeywords()
	if len(got) != 2 || got[0] != "spam" || got[1] != "scam" {
		t.Errorf("blacklist clobbered by the toggle: %v", got)
	}
	if wl := store.updated.GetWhitelistKeywords(); len(wl) != 1 || wl[0] != "gold" {
		t.Errorf("whitelist clobbered by the toggle: %v", wl)
	}

	// Flip back: the same words must come back up, unchanged.
	rec2 := httptest.NewRecorder()
	h.ToggleFilter(rec2, reqFilter("POST", "/api/filters/f1/toggle", "", 5094837833))
	if rec2.Code != http.StatusOK {
		t.Fatalf("expected 200 on re-enable, got %d", rec2.Code)
	}
	var out2 map[string]any
	if err := json.Unmarshal(rec2.Body.Bytes(), &out2); err != nil {
		t.Fatal(err)
	}
	if out2["is_enabled"] != true {
		t.Errorf("toggle must flip back to enabled, got %v", out2["is_enabled"])
	}
	if len(store.stored["f1"].GetBlacklistKeywords()) != 2 {
		t.Errorf("words lost across the disable/re-enable cycle")
	}
}

// Another user's filter is a 404 on every management route: the API must not
// confirm whose it is, and must never hand the bytes over.
func TestFilterRoutesRejectOtherOwner(t *testing.T) {
	store := &fakeFilterStoreClient{
		owner: 5094837833,
		stored: map[string]*pb.FilterRule{
			"theirs": {Id: "theirs", OwnerUserId: 999, BlacklistKeywords: []string{"secret"}, IsEnabled: enabled(true)},
		},
	}
	h := filterStoreHandler(store)
	for _, tc := range []struct {
		name   string
		method string
		target string
	}{
		{"get other owner's filter", "GET", "/api/filters/theirs"},
		{"toggle other owner's filter", "POST", "/api/filters/theirs/toggle"},
		{"update other owner's filter", "PUT", "/api/filters/theirs"},
		{"delete other owner's filter", "DELETE", "/api/filters/theirs"},
	} {
		rec := httptest.NewRecorder()
		var hfn func(http.ResponseWriter, *http.Request)
		switch tc.method {
		case "GET":
			hfn = h.GetFilter
		case "POST":
			hfn = h.ToggleFilter
		case "PUT":
			hfn = h.UpdateFilter
		default:
			hfn = h.DeleteFilter
		}
		hfn(rec, reqFilter(tc.method, tc.target, `{}`, 5094837833))
		if rec.Code != http.StatusNotFound {
			t.Errorf("%s: expected 404, got %d: %s", tc.name, rec.Code, rec.Body.String())
		}
	}
	if store.updated != nil {
		t.Errorf("an update crossed the ownership boundary: %v", store.updated)
	}
}

// An unknown id is also a 404 — the client treats "not yours" and "does not
// exist" identically, and so must the route.
func TestGetFilterUnknownIdIs404(t *testing.T) {
	store := &fakeFilterStoreClient{owner: 5094837833, stored: map[string]*pb.FilterRule{}}
	h := filterStoreHandler(store)
	rec := httptest.NewRecorder()
	h.GetFilter(rec, reqFilter("GET", "/api/filters/does-not-exist", "", 5094837833))
	if rec.Code != http.StatusNotFound {
		t.Errorf("expected 404 for unknown id, got %d", rec.Code)
	}
}
