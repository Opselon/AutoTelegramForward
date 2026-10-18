package ratelimit

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Key derivation: the part of a rate limiter that is a security control.
//
// A limit whose key a caller can choose is not a limit. These tests pin the
// three ways that could go wrong: the identity must come from verification, the
// address must come from the transport, and the two tenants of one deployment.
// must never share a bucket.
// ---------------------------------------------------------------------------

// One tenant's traffic must not consume another tenant's allowance, in either
// direction.
func TestTenantsDoNotShareABudget(t *testing.T) {
	h := newHarness(t, AlgoBucket)
	cfg := testConfig()
	cfg.Policies[ClassRead] = Policy{Class: ClassRead, Budget: budget(3, time.Minute, 3), Fail: FailLocal, Scope: ScopeTenant, Cost: 1}
	rt := newTestRuntime(t, h, cfg)

	call := func(uid int64) int {
		handler := rt.WrapClass(ClassRead, func(*http.Request) (int64, bool, bool) { return uid, false, true },
			http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/rules", nil))
		return w.Code
	}

	for i := 0; i < 3; i++ {
		if code := call(5094837833); code != http.StatusOK {
			t.Fatalf("tenant A request %d returned %d", i, code)
		}
	}
	if code := call(5094837833); code != http.StatusTooManyRequests {
		t.Fatalf("tenant A's 4th request returned %d, want 429", code)
	}
	// Tenant B has spent nothing: A's exhaustion must not touch B.
	for i := 0; i < 3; i++ {
		if code := call(5989354154); code != http.StatusOK {
			t.Fatalf("tenant B request %d returned %d — tenant A's bucket leaked", i, code)
		}
	}
}

// The identity used for the key is the one the API passes in (the verified
// token), so nothing a client sends can move it. This test sends every
// client-controlled field a naive implementation might read.
func TestClientSuppliedIdentityIsIgnored(t *testing.T) {
	h := newHarness(t, AlgoBucket)
	cfg := testConfig()
	cfg.Policies[ClassRead] = Policy{Class: ClassRead, Budget: budget(2, time.Minute, 2), Fail: FailLocal, Scope: ScopeTenant, Cost: 1}
	rt := newTestRuntime(t, h, cfg)

	// The API's identity accessor: verified, fixed, and deliberately not
	// reading anything from the request.
	verified := func(*http.Request) (int64, bool, bool) { return 42, false, true }
	handler := rt.WrapClass(ClassRead, verified, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	spoof := func(headerName, headerValue, query string) int {
		req := httptest.NewRequest(http.MethodGet, "/api/rules?owner_user_id="+query, nil)
		if headerName != "" {
			req.Header.Set(headerName, headerValue)
		}
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w.Code
	}

	// Two requests exhaust the verified tenant's bucket.
	if c := spoof("X-User-Id", "1", "1"); c != http.StatusOK {
		t.Fatalf("request 1: %d", c)
	}
	if c := spoof("X-User-Id", "2", "2"); c != http.StatusOK {
		t.Fatalf("request 2: %d", c)
	}
	// Every request below claims to be someone else; the bucket must still be
	// the verified tenant's, and it is empty.
	cases := []struct{ hdr, val, q string }{
		{"X-User-Id", "999", "999"},
		{"X-Forwarded-User", "999", "999"},
		{"X-Owner-User-Id", "999", "999"},
		{"", "", "999"},
	}
	for _, c := range cases {
		if code := spoof(c.hdr, c.val, c.q); code != http.StatusTooManyRequests {
			t.Fatalf("a client-supplied identity (%s:%s?owner_user_id=%s) escaped the verified tenant's bucket: %d",
				c.hdr, c.val, c.q, code)
		}
	}
}

// X-Forwarded-For is believed only behind a declared trusted proxy: otherwise a
// client mints a fresh identity per request and the per-address limit vanishes.
func TestForwardedForIsOnlyTrustedBehindADeclaredProxy(t *testing.T) {
	cfg := testConfig()

	// No trusted proxies configured (the default): the header is ignored.
	req := httptest.NewRequest(http.MethodGet, "/api/rules", nil)
	req.RemoteAddr = "203.0.113.9:4444"
	req.Header.Set("X-Forwarded-For", "1.2.3.4")
	if got := cfg.clientAddr(req); got != "203.0.113.9" {
		t.Fatalf("without a trusted proxy the peer address must win, got %q", got)
	}

	// A trusted proxy is declared, but the request did NOT come from it.
	cfg.TrustedProxies = parseCIDRs("10.0.0.0/8")
	if got := cfg.clientAddr(req); got != "203.0.113.9" {
		t.Fatalf("a direct caller must not be able to spoof the header, got %q", got)
	}

	// The same header arriving FROM the proxy is believed.
	req.RemoteAddr = "10.1.2.3:5000"
	if got := cfg.clientAddr(req); got != "1.2.3.4" {
		t.Fatalf("the proxy's X-Forwarded-For must be honoured, got %q", got)
	}

	// Only the left-most entry is the client; the rest are proxies.
	req.Header.Set("X-Forwarded-For", "1.2.3.4, 10.0.0.9, 10.0.0.10")
	if got := cfg.clientAddr(req); got != "1.2.3.4" {
		t.Fatalf("expected the original client, got %q", got)
	}

	// A malformed header falls back to the peer, never to the raw value.
	req.Header.Set("X-Forwarded-For", "not-an-ip")
	if got := cfg.clientAddr(req); got != "10.1.2.3" {
		t.Fatalf("a malformed header must fall back to the peer, got %q", got)
	}

	// X-Real-IP is the other common proxy header and is trusted the same way.
	req.Header.Del("X-Forwarded-For")
	req.Header.Set("X-Real-IP", "198.51.100.7")
	if got := cfg.clientAddr(req); got != "198.51.100.7" {
		t.Fatalf("X-Real-IP from a trusted proxy must be honoured, got %q", got)
	}
}

// The endpoint dimension: one busy endpoint must not spend a tenant's whole
// budget, and ids in the path must not multiply the keyspace.
func TestRouteGroupCollapsesIDsAndSeparatesEndpoints(t *testing.T) {
	cases := map[string]string{
		"/api/rules": "rules",
		"/api/rules/ea565d5c-ccce-4a11-9f3b-2a4e6f8b1c22/settings": "rules/settings",
		"/api/rules/12345":                    "rules",
		"/api/logs/export":                    "logs/export",
		"/api/bot-tokens/7878773795/defaults": "bot-tokens/defaults",
		"/api/ai-configs/providers":           "ai-configs/providers",
		// The group is the first two meaningful segments: an id never creates a
		// new group, and a deeper path stays under its parent's budget.
		"/api/admin/users/5094837833/status": "admin/users",
		"/healthz":                           "healthz",
		"/":                                  "root",
	}
	for path, want := range cases {
		if got := routeGroup(path); got != want {
			t.Errorf("routeGroup(%q) = %q, want %q", path, got, want)
		}
	}
}

// Two endpoints of the same tenant have independent budgets, so a log export
// cannot lock an account out of its own rules.
func TestEndpointsHaveIndependentBudgets(t *testing.T) {
	h := newHarness(t, AlgoBucket)
	cfg := testConfig()
	cfg.Policies[ClassRead] = Policy{Class: ClassRead, Budget: budget(2, time.Minute, 2), Fail: FailLocal, Scope: ScopeTenant, Cost: 1}
	rt := newTestRuntime(t, h, cfg)
	handler := rt.WrapClass(ClassRead, func(*http.Request) (int64, bool, bool) { return 77, false, true },
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))

	hit := func(path string) int {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, path, nil))
		return w.Code
	}
	for i := 0; i < 2; i++ {
		if code := hit("/api/rules"); code != http.StatusOK {
			t.Fatalf("rules request %d: %d", i, code)
		}
	}
	if code := hit("/api/rules"); code != http.StatusTooManyRequests {
		t.Fatalf("rules budget should be spent, got %d", code)
	}
	if code := hit("/api/logs"); code != http.StatusOK {
		t.Fatalf("/api/logs shares /api/rules' budget: %d", code)
	}
}

// A tenant-scoped limiter reached without a verified identity must attribute
// the request to the address, never to a fabricated account.
func TestTenantScopeWithoutIdentityFallsBackToTheAddress(t *testing.T) {
	h := newHarness(t, AlgoBucket)
	cfg := testConfig()
	p := Policy{Class: ClassRead, Budget: budget(1, time.Minute, 1), Fail: FailLocal, Scope: ScopeTenant, Cost: 1}
	rt := newTestRuntime(t, h, cfg)

	req := httptest.NewRequest(http.MethodGet, "/api/rules", nil)
	req.RemoteAddr = "198.51.100.4:1111"
	key := rt.key(ClassRead, p, tenantPart(0), req)
	if key == rt.cfg.KeyPrefix+string(cfg.Redis.Algo)+":read:rules:u:0" {
		t.Fatal("uid 0 produced a tenant bucket: an unverified caller would share one")
	}
	want := rt.cfg.KeyPrefix + string(cfg.Redis.Algo) + ":read:rules:ip:198.51.100.4"
	if key != want {
		t.Fatalf("key %q, want %q", key, want)
	}
}

// Liveness polling is exempt: a monitor that loops must not be recorded as an
// abuser, and a probe must keep working when a limiter class would refuse.
func TestProbePathsBypassTheLimiter(t *testing.T) {
	h := newHarness(t, AlgoBucket)
	cfg := testConfig()
	cfg.Policies[ClassRead] = Policy{Class: ClassRead, Budget: budget(1, time.Minute, 1), Fail: FailClosed, Scope: ScopeTenant, Cost: 1}
	rt := newTestRuntime(t, h, cfg)
	handler := rt.WrapClass(ClassRead, nil, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	for i := 0; i < 50; i++ {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/healthz", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("probe %d returned %d", i, w.Code)
		}
	}
	if rt.stats.Snapshot().Probes != 50 {
		t.Fatalf("probes counted: %+v", rt.stats.Snapshot())
	}
}

// ---------------------------------------------------------------------------
// Configuration.
// ---------------------------------------------------------------------------

func TestConfigFromEnvDefaults(t *testing.T) {
	// Nothing configured: no limiter, and no Redis address invented.
	cfg := ConfigFromEnv(func(string) string { return "" })
	if cfg.Enabled {
		t.Fatal("the limiter must be off when no store is configured")
	}
	if cfg.Redis.Algo != AlgoBucket {
		t.Fatalf("default algorithm %q, want the token bucket", cfg.Redis.Algo)
	}
	if p := cfg.Policy(ClassAuth); p.Fail != FailClosed {
		t.Fatalf("the auth class must fail closed by default, got %q", p.Fail)
	}
	if p := cfg.Policy(ClassRead); p.Fail != FailLocal {
		t.Fatalf("the read class must degrade locally by default, got %q", p.Fail)
	}
	if p := cfg.Policy(ClassAuth); p.Scope != ScopeIP {
		t.Fatalf("the auth class must key on the address by default, got %q", p.Scope)
	}
	if p := cfg.Policy(ClassRead); p.Scope != ScopeTenant {
		t.Fatalf("the read class must key on the tenant by default, got %q", p.Scope)
	}
}

func TestConfigFromEnvOverrides(t *testing.T) {
	env := map[string]string{
		"ATF_REDIS_ADDR":                "redis:6379,redis2:6379",
		"ATF_REDIS_PASSWORD":            "not-a-real-password",
		"ATF_REDIS_DB":                  "2",
		"ATF_REDIS_TLS":                 "1",
		"ATF_RATELIMIT_ALGO":            "sw",
		"ATF_RATELIMIT_READ_PER_MINUTE": "42",
		"ATF_RATELIMIT_READ_BURST":      "84",
		"ATF_RATELIMIT_READ_FAIL_MODE":  "open",
		"ATF_RATELIMIT_AUTH_SCOPE":      "uip",
		"ATF_TRUSTED_PROXIES":           "10.0.0.0/8, 192.168.1.5",
		"ATF_RATELIMIT_ENABLED":         "1",
	}
	cfg := ConfigFromEnv(func(k string) string { return env[k] })
	if !cfg.Enabled {
		t.Fatal("configured Redis must enable the limiter")
	}
	if len(cfg.Redis.Addrs) != 2 || cfg.Redis.Addrs[0] != "redis:6379" || cfg.Redis.Addrs[1] != "redis2:6379" {
		t.Fatalf("addresses: %v", cfg.Redis.Addrs)
	}
	if cfg.Redis.DB != 2 || !cfg.Redis.TLS {
		t.Fatalf("db/tls: %+v", cfg.Redis)
	}
	if cfg.Redis.Algo != AlgoWindow {
		t.Fatalf("algorithm %q", cfg.Redis.Algo)
	}
	p := cfg.Policy(ClassRead)
	if p.Budget.Limit != 42 || p.Budget.Burst != 84 || p.Fail != FailOpen {
		t.Fatalf("read policy: %+v", p)
	}
	if cfg.Policy(ClassAuth).Scope != ScopeTenantIP {
		t.Fatalf("auth scope: %q", cfg.Policy(ClassAuth).Scope)
	}
	if len(cfg.TrustedProxies) != 2 {
		t.Fatalf("trusted proxies: %v", cfg.TrustedProxies)
	}
}

// An explicit "off" is honoured even when a store is configured, and an address
// without an explicit flag is still enabled: the deployment that wires Redis
// but forgets a flag gets the protection.
func TestExplicitDisableWins(t *testing.T) {
	cfg := ConfigFromEnv(func(k string) string {
		if k == "ATF_REDIS_ADDR" {
			return "redis:6379"
		}
		if k == "ATF_RATELIMIT_ENABLED" {
			return "0"
		}
		return ""
	})
	if cfg.Enabled {
		t.Fatal("ATF_RATELIMIT_ENABLED=0 must disable the limiter")
	}
}

// A configured-but-unreachable Redis is not a startup failure: the process must
// come up and apply each class's policy. "Enabled" means an authoritative
// limiter is WIRED (and therefore that its outage policy governs); whether the
// store answers right now is a separate, reported fact.
func TestRuntimeStartsWithoutRedis(t *testing.T) {
	cfg := testConfig()
	cfg.Redis.Addrs = []string{"127.0.0.1:1"} // nothing listens here
	cfg.Redis.DialTimeout = 50 * time.Millisecond
	cfg.Redis.OpTimeout = 50 * time.Millisecond
	rt, err := NewRuntime(context.Background(), cfg, t.Logf)
	if err != nil {
		t.Fatalf("a dead Redis must not be fatal: %v", err)
	}
	if !rt.Enabled() {
		t.Fatal("a configured store must read as enabled, so the class outage policy governs")
	}
	if rep := rt.Status(context.Background()); rep.Reachable {
		t.Fatalf("the report must say the store is unreachable: %+v", rep)
	}
	// And the classes still enforce, per-process.
	handler := rt.WrapClass(ClassRead, func(*http.Request) (int64, bool, bool) { return 5, false, true },
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/rules", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("the per-process limit refused the first request: %d", w.Code)
	}
}

func TestUnknownAlgorithmIsRefused(t *testing.T) {
	cfg := testConfig()
	cfg.Redis.Addrs = []string{"127.0.0.1:1"}
	cfg.Redis.Algo = Algo("sliding-something")
	if _, err := NewRedisLimiter(context.Background(), cfg.Redis); err == nil {
		t.Fatal("an unknown algorithm must be a configuration error, not a silent fallback")
	}
}
