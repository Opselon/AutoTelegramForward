package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

// The worker-ops routes are a PROXY, not a collector: they must never
// fabricate a snapshot when the worker is unreachable, and they must pass
// the worker's own payload through unmodified when it is. These pin both
// halves of that contract against a local stand-in for the worker's :8090.

func withWorkerOps(t *testing.T, fn func()) {
	t.Helper()
	prev := os.Getenv("ATF_WORKER_OPS_URL")
	os.Setenv("ATF_WORKER_OPS_URL", "http://127.0.0.1:1") // nothing listens
	defer func() { os.Setenv("ATF_WORKER_OPS_URL", prev) }()
	fn()
}

func TestWorkerRoutes_ReportUnreachableInsteadOfZeroes(t *testing.T) {
	withWorkerOps(t, func() {
		for _, route := range []string{"/worker/status", "/worker/queue", "/worker/tasks", "/worker/errors"} {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, route, nil)
			switch route {
			case "/worker/status":
				(&Handlers{}).WorkerStatus(rec, req)
			case "/worker/queue":
				(&Handlers{}).WorkerQueue(rec, req)
			case "/worker/tasks":
				(&Handlers{}).WorkerTasks(rec, req)
			case "/worker/errors":
				(&Handlers{}).WorkerErrors(rec, req)
			}
			if rec.Code != http.StatusServiceUnavailable {
				t.Fatalf("%s: unreachable worker must 503, got %d", route, rec.Code)
			}
			var body map[string]any
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("%s: bad json: %v", route, err)
			}
			// A zeroed snapshot would look like an idle, healthy worker —
			// exactly the misleading state this API must never produce.
			if body["worker_reachable"] != false {
				t.Fatalf("%s: expected worker_reachable=false, got %v", route, body["worker_reachable"])
			}
			if _, hasErr := body["error"]; !hasErr {
				t.Fatalf("%s: unreachable response must name the failure", route)
			}
		}
	})
}

func TestWorkerStatus_PassesThroughWorkerPayload(t *testing.T) {
	worker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/ops" {
			t.Errorf("status route must hit /ops, hit %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"worker": {"state": "ready", "commit_sha": "82f1982", "utilization": 0.25}}`))
	}))
	defer worker.Close()
	os.Setenv("ATF_WORKER_OPS_URL", worker.URL)
	defer os.Unsetenv("ATF_WORKER_OPS_URL")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/worker/status", nil)
	(&Handlers{}).WorkerStatus(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if body["worker_reachable"] != true {
		t.Fatalf("expected worker_reachable=true, got %v", body["worker_reachable"])
	}
	w, ok := body["worker"].(map[string]any)
	if !ok {
		t.Fatalf("worker object missing: %v", body)
	}
	if w["commit_sha"] != "82f1982" {
		t.Fatalf("payload not passed through: %v", w)
	}
}

func TestWorkerErrors_QueryIsBoundedAndFiltered(t *testing.T) {
	var seenQuery string
	worker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"total": 1, "items": []}`))
	}))
	defer worker.Close()
	os.Setenv("ATF_WORKER_OPS_URL", worker.URL)
	defer os.Unsetenv("ATF_WORKER_OPS_URL")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet,
		"/worker/errors?severity=WARN&component=worker.delivery&rule_id=abc&since=1700000000&limit=99999", nil)
	(&Handlers{}).WorkerErrors(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	for _, want := range []string{"severity=WARN", "component=worker.delivery", "rule_id=abc", "since=1700000000"} {
		if !strings.Contains(seenQuery, want) {
			t.Errorf("filter %q not forwarded (query was %q)", want, seenQuery)
		}
	}
	if !strings.Contains(seenQuery, "limit=256") {
		t.Errorf("limit must be capped at 256 (query was %q)", seenQuery)
	}
}

func TestWorkerErrors_RejectsNonNumericSince(t *testing.T) {
	worker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{}`))
	}))
	defer worker.Close()
	os.Setenv("ATF_WORKER_OPS_URL", worker.URL)
	defer os.Unsetenv("ATF_WORKER_OPS_URL")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/worker/errors?since=not-a-number&limit=abc", nil)
	(&Handlers{}).WorkerErrors(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("malformed params are dropped, not fatal: got %d", rec.Code)
	}
}

// The incidents routes add a FILTERED proxy (state must be one of the
// lifecycle states) and a POST-only ack. Pin both contracts: an invalid
// state is rejected before the worker is even asked, a GET ack is 405, a
// missing id is 400, and an unreachable worker is 503 with
// worker_reachable:false — never a fabricated incident list.

func TestWorkerIncidents_RejectInvalidStateFilter(t *testing.T) {
	withWorkerOps(t, func() {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/worker/incidents?state=definitely-not-a-lifecycle", nil)
		(&Handlers{}).WorkerIncidents(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("invalid state filter: got %d, want 400 (reject before asking the worker)", rec.Code)
		}
	})
}

func TestWorkerIncidents_UnreachableIs503(t *testing.T) {
	withWorkerOps(t, func() {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/worker/incidents", nil)
		(&Handlers{}).WorkerIncidents(rec, req)
		if rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("unreachable worker: got %d, want 503", rec.Code)
		}
		var body map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatalf("bad json: %v", err)
		}
		if body["worker_reachable"] != false {
			t.Errorf("worker_reachable = %v, want false (never fabricate incidents)", body["worker_reachable"])
		}
	})
}

func TestWorkerIncidentAck_MethodAndIDContract(t *testing.T) {
	// GET is 405 with the Allow header naming POST: a prefetch or a
	// browser navigation must never ackincident anything.
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/worker/incidents/ack?id=abc", nil)
	(&Handlers{}).WorkerIncidentAck(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET ack: got %d, want 405", rec.Code)
	}
	if rec.Header().Get("Allow") != http.MethodPost {
		t.Errorf("Allow = %q, want POST", rec.Header().Get("Allow"))
	}

	// Missing id is 400, not a proxy call with an empty id.
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "/worker/incidents/ack", nil)
	(&Handlers{}).WorkerIncidentAck(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("POST ack without id: got %d, want 400", rec.Code)
	}

	// Valid id + unreachable worker = 503, never a fake success.
	withWorkerOps(t, func() {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/worker/incidents/ack?id=abc", nil)
		(&Handlers{}).WorkerIncidentAck(rec, req)
		if rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("ack with unreachable worker: got %d, want 503", rec.Code)
		}
	})
}
