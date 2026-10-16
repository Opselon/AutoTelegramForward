package middleware

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"google.golang.org/grpc"

	"autoforward/internal/logpolicy"
	pb "autoforward/proto"
)

// These tests pin the Go producer-side collection policy at the boundary that
// actually emits: the two middleware loggers. The API had TWO emission paths, and
// a policy applied to only one of them is not a policy (the other one keeps
// writing the rows the contract says must not be collected), so both are tested
// here rather than trusting that they were changed together.

// failingLogsClient would surface a transport failure if it were ever called. It
// exists so a policy refusal can be proven to be silent: a refusal that still
// attempted the RPC would show up as calls > 0 and a logged transport failure.
type failingLogsClient struct {
	*recordingLogsClient
}

func (c *failingLogsClient) Log(_ context.Context, in *pb.LogRequest, _ ...grpc.CallOption) (*pb.StatusResponse, error) {
	c.recordingLogsClient.Log(context.Background(), in)
	return nil, context.DeadlineExceeded
}

// blockingLogsClient never answers, standing in for an unavailable Logger.
type blockingLogsClient struct {
	*recordingLogsClient
	release chan struct{}
}

func (c *blockingLogsClient) Log(_ context.Context, in *pb.LogRequest, _ ...grpc.CallOption) (*pb.StatusResponse, error) {
	<-c.release
	return &pb.StatusResponse{Success: true}, nil
}

func defaultPolicy() *logpolicy.Policy { return logpolicy.New(nil, 0) }

func raisedPolicy() *logpolicy.Policy { return logpolicy.New([]string{"CRITICAL"}, 0) }

func okHandler(status int) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status) })
}

// --- the requirement the whole workstream exists for ------------------------- //

func TestAccessLogger_SuppressesOrdinaryInfoUnderTheDefaultPolicy(t *testing.T) {
	client := newRecordingLogsClient()
	policy := defaultPolicy()
	h := AccessLogger(&ErrorLogSink{Client: client, Policy: policy}, okHandler(http.StatusOK))

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/rules", nil))
	time.Sleep(50 * time.Millisecond)

	if n := client.count(); n != 0 {
		t.Fatalf("a successful ordinary request must not be collected by default, got %d entries", n)
	}
	snapshot := policy.Snapshot()
	if snapshot.SuppressedLevel != 1 {
		t.Fatalf("the refusal must be visible as a suppression, got %+v", snapshot.Counters)
	}
	if snapshot.Emitted != 0 {
		t.Fatalf("nothing should have been emitted, got %+v", snapshot.Counters)
	}
}

func TestAccessLogger_KeepsWarnErrorAndCritical(t *testing.T) {
	for _, tc := range []struct {
		status int
		level  string
	}{
		{http.StatusNotFound, "WARN"},
		{http.StatusBadRequest, "WARN"},
		{http.StatusInternalServerError, "ERROR"},
		{http.StatusBadGateway, "ERROR"},
	} {
		client := newRecordingLogsClient()
		h := AccessLogger(&ErrorLogSink{Client: client, Policy: defaultPolicy()}, okHandler(tc.status))
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/rules", nil))
		if got := client.wait(t).Level; got != tc.level {
			t.Fatalf("status %d must still be collected at %s, got %s", tc.status, tc.level, got)
		}
	}
}

// --- security evidence survives the threshold, the namespace does not ------- //

func TestAccessLogger_KeepsSecurityEvidenceBelowTheThreshold(t *testing.T) {
	// A raised threshold must not disarm auditing: an authorization denial is
	// WARN here, and WARN is *not* in this policy's set.
	for _, tc := range []struct {
		name   string
		method string
		path   string
		status int
	}{
		{"unauthorized", "GET", "/api/auth/me", http.StatusUnauthorized},
		{"forbidden", "GET", "/api/tenants/42/rules", http.StatusForbidden},
		{"admin change", "PUT", "/api/admin/settings", http.StatusOK},
		{"admin change on a nested route", "DELETE", "/api/admin/users/7", http.StatusOK},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := newRecordingLogsClient()
			policy := raisedPolicy()
			h := AccessLogger(&ErrorLogSink{Client: client, Policy: policy}, okHandler(tc.status))

			h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(tc.method, tc.path, nil))

			entry := client.wait(t)
			if entry.Level != "INFO" && entry.Level != "WARN" {
				t.Fatalf("unexpected level %q: the point is that it passed the CRITICAL threshold", entry.Level)
			}
			if got := policy.Snapshot().EmittedSecurity; got != 1 {
				t.Fatalf("the event must be counted as a security emission, got %d", got)
			}
		})
	}
}

func TestAccessLogger_DoesNotBlanketExemptTheAuthNamespace(t *testing.T) {
	// The exemption is about evidence, not about a URL. A successful read on the
	// auth routes is ordinary traffic and must stay out of the default store,
	// otherwise "exempt auth.*" would quietly rebuild the original volume problem.
	client := newRecordingLogsClient()
	h := AccessLogger(&ErrorLogSink{Client: client, Policy: defaultPolicy()}, okHandler(http.StatusOK))

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/auth/me", nil))
	time.Sleep(50 * time.Millisecond)

	if n := client.count(); n != 0 {
		t.Fatalf("a successful auth read is not security evidence, got %d entries", n)
	}
}

// --- suppression is not a loss --------------------------------------------- //

func TestPolicyRefusalIsNotATransportFailure(t *testing.T) {
	client := &failingLogsClient{recordingLogsClient: newRecordingLogsClient()}
	policy := defaultPolicy()
	h := AccessLogger(&ErrorLogSink{Client: client, Policy: policy}, okHandler(http.StatusOK))

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/rules", nil))
	time.Sleep(50 * time.Millisecond)

	if n := client.count(); n != 0 {
		t.Fatalf("a refused event must not reach the transport at all, got %d attempts", n)
	}
	snapshot := policy.Snapshot()
	if snapshot.SuppressedLevel != 1 || snapshot.Emitted != 0 {
		t.Fatalf("a refusal is counted as suppression only: %+v", snapshot.Counters)
	}
	// The suppression counters are the policy's own. There is deliberately no
	// failing/retrying state here to confuse them with: no RPC was attempted, so
	// no transport counter could have moved even if one existed.
	if policy.Allow("ERROR", "forward.dropped", "SEND_FAILED") != true {
		t.Fatal("the policy must still collect real failures")
	}
	if got := snapshot.SuppressedLevel; got != 1 {
		t.Fatalf("a real failure must not be counted as suppression, got %d", got)
	}
}

func TestDurableEvidenceSurvivesDisabledDiagnosticCollection(t *testing.T) {
	// Ordinary diagnostics are off (INFO suppressed); the audit-worthy event is
	// still persisted. "We stopped collecting chatter" must never mean "we
	// stopped keeping evidence".
	client := newRecordingLogsClient()
	policy := defaultPolicy()
	h := AccessLogger(&ErrorLogSink{Client: client, Policy: policy}, http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusUnauthorized) }))

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("POST", "/api/auth/login", nil))

	entry := client.wait(t)
	if entry.Level != "WARN" {
		t.Fatalf("the login failure must be recorded, got %q", entry.Level)
	}
	if policy.Snapshot().EmittedSecurity != 1 {
		t.Fatal("the recorded failure must be attributed as security evidence")
	}
}

// --- the other emission path ------------------------------------------------ //

func TestErrorLoggerAlsoHonoursThePolicy(t *testing.T) {
	// ErrorLogger is the narrower legacy path. If it bypassed the policy, a
	// deployment that still wires it would keep emitting whatever it liked.
	client := newRecordingLogsClient()
	policy := raisedPolicy()
	h := ErrorLogger(&ErrorLogSink{Client: client, Policy: policy}, okHandler(http.StatusInternalServerError))

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/rules", nil))
	time.Sleep(50 * time.Millisecond)

	if n := client.count(); n != 0 {
		t.Fatalf("an ERROR below a CRITICAL threshold must be refused on this path too, got %d entries", n)
	}
	if policy.Snapshot().SuppressedLevel != 1 {
		t.Fatal("the refusal must be visible in the policy counters")

	}

	// The same path must still let security evidence through.
	securityClient := newRecordingLogsClient()
	securityPolicy := raisedPolicy()
	securityHandler := ErrorLogger(&ErrorLogSink{Client: securityClient, Policy: securityPolicy}, okHandler(http.StatusForbidden))
	securityHandler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/rules", nil))
	if got := securityClient.wait(t).Level; got != "WARN" {
		t.Fatalf("a forbidden response must be kept, got %q", got)
	}
}

func TestNilPolicyPreservesUnfilteredBehaviour(t *testing.T) {
	// The middleware is used standalone (and by tests) without a policy; that must
	// keep working exactly as before rather than silently dropping everything.
	client := newRecordingLogsClient()
	h := AccessLogger(&ErrorLogSink{Client: client}, okHandler(http.StatusOK))

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/rules", nil))
	if got := client.wait(t).Level; got != "INFO" {
		t.Fatalf("without a policy nothing may be filtered, got %q", got)
	}
}

// --- the API must never wait on the logger ---------------------------------- //

func TestRequestPathStaysResponsiveWhileTheLoggerIsUnavailable(t *testing.T) {
	client := &blockingLogsClient{
		recordingLogsClient: newRecordingLogsClient(),
		release:             make(chan struct{}),
	}
	defer close(client.release)

	h := AccessLogger(&ErrorLogSink{Client: client, Policy: defaultPolicy()}, okHandler(http.StatusInternalServerError))

	done := make(chan int, 1)
	go func() {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/rules", nil))
		done <- rec.Code
	}()

	select {
	case code := <-done:
		if code != http.StatusInternalServerError {
			t.Fatalf("the response must be the handler's own, got %d", code)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("the request path must not block on the logging transport")
	}
}

// --- classification is structural, never text-based ------------------------- //

func TestClassifyOutcomeMapping(t *testing.T) {
	for _, tc := range []struct {
		method, path string
		status       int
		eventType    string
		reasonCode   string
	}{
		{"GET", "/api/rules", 200, "http.request", ""},
		{"GET", "/api/rules", 401, "http.request", "UNAUTHORIZED"},
		{"POST", "/api/auth/login", 401, "auth.failure", "UNAUTHORIZED"},
		{"GET", "/api/auth/me", 200, "http.request", ""},
		{"PUT", "/api/admin/policy", 200, "admin.change", ""},
		{"GET", "/api/admin/users", 200, "http.request", ""},
		{"GET", "/api/admin/users", 403, "admin.change", "FORBIDDEN"},
		{"GET", "/api/tenants/9/rules", 403, "tenant.access", "FORBIDDEN"},
		{"GET", "/api/tenants/9/rules", 200, "http.request", ""},
	} {
		req := httptest.NewRequest(tc.method, tc.path, nil)
		eventType, reasonCode := classifyOutcome(req, tc.status)
		if eventType != tc.eventType || reasonCode != tc.reasonCode {
			t.Errorf("%s %s -> %d classified as (%s, %s), want (%s, %s)",
				tc.method, tc.path, tc.status, eventType, reasonCode, tc.eventType, tc.reasonCode)
		}
	}
}

func TestClassifiedEventsSatisfyTheSharedPolicyContract(t *testing.T) {
	// Tie the middleware's own classification to the shared fixture semantics:
	// whatever it produces must be judged the same way both producers judge it.
	policy := logpolicy.New(nil, 0)
	eventType, reasonCode := classifyOutcome(httptest.NewRequest("POST", "/api/admin/settings", nil), http.StatusOK)
	if !policy.Allow("INFO", eventType, reasonCode) {
		t.Fatalf("an admin change (%s/%s) must pass the default policy", eventType, reasonCode)
	}
	eventType, reasonCode = classifyOutcome(httptest.NewRequest("GET", "/api/rules", nil), http.StatusOK)
	if policy.Allow("INFO", eventType, reasonCode) {
		t.Fatalf("ordinary traffic (%s/%s) must not pass the default policy", eventType, reasonCode)
	}
}
