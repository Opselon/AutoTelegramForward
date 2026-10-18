package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"autoforward/internal/ratelimit"

	"github.com/alicebob/miniredis/v2"
)

// Readiness answers the question a deployment actually needs answered: can every
// fail-closed dependency enforce what this stack promises?
//
// The defect these tests pin was observed live. Compose substituted an EMPTY
// ATF_REDIS_PASSWORD (the deploy was pointed at an env file that did not carry
// it, while the redis service still required one). The API started, logged
// "ratelimit: enabled (redis:tb, algo=tb, addrs=redis:6379)", and then refused
// every token exchange with 503 rate_limit_unavailable — while GET /healthz
// answered 200 and `docker compose ps` showed five healthy containers. The deploy
// therefore finished with its "== healthy ==" banner, and the breakage stayed
// invisible until a user tried to log in.
//
// These tests drive a REAL Redis client. They use miniredis for the reachable
// case and, for the unready cases, the same miniredis closed (connection refused)
// or requiring a credential the client does not have — byte-for-byte the live
// failure. Nothing here mocks the code under test.

// limiterRuntime builds a real limiter runtime pointed at addr. A store that
// cannot answer is not a configuration error: NewRuntime keeps the authoritative
// limiter wired (that is what makes the classes fail closed), so this harness
// asserts the distinction the readiness check depends on — enabled but NOT
// reachable — rather than quietly degrading to per-process limits.
func limiterRuntime(t *testing.T, addr, password string) *ratelimit.Runtime {
	t.Helper()
	cfg := ratelimit.ConfigFromEnv(func(key string) string {
		switch key {
		case "ATF_REDIS_ADDR":
			return addr
		case "ATF_REDIS_PASSWORD":
			return password
		case "ATF_RATELIMIT_ENABLED":
			return "1"
		}
		return ""
	})
	rt, err := ratelimit.NewRuntime(context.Background(), cfg, t.Logf)
	if err != nil {
		t.Fatalf("NewRuntime: %v", err)
	}
	t.Cleanup(func() { _ = rt.Close() })
	if !rt.Enabled() {
		t.Fatal("the harness needs an ENABLED limiter: an unreachable store must stay authoritative, not fall back to per-process limits")
	}
	return rt
}

// readyResponse runs the handler and decodes its body.
func readyResponse(t *testing.T, h *Handlers) (int, map[string]any, string) {
	t.Helper()
	rec := httptest.NewRecorder()
	h.Ready(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	raw := rec.Body.String()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("the readiness body must be JSON: %v (body %s)", err, raw)
	}
	return rec.Code, body, raw
}

// failingCheck returns the reported check with the given name.
func failingCheck(t *testing.T, body map[string]any, name string) map[string]any {
	t.Helper()
	checks, ok := body["checks"].([]any)
	if !ok {
		t.Fatalf("the readiness body must carry a checks list; got %v", body)
	}
	for _, c := range checks {
		m, _ := c.(map[string]any)
		if m["name"] == name {
			return m
		}
	}
	t.Fatalf("no %q check in the readiness body: %v", name, body)
	return nil
}

func TestReady_HoldsWhenTheLimiterCanEnforce(t *testing.T) {
	mr := miniredis.RunT(t)
	h := &Handlers{RateLimit: limiterRuntime(t, mr.Addr(), "")}

	code, body, raw := readyResponse(t, h)

	if code != http.StatusOK {
		t.Fatalf("a limiter that answers must be ready: got %d, body %s", code, raw)
	}
	if ready, _ := body["ready"].(bool); !ready {
		t.Fatalf("ready must be true; body %s", raw)
	}
	if got, _ := failingCheck(t, body, "ratelimit")["ok"].(bool); !got {
		t.Fatalf("the ratelimit check must pass; body %s", raw)
	}
}

func TestReady_IsRefusedWhenTheStoreRequiresACredentialWeDoNotHave(t *testing.T) {
	mr := miniredis.RunT(t)
	const storePassword = "s3cret-store-credential"
	mr.RequireAuth(storePassword)
	// The live failure: the store requires a password, the client was given none.
	h := &Handlers{RateLimit: limiterRuntime(t, mr.Addr(), "")}

	code, body, raw := readyResponse(t, h)

	if code != http.StatusServiceUnavailable {
		t.Fatalf("a store that refuses our credentials cannot enforce a limit: want 503, got %d (body %s)", code, raw)
	}
	if ready, _ := body["ready"].(bool); ready {
		t.Fatalf("ready must be false; body %s", raw)
	}
	if got, _ := failingCheck(t, body, "ratelimit")["ok"].(bool); got {
		t.Fatalf("the ratelimit check must fail; body %s", raw)
	}
	if detail, _ := failingCheck(t, body, "ratelimit")["detail"].(string); detail == "" {
		t.Fatalf("an unready dependency must say why; body %s", raw)
	}
}

func TestReady_IsRefusedWhenTheStoreIsGone(t *testing.T) {
	mr := miniredis.RunT(t)
	addr := mr.Addr()
	mr.Close() // connection refused, as during a Redis outage
	h := &Handlers{RateLimit: limiterRuntime(t, addr, "")}

	code, body, raw := readyResponse(t, h)

	if code != http.StatusServiceUnavailable {
		t.Fatalf("a store that is gone cannot enforce a limit: want 503, got %d (body %s)", code, raw)
	}
	if ready, _ := body["ready"].(bool); ready {
		t.Fatalf("ready must be false; body %s", raw)
	}
}

// A deployment without a shared store is a supported mode: the in-process
// limiter is the whole limit, nothing is failing closed, and reporting it
// unready would refuse to deploy a working single-node install.
func TestReady_HoldsWithoutASharedStore(t *testing.T) {
	code, body, raw := readyResponse(t, &Handlers{})

	if code != http.StatusOK {
		t.Fatalf("per-process limits are not a defect: got %d, body %s", code, raw)
	}
	if ready, _ := body["ready"].(bool); !ready {
		t.Fatalf("ready must be true; body %s", raw)
	}
}

// The endpoint is unauthenticated (a deploy gate and a container probe both call
// it before any session exists), so it must never echo the credential, the
// store address or the key prefix.
func TestReady_NeverLeaksTheStoreCredential(t *testing.T) {
	mr := miniredis.RunT(t)
	const storePassword = "s3cret-store-credential"
	mr.RequireAuth(storePassword)
	h := &Handlers{RateLimit: limiterRuntime(t, mr.Addr(), "")}

	_, _, raw := readyResponse(t, h)

	for _, leak := range []string{storePassword, "password", "redis://"} {
		if strings.Contains(raw, leak) {
			t.Fatalf("the readiness body must not carry %q; body %s", leak, raw)
		}
	}
}
