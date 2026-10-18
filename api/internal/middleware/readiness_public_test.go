package middleware

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"autoforward/internal/probes"
)

// The deploy gate, a container probe and a load balancer all call the readiness
// endpoint with no session and no API key. If the API-key gate refuses them, the
// only observer that decides whether to deploy or roll back reads 401 from a
// stack that is actually healthy — and rolls it back.
func TestReadinessProbesAreReachableWithoutCredentials(t *testing.T) {
	served := 0
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		served++
		w.WriteHeader(http.StatusOK)
	})
	h := APIKeyAuth("an-installed-key", next)

	for _, path := range []string{"/readyz", "/ready", "/healthz", "/health"} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest("GET", path, nil))
		if rec.Code != http.StatusOK {
			t.Errorf("GET %s without credentials: got %d, want 200 — a probe must not "+
				"need a key (it is the caller that has none)", path, rec.Code)
		}
	}
	// A stale or wrong key must not make a probe fail either: the probe is not
	// authenticated at all, so presenting a credential is irrelevant to it.
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/readyz", nil)
	req.Header.Set("X-API-Key", "a-stale-key")
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Errorf("GET /readyz with a stale key: got %d, want 200", rec.Code)
	}
	if served != 5 {
		t.Errorf("the probe handler must have run for every probe, ran %d times", served)
	}
}

// The negative control for the test above: widening the exemption for probes must
// not have widened it for anything else.
func TestAPIKeyGateStillRefusesEverythingElse(t *testing.T) {
	h := APIKeyAuth("an-installed-key", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	cases := []struct {
		name   string
		path   string
		header string
		value  string
		want   int
	}{
		{name: "no credential", path: "/api/rules", want: http.StatusUnauthorized},
		{name: "wrong key", path: "/api/rules", header: "X-API-Key", value: "nope", want: http.StatusUnauthorized},
		{name: "right key", path: "/api/rules", header: "X-API-Key", value: "an-installed-key", want: http.StatusOK},
		{name: "readiness is not a bypass for a sibling path", path: "/readyz/../api/rules", want: http.StatusUnauthorized},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest("GET", tc.path, nil)
			if tc.header != "" {
				req.Header.Set(tc.header, tc.value)
			}
			h.ServeHTTP(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("GET %s (%s): got %d, want %d", tc.path, tc.name, rec.Code, tc.want)
			}
		})
	}
}

// A successful probe is polling noise. A failed probe is the incident: the whole
// point of a readiness endpoint is that its failure is visible, so it must reach
// the log store instead of being filtered out as routine traffic.
func TestAccessLoggerArchivesAFailedProbe(t *testing.T) {
	client := newRecordingLogsClient()
	status := http.StatusOK
	h := AccessLogger(&ErrorLogSink{Client: client}, http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status) }))

	// Routine success: not archived.
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/readyz", nil))
	time.Sleep(50 * time.Millisecond)
	if n := client.count(); n != 0 {
		t.Fatalf("a successful probe must not be archived, got %d entries", n)
	}

	// The same path, now failing.
	status = http.StatusServiceUnavailable
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/readyz", nil))
	entry := client.wait(t)
	if entry.Level != "ERROR" {
		t.Errorf("a refused readiness probe must be archived at ERROR, got %q", entry.Level)
	}
	// The archived entry must identify the failing probe: an operator reading the
	// log store has to be able to tell WHICH endpoint is refusing and why.
	var detail map[string]any
	if err := json.Unmarshal([]byte(entry.Detail), &detail); err != nil {
		t.Fatalf("the archived probe detail must be JSON: %v", err)
	}
	if detail["path"] != "/readyz" {
		t.Errorf("the archived entry must carry the probe path, got %v", detail["path"])
	}
	if detail["status"] != float64(http.StatusServiceUnavailable) {
		t.Errorf("the archived entry must carry the refusal status, got %v", detail["status"])
	}
}

func TestProbePathsDoNotIncludeAuthenticatedSurfaces(t *testing.T) {
	for _, p := range []string{"/api/auth/login", "/api/rules", "/api/stats", "/logs/health", "/health/services"} {
		if probes.IsProbePath(p) {
			t.Errorf("%s must not be exempt from the API key: it is an authenticated surface", p)
		}
	}
}
