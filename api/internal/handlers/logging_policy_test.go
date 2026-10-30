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

	"autoforward/internal/grpcclient"
	"autoforward/internal/logpolicy"
	pb "autoforward/proto"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
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

// fakeCorePolicyClient stands in for atf-core's SystemStatusControlService. It
// answers GetLogPolicy (or fails) so the control plane's behaviour can be pinned
// without a live core: what matters is that the API reports what core said,
// including when core says nothing readable.
type fakeCorePolicyClient struct {
	pb.SystemStatusControlServiceClient
	resp *pb.LogPolicyResponse
	err  error
}

func (f *fakeCorePolicyClient) GetLogPolicy(
	ctx context.Context, in *pb.LogPolicyRequest, opts ...grpc.CallOption,
) (*pb.LogPolicyResponse, error) {
	if f.err != nil {
		return nil, f.err
	}
	return f.resp, nil
}

// withCore attaches a core client to otherwise policy-only handlers.
func withCore(h *Handlers, system pb.SystemStatusControlServiceClient) *Handlers {
	h.c = &grpcclient.Clients{System: system}
	return h
}

func TestLogPolicy_ReportsCorePolicyReadFromTheCoreProcess(t *testing.T) {
	policy := logpolicy.New(nil, 0)
	// Core reports lower-case levels (its own configured/effective sets) plus a
	// live diagnostic window, so the API's rendering of them is what is pinned.
	expires := time.Now().Add(3 * time.Minute).Unix()
	h, _ := newPolicyHandlers(policy)
	withCore(h, &fakeCorePolicyClient{resp: &pb.LogPolicyResponse{
		Available:             true,
		ConfiguredLevels:      []string{"ERROR", "WARN", "CRITICAL"},
		EffectiveLevels:       []string{"WARN", "ERROR", "CRITICAL"},
		DiagnosticActive:      true,
		DiagnosticUntil:       expires,
		VerboseRatePerSecond:  20,
		Emitted:               41,
		EmittedSecurity:       3,
		SuppressedLevel:       7,
		SuppressedRate:        2,
		DiagnosticActivations: 1,
	}})

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

	// The core scope must be what core answered — not defaults the API assumed.
	core, ok := scopes["core"].(map[string]any)
	if !ok {
		t.Fatal("the core scope must be present")
	}
	if core["available"] != true {
		t.Fatalf("a core that answered must be reported as available: %v", core)
	}
	coreLevels, _ := core["effective_levels"].([]any)
	if len(coreLevels) != 3 || coreLevels[0] != "WARN" {
		t.Fatalf("core's effective levels must be the ones core reported, got %v", core["effective_levels"])
	}
	coreCounters, _ := core["counters"].(map[string]any)
	if coreCounters == nil {
		t.Fatalf("core's counters must be reported when core reports them: %v", core)
	}
	for key, want := range map[string]float64{
		"emitted": 41, "emitted_security": 3, "suppressed_level": 7,
		"suppressed_rate": 2, "diagnostic_activations": 1,
	} {
		if got, _ := coreCounters[key].(float64); got != want {
			t.Fatalf("core counter %q = %v, want %v (%v)", key, coreCounters[key], want, coreCounters)
		}
	}
	if core["diagnostic_active"] != true {
		t.Fatal("an open core diagnostic window must be visible here")
	}
	if remaining, _ := core["diagnostic_seconds_remaining"].(float64); remaining <= 0 || remaining > 180 {
		t.Fatalf("the remaining window must be derived from core's expiry, got %v", core["diagnostic_seconds_remaining"])
	}
	// A core counter is process-local: presenting it as a system total would let
	// an operator believe suppression is over when another process is doing it.
	if scope, _ := core["counter_scope"].(string); !strings.Contains(scope, "atf-core") {
		t.Fatalf("core counters must be labelled process-local to core, got %q", scope)
	}
}

// The regression pin for this feature: before GetLogPolicy existed the core scope
// was hard-coded unavailable, and the tempting "fix" is to render zeros instead.
// A zero reads as "nothing was suppressed", so every unreadable state must stay
// unavailable, name the reason, and carry no counters or levels at all.
func TestLogPolicy_UnreadableCoreScopeIsNeverZeroFilled(t *testing.T) {
	cases := []struct {
		name       string
		system     pb.SystemStatusControlServiceClient
		wantReason string
	}{
		{
			name:       "no core client configured",
			system:     nil,
			wantReason: "client",
		},
		{
			name:       "core unreachable",
			system:     &fakeCorePolicyClient{err: status.Error(codes.Unavailable, "connection refused")},
			wantReason: "failed",
		},
		{
			name:       "core answers without a policy",
			system:     &fakeCorePolicyClient{resp: &pb.LogPolicyResponse{Available: false, Reason: "no collection policy attached to this core process"}},
			wantReason: "no collection policy attached",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h, _ := newPolicyHandlers(logpolicy.New(nil, 0))
			if tc.system != nil {
				withCore(h, tc.system)
			}
			rec := httptest.NewRecorder()
			h.LogPolicy(rec, adminRequest("GET", "/logs/policy", ""))
			if rec.Code != http.StatusOK {
				t.Fatalf("an unreadable scope is still a successful read, got %d", rec.Code)
			}
			core := decode(t, rec)["scopes"].(map[string]any)["core"].(map[string]any)
			if core["available"] != false {
				t.Fatalf("an unreadable core scope must not claim availability: %v", core)
			}
			reason, _ := core["reason"].(string)
			if !strings.Contains(reason, tc.wantReason) {
				t.Fatalf("reason %q must explain the situation (want %q)", reason, tc.wantReason)
			}
			if _, invented := core["counters"]; invented {
				t.Fatal("an unreadable scope must not carry invented counters")
			}
			if _, invented := core["effective_levels"]; invented {
				t.Fatal("an unreadable scope must not carry levels it did not read")
			}
		})
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

func TestLoggingHealth_ReportsCoreFromCoreItself(t *testing.T) {
	h, _ := newPolicyHandlers(logpolicy.New(nil, 0))
	withCore(h, &fakeCorePolicyClient{resp: &pb.LogPolicyResponse{
		Available:       true,
		EffectiveLevels: []string{"WARN", "ERROR", "CRITICAL"},
		Emitted:         17,
		SuppressedLevel: 4,
	}})
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
			// Core's policy is readable now, so "unknown" would hide a real
			// answer and "healthy" without evidence would be an invention.
			if svc["status"] != "healthy" {
				t.Fatalf("a core that answered its policy is healthy, got %v (%v)", svc["status"], svc)
			}
			counters, _ := svc["counters"].(map[string]any)
			if counters == nil {
				t.Fatalf("core's counters must be surfaced when readable: %v", svc)
			}
			if got, _ := counters["emitted"].(float64); got != 17 {
				t.Fatalf("core's emitted count must be core's own, got %v", counters["emitted"])
			}
			if scope, _ := svc["counter_scope"].(string); !strings.Contains(scope, "atf-core") {
				t.Fatalf("core counters must be labelled process-local, got %q", scope)
			}
		}
		if _, ok := svc["observed_at"]; !ok {
			t.Fatalf("every entry needs freshness information: %v", svc)
		}
	}
	if !sawCore {
		t.Fatal("core must appear in the logging chain")
	}
}

// An unreachable core must degrade the chain rather than being omitted or shown
// as healthy: the point of this endpoint is telling "quiet" from "not being fed".
func TestLoggingHealth_DegradesWhenCoreCannotAnswer(t *testing.T) {
	h, _ := newPolicyHandlers(logpolicy.New(nil, 0))
	withCore(h, &fakeCorePolicyClient{err: status.Error(codes.Unavailable, "connection refused")})
	rec := httptest.NewRecorder()
	h.LoggingHealth(rec, adminRequest("GET", "/logs/health", ""))

	body := decode(t, rec)
	if body["status"] != "degraded" {
		t.Fatalf("a core that cannot answer must degrade the chain, got %v", body["status"])
	}
	for _, raw := range body["services"].([]any) {
		svc := raw.(map[string]any)
		if svc["service"] != "atf-core" {
			continue
		}
		if svc["status"] != "unavailable" {
			t.Fatalf("an unreachable core is unavailable, got %v", svc["status"])
		}
		if detail, _ := svc["detail"].(string); !strings.Contains(detail, "failed") {
			t.Fatalf("the failure must be named, got %q", detail)
		}
		return
	}
	t.Fatal("core must appear in the logging chain")
}

// Issue #102: a producer policy that admits INFO while the engine refuses it
// read to the operator as "collected", which is not "queryable". The answer now
// carries the engine's own gate next to the producer's, so the disagreement is
// visible in the one place the operator looks.
func TestLogPolicy_ReportsTheEngineAdmissionGateNextToTheProducer(t *testing.T) {
	policy := logpolicy.New(nil, 0)
	h, _ := newPolicyHandlers(policy)
	h.c = &grpcclient.Clients{
		Logs: &fakeEngineAdmissionClient{resp: &pb.LogStatsResponse{
			Total: 660,
			Admission: &pb.AdmissionPolicy{
				CollectLevels:  []string{"WARN", "ERROR", "CRITICAL"},
				OmittedLevels:  []string{"TRACE", "DEBUG", "INFO", "SUCCESS"},
				ConfiguredBy:   "ATF_LOGGER_COLLECT_LEVELS",
				FilteredTotal:  3,
				FilteredByLevel: map[string]int64{"INFO": 3},
				CollectUnknown: false,
			},
		}},
	}

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
	engine, ok := scopes["engine"].(map[string]any)
	if !ok {
		t.Fatal("the engine scope must be present: its admission gate is the half the producer's answer cannot show")
	}
	if engine["available"] != true {
		t.Fatalf("an engine that answered must be reported as available: %v", engine)
	}
	collect, _ := engine["configured_levels"].([]any)
	if len(collect) != 3 || collect[0] != "WARN" {
		t.Fatalf("the engine's collected levels must be its own: %v", engine["configured_levels"])
	}
	omitted, _ := engine["effective_levels"].([]any)
	if len(omitted) == 0 || omitted[len(omitted)-1] != "SUCCESS" {
		t.Fatalf("the engine's omitted levels must be reported as the effective set it will not store: %v", engine["effective_levels"])
	}
	counters, _ := engine["counters"].(map[string]any)
	if got, _ := counters["suppressed_level"].(float64); got != 3 {
		t.Fatalf("engine refusals must be counted: got %v, want 3 (%v)", counters["suppressed_level"], counters)
	}
	if scope, _ := engine["counter_scope"].(string); !strings.Contains(scope, "engine") {
		t.Fatalf("engine counters must be labelled as the engine's, got %q", scope)
	}
}

// An engine that predates the admission field must say so, not answer a scope
// that looks like "the engine admits everything".
func TestLogPolicy_AnEngineWithoutAnAdmissionReportSaysSo(t *testing.T) {
	policy := logpolicy.New(nil, 0)
	h, _ := newPolicyHandlers(policy)
	h.c = &grpcclient.Clients{Logs: &fakeEngineAdmissionClient{resp: &pb.LogStatsResponse{Total: 10}}}

	rec := httptest.NewRecorder()
	h.LogPolicy(rec, adminRequest("GET", "/logs/policy", ""))
	body := decode(t, rec)
	scopes, _ := body["scopes"].(map[string]any)
	engine, _ := scopes["engine"].(map[string]any)
	if engine["available"] != false {
		t.Fatalf("an engine without an admission report is not 'available': %v", engine)
	}
	if reason, _ := engine["reason"].(string); !strings.Contains(reason, "admission") {
		t.Fatalf("the reason must name the missing admission report, got %q", reason)
	}
	if _, hasCounters := engine["counters"]; hasCounters {
		t.Fatalf("an unavailable engine scope must carry no counters: %v", engine)
	}
}

// fakeEngineAdmissionClient stands in for the logger's LogControlService: the
// engine reports its own admission policy on LogStats, so that is the call the
// handler has to make.
type fakeEngineAdmissionClient struct {
	pb.LogControlServiceClient
	resp *pb.LogStatsResponse
	err  error
}

func (f *fakeEngineAdmissionClient) LogStats(
	ctx context.Context, in *pb.LogStatsRequest, opts ...grpc.CallOption,
) (*pb.LogStatsResponse, error) {
	if f.err != nil {
		return nil, f.err
	}
	return f.resp, nil
}
