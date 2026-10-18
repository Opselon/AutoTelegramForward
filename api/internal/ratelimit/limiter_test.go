package ratelimit

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

// ---------------------------------------------------------------------------
// Test harness.
//
// Two backends, deliberately:
//
//   - miniredis (in-process, always run): fast, deterministic, no dependency,
//     so the algorithm tests run on every `go test ./...` including a laptop
//     with no Docker. It executes the SAME Lua through gopher-lua.
//   - a real Redis (ATF_TEST_REDIS_ADDR, skipped when unset): the semantics the
//     deployment actually gets — real Lua, real TTLs, real concurrency, real
//     memory accounting. CI sets it from a service container, and the
//     benchmark numbers in docs/benchmarks are measured against it.
//
// A test that only ever ran against miniredis would be testing gopher-lua's
// approximation of Redis; a test that only ran against a real server would keep
// the suite unusable without Docker. Both, and the difference is visible.
// ---------------------------------------------------------------------------

type harness struct {
	lim      *redisLimiter
	mr       *miniredis.Miniredis
	client   redis.UniversalClient
	realAddr string
}

// now is the fake clock shared by the limiter under test.
var testClock atomic.Int64

// fakeClock returns a clock the test can advance.
func fakeClock(t *testing.T) func() time.Time {
	t.Helper()
	start := time.Now()
	testClock.Store(start.UnixNano())
	return func() time.Time { return time.Unix(0, testClock.Load()) }
}

func advance(d time.Duration) { testClock.Add(int64(d)) }

// newHarness builds a limiter over miniredis with an injected clock.
func newHarness(t *testing.T, algo Algo) *harness {
	t.Helper()
	mr := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	opts := DefaultRedisOptions()
	opts.Algo = algo
	l := newRedisLimiter(client, opts)
	l.clock = fakeClock(t)
	t.Cleanup(func() { _ = client.Close() })
	return &harness{lim: l, mr: mr, client: client}
}

// newRealHarness builds a limiter over the real Redis from the environment.
func newRealHarness(t *testing.T, algo Algo) *harness {
	t.Helper()
	addr := os.Getenv("ATF_TEST_REDIS_ADDR")
	if addr == "" {
		t.Skip("ATF_TEST_REDIS_ADDR is not set: real-Redis coverage is skipped")
	}
	client := redis.NewClient(&redis.Options{Addr: addr})
	if err := client.Ping(context.Background()).Err(); err != nil {
		t.Skipf("ATF_TEST_REDIS_ADDR=%s is not reachable: %v", addr, err)
	}
	// A distinct prefix per run keeps parallel test binaries and repeated runs
	// from inheriting each other's counters.
	opts := DefaultRedisOptions()
	opts.Algo = algo
	opts.Prefix = "atf:test:" + algo.testPrefix(t) + ":"
	l := newRedisLimiter(client, opts)
	l.clock = fakeClock(t)
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		flushPrefix(ctx, client, opts.Prefix)
		_ = client.Close()
	})
	return &harness{lim: l, client: client, realAddr: addr}
}

// testPrefix returns a per-algorithm, per-test prefix.
func (a Algo) testPrefix(t *testing.T) string {
	return string(a) + ":" + sanitize(t.Name())
}

func sanitize(s string) string {
	out := make([]rune, 0, len(s))
	for _, r := range s {
		if r == '_' || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			out = append(out, r)
		} else {
			out = append(out, '_')
		}
	}
	return string(out)
}

// flushPrefix removes every key a test created, without ever running FLUSHDB on
// a server that may hold another test's — or a deployment's — data.
func flushPrefix(ctx context.Context, client redis.UniversalClient, prefix string) {
	iter := client.Scan(ctx, 0, prefix+"*", 200).Iterator()
	for iter.Next(ctx) {
		client.Del(ctx, iter.Val())
	}
}

// everyBackend runs fn against miniredis and, when configured, real Redis.
func everyBackend(t *testing.T, fn func(t *testing.T, h *harness)) {
	t.Helper()
	t.Run("miniredis", func(t *testing.T) { fn(t, newHarness(t, AlgoBucket)) })
	t.Run("realredis", func(t *testing.T) { fn(t, newRealHarness(t, AlgoBucket)) })
}

func budget(limit int64, window time.Duration, burst int64) Budget {
	return Budget{Limit: limit, Window: window, Burst: burst}
}

// ---------------------------------------------------------------------------
// Token bucket semantics.
// ---------------------------------------------------------------------------

// A bucket starts full, spends one token per request, and refuses when empty —
// the whole contract of "100 requests per minute".
func TestTokenBucketAllowsUpToCapacityThenRefuses(t *testing.T) {
	everyBackend(t, func(t *testing.T, h *harness) {
		b := budget(10, time.Minute, 10)
		for i := 0; i < 10; i++ {
			dec, err := h.lim.Allow(context.Background(), "k", 1, b)
			if err != nil {
				t.Fatalf("request %d: %v", i, err)
			}
			if !dec.Allowed {
				t.Fatalf("request %d was refused before the budget was spent", i)
			}
			if dec.Limit != 10 {
				t.Fatalf("limit reported as %d, want 10", dec.Limit)
			}
		}
		dec, err := h.lim.Allow(context.Background(), "k", 1, b)
		if err != nil {
			t.Fatalf("refusal errored: %v", err)
		}
		if dec.Allowed {
			t.Fatal("the 11th request was allowed: the bucket holds 10")
		}
		if dec.RetryAfter <= 0 {
			t.Fatal("a refusal must tell the client how long to wait")
		}
		// 1 token of a 10/minute bucket is 6s.
		if dec.RetryAfter < 5*time.Second || dec.RetryAfter > 7*time.Second {
			t.Fatalf("retry-after %v, want ~6s", dec.RetryAfter)
		}
	})
}

// Refill is proportional to elapsed time, not a fixed window reset.
func TestTokenBucketRefillsProportionally(t *testing.T) {
	everyBackend(t, func(t *testing.T, h *harness) {
		b := budget(10, time.Minute, 10)
		for i := 0; i < 10; i++ {
			if dec, _ := h.lim.Allow(context.Background(), "k", 1, b); !dec.Allowed {
				t.Fatalf("spend %d refused", i)
			}
		}
		// Half a minute of a 10/minute budget is five tokens.
		advance(30 * time.Second)
		allowed := 0
		for i := 0; i < 10; i++ {
			dec, _ := h.lim.Allow(context.Background(), "k", 1, b)
			if dec.Allowed {
				allowed++
			}
		}
		if allowed < 4 || allowed > 5 {
			t.Fatalf("after 30s of a 10/min bucket, %d requests were allowed, want 4-5", allowed)
		}
	})
}

// A burst larger than the sustained rate is spendable at once, and the bucket
// still refuses past the burst.
func TestTokenBucketBurstIsCappedAtCapacity(t *testing.T) {
	everyBackend(t, func(t *testing.T, h *harness) {
		b := budget(60, time.Minute, 120) // 1/s sustained, 120 at once
		allowed := 0
		for i := 0; i < 130; i++ {
			if dec, _ := h.lim.Allow(context.Background(), "k", 1, b); dec.Allowed {
				allowed++
			}
		}
		if allowed != 120 {
			t.Fatalf("burst bucket allowed %d, want exactly the capacity 120", allowed)
		}
	})
}

// The keyspace must be self-limiting: every key carries a TTL, so a client that
// invents a million scopes cannot grow Redis without bound.
func TestEveryKeyExpires(t *testing.T) {
	everyBackend(t, func(t *testing.T, h *harness) {
		b := budget(10, time.Minute, 10)
		if _, err := h.lim.Allow(context.Background(), h.lim.opts.Prefix+"ttl:key", 1, b); err != nil {
			t.Fatal(err)
		}
		ttl := h.client.TTL(context.Background(), h.lim.opts.Prefix+"ttl:key").Val()
		if ttl <= 0 {
			t.Fatalf("key has no TTL (%v): an invented scope would live forever", ttl)
		}
		if ttl > 3*time.Minute {
			t.Fatalf("TTL %v is longer than the bucket can possibly matter", ttl)
		}
	})
}

// The atomicity claim, tested the only way it can be: concurrently, with more
// requests than the budget, asserting that the number admitted is EXACTLY the
// budget. A read-modify-write from the client would overshoot here.
func TestConcurrentRequestsCannotOvershootTheBudget(t *testing.T) {
	everyBackend(t, func(t *testing.T, h *harness) {
		const limit = 500
		b := budget(limit, time.Hour, limit) // a full hour: no refill during the test
		var allowed atomic.Int64
		var wg sync.WaitGroup
		for w := 0; w < 64; w++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for i := 0; i < 40; i++ {
					dec, err := h.lim.Allow(context.Background(), h.lim.opts.Prefix+"hot", 1, b)
					if err != nil {
						continue
					}
					if dec.Allowed {
						allowed.Add(1)
					}
				}
			}()
		}
		wg.Wait()
		if got := allowed.Load(); got != limit {
			t.Fatalf("64 goroutines × 40 requests admitted %d, want exactly %d (overshoot = a broken limit)", got, limit)
		}
	})
}

// ---------------------------------------------------------------------------
// Sliding window: semantics and accuracy against the exact implementation.
// ---------------------------------------------------------------------------

func TestSlidingWindowCountsAndRefuses(t *testing.T) {
	for _, algo := range []Algo{AlgoWindow, AlgoExactWindow} {
		t.Run(string(algo), func(t *testing.T) {
			h := newHarness(t, algo)
			b := budget(10, time.Minute, 10)
			allowed := 0
			for i := 0; i < 12; i++ {
				if dec, _ := h.lim.Allow(context.Background(), "w", 1, b); dec.Allowed {
					allowed++
				}
			}
			if allowed != 10 {
				t.Fatalf("window allowed %d of 12 with limit 10", allowed)
			}
			// A full window later the budget is available again. The bounded
			// (bucketed) implementation interpolates between two buckets, so
			// ONE window can still carry weight from the previous bucket —
			// that carry-over is the documented ~10% drift, pinned next door.
			// The guarantee the implementation actually makes is therefore
			// "two windows later nothing from before can weigh", and asserting
			// the tighter version would fail at bucket boundaries only, which
			// is a flaky test and not a finding.
			advance(2*time.Minute + time.Second)
			if dec, _ := h.lim.Allow(context.Background(), "w", 1, b); !dec.Allowed {
				t.Fatal("two windows after the burst the whole budget must be back")
			}
		})
	}
}

// What the approximation actually costs on ordinary traffic.
//
// The two-bucket counter interpolates: it assumes traffic inside the previous
// bucket was spread evenly, so it gets back some of that bucket as the bucket
// ages. On steady traffic that shows up as a slow drift, measured here at
// roughly +10% of the allowance over two minutes. The tolerance below is the
// MEASURED error, not the error we would like: it is the reason the sliding
// window is not the default (the token bucket's ceiling is exact, see
// TestBoundedWindowBoundaryBurstIsBoundedByOneBucket and the benchmark notes),
// and it is small enough that the approximation is fine where a window's shape
// matters more than an exact ceiling.
func TestBoundedWindowMatchesTheExactWindowOnSteadyTraffic(t *testing.T) {
	const limit = 100
	b := budget(limit, time.Minute, limit)

	bounded := newHarness(t, AlgoWindow)
	exact := newHarness(t, AlgoExactWindow)

	var boundedAllowed, exactAllowed int
	for step := 0; step < 40; step++ {
		advance(3 * time.Second)
		for i := 0; i < 8; i++ { // 8 per 3s = 160/min against a 100/min limit
			if dec, _ := bounded.lim.Allow(context.Background(), "b", 1, b); dec.Allowed {
				boundedAllowed++
			}
			if dec, _ := exact.lim.Allow(context.Background(), "b", 1, b); dec.Allowed {
				exactAllowed++
			}
		}
	}
	diff := boundedAllowed - exactAllowed
	if diff < 0 {
		diff = -diff
	}
	// 15% of the exact count: comfortably above the measured ~10% drift, far
	// below the "different limit" that would make the approximation unusable.
	tolerance := exactAllowed * 15 / 100
	if tolerance < 5 {
		tolerance = 5
	}
	if diff > tolerance {
		t.Fatalf("steady traffic: bounded admitted %d vs exact %d (diff %d > tolerance %d)",
			boundedAllowed, exactAllowed, diff, tolerance)
	}
	t.Logf("steady traffic: bounded=%d exact=%d diff=%d (+%.1f%%)", boundedAllowed, exactAllowed, diff,
		100*float64(diff)/float64(exactAllowed))
}

// The adversarial case, MEASURED rather than asserted away: a client that
// empties its allowance at the very end of one bucket and again at the start of
// the next gets some of the previous bucket back, because the weighted counter
// assumes traffic inside a bucket is spread evenly. The worst case is one
// bucket's worth of extra admission — the same ceiling a plain fixed window has
// — which is exactly why the TOKEN BUCKET, whose guarantee is exact
// (admitted <= burst + rate*elapsed, with no boundary to exploit), is the
// default and this counter is the documented alternative.
func TestBoundedWindowBoundaryBurstIsBoundedByOneBucket(t *testing.T) {
	const limit = 100
	b := budget(limit, time.Minute, limit)

	bounded := newHarness(t, AlgoWindow)
	exact := newHarness(t, AlgoExactWindow)

	// Fill the allowance just before the bucket boundary…
	advance(59 * time.Second)
	spend := func(h *harness, n int) int {
		allowed := 0
		for i := 0; i < n; i++ {
			if dec, _ := h.lim.Allow(context.Background(), "b", 1, b); dec.Allowed {
				allowed++
			}
		}
		return allowed
	}
	boundedFirst, exactFirst := spend(bounded, limit+20), spend(exact, limit+20)
	// …then burst again right after it.
	advance(2 * time.Second)
	boundedSecond := spend(bounded, limit+20)
	exactSecond := spend(exact, limit+20)

	t.Logf("boundary burst: bounded=%d+%d=%d exact=%d+%d=%d over a 61s span (limit %d/min)",
		boundedFirst, boundedSecond, boundedFirst+boundedSecond,
		exactFirst, exactSecond, exactFirst+exactSecond, limit)

	if exactFirst+exactSecond > limit+2 {
		t.Fatalf("the exact window admitted %d in one span, above its limit %d", exactFirst+exactSecond, limit)
	}
	if boundedFirst+boundedSecond > 2*limit {
		t.Fatalf("the bounded window admitted %d in one span, above the documented worst case %d",
			boundedFirst+boundedSecond, 2*limit)
	}
}

// Memory: the whole reason the bucketed counter is the default. One key with
// three small fields versus one sorted-set member per admitted request.
func TestBoundedWindowUsesFarLessMemoryThanTheExactWindow(t *testing.T) {
	// MEMORY USAGE is the server's own accounting, so this test needs a real
	// Redis; miniredis does not implement it and would skip anyway.
	if os.Getenv("ATF_TEST_REDIS_ADDR") == "" {
		t.Skip("ATF_TEST_REDIS_ADDR is not set: memory accounting needs a real Redis")
	}
	b := budget(2000, time.Minute, 2000)

	bounded := newRealHarness(t, AlgoWindow)
	exact := newRealHarness(t, AlgoExactWindow)
	for i := 0; i < 2000; i++ {
		_, _ = bounded.lim.Allow(context.Background(), "m", 1, b)
		_, _ = exact.lim.Allow(context.Background(), "m", 1, b)
	}

	size := func(h *harness) int64 {
		used, err := h.client.MemoryUsage(context.Background(), h.lim.prefixKey("m")).Result()
		if err != nil {
			t.Skipf("MEMORY USAGE unavailable: %v", err)
		}
		return used
	}
	boundedBytes := size(bounded)
	exactBytes := size(exact)
	if boundedBytes >= exactBytes {
		t.Fatalf("bounded window uses %d bytes vs exact %d: the cheap one must be cheaper", boundedBytes, exactBytes)
	}
	t.Logf("2000 requests in one key: bounded=%d bytes, exact=%d bytes (%.1fx)",
		boundedBytes, exactBytes, float64(exactBytes)/float64(boundedBytes))
}

// ---------------------------------------------------------------------------
// The fallback limiter.
// ---------------------------------------------------------------------------

func TestLocalLimiterEnforcesTheSameBudget(t *testing.T) {
	l := newLocalLimiter(1000)
	l.nowFunc = fakeClock(t)
	b := budget(5, time.Minute, 5)
	allowed := 0
	for i := 0; i < 8; i++ {
		if dec, err := l.Allow(context.Background(), "k", 1, b); err == nil && dec.Allowed {
			allowed++
		}
	}
	if allowed != 5 {
		t.Fatalf("local fallback allowed %d of 8 with limit 5", allowed)
	}
	advance(24 * time.Second) // 5/min -> one token per 12s
	if dec, _ := l.Allow(context.Background(), "k", 1, b); !dec.Allowed {
		t.Fatal("the local bucket did not refill")
	}
}

// The fallback is bounded: it runs while Redis is down, which is a bad moment
// to leak memory per invented scope.
func TestLocalLimiterBoundsItsKeyspace(t *testing.T) {
	l := newLocalLimiter(640) // 10 per shard
	b := budget(100, time.Hour, 100)
	for i := 0; i < 5000; i++ {
		key := "scope-" + string(rune('a'+i%26)) + "-" + itoa(i)
		_, _ = l.Allow(context.Background(), key, 1, b)
	}
	if got := l.Size(); got > 640 {
		t.Fatalf("fallback holds %d keys, cap is 640", got)
	}
	if removed := l.Trim(b); removed == 0 {
		t.Log("trim removed nothing (every scope still had state) — acceptable, the cap still held")
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var buf [20]byte
	pos := len(buf)
	for i > 0 {
		pos--
		buf[pos] = byte('0' + i%10)
		i /= 10
	}
	return string(buf[pos:])
}

// A limiter must never be the reason a request hangs: the context deadline is
// honoured even when the backend does not answer.
func TestAllowRespectsTheContextDeadline(t *testing.T) {
	h := newHarness(t, AlgoBucket)
	ctx, cancel := context.WithTimeout(context.Background(), time.Nanosecond)
	defer cancel()
	time.Sleep(time.Millisecond) // let the deadline pass
	if _, err := h.lim.Allow(ctx, "k", 1, budget(10, time.Minute, 10)); err == nil {
		t.Fatal("an expired context must fail the check, not silently allow it")
	}
}

// ---------------------------------------------------------------------------
// Refusal shape at the HTTP boundary.
// ---------------------------------------------------------------------------

func newTestRuntime(t *testing.T, h *harness, cfg Config) *Runtime {
	t.Helper()
	rt := &Runtime{cfg: cfg, primary: h.lim, fallback: newLocalLimiter(1000), stats: &Stats{}, logf: t.Logf}
	rt.fallback.nowFunc = fakeClock(t)
	return rt
}

func testConfig() Config {
	cfg := ConfigFromEnv(func(string) string { return "" })
	cfg.Enabled = true
	cfg.Redis.Algo = AlgoBucket
	cfg.KeyPrefix = "atf:test:cfg:"
	return cfg
}

// A refused request names itself the way every other refusal in this API does,
// carries Retry-After in the header AND the body, and reports the quota on
// every answer.
func TestRefusalShapeAndQuotaHeaders(t *testing.T) {
	h := newHarness(t, AlgoBucket)
	cfg := testConfig()
	cfg.Policies[ClassRead] = Policy{Class: ClassRead, Budget: budget(2, time.Minute, 2), Fail: FailLocal, Scope: ScopeTenant, Cost: 1}
	rt := newTestRuntime(t, h, cfg)

	var statuses []int
	var bodies []string
	var w *httptest.ResponseRecorder
	handler := rt.WrapClass(ClassRead, func(*http.Request) (int64, bool, bool) { return 42, false, true },
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))
	for i := 0; i < 3; i++ {
		w = httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/rules", nil))
		statuses = append(statuses, w.Code)
		bodies = append(bodies, w.Body.String())
	}
	if statuses[0] != 200 || statuses[1] != 200 {
		t.Fatalf("statuses %v: the first two requests are inside the budget", statuses)
	}
	if statuses[2] != http.StatusTooManyRequests {
		t.Fatalf("third status %d, want 429", statuses[2])
	}
	if !contains(bodies[2], `"code":"rate_limited"`) || !contains(bodies[2], "retry_after_seconds") {
		t.Fatalf("refusal body must name itself and the wait: %s", bodies[2])
	}
	if w.Header().Get("Retry-After") == "" {
		t.Fatal("a 429 must carry Retry-After")
	}
	if w.Header().Get("X-RateLimit-Limit") != "2" {
		t.Fatalf("X-RateLimit-Limit=%q, want 2", w.Header().Get("X-RateLimit-Limit"))
	}
	if w.Header().Get("X-RateLimit-Remaining") != "0" {
		t.Fatalf("X-RateLimit-Remaining=%q, want 0", w.Header().Get("X-RateLimit-Remaining"))
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (haystack == needle || indexOf(haystack, needle) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
