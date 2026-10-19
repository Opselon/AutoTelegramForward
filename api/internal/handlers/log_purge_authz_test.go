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
)

// The log archive is one shared store: it has no tenant dimension on the purge
// path. So an unscoped `POST /api/logs/purge` deletes rows for every tenant,
// and it was reachable by any authenticated user — a tenant could erase another
// tenant's history (the evidence trail this API exists to keep) with nothing
// but their own dashboard session. The ownership check on `rule_id` bounded
// nothing, because omitting `rule_id` was allowed.
//
// The boundary is now the one the read path already uses: a non-admin must name
// a task they own, and the installation-wide form is the administrator plane.

func purgeRequest(t *testing.T, uid int64, isAdmin bool, body string) *http.Request {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/logs/purge", strings.NewReader(body))
	return req.WithContext(context.WithValue(req.Context(), identityKey{},
		&Identity{UserID: uid, IsAdmin: isAdmin}))
}

func purgeHandlerWithRule(owner int64) (*Handlers, *fakeLogsClient) {
	fake := &fakeLogsClient{}
	return &Handlers{c: &grpcclient.Clients{
		Logs:  fake,
		Rules: &fakeRulesClient{owned: []*pb.ForwardRule{{Id: "rule-mine", OwnerUserId: owner}}},
	}}, fake
}

func TestPurgeLogs_UnscopedPurgeIsRefusedForATenant(t *testing.T) {
	for _, body := range []string{
		``,
		`{}`,
		`{"older_than_hours":1}`,
		`{"level":"ERROR"}`,
		`{"service":"api","category":"http"}`,
	} {
		h, fake := purgeHandlerWithRule(222)
		rec := httptest.NewRecorder()
		h.PurgeLogs(rec, purgeRequest(t, 222, false, body))

		if rec.Code != http.StatusForbidden {
			t.Fatalf("unscoped purge %q must be 403 for a tenant, got %d: %s",
				body, rec.Code, rec.Body.String())
		}
		var out map[string]string
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("refusal must be JSON: %v (%s)", err, rec.Body.String())
		}
		if out["error_code"] != "rule_scope_required" {
			t.Fatalf("refusal must name the reason so a client can explain it, got %q", out["error_code"])
		}
		if fake.lastPurge != nil {
			t.Fatalf("a refused purge must never reach the logger: %+v", fake.lastPurge)
		}
	}
}

// The scrape-the-whole-archive query form must not be a way around the guard.
func TestPurgeLogs_QueryStringCannotBypassTheRuleScope(t *testing.T) {
	h, fake := purgeHandlerWithRule(222)
	req := purgeRequest(t, 222, false, ``)
	req.URL.RawQuery = "older_than_hours=1&level=ERROR"
	rec := httptest.NewRecorder()

	h.PurgeLogs(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("want 403, got %d: %s", rec.Code, rec.Body.String())
	}
	if fake.lastPurge != nil {
		t.Fatalf("a refused purge must never reach the logger: %+v", fake.lastPurge)
	}
}

// The administrator plane keeps the installation-wide control: that is what it
// is for, and it is the same gate PUT /api/logs/policy already has.
func TestPurgeLogs_AdminMayPurgeUnscoped(t *testing.T) {
	h, fake := purgeHandlerWithRule(222)
	rec := httptest.NewRecorder()

	h.PurgeLogs(rec, purgeRequest(t, 5094837833, true, `{"older_than_hours":48}`))

	if rec.Code != http.StatusOK {
		t.Fatalf("an admin's unscoped purge must be allowed, got %d: %s", rec.Code, rec.Body.String())
	}
	if fake.lastPurge == nil || fake.lastPurge.OlderThanHours != 48 {
		t.Fatalf("purge filters not forwarded: %+v", fake.lastPurge)
	}
}

// An identified caller may still purge their own task's history: that is the
// tenant-facing feature the dashboard uses, and it is already owner-checked.
func TestPurgeLogs_TenantMayPurgeAnOwnedRule(t *testing.T) {
	h, fake := purgeHandlerWithRule(222)
	rec := httptest.NewRecorder()

	h.PurgeLogs(rec, purgeRequest(t, 222, false, `{"rule_id":"rule-mine","older_than_hours":0}`))

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if fake.lastPurge.GetRuleId() != "rule-mine" {
		t.Fatalf("rule scope not forwarded: %+v", fake.lastPurge)
	}
}
