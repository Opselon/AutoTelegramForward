package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"

	"autoforward/internal/grpcclient"
	pb "autoforward/proto"
)

// The log aggregate is the second-slowest endpoint in the stack and the
// dashboard polls it, so two things must be true: a warm read must not touch
// the logger at all, and a tenant must never see another tenant's aggregate.

// slowStatsClient counts calls and can be made arbitrarily slow, so a test can
// tell "answered from cache" from "answered quickly by luck".
type slowStatsClient struct {
	fakeLogsClient
	delay time.Duration
	calls atomic.Int64
	val   atomic.Int64
}

func (s *slowStatsClient) LogStats(ctx context.Context, in *pb.LogStatsRequest, _ ...grpc.CallOption) (*pb.LogStatsResponse, error) {
	s.calls.Add(1)
	if s.delay > 0 {
		select {
		case <-time.After(s.delay):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	s.lastStats = in
	return &pb.LogStatsResponse{
		Total: s.val.Load(), ByLevel: map[string]int32{"ERROR": 1},
		ByRule: map[string]int32{"rule-1": 1}, ByService: map[string]int32{"api": 1},
	}, nil
}

func readStats(t *testing.T, h *Handlers, url string, uid int64) (map[string]any, time.Duration) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, url, nil)
	if uid != 0 {
		req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid}))
	}
	rec := httptest.NewRecorder()
	start := time.Now()
	h.LogStats(rec, req)
	elapsed := time.Since(start)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return body, elapsed
}

// TestLogStatsWarmReadDoesNotReachTheLogger is the regression test for the
// 42.55ms p50: after the first computation, a poll must be answered from the
// cache — no RPC, no aggregation, and measurably faster than the downstream.
func TestLogStatsWarmReadDoesNotReachTheLogger(t *testing.T) {
	fake := &slowStatsClient{delay: 20 * time.Millisecond}
	fake.val.Store(9)
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}

	first, cold := readStats(t, h, "/api/logs/stats", 0)
	if got := fake.calls.Load(); got != 1 {
		t.Fatalf("cold read made %d RPCs, want 1", got)
	}
	if cold < 20*time.Millisecond {
		t.Fatalf("the cold read should have waited for the (slow) logger, took %v", cold)
	}
	if first["cached"] != false {
		t.Fatalf("the first answer is computed, not cached: %#v", first)
	}

	for i := 0; i < 5; i++ {
		body, warm := readStats(t, h, "/api/logs/stats", 0)
		if got := fake.calls.Load(); got != 1 {
			t.Fatalf("warm read %d reached the logger (%d RPCs); the cache must absorb polling", i, got)
		}
		if warm > 5*time.Millisecond {
			t.Fatalf("warm read took %v; a cache hit must not cost milliseconds", warm)
		}
		if body["cached"] != true {
			t.Fatalf("a served-from-cache answer must say so: %#v", body)
		}
		if body["total"].(float64) != 9 {
			t.Fatalf("warm answer lost the aggregate: %#v", body)
		}
	}
}

// TestLogStatsStaleReadIsServedThenRefreshed pins stale-while-revalidate: past
// the TTL the caller gets the old answer immediately, one refresh runs, and the
// next read sees the new number.
func TestLogStatsStaleReadIsServedThenRefreshed(t *testing.T) {
	fake := &slowStatsClient{delay: 10 * time.Millisecond}
	fake.val.Store(1)
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}
	cache := h.statsCache()
	cache.ttl = 30 * time.Millisecond

	if _, _ = readStats(t, h, "/api/logs/stats", 0); fake.calls.Load() != 1 {
		t.Fatalf("cold read must compute once, got %d", fake.calls.Load())
	}

	fake.val.Store(2)
	time.Sleep(40 * time.Millisecond) // past the TTL: the entry is stale

	body, elapsed := readStats(t, h, "/api/logs/stats", 0)
	if body["cached"] != true {
		t.Fatalf("a stale read is served from cache: %#v", body)
	}
	if body["total"].(float64) != 1 {
		t.Fatalf("the stale read must return the cached value, got %v", body["total"])
	}
	if elapsed > 5*time.Millisecond {
		t.Fatalf("the stale read waited %v for the refresh; it must answer immediately", elapsed)
	}
	if age, _ := body["age_ms"].(float64); age < 30 {
		t.Fatalf("a stale answer must report its age, got %v ms", body["age_ms"])
	}

	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if body, _ := readStats(t, h, "/api/logs/stats", 0); body["total"].(float64) == 2 {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("the background refresh never landed")
}

// TestLogStatsCacheIsPerTenant: the aggregate includes a user's own rows, so a
// cached answer must never cross tenants.
func TestLogStatsCacheIsPerTenant(t *testing.T) {
	fake := &slowStatsClient{}
	fake.val.Store(1)
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}

	readStats(t, h, "/api/logs/stats?mine=1", 11)
	fake.val.Store(2)
	body, _ := readStats(t, h, "/api/logs/stats?mine=1", 22)

	if got := fake.calls.Load(); got != 2 {
		t.Fatalf("two tenants must not share one cached aggregate (%d RPCs)", got)
	}
	if body["cached"] != false || body["total"].(float64) != 2 {
		t.Fatalf("tenant 22 must get its own, freshly computed aggregate: %#v", body)
	}
}

// TestLogStatsFreshBypassesTheCache: an operator verifying a purge needs the
// number now, not one from up to a TTL ago.
func TestLogStatsFreshBypassesTheCache(t *testing.T) {
	fake := &slowStatsClient{}
	fake.val.Store(3)
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}

	readStats(t, h, "/api/logs/stats", 0)
	fake.val.Store(4)

	body, _ := readStats(t, h, "/api/logs/stats?fresh=1", 0)
	if body["cached"] != false || body["total"].(float64) != 4 {
		t.Fatalf("?fresh=1 must compute now: %#v", body)
	}
	if got := fake.calls.Load(); got != 2 {
		t.Fatalf("fresh=1 made %d RPCs, want 2 (cold + forced)", got)
	}
}

// TestLogStatsSingleFlightUnderConcurrentPollers: a burst on a cold key must
// produce one computation, not one per request.
func TestLogStatsSingleFlightUnderConcurrentPollers(t *testing.T) {
	fake := &slowStatsClient{delay: 30 * time.Millisecond}
	fake.val.Store(7)
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}}

	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			readStats(t, h, "/api/logs/stats", 0)
		}()
	}
	wg.Wait()

	if got := fake.calls.Load(); got != 1 {
		t.Fatalf("12 concurrent cold pollers made %d aggregations, want 1", got)
	}
}

// TestPurgeInvalidatesStatsCache: a purge makes the aggregate wrong (not just
// old), so the next poll must recompute rather than repeat the pre-purge total.
func TestPurgeInvalidatesStatsCache(t *testing.T) {
	fake := &slowStatsClient{}
	fake.val.Store(10)
	h := &Handlers{c: &grpcclient.Clients{Logs: fake}, LogAudit: func(int64, string, string, map[string]any) {}}

	readStats(t, h, "/api/logs/stats", 0)

	req := httptest.NewRequest(http.MethodPost, "/api/logs/purge", nil)
	// The installation-wide purge is the administrator plane, so the invalidating
	// caller here is an admin issuing the form that actually empties the archive.
	req = req.WithContext(context.WithValue(req.Context(), identityKey{},
		&Identity{UserID: 5, IsAdmin: true}))
	rec := httptest.NewRecorder()
	h.PurgeLogs(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("purge status = %d body=%s", rec.Code, rec.Body.String())
	}

	fake.val.Store(0)
	body, _ := readStats(t, h, "/api/logs/stats", 0)
	if body["cached"] != false || body["total"].(float64) != 0 {
		t.Fatalf("a purge must invalidate the cached aggregate: %#v", body)
	}
}

// TestStatsCacheEvictsOldestScope keeps the map bounded: the key space is
// tenants × tasks, so an unbounded cache would be a slow leak.
func TestStatsCacheEvictsOldestScope(t *testing.T) {
	c := newTTLCache[int](time.Minute)
	c.limit = 2
	for _, key := range []string{"a", "b", "c"} {
		if _, _, _, err := c.get(key, func() (int, error) { return 1, nil }); err != nil {
			t.Fatalf("fill %s: %v", key, err)
		}
	}
	c.mu.Lock()
	size := len(c.entries)
	_, hasA := c.entries["a"]
	c.mu.Unlock()
	if size != 2 || hasA {
		t.Fatalf("cache must stay at its limit and evict the oldest (size=%d hasA=%v)", size, hasA)
	}
}
