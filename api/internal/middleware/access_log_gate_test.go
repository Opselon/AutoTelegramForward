package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// A refused API key must leave evidence in the central logger.
//
// The gate is composed inside AccessLogger in main.go so that an
// unauthenticated request is still recorded. Wrapped the other way round, the
// request is answered before any audited middleware runs: the access trail has
// no entry, and RequestLogger (which also sits inside the gate) prints nothing,
// so a probe storm against the API is invisible in every signal an operator has.
// This test pins the composition, not just the gate.
func TestAccessLogger_RecordsAPIKeyRejection(t *testing.T) {
	client := newRecordingLogsClient()
	inner := http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("a request with a wrong API key must never reach the handler")
	})
	// Exactly the chain main.go builds: AccessLogger outside APIKeyAuth.
	h := AccessLogger(&ErrorLogSink{Client: client}, APIKeyAuth("the-right-key", inner))

	req := httptest.NewRequest("GET", "/api/system/health", nil)
	req.Header.Set("X-API-Key", "the-wrong-key")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("a wrong key must be refused with 401, got %d", rec.Code)
	}
	entry := client.wait(t)
	if entry.Level != "WARN" {
		t.Fatalf("a refused key is a 4xx: must be a WARN, got %q", entry.Level)
	}
	if entry.Message != "Unauthorized GET /api/system/health" {
		t.Fatalf("the entry must name the refused action, got %q", entry.Message)
	}
	if entry.Service != "api" || entry.Category != "http" {
		t.Fatalf("the refusal must join the API access trail: %+v", entry)
	}
	if entry.Detail == "" {
		t.Fatal("the refusal must carry its request detail")
	}
}

// A request that never gets past the gate still must not be attributed to a
// user, and the code path must stay free of a second emission.
func TestAccessLogger_RecordsOneEntryPerRejectedRequest(t *testing.T) {
	client := newRecordingLogsClient()
	inner := http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("the handler must not run")
	})
	h := AccessLogger(&ErrorLogSink{Client: client}, APIKeyAuth("the-right-key", inner))

	req := httptest.NewRequest("POST", "/api/rules", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	entry := client.wait(t)
	if entry.UserId != 0 {
		t.Fatalf("an unauthenticated refusal has no actor, got %d", entry.UserId)
	}
	if got := client.count(); got != 1 {
		t.Fatalf("one refused request must produce exactly one entry, got %d", got)
	}
}

// The shipped contract describes the API to the world: it must never sit
// behind the API-key gate (the gate guardrails data routes, not the "how do I
// even authenticate" document). Regression for the 401 the operator saw on a
// live deployment with ATF_API_KEY configured.
func TestAPIKeyAuthallowsTheOpenAPIContract(t *testing.T) {
	rec := httptest.NewRecorder()
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("openapi: 3.1.0"))
	})
	APIKeyAuth("the-right-key", inner).ServeHTTP(rec, httptest.NewRequest("GET", "/api/openapi.yaml", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("openapi must pass the key gate without a key, got %d: %s", rec.Code, rec.Body.String())
	}
	// But it must NOT become a data-route exception: a task or rules path still refuses.
	rec2 := httptest.NewRecorder()
	APIKeyAuth("the-right-key", inner).ServeHTTP(rec2, httptest.NewRequest("GET", "/api/rules", nil))
	if rec2.Code != http.StatusUnauthorized {
		t.Fatalf("data route without key must be refused 401, got %d", rec2.Code)
	}
}
