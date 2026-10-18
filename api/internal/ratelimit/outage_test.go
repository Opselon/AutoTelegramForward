package ratelimit

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Outage policy: what happens when the store that enforces the limit is gone.
//
// The one thing that must never happen is "the limit silently disappeared". A
// limiter that fails open by accident is worse than no limiter, because the
// deployment believes it is protected. So each class is tested in both
// directions: the security class refuses, the ordinary class degrades and SAYS
// it degraded.
// ---------------------------------------------------------------------------

// downHarness returns a harness whose Redis is already gone.
func downHarness(t *testing.T, algo Algo) *harness {
	t.Helper()
	h := newHarness(t, algo)
	h.mr.Close()
	return h
}

func TestAuthClassFailsClosedWhenRedisIsGone(t *testing.T) {
	h := downHarness(t, AlgoBucket)
	cfg := testConfig()
	cfg.Policies[ClassAuth] = Policy{Class: ClassAuth, Budget: budget(30, time.Minute, 10), Fail: FailClosed, Scope: ScopeIP, Cost: 1}
	rt := newTestRuntime(t, h, cfg)

	handler := rt.WrapClass(ClassAuth, nil, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("an auth request reached the handler while its limit could not be enforced")
	}))
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/api/auth/login", nil))

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status %d, want 503 (fail-closed), body %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"code":"rate_limit_unavailable"`) {
		t.Fatalf("the refusal must name itself: %s", w.Body.String())
	}
	if w.Header().Get("Retry-After") == "" {
		t.Fatal("a 503 from a failed limit must tell the client to retry")
	}
}

func TestReadClassDegradesToTheLocalLimit(t *testing.T) {
	h := downHarness(t, AlgoBucket)
	cfg := testConfig()
	cfg.Policies[ClassRead] = Policy{Class: ClassRead, Budget: budget(3, time.Minute, 3), Fail: FailLocal, Scope: ScopeTenant, Cost: 1}
	rt := newTestRuntime(t, h, cfg)

	served := 0
	handler := rt.WrapClass(ClassRead, func(*http.Request) (int64, bool, bool) { return 9, false, true },
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { served++; w.WriteHeader(http.StatusOK) }))

	var last *httptest.ResponseRecorder
	for i := 0; i < 3; i++ {
		last = httptest.NewRecorder()
		handler.ServeHTTP(last, httptest.NewRequest(http.MethodGet, "/api/rules", nil))
		if last.Code != http.StatusOK {
			t.Fatalf("request %d during the outage returned %d", i, last.Code)
		}
		if last.Header().Get("X-RateLimit-Degraded") != "1" {
			t.Fatalf("a degraded answer must say so: headers %v", last.Header())
		}
	}
	if served != 3 {
		t.Fatalf("handler served %d, want 3", served)
	}
	// The fallback is a real limit, not a bypass.
	last = httptest.NewRecorder()
	handler.ServeHTTP(last, httptest.NewRequest(http.MethodGet, "/api/rules", nil))
	if last.Code != http.StatusTooManyRequests {
		t.Fatalf("the local fallback let the 4th request through: %d", last.Code)
	}
	if rt.stats.Snapshot().Degraded == 0 {
		t.Fatal("degraded answers must be counted, or an operator never learns the store was down")
	}
}

func TestFailOpenIsExplicitAndCounted(t *testing.T) {
	h := downHarness(t, AlgoBucket)
	cfg := testConfig()
	cfg.Policies[ClassWrite] = Policy{Class: ClassWrite, Budget: budget(1, time.Minute, 1), Fail: FailOpen, Scope: ScopeTenant, Cost: 1}
	rt := newTestRuntime(t, h, cfg)
	handler := rt.WrapClass(ClassWrite, func(*http.Request) (int64, bool, bool) { return 3, false, true },
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))
	for i := 0; i < 5; i++ {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/api/rules", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("fail-open class refused request %d: %d", i, w.Code)
		}
	}
	if rt.stats.Snapshot().Unavailable == 0 {
		t.Fatal("fail-open is a decision and must be visible in the counters")
	}
}

// The breaker is what keeps a Redis outage from becoming an API-wide latency
// spike: after the threshold, checks are short-circuited instead of waiting for
// a timeout each time.
func TestBreakerOpensAndStopsHammeringADeadStore(t *testing.T) {
	h := downHarness(t, AlgoBucket)
	cfg := testConfig()
	cfg.Redis.OpTimeout = 40 * time.Millisecond
	cfg.Policies[ClassRead] = Policy{Class: ClassRead, Budget: budget(10, time.Minute, 10), Fail: FailLocal, Scope: ScopeTenant, Cost: 1}
	rt := newTestRuntime(t, h, cfg)

	handler := rt.WrapClass(ClassRead, func(*http.Request) (int64, bool, bool) { return 1, false, true },
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))
	for i := 0; i < 20; i++ {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/rules", nil))
	}
	state := h.lim.breaker.state()
	if state.State == "closed" {
		t.Fatalf("the breaker never opened while Redis was down: %+v", state)
	}
	if state.Rejected == 0 {
		t.Fatalf("the breaker opened but short-circuited nothing: %+v", state)
	}
	t.Logf("breaker after 20 failed checks: %+v", state)
}

// A single healthy answer must close the breaker again: carrying the open
// window past the outage refuses traffic that would now succeed.
func TestBreakerClosesOnSuccess(t *testing.T) {
	b := newBreaker(2, 50*time.Millisecond)
	b.failure()
	b.failure()
	if b.state().State != "open" {
		t.Fatalf("expected open, got %+v", b.state())
	}
	if !b.open() {
		t.Fatal("an open breaker must short-circuit")
	}
	time.Sleep(60 * time.Millisecond) // cooldown elapses -> half-open probe
	if b.open() {
		t.Fatal("after the cooldown the first call must be allowed through as a probe")
	}
	b.success()
	if got := b.state().State; got != "closed" {
		t.Fatalf("a successful probe must close the breaker, got %q", got)
	}
}

// The fallback must not be reachable by breaking Redis: with the breaker open
// the counters still show the class as degraded, so the condition is visible
// rather than silent.
func TestDegradedStateIsReported(t *testing.T) {
	h := downHarness(t, AlgoBucket)
	cfg := testConfig()
	rt := newTestRuntime(t, h, cfg)
	rep := rt.Status(context.Background())
	if rep.Enabled != true {
		t.Fatalf("a wired limiter reports enabled even while unreachable: %+v", rep)
	}
	if rep.Reachable {
		t.Fatal("the status report must say Redis is unreachable")
	}
	if len(rep.Policies) == 0 {
		t.Fatal("the status report must list the effective policy per class")
	}
	for _, p := range rep.Policies {
		if p.FailMode == "" || p.Scope == "" || p.PerMinute <= 0 {
			t.Fatalf("incomplete policy in the report: %+v", p)
		}
	}
	if rep.Addrs != nil {
		for _, a := range rep.Addrs {
			if strings.Contains(a, "@") && !strings.Contains(a, "[REDACTED]") {
				t.Fatalf("address with credentials in the report: %q", a)
			}
		}
	}
}

// The status report is read by an operator and pasted into tickets: it must not
// carry the password, even one embedded in a URL-shaped address.
func TestStatusRedactsCredentials(t *testing.T) {
	addrs := redactAddrs([]string{"redis://default:supersecret@redis:6379", "redis2:6379"})
	for _, a := range addrs {
		if strings.Contains(a, "supersecret") {
			t.Fatalf("credential leaked in %q", a)
		}
	}
	if addrs[1] != "redis2:6379" {
		t.Fatalf("a plain address must pass through: %v", addrs)
	}
	err := redactErr(context.DeadlineExceeded, "supersecret")
	if strings.Contains(err, "supersecret") {
		t.Fatalf("password leaked through an error string: %q", err)
	}
}
