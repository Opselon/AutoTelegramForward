package ratelimit

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

// Benchmarks.
//
// They answer the only questions worth asking about a limiter's cost:
//
//   - How many nanoseconds does one decision add to a request?
//   - What does that cost per second with N replicas, and where does it stop
//     scaling (pool saturation, one hot key)?
//   - Which algorithm affordable?
//
// The Redis benchmarks need a real server (ATF_TEST_REDIS_ADDR) and report the
// LATENCY THE LIMITER SEES, not a measured RTT: the number an API request
// actually pays. `-benchmem` numbers are reported alongside so the allocation
// claim in the docs is reproducible.
//
//	go test ./internal/ratelimit -run '^$' -bench . -benchmem \
//	  -cpu 1,2,4,8 -benchtime 3s
//
// A benchmark that runs against miniredis would measure an in-process map and
// flatter Redis by ~100x; it is deliberately not provided.

func benchBudget() Budget { return Budget{Limit: 100000, Window: time.Minute, Burst: 100000} }

// BenchmarkLocalAllow is the baseline: the same decision with no network.
func BenchmarkLocalAllow(b *testing.B) {
	l := newLocalLimiter(1 << 20)
	l.nowFunc = time.Now
	ctx := context.Background()
	bb := benchBudget()
	b.ReportAllocs()
	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			if _, err := l.Allow(ctx, "bench", 1, bb); err != nil {
				b.Fatal(err)
			}
		}
	})
}

// BenchmarkRedisAllow is the cost of the authoritative decision, one key.
func BenchmarkRedisAllow(b *testing.B) {
	for _, algo := range []Algo{AlgoBucket, AlgoWindow, AlgoExactWindow} {
		b.Run(string(algo), func(b *testing.B) {
			addr := os.Getenv("ATF_TEST_REDIS_ADDR")
			if addr == "" {
				b.Skip("ATF_TEST_REDIS_ADDR is not set: the Redis benchmark needs a real server")
			}
			h := newRealHarnessB(b, algo)
			ctx := context.Background()
			bb := benchBudget()
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if _, err := h.lim.Allow(ctx, "bench", 1, bb); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}

// BenchmarkRedisAllowParallel is the same decision with many in-flight
// requests: it shows the connection pool doing its job, and (with -cpu) where
// the pool stops scaling.
func BenchmarkRedisAllowParallel(b *testing.B) {
	for _, algo := range []Algo{AlgoBucket, AlgoWindow} {
		b.Run(string(algo), func(b *testing.B) {
			addr := os.Getenv("ATF_TEST_REDIS_ADDR")
			if addr == "" {
				b.Skip("ATF_TEST_REDIS_ADDR is not set")
			}
			h := newRealHarnessB(b, algo)
			ctx := context.Background()
			bb := benchBudget()
			b.ReportAllocs()
			b.ResetTimer()
			b.RunParallel(func(pb *testing.PB) {
				for pb.Next() {
					if _, err := h.lim.Allow(ctx, "bench", 1, bb); err != nil {
						b.Fatal(err)
					}
				}
			})
		})
	}
}

// BenchmarkRedisAllowManyHotKey is the worst case for ANY shared limiter: every
// client in the fleet hits one key (the login endpoint of one address, say).
// Redis serialises it, so this measures the ceiling per key — the number behind
// the "one address cannot be limited faster than Redis executes one script"
// statement in the docs.
func BenchmarkRedisAllowManyHotKey(b *testing.B) {
	addr := os.Getenv("ATF_TEST_REDIS_ADDR")
	if addr == "" {
		b.Skip("ATF_TEST_REDIS_ADDR is not set")
	}
	h := newRealHarnessB(b, AlgoBucket)
	ctx := context.Background()
	bb := benchBudget()
	b.ReportAllocs()
	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			if _, err := h.lim.Allow(ctx, "hot", 1, bb); err != nil {
				b.Fatal(err)
			}
		}
	})
}

// BenchmarkRedisAllowAllPipelined is the cost of a request subject to two
// limits (address + tenant), pipelined into one round trip, against the same
// two checks run sequentially — the reason AllowAll exists.
func BenchmarkRedisAllowAllPipelined(b *testing.B) {
	addr := os.Getenv("ATF_TEST_REDIS_ADDR")
	if addr == "" {
		b.Skip("ATF_TEST_REDIS_ADDR is not set")
	}
	h := newRealHarnessB(b, AlgoBucket)
	ctx := context.Background()
	bb := benchBudget()
	checks := []Check{
		{Key: "ip:198.51.100.1", Budget: bb, Cost: 1},
		{Key: "u:5094837833", Budget: bb, Cost: 1},
	}
	b.Run("pipelined", func(b *testing.B) {
		b.ReportAllocs()
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			if _, err := h.lim.AllowAll(ctx, checks); err != nil {
				b.Fatal(err)
			}
		}
	})
	b.Run("sequential", func(b *testing.B) {
		b.ReportAllocs()
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			for _, c := range checks {
				if _, err := h.lim.Allow(ctx, c.Key, c.Cost, c.Budget); err != nil {
					b.Fatal(err)
				}
			}
		}
	})
}

// BenchmarkKeyDerivation guards the other half of the cost: building the key
// must not become the expensive part of a cheap decision.
func BenchmarkKeyDerivation(b *testing.B) {
	cfg := testConfig()
	rt := &Runtime{cfg: cfg, fallback: newLocalLimiter(10), stats: &Stats{}, logf: func(string, ...any) {}}
	p := cfg.Policy(ClassRead)
	req := benchRequest("/api/rules/ea565d5c-ccce-4a11-9f3b-2a4e6f8b1c22/settings")
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = rt.key(ClassRead, p, "u:5094837833", req)
	}
}

var benchCounter atomic.Int64

// benchRequest builds a request with a realistic peer address.
func benchRequest(path string) *http.Request {
	req := httptest.NewRequest("GET", path, nil)
	req.RemoteAddr = fmt.Sprintf("198.51.100.%d:5000", benchCounter.Add(1)%200+1)
	return req
}

// newRealHarnessB is the benchmark's harness: same wiring as the tests, but
// reported through *testing.B (a skipped benchmark must say why, not fail).
func newRealHarnessB(b *testing.B, algo Algo) *harness {
	b.Helper()
	addr := os.Getenv("ATF_TEST_REDIS_ADDR")
	client := redis.NewClient(&redis.Options{Addr: addr})
	if err := client.Ping(context.Background()).Err(); err != nil {
		b.Skipf("ATF_TEST_REDIS_ADDR=%s is not reachable: %v", addr, err)
	}
	opts := DefaultRedisOptions()
	opts.Algo = algo
	opts.Prefix = "atf:bench:" + string(algo) + ":" + sanitize(b.Name()) + ":"
	l := newRedisLimiter(client, opts)
	b.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		flushPrefix(ctx, client, opts.Prefix)
		_ = client.Close()
	})
	return &harness{lim: l, client: client, realAddr: addr}
}
