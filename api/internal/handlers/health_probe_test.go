package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// The health endpoint is the dashboard's outage signal, so it has two jobs that
// pull in opposite directions: answer fast, and tell the truth. These tests pin
// both — a hung hop is cut off at the per-check timeout instead of stalling the
// response, and a cached answer is labelled as cached instead of being served
// as if it were live.

// TestProberCutsOffAHungCheck is the regression test for the failure mode that
// made this endpoint the slowest in the stack: one downstream that does not
// answer must cost the per-check timeout and no more.
func TestProberCutsOffAHungCheck(t *testing.T) {
	block := make(chan struct{})
	defer close(block)

	p := newServiceProber([]probeCheck{
		{name: "fast", fn: func(context.Context) (bool, string) { return true, "ok" }},
		{name: "hung", fn: func(context.Context) (bool, string) {
			// Deliberately ignores its context: a real hop can be
			// non-cancellable (a DNS lookup, a wedged client), and the prober
			// must bound it anyway.
			<-block
			return true, "eventually"
		}},
	})
	p.timeout = 60 * time.Millisecond

	start := time.Now()
	results, allOK, probed, _ := p.probe(context.Background())
	elapsed := time.Since(start)

	if !probed {
		t.Fatal("a cold probe must report that it probed")
	}
	if elapsed > 500*time.Millisecond {
		t.Fatalf("a hung check held the probe for %v; the per-check timeout is %v", elapsed, p.timeout)
	}
	if elapsed < p.timeout {
		t.Fatalf("the probe returned in %v, before the %v timeout it must wait out", elapsed, p.timeout)
	}
	if allOK {
		t.Fatal("a timed-out check must not report the stack healthy")
	}
	if len(results) != 2 {
		t.Fatalf("expected one result per check, got %+v", results)
	}
	if results[0].Name != "fast" || !results[0].Online {
		t.Fatalf("the healthy check must keep its place and its verdict: %+v", results)
	}
	if results[1].Name != "hung" || results[1].Online {
		t.Fatalf("the hung check must be reported offline: %+v", results)
	}
	if !strings.Contains(results[1].Detail, "timeout") {
		t.Fatalf("the timeout must be named in the detail, got %q", results[1].Detail)
	}
}

// TestProberCacheServesUntilTTLThenRevalidatesInBackground pins the staleness
// contract: inside the TTL nothing is re-probed, and once the entry is stale the
// caller still gets the last known answer immediately while exactly one refresh
// runs behind it.
func TestProberCacheServesUntilTTLThenRevalidatesInBackground(t *testing.T) {
	var runs atomic.Int64
	online := atomic.Bool{}
	online.Store(true)

	p := newServiceProber([]probeCheck{
		{name: "core", fn: func(context.Context) (bool, string) {
			runs.Add(1)
			return online.Load(), "checked"
		}},
	})
	p.ttl = time.Second
	clock := time.Unix(1_700_000_000, 0)
	var clockMu sync.Mutex
	p.now = func() time.Time {
		clockMu.Lock()
		defer clockMu.Unlock()
		return clock
	}
	advance := func(d time.Duration) {
		clockMu.Lock()
		clock = clock.Add(d)
		clockMu.Unlock()
	}

	// Cold: one round.
	if _, ok, probed, _ := p.probe(context.Background()); !ok || !probed {
		t.Fatal("first probe must be a live round")
	}
	if got := runs.Load(); got != 1 {
		t.Fatalf("first probe ran the check %d times, want 1", got)
	}

	// Warm, inside the TTL: no new work at all.
	advance(500 * time.Millisecond)
	if _, _, probed, age := p.probe(context.Background()); probed || age != 500*time.Millisecond {
		t.Fatalf("a cache hit inside the TTL must not re-probe and must carry its real age, got probed=%v age=%v", probed, age)
	}
	if got := runs.Load(); got != 1 {
		t.Fatalf("a cache hit re-probed the downstream (%d runs)", got)
	}

	// Past the TTL: the stale snapshot is served now, and one refresh runs.
	online.Store(false) // the downstream went down while it was cached
	advance(1500 * time.Millisecond)
	results, allOK, probed, age := p.probe(context.Background())
	if probed {
		t.Fatal("an entry older than the TTL is served from cache; the refresh runs behind the caller")
	}
	if !allOK || len(results) != 1 || !results[0].Online {
		t.Fatalf("the stale snapshot must be served as it was taken (healthy), got %+v", results)
	}
	// Taken at t0, read at t0+2s (500ms TTL hit + 1.5s past the TTL).
	if age != 2*time.Second {
		t.Fatalf("age of a stale entry = %v, want 2s", age)
	}
	p.waitForRefresh()
	if got := runs.Load(); got != 2 {
		t.Fatalf("the stale read must trigger exactly one background refresh, %d runs", got)
	}

	// The refresh landed: the next read sees the new truth, fresh.
	if _, allOK, probed, _ := p.probe(context.Background()); probed || allOK {
		t.Fatalf("after the background refresh the outage must be visible: probed=%v allOK=%v", probed, allOK)
	}
}

// TestProberSingleFlight: a cold cache hit by N concurrent requests must run one
// probe round, not N. Without this the cache would merely move the fan-out from
// "per request" to "per request when a burst arrives together".
func TestProberSingleFlight(t *testing.T) {
	var runs atomic.Int64
	release := make(chan struct{})
	p := newServiceProber([]probeCheck{
		{name: "core", fn: func(context.Context) (bool, string) {
			runs.Add(1)
			<-release
			return true, "ok"
		}},
	})

	var wg sync.WaitGroup
	got := make([]bool, 8)
	for i := 0; i < len(got); i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, ok, _, _ := p.probe(context.Background())
			got[i] = ok
		}(i)
	}
	// Let every caller arrive before the probe can finish.
	time.Sleep(50 * time.Millisecond)
	close(release)
	wg.Wait()

	if n := runs.Load(); n != 1 {
		t.Fatalf("8 concurrent cold requests ran %d probe rounds, want 1", n)
	}
	for i, ok := range got {
		if !ok {
			t.Fatalf("caller %d got no snapshot", i)
		}
	}
}

// TestServicesHealthEnvelope: the endpoint must keep its status/services shape
// (clients parse it) and add the cache metadata the dashboard needs to avoid
// presenting a stale answer as live.
func TestServicesHealthEnvelope(t *testing.T) {
	h := New(nil)
	h.probes = newServiceProber([]probeCheck{
		{name: "core", fn: func(context.Context) (bool, string) { return true, "uptime=1s" }},
	})
	h.probes.ttl = time.Minute

	read := func() map[string]any {
		rec := httptest.NewRecorder()
		h.ServicesHealth(rec, httptest.NewRequest(http.MethodGet, "/api/v1/health/services", nil))
		if rec.Code != http.StatusOK {
			t.Fatalf("status = %d", rec.Code)
		}
		var payload map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		return payload
	}

	first := read()
	if first["status"] != "healthy" {
		t.Fatalf("status = %v", first["status"])
	}
	if first["cached"] != false {
		t.Fatalf("the first answer is probed live, cached = %v", first["cached"])
	}
	services, ok := first["services"].([]any)
	if !ok || len(services) != 1 {
		t.Fatalf("services = %#v", first["services"])
	}
	row, _ := services[0].(map[string]any)
	for _, key := range []string{"name", "online", "detail", "latency_ms"} {
		if _, present := row[key]; !present {
			t.Fatalf("service row lost its %q field: %#v", key, row)
		}
	}

	second := read()
	if second["cached"] != true {
		t.Fatalf("a second read inside the TTL must be labelled cached: %#v", second)
	}
	if _, present := second["age_ms"]; !present {
		t.Fatalf("a cached answer must report its age: %#v", second)
	}
}
