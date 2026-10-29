package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"

	"autoforward/internal/grpcclient"
	pb "autoforward/proto"
)

// --------------------------------------------------------------------------- //
// logFilters: the query surface must be parseable from both the friendly form
// (page/hours) and the precise form (offset/since/until) clients send.
// --------------------------------------------------------------------------- //

func TestParseLogFilters_DefaultsAndBounds(t *testing.T) {
	req := httptest.NewRequest("GET", "/api/logs", nil)
	f := parseLogFilters(req)
	if f.Limit != 100 {
		t.Fatalf("default limit must be 100, got %d", f.Limit)
	}
	if f.Offset != 0 || f.Ascending {
		t.Fatalf("defaults must be newest-first, offset 0: %+v", f)
	}

	// A limit above the page ceiling is clamped, not silently honoured: one
	// request must not be able to pull the whole archive.
	req = httptest.NewRequest("GET", "/api/logs?limit=99999&offset=-5", nil)
	f = parseLogFilters(req)
	if f.Limit != 100 {
		t.Fatalf("oversized limit must fall back to the default, got %d", f.Limit)
	}
	if f.Offset != 0 {
		t.Fatalf("negative offset must clamp to 0, got %d", f.Offset)
	}
}

func TestParseLogFilters_PagingForms(t *testing.T) {
	// page=3&limit=50 is the same window as offset=100.
	req := httptest.NewRequest("GET", "/api/logs?page=3&limit=50", nil)
	f := parseLogFilters(req)
	if f.Offset != 100 {
		t.Fatalf("page 3 at limit 50 must be offset 100, got %d", f.Offset)
	}
	// page=1 is offset 0 (and must not be treated as an out-of-range page).
	req = httptest.NewRequest("GET", "/api/logs?page=1&limit=50", nil)
	if got := parseLogFilters(req).Offset; got != 0 {
		t.Fatalf("page 1 must be offset 0, got %d", got)
	}
	req = httptest.NewRequest("GET", "/api/logs?offset=250", nil)
	if got := parseLogFilters(req).Offset; got != 250 {
		t.Fatalf("explicit offset lost, got %d", got)
	}
}

func TestParseLogFilters_RejectsOutOfRangePaging(t *testing.T) {
	// An offset wider than int32 must not wrap negative: a negative offset in
	// the request would silently flip "the oldest events" into the newest page.
	for _, raw := range []string{"2147483648", "9223372036854775807", "-5"} {
		req := httptest.NewRequest("GET", "/api/logs?offset="+raw, nil)
		if got := parseLogFilters(req).Offset; got != 0 {
			t.Fatalf("offset=%s must be rejected, got %d", raw, got)
		}
	}
	// A huge offset that still fits must be clamped, never wrapped.
	req := httptest.NewRequest("GET", "/api/logs?offset=1048575", nil)
	if got := parseLogFilters(req).Offset; got != 1048575 {
		t.Fatalf("in-range offset lost, got %d", got)
	}

	// A page whose window would overflow is rejected outright, so the caller
	// gets the first page rather than a silent wrap to a negative offset. It is
	// never clamped: inventing a window the caller did not ask for would show
	// them rows they cannot place, and the answer is the same either way.
	req = httptest.NewRequest("GET", "/api/logs?page=9223372036854775807&limit=500", nil)
	if got := parseLogFilters(req).Offset; got != 0 {
		t.Fatalf("overflowing page must be rejected, got offset %d", got)
	}
	// The last in-range page is honoured exactly...
	req = httptest.NewRequest("GET", "/api/logs?page=2098&limit=500", nil)
	if got := parseLogFilters(req).Offset; got != 1048500 {
		t.Fatalf("in-range page must be exact, got %d", got)
	}
	// ...and one page further is capped at the maximum window, not wrapped.
	req = httptest.NewRequest("GET", "/api/logs?page=2099&limit=500", nil)
	if got := parseLogFilters(req).Offset; got != maxLogOffset {
		t.Fatalf("page past the window must cap at %d, got %d", maxLogOffset, got)
	}

	// limit above the cap falls back to the default rather than being honoured.
	req = httptest.NewRequest("GET", "/api/logs?limit=100000", nil)
	if got := parseLogFilters(req).Limit; got != 100 {
		t.Fatalf("over-cap limit must fall back to the default, got %d", got)
	}
	req = httptest.NewRequest("GET", "/api/logs?limit=500", nil)
	if got := parseLogFilters(req).Limit; got != 500 {
		t.Fatalf("limit at the cap must be honoured, got %d", got)
	}

	// An absurd ?hours= would overflow time.Duration and produce a future
	// `since`, which would hide every event.
	req = httptest.NewRequest("GET", "/api/logs?hours=999999999999", nil)
	if got := parseLogFilters(req).Since; got != 0 {
		t.Fatalf("absurd hours must be rejected, got since=%d", got)
	}
}

func TestParseLogFilters_AttributionAndSeverities(t *testing.T) {
	req := httptest.NewRequest("GET",
		"/api/logs?rule_id=rule-77&correlation_id=corr-9&levels=ERROR,WARN&levels=DEBUG&until=1700000000", nil)
	f := parseLogFilters(req)
	if f.RuleID != "rule-77" || f.CorrelationID != "corr-9" {
		t.Fatalf("attribution filters lost: %+v", f)
	}
	want := []string{"ERROR", "WARN", "DEBUG"}
	if len(f.Levels) != len(want) {
		t.Fatalf("levels must accumulate across repeats and commas, got %v", f.Levels)
	}
	for i, w := range want {
		if f.Levels[i] != w {
			t.Fatalf("levels[%d] = %q, want %q", i, f.Levels[i], w)
		}
	}
	if f.Until != 1700000000 {
		t.Fatalf("until lost, got %d", f.Until)
	}
}

func TestParseLogFilters_HoursBecomesSince(t *testing.T) {
	before := time.Now().Unix()
	req := httptest.NewRequest("GET", "/api/logs?hours=6", nil)
	f := parseLogFilters(req)
	if f.Since < before-6*3600-5 || f.Since > before-6*3600+5 {
		t.Fatalf("hours=6 must resolve to ~6h ago, got %d", f.Since)
	}
	// An explicit since must win over the hours shorthand.
	req = httptest.NewRequest("GET", "/api/logs?hours=6&since=1700000000", nil)
	if got := parseLogFilters(req).Since; got != 1700000000 {
		t.Fatalf("explicit since must win, got %d", got)
	}
}

func TestParseLogFilters_MineScopesToCaller(t *testing.T) {
	req := httptest.NewRequest("GET", "/api/logs?mine=1", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 5094837833}))
	if got := parseLogFilters(req).UserID; got != 5094837833 {
		t.Fatalf("mine=1 must scope to the caller, got %d", got)
	}
	// Without mine, a tenant is still pinned to their own id: the archive is
	// one shared store and user_id == 0 means "every tenant" in the logger,
	// so an unscoped default would serve the whole installation to anyone
	// (see log_tenant_scope_test.go for the adversarial contract).
	req = httptest.NewRequest("GET", "/api/logs", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 5094837833}))
	if got := parseLogFilters(req).UserID; got != 5094837833 {
		t.Fatalf("a tenant default must stay pinned to their own id, got %d", got)
	}
	// An admin keeps the installation-wide default (user id 0 = every tenant).
	req = httptest.NewRequest("GET", "/api/logs", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 1, IsAdmin: true}))
	if got := parseLogFilters(req).UserID; got != 0 {
		t.Fatalf("admin default must stay unscoped, got %d", got)
	}
}

// --------------------------------------------------------------------------- //
// handlers
// --------------------------------------------------------------------------- //

type fakeLogsClient struct {
	lastQuery *pb.QueryLogsRequest
	lastStats *pb.LogStatsRequest
	lastPurge *pb.PurgeLogsRequest
	calls     int
	page      *pb.QueryLogsResponse
}

func (f *fakeLogsClient) Log(context.Context, *pb.LogRequest, ...grpc.CallOption) (*pb.StatusResponse, error) {
	return &pb.StatusResponse{Success: true}, nil
}

func (f *fakeLogsClient) QueryLogs(_ context.Context, in *pb.QueryLogsRequest, _ ...grpc.CallOption) (*pb.QueryLogsResponse, error) {
	f.calls++
	f.lastQuery = in
	if f.page != nil {
		return f.page, nil
	}
	return &pb.QueryLogsResponse{
		Total:   3,
		HasMore: true,
		Logs: []*pb.LogEntry{{
			Ts: 1700000000, Level: "ERROR", Service: "api", Category: "http",
			Message: "boom", Detail: `{"path":"/api/rules"}`, RuleId: "rule-1",
			UserId: 42, CorrelationId: "corr-1",
		}},
	}, nil
}

func (f *fakeLogsClient) LogStats(_ context.Context, in *pb.LogStatsRequest, _ ...grpc.CallOption) (*pb.LogStatsResponse, error) {
	f.lastStats = in
	return &pb.LogStatsResponse{
		Total: 9, ByLevel: map[string]int32{"ERROR": 1},
		ByCategory: map[string]int32{"http": 1}, ByRule: map[string]int32{"rule-1": 7},
		ByService: map[string]int32{"api": 9}, OldestTs: 5, NewestTs: 9,
	}, nil
}

func (f *fakeLogsClient) PurgeLogs(_ context.Context, in *pb.PurgeLogsRequest, _ ...grpc.CallOption) (*pb.PurgeLogsResponse, error) {
	f.lastPurge = in
	return &pb.PurgeLogsResponse{Success: true, Purged: 2, Remaining: 1}, nil
}

func TestQueryLogs_ReturnsTotalHasMoreAndAttribution(t *testing.T) {
	fake := &fakeLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}
	req := httptest.NewRequest("GET", "/api/logs?limit=50&offset=100&rule_id=rule-1", nil)
	rec := httptest.NewRecorder()
	h.QueryLogs(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if fake.lastQuery.Offset != 100 || fake.lastQuery.Limit != 50 {
		t.Fatalf("paging not forwarded: %+v", fake.lastQuery)
	}
	if fake.lastQuery.RuleId != "rule-1" {
		t.Fatalf("rule filter not forwarded: %+v", fake.lastQuery)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("bad JSON: %v", err)
	}
	// total is the real match count; count is this page's size. Conflating the
	// two is what made the old endpoint look like history only went back 100 rows.
	if body["total"] != float64(3) {
		t.Fatalf("total must be the match count, got %v", body["total"])
	}
	if body["count"] != float64(1) {
		t.Fatalf("count must be the page size, got %v", body["count"])
	}
	if body["has_more"] != true {
		t.Fatalf("has_more must be surfaced, got %v", body["has_more"])
	}
	logs := body["logs"].([]any)
	entry := logs[0].(map[string]any)
	for _, key := range []string{"rule_id", "user_id", "correlation_id", "detail", "level"} {
		if _, ok := entry[key]; !ok {
			t.Fatalf("log entry must expose %q: %v", key, entry)
		}
	}
	if entry["rule_id"] != "rule-1" {
		t.Fatalf("rule_id lost in translation: %v", entry["rule_id"])
	}
}

func TestQueryLogs_EmptyResultIsArrayNotNull(t *testing.T) {
	fake := &fakeLogsClient{page: &pb.QueryLogsResponse{Total: 0, HasMore: false}}
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}
	rec := httptest.NewRecorder()
	h.QueryLogs(rec, httptest.NewRequest("GET", "/api/logs", nil))

	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("bad JSON: %v", err)
	}
	// A nil log list marshals to null and breaks every client-side .length check.
	if _, ok := body["logs"].([]any); !ok {
		t.Fatalf("logs must be an empty array, got %T", body["logs"])
	}
}

func TestQueryLogs_ServiceDownIsBadGatewayNot500(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{}}
	rec := httptest.NewRecorder()
	h.QueryLogs(rec, httptest.NewRequest("GET", "/api/logs", nil))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("want 502 when the logger is absent, got %d", rec.Code)
	}
}

// --------------------------------------------------------------------------- //
// per-task logs
// --------------------------------------------------------------------------- //

func TestRuleLogs_RejectsForeignRule(t *testing.T) {
	fake := &fakeLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{
		Logs:  fake,
		Rules: &fakeRulesClient{owned: []*pb.ForwardRule{{Id: "rule-mine", OwnerUserId: 222}}},
	}}
	req := httptest.NewRequest("GET", "/api/rules/rule-theirs/logs", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	req.SetPathValue("id", "rule-theirs")
	rec := httptest.NewRecorder()
	h.RuleLogs(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("foreign rule logs must 404, got %d", rec.Code)
	}
	if fake.calls != 0 {
		t.Fatalf("a rejected read must not reach the logger at all, calls=%d", fake.calls)
	}
}

func TestRuleLogs_ForcesRuleFilterAndClearsTenantFilter(t *testing.T) {
	fake := &fakeLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{
		Logs:  fake,
		Rules: &fakeRulesClient{owned: []*pb.ForwardRule{{Id: "rule-mine", OwnerUserId: 222}}},
	}}
	// The caller tries to widen the read with user_id; the rule filter must win.
	req := httptest.NewRequest("GET", "/api/rules/rule-mine/logs?rule_id=someone-elses&user_id=999", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 222}))
	req.SetPathValue("id", "rule-mine")
	rec := httptest.NewRecorder()
	h.RuleLogs(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200 for an owned rule, got %d", rec.Code)
	}
	if fake.lastQuery.RuleId != "rule-mine" {
		t.Fatalf("the path's rule id must override the query string, got %q", fake.lastQuery.RuleId)
	}
	if fake.lastQuery.UserId != 0 {
		t.Fatalf("tenant filter must be cleared for a rule-scoped read, got %d", fake.lastQuery.UserId)
	}
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body["rule_id"] != "rule-mine" {
		t.Fatalf("response must echo the rule id, got %v", body["rule_id"])
	}
}

// --------------------------------------------------------------------------- //
// stats + purge
// --------------------------------------------------------------------------- //

func TestLogStats_ScopedByRuleAndMine(t *testing.T) {
	fake := &fakeLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}
	req := httptest.NewRequest("GET", "/api/logs/stats?rule_id=rule-1&mine=1", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 7}))
	rec := httptest.NewRecorder()
	h.LogStats(rec, req)

	if fake.lastStats.RuleId != "rule-1" || fake.lastStats.UserId != 7 {
		t.Fatalf("stats scope not forwarded: %+v", fake.lastStats)
	}
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	for _, key := range []string{"by_rule", "by_service", "oldest_ts", "newest_ts"} {
		if _, ok := body[key]; !ok {
			t.Fatalf("stats must expose %q so the UI can show history depth", key)
		}
	}
}

func TestPurgeLogs_RejectsForeignRuleScope(t *testing.T) {
	fake := &fakeLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{
		Logs:  fake,
		Rules: &fakeRulesClient{owned: []*pb.ForwardRule{{Id: "rule-mine", OwnerUserId: 222}}},
	}}
	req := httptest.NewRequest("POST", "/api/logs/purge", strings.NewReader(`{"rule_id":"rule-theirs"}`))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 222}))
	rec := httptest.NewRecorder()
	h.PurgeLogs(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("purging another tenant's task must 404, got %d", rec.Code)
	}
	if fake.lastPurge != nil {
		t.Fatalf("a rejected purge must never reach the logger: %+v", fake.lastPurge)
	}
}

func TestPurgeLogs_KeepsSafetyWindowAndForwardsRule(t *testing.T) {
	fake := &fakeLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{
		Logs:  fake,
		Rules: &fakeRulesClient{owned: []*pb.ForwardRule{{Id: "rule-mine", OwnerUserId: 222}}},
	}}
	req := httptest.NewRequest("POST", "/api/logs/purge", strings.NewReader(`{"rule_id":"rule-mine"}`))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 222}))
	rec := httptest.NewRecorder()
	h.PurgeLogs(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if fake.lastPurge.RuleId != "rule-mine" {
		t.Fatalf("rule scope not forwarded: %+v", fake.lastPurge)
	}
	// The 24h protection default must survive a scoped purge: deleting the last
	// day of a task's history is never what a user means by "clean up".
	if fake.lastPurge.OlderThanHours != 24 {
		t.Fatalf("safety window must default to 24h, got %d", fake.lastPurge.OlderThanHours)
	}
}

// --------------------------------------------------------------------------- //
// export
// --------------------------------------------------------------------------- //

func TestExportLogs_PagesThroughHistoryAndSetsDownloadHeaders(t *testing.T) {
	// First page claims more; second page ends the walk. The handler must
	// follow has_more rather than trusting a single page.
	fake := &pagingLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}
	req := httptest.NewRequest("GET", "/api/logs/export?format=json", nil)
	rec := httptest.NewRecorder()
	h.ExportLogs(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if cd := rec.Header().Get("Content-Disposition"); !strings.Contains(cd, "attachment") ||
		!strings.Contains(cd, "atf-logs-") {
		t.Fatalf("export must download as a file, got %q", cd)
	}
	var body struct {
		Rows int `json:"rows"`
		Logs []struct {
			Ts     int64  `json:"ts"`
			RuleID string `json:"rule_id"`
		} `json:"logs"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("bad JSON: %v", err)
	}
	if body.Rows != 3 {
		t.Fatalf("paging must collect every row (1+2), got %d", body.Rows)
	}
	if fake.pagesServed != 2 {
		t.Fatalf("expected 2 pages walked, got %d", fake.pagesServed)
	}
	if fake.ascendingSeen != true {
		t.Fatalf("an exported archive must be read oldest-first")
	}
}

func TestExportLogs_CsvHasHeaderAndOneRowPerEntry(t *testing.T) {
	fake := &pagingLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}
	req := httptest.NewRequest("GET", "/api/logs/export?format=csv", nil)
	rec := httptest.NewRecorder()
	h.ExportLogs(rec, req)

	if !strings.HasPrefix(rec.Header().Get("Content-Type"), "text/csv") {
		t.Fatalf("csv export must declare text/csv, got %q", rec.Header().Get("Content-Type"))
	}
	lines := strings.Split(strings.TrimSpace(rec.Body.String()), "\n")
	if len(lines) != 4 { // header + 3 rows
		t.Fatalf("want header + 3 rows, got %d lines:\n%s", len(lines), rec.Body.String())
	}
	if !strings.Contains(lines[0], "rule_id") || !strings.Contains(lines[0], "correlation_id") {
		t.Fatalf("csv header must carry attribution columns, got %q", lines[0])
	}
}

func TestExportLogs_ServiceDownIsBadGateway(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{}}
	rec := httptest.NewRecorder()
	h.ExportLogs(rec, httptest.NewRequest("GET", "/api/logs/export", nil))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d", rec.Code)
	}
}

// pagingLogsClient serves 1 row then 2 rows, then stops.
type pagingLogsClient struct {
	pagesServed   int
	ascendingSeen bool
}

func (p *pagingLogsClient) Log(context.Context, *pb.LogRequest, ...grpc.CallOption) (*pb.StatusResponse, error) {
	return &pb.StatusResponse{Success: true}, nil
}

func (p *pagingLogsClient) QueryLogs(_ context.Context, in *pb.QueryLogsRequest, _ ...grpc.CallOption) (*pb.QueryLogsResponse, error) {
	p.pagesServed++
	p.ascendingSeen = in.Ascending
	switch p.pagesServed {
	case 1:
		return &pb.QueryLogsResponse{
			Total: 3, HasMore: true,
			Logs: []*pb.LogEntry{{Ts: 1, Level: "INFO", Message: "one", RuleId: "r"}},
		}, nil
	default:
		return &pb.QueryLogsResponse{
			Total: 3, HasMore: false,
			Logs: []*pb.LogEntry{
				{Ts: 2, Level: "INFO", Message: "two", RuleId: "r"},
				{Ts: 3, Level: "WARN", Message: "three", RuleId: "r"},
			},
		}, nil
	}
}

func (p *pagingLogsClient) LogStats(context.Context, *pb.LogStatsRequest, ...grpc.CallOption) (*pb.LogStatsResponse, error) {
	return &pb.LogStatsResponse{}, nil
}

func (p *pagingLogsClient) PurgeLogs(context.Context, *pb.PurgeLogsRequest, ...grpc.CallOption) (*pb.PurgeLogsResponse, error) {
	return &pb.PurgeLogsResponse{Success: true}, nil
}
