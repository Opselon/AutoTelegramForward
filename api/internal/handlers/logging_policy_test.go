package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"autoforward/internal/logpolicy"
)

// The control plane is where an operator changes what the system records, so the
// tests here are mostly about what it must refuse: non-admins, nonsense
// durations, repeated clicks, and — above all — reporting a scope it cannot
// actually see instead of a plausible zero.

func newPolicyHandlers(policy *logpolicy.Policy) (*Handlers, *[]map[string]any) {
	h := &Handlers{Policy: policy}
	recorded := &[]map[string]any{}
	var mu sync.Mutex
	h.LogAudit = func(actor int64, action, reasonCode string, detail map[string]any) {
		mu.Lock()
		defer mu.Unlock()
		*recorded = append(*recorded, map[string]any{
			"actor": actor, "action": action, "reason_code": reasonCode, "detail": detail,
		})
	}
	return h, recorded
}

func adminRequest(method, target, body string) *http.Request {
	var reader *strings.Reader
	if body == "" {
		reader = strings.NewReader("")
	} else {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, target, reader)
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{
		UserID: 5094837833, IsAdmin: true, Username: "Capxi",
	}))
}

func decode(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("response must be JSON: %v (%s)", err, rec.Body.String())
	}
	return out
}

func TestLogPolicy_ReportsTheScopeOfEachAnswer(t *testing.T) {
	policy := logpolicy.New(nil, 0)
	h, _ := newPolicyHandlers(policy)

	rec := httptest.NewRecorder()
	h.LogPolicy(rec, adminRequest("GET", "/logs/policy", ""))
	if rec.Code != http.StatusOK {
		t.Fatalf("policy read must succeed, got %d", rec.Code)
	}
	body := decode(t, rec)
	scopes, ok := body["scopes"].(map[string]any)
	if !ok {
		t.Fatalf("response must be scoped per process: %v", body)
	}

	api, ok := scopes["api"].(map[string]any)
	if !ok {
		t.Fatal("the API's own policy must be reported")
	}
	if api["available"] != true {
		t.Fatalf("the API scope is available, got %v", api["available"])
	}
	levels, _ := api["effective_levels"].([]any)
	if len(levels) != 3 {
		t.Fatalf("effective levels must be the documented defaults, got %v", api["effective_levels"])
	}
	counters, _ := api["counters"].(map[string]any)
	if counters == nil {
		t.Fatal("counters must be present for a scope that has them")
	}
	// The two suppression counters and the emitted counter must be distinct
	// labels, not one number under three names.
	for _, key := range []string{"emitted", "emitted_security", "suppressed_level", "suppressed_rate"} {
		if _, ok := counters[key]; !ok {
			t.Fatalf("counters must distinguish %q: %v", key, counters)
		}
	}
	if api["counter_scope"] == nil {
		t.Fatal("process-local counters must say that they are process-local")
	}

	core, ok := scopes["core"].(map[string]any)
	if !ok {
		t.Fatal("the core scope must be present")
	}
	if core["available"] != false {
		t.Fatalf("core has no readable policy; reporting it as available would be a lie: %v", core)
	}
	if _, invented := core["counters"]; invented {
		t.Fatal("an unreadable scope must not carry invented counters")
	}
	if reason, _ := core["reason"].(string); reason == "" {
		t.Fatal("an unavailable scope must explain why")
	}
}

func TestLogPolicy_WithoutAPolicySaysSoRatherThanShowingDefaults(t *testing.T) {
	h, _ := newPolicyHandlers(nil)
	rec := httptest.NewRecorder()
	h.LogPolicy(rec, adminRequest("GET", "/logs/policy", ""))

	body := decode(t, rec)
	scopes := body["scopes"].(map[string]any)
	api := scopes["api"].(map[string]any)
	if api["available"] != false {
		t.Fatal("an instance with no policy must not report effective levels it is not enforcing")
	}
}

func TestDiagnosticsStart_OpensABoundedWindowAndAuditsIt(t *testing.T) {
	policy := logpolicy.New(nil, 0)
	h, recorded := newPolicyHandlers(policy)

	rec := httptest.NewRecorder()
	h.LogDiagnosticsStart(rec, adminRequest("POST", "/logs/diagnostics/start", `{"seconds": 120}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("a valid start must succeed, got %d (%s)", rec.Code, rec.Body.String())
	}
	body := decode(t, rec)
	if body["started"] != true {
		t.Fatalf("start must report success: %v", body)
	}
	if body["seconds"].(float64) != 120 {
		t.Fatalf("the window must honor the requested duration, got %v", body["seconds"])
	}
	if expiry, _ := body["expires_at"].(float64); expiry <= float64(time.Now().Unix()) {
		t.Fatalf("expires_at must be in the future, got %v", body["expires_at"])
	}
	if !policy.DiagnosticActive() {
		t.Fatal("the policy must actually be in diagnostic mode")
	}
	if !policy.Allow("DEBUG", "http.request", "") {
		t.Fatal("an open window must collect a verbose level")
	}

	if len(*recorded) != 1 {
		t.Fatalf("the change must be audited, got %d records", len(*recorded))
	}
	audit := (*recorded)[0]
	if audit["actor"] != int64(5094837833) {
		t.Fatalf("the audit record must name the actor, got %v", audit["actor"])
	}
	if audit["action"] != "logging.diagnostics.start" {
		t.Fatalf("the audit record must name the action, got %v", audit["action"])
	}
}

func TestDiagnosticsStart_UsesTheDefaultWhenNoDurationIsGiven(t *testing.T) {
	h, _ := newPolicyHandlers(logpolicy.New(nil, 0))
	rec := httptest.NewRecorder()
	h.LogDiagnosticsStart(rec, adminRequest("POST", "/logs/diagnostics/start", ""))
	if rec.Code != http.StatusOK {
		t.Fatalf("a body-less start must use the default, got %d", rec.Code)
	}
	if got := decode(t, rec)["seconds"].(float64); int(got) != DefaultDiagnosticSeconds {
		t.Fatalf("default window = %v, want %d", got, DefaultDiagnosticSeconds)
	}
}

func TestDiagnosticsStart_RejectsOutOfRangeDurations(t *testing.T) {
	for _, body := range []string{`{"seconds": 0}`, `{"seconds": -1}`, `{"seconds": 999999}`, `{"seconds": 3601}`} {
		h, recorded := newPolicyHandlers(logpolicy.New(nil, 0))
		rec := httptest.NewRecorder()
		h.LogDiagnosticsStart(rec, adminRequest("POST", "/logs/diagnostics/start", body))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body %s must be refused with 400, got %d", body, rec.Code)
		}
		if h.Policy.DiagnosticActive() {
			t.Fatalf("body %s must not have opened a window", body)
		}
		if len(*recorded) != 0 {
			t.Fatalf("body %s must not be audited as a successful change", body)
		}
		// A useful error, not a bare status: the caller has to learn the bound.
		if decode(t, rec)["max_seconds"] == nil {
			t.Fatalf("body %s must report the accepted range", body)
		}
	}
}

func TestDiagnosticsStart_RefusesGarbageBody(t *testing.T) {
	h, _ := newPolicyHandlers(logpolicy.New(nil, 0))
	rec := httptest.NewRecorder()
	h.LogDiagnosticsStart(rec, adminRequest("POST", "/logs/diagnostics/start", `{"seconds": "ten minutes"}`))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("a non-numeric duration must be refused, got %d", rec.Code)
	}
}

func TestDiagnosticsStart_RepeatedPressesCannotHoldTheWindowOpen(t *testing.T) {
	policy := logpolicy.New(nil, 0)
	h, recorded := newPolicyHandlers(policy)

	first := httptest.NewRecorder()
	h.LogDiagnosticsStart(first, adminRequest("POST", "/logs/diagnostics/start", `{"seconds": 60}`))
	if first.Code != http.StatusOK {
		t.Fatalf("the first start must succeed, got %d", first.Code)
	}

	second := httptest.NewRecorder()
	h.LogDiagnosticsStart(second, adminRequest("POST", "/logs/diagnostics/start", `{"seconds": 3600}`))
	if second.Code != http.StatusConflict {
		t.Fatalf("a second press inside the guard must be refused with 409, got %d", second.Code)
	}
	if second.Header().Get("Retry-After") == "" {
		t.Fatal("a conflict must tell the client when to try again")
	}
	if policy.Snapshot().DiagnosticActivation != 1 {
		t.Fatalf("a refused press must not count as an activation: %+v", policy.Snapshot().Counters)
	}
	if len(*recorded) != 1 {
		t.Fatalf("only the accepted change may be audited, got %d", len(*recorded))
	}
}

func TestDiagnosticsStart_SaysWhenItReplacedAnActiveWindow(t *testing.T) {
	policy := logpolicy.New(nil, 0)
	h, _ := newPolicyHandlers(policy)

	// Open a window directly (bypassing the guard) as an operator's earlier
	// session would have.
	policy.EnableDiagnostics(60)
	rec := httptest.NewRecorder()
	h.LogDiagnosticsStart(rec, adminRequest("POST", "/logs/diagnostics/start", `{"seconds": 90}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("start must succeed, got %d", rec.Code)
	}
	if decode(t, rec)["replaced_active"] != true {
		t.Fatal("the response must disclose that an active window was replaced, not silently extend it")
	}
	if policy.Snapshot().DiagnosticActivation != 2 {
		t.Fatal("a replacing start is still an activation")
	}
}

func TestDiagnosticsStop_IsIdempotentButHonest(t *testing.T) {
	policy := logpolicy.New(nil, 0)
	h, recorded := newPolicyHandlers(policy)
	policy.EnableDiagnostics(60)

	rec := httptest.NewRecorder()
	h.LogDiagnosticsStop(rec, adminRequest("POST", "/logs/diagnostics/stop", ""))
	if rec.Code != http.StatusOK {
		t.Fatalf("stop must succeed, got %d", rec.Code)
	}
	if decode(t, rec)["was_active"] != true {
		t.Fatal("stop must report that a window was actually open")
	}
	if policy.DiagnosticActive() {
		t.Fatal("stop must close the window")
	}
	if !policy.Allow("ERROR", "forward.dropped", "SEND_FAILED") {
		t.Fatal("closing diagnostics must not disable ordinary error collection")
	}

	// A retrying client repeating the stop must not see a confusing failure, but
	// the response must not claim it stopped something.
	again := httptest.NewRecorder()
	h.LogDiagnosticsStop(again, adminRequest("POST", "/logs/diagnostics/stop", ""))
	if again.Code != http.StatusOK {
		t.Fatalf("a redundant stop must not fail, got %d", again.Code)
	}
	if decode(t, again)["was_active"] != false {
		t.Fatal("a redundant stop must report that nothing was active")
	}
	if len(*recorded) != 2 {
		t.Fatalf("both stop attempts are auditable events about the control plane, got %d", len(*recorded))
	}
}

func TestDiagnosticsMutations_RefuseAnInstanceWithoutAPolicy(t *testing.T) {
	h, _ := newPolicyHandlers(nil)
	for _, call := range []func(http.ResponseWriter, *http.Request){h.LogDiagnosticsStart, h.LogDiagnosticsStop} {
		rec := httptest.NewRecorder()
		call(rec, adminRequest("POST", "/logs/diagnostics/start", `{"seconds": 60}`))
		if rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("a mutation with no policy must be refused explicitly, got %d", rec.Code)
		}
	}
}

func TestLoggingHealth_ReportsUnknownRatherThanHealthyForWhatItCannotSee(t *testing.T) {
	h, _ := newPolicyHandlers(logpolicy.New(nil, 0))
	rec := httptest.NewRecorder()
	h.LoggingHealth(rec, adminRequest("GET", "/logs/health", ""))

	body := decode(t, rec)
	services, _ := body["services"].([]any)
	if len(services) == 0 {
		t.Fatalf("health must list the logging chain: %v", body)
	}
	var sawCore bool
	for _, raw := range services {
		svc := raw.(map[string]any)
		if svc["service"] == "atf-core" {
			sawCore = true
			// Core has no logging telemetry yet. Reporting it "healthy" would be
			// the same invention as reporting zero suppressed events.
			if svc["status"] != "unknown" {
				t.Fatalf("core's logging health is unknown, got %v", svc["status"])
			}
		}
		if _, ok := svc["observed_at"]; !ok {
			t.Fatalf("every entry needs freshness information: %v", svc)
		}
	}
	if !sawCore {
		t.Fatal("core must appear in the logging chain, even as unknown")
	}
}
