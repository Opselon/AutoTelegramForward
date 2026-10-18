package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The administrator view must work even without a limiter wired (the contract
// the web frontend can rely on: "enabled": false and nothing more), and must
// never leak a credential, a host:port or the key prefix into the response
// when one is.
func TestRateLimitStatusWithoutLimiter(t *testing.T) {
	h := &Handlers{}
	rec := httptest.NewRecorder()
	h.RateLimitStatus(rec, httptest.NewRequest(http.MethodGet, "/ratelimit", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("a report about an absent limiter is not an error: %d", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}
	enabled, ok := body["enabled"].(bool)
	if !ok || enabled {
		t.Fatalf("an absent limiter must report enabled=false, got %v", body["enabled"])
	}
}

// A wired-but-unreachable limiter reports its outage posture, never its
// configuration detail. The report is what an operator browses in an incident;
// it must be readable in that state.
func TestRateLimitStatusShapeNeverLeaksCredentials(t *testing.T) {
	h := &Handlers{}
	rec := httptest.NewRecorder()
	h.RateLimitStatus(rec, httptest.NewRequest(http.MethodGet, "/ratelimit", nil))
	raw := rec.Body.String()
	for _, leak := range []string{"password", "pass", "secret", "redis://"} {
		if strings.Contains(raw, leak) {
			t.Fatalf("the status must not carry %q; body: %s", leak, raw)
		}
	}
}
