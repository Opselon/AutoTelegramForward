package middleware

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"

	pb "autoforward/proto"
)

// recordingLogsClient captures every entry the middleware persists.
type recordingLogsClient struct {
	mu      sync.Mutex
	entries []*pb.LogEntry
	signal  chan struct{}
}

func newRecordingLogsClient() *recordingLogsClient {
	return &recordingLogsClient{signal: make(chan struct{}, 64)}
}

func (c *recordingLogsClient) Log(_ context.Context, in *pb.LogRequest, _ ...grpc.CallOption) (*pb.StatusResponse, error) {
	c.mu.Lock()
	c.entries = append(c.entries, in.Entry)
	c.mu.Unlock()
	select {
	case c.signal <- struct{}{}:
	default:
	}
	return &pb.StatusResponse{Success: true}, nil
}

func (c *recordingLogsClient) QueryLogs(context.Context, *pb.QueryLogsRequest, ...grpc.CallOption) (*pb.QueryLogsResponse, error) {
	return &pb.QueryLogsResponse{}, nil
}

func (c *recordingLogsClient) LogStats(context.Context, *pb.LogStatsRequest, ...grpc.CallOption) (*pb.LogStatsResponse, error) {
	return &pb.LogStatsResponse{}, nil
}

func (c *recordingLogsClient) PurgeLogs(context.Context, *pb.PurgeLogsRequest, ...grpc.CallOption) (*pb.PurgeLogsResponse, error) {
	return &pb.PurgeLogsResponse{}, nil
}

func (c *recordingLogsClient) wait(t *testing.T) *pb.LogEntry {
	t.Helper()
	select {
	case <-c.signal:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for the access log entry")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.entries) == 0 {
		t.Fatal("no entry captured")
	}
	return c.entries[len(c.entries)-1]
}

func (c *recordingLogsClient) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.entries)
}

func TestAccessLogger_LogsSuccessNotJustFailure(t *testing.T) {
	client := newRecordingLogsClient()
	h := AccessLogger(&ErrorLogSink{Client: client}, http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"ok":true}`))
		}))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/rules", nil))

	entry := client.wait(t)
	if entry.Level != "INFO" {
		t.Fatalf("a 200 must be logged at INFO, got %q", entry.Level)
	}
	if entry.Service != "api" || entry.Category != "http" {
		t.Fatalf("service/category must mark the API access trail: %+v", entry)
	}
	if entry.Ts == 0 {
		t.Fatal("entry must carry a timestamp")
	}
	var detail map[string]any
	if err := json.Unmarshal([]byte(entry.Detail), &detail); err != nil {
		t.Fatalf("detail must be valid JSON: %v", err)
	}
	// The outcome and cost of the call are the point of an access trail.
	for _, key := range []string{"method", "path", "status", "ms", "bytes"} {
		if _, ok := detail[key]; !ok {
			t.Fatalf("detail must record %q, got %v", key, detail)
		}
	}
	if detail["status"] != float64(200) {
		t.Fatalf("detail status must be 200, got %v", detail["status"])
	}
}

func TestAccessLogger_LevelTracksOutcome(t *testing.T) {
	for _, tc := range []struct {
		status int
		level  string
	}{
		{http.StatusCreated, "INFO"},
		{http.StatusNotFound, "WARN"},
		{http.StatusBadRequest, "WARN"},
		{http.StatusInternalServerError, "ERROR"},
		{http.StatusBadGateway, "ERROR"},
	} {
		client := newRecordingLogsClient()
		status := tc.status
		h := AccessLogger(&ErrorLogSink{Client: client}, http.HandlerFunc(
			func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status) }))
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("POST", "/api/rules", nil))
		if got := client.wait(t).Level; got != tc.level {
			t.Fatalf("status %d must log %s, got %s", tc.status, tc.level, got)
		}
	}
}

func TestAccessLogger_AttributesRuleFromPath(t *testing.T) {
	client := newRecordingLogsClient()
	h := AccessLogger(&ErrorLogSink{Client: client}, http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			// Mimic the auth layer filling in the verified actor.
			if m := Meta(r.Context()); m != nil {
				m.UserID = 5094837833
				m.Username = "Capxi"
			}
			w.WriteHeader(http.StatusOK)
		}))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("PATCH", "/api/rules/rule-abc/settings", nil))

	entry := client.wait(t)
	if entry.RuleId != "rule-abc" {
		t.Fatalf("the subject rule must be attributed, got %q", entry.RuleId)
	}
	if entry.UserId != 5094837833 {
		t.Fatalf("the verified actor must be attributed, got %d", entry.UserId)
	}
	if entry.CorrelationId == "" {
		t.Fatal("entry must carry a correlation id so it can be found again")
	}
	if rec.Header().Get("X-Correlation-ID") != entry.CorrelationId {
		t.Fatalf("the logged id must match the one handed to the client: %q vs %q",
			rec.Header().Get("X-Correlation-ID"), entry.CorrelationId)
	}
}

func TestAccessLogger_ReusesUpstreamCorrelationID(t *testing.T) {
	client := newRecordingLogsClient()
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	// CorrelationID runs outside AccessLogger in the real chain; that id must
	// be the one written to the archive, not a fresh unrelated value.
	h := CorrelationID(AccessLogger(&ErrorLogSink{Client: client}, inner))

	req := httptest.NewRequest("GET", "/api/stats", nil)
	req.Header.Set("X-Correlation-ID", "corr-existing-from-gateway")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if got := client.wait(t).CorrelationId; got != "corr-existing-from-gateway" {
		t.Fatalf("the upstream correlation id must be preserved, got %q", got)
	}
}

func TestAccessLogger_SkipsLivenessProbes(t *testing.T) {
	client := newRecordingLogsClient()
	h := AccessLogger(&ErrorLogSink{Client: client}, http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))

	for _, path := range []string{"/healthz", "/health", "/api/version", "/api/health/services"} {
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", path, nil))
	}
	// Give any stray goroutine a chance to fire before asserting silence.
	time.Sleep(50 * time.Millisecond)
	if n := client.count(); n != 0 {
		t.Fatalf("probe traffic must not be archived, got %d entries", n)
	}

	// A real call on the same chain is still archived — proof the skip is a
	// filter, not a broken logger.
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/rules", nil))
	_ = client.wait(t)
}

func TestAccessLogger_NilSinkStillServesAndExposesMeta(t *testing.T) {
	var sawMeta *RequestMeta
	h := AccessLogger(nil, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawMeta = Meta(r.Context())
		w.WriteHeader(http.StatusOK)
	}))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/rules/rule-9", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("a logger outage must never break the request, got %d", rec.Code)
	}
	if sawMeta == nil || sawMeta.RuleID != "rule-9" {
		t.Fatalf("meta must still be available without a sink: %+v", sawMeta)
	}
}

func TestMeta_NilWhenAbsent(t *testing.T) {
	if Meta(context.Background()) != nil {
		t.Fatal("Meta must return nil when no access logger is installed")
	}
}
