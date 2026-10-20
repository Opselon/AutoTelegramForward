package handlers

// Service reachability for the topology view — and the reason this file exists.
//
// The old implementation was *concurrent* (four goroutines) and still answered
// in ~52ms warm, because of how it waited: it polled `len(results) < 4` with
// `time.After(50 * time.Millisecond)` and a 4.5s outer deadline. Every probe
// finished in well under a millisecond, and every request then slept until the
// next 50ms tick — the endpoint's latency was the polling interval, not the
// work. Measured: p50 51.94ms, p95 54.67ms, 19 req/s, the worst endpoint in the
// stack by two orders of magnitude.
//
// What replaces it:
//
//   * no polling — a WaitGroup, so the answer is ready as soon as the last
//     probe is;
//   * a per-check timeout enforced by *racing* the check against its context,
//     so one hung hop cannot hold the response (the old code's 4s per check
//     plus a 4.5s outer loop meant a single dead service stalled the endpoint
//     for seconds);
//   * a short TTL cache with stale-while-revalidate. A warm request is a
//     mutex + a slice copy; when the entry is stale the caller still gets the
//     last known answer immediately and one background refresh updates it. The
//     dashboard polls this endpoint, so the cache is also what keeps a slow
//     downstream from turning into a slow UI;
//   * single-flight: N concurrent requests arriving on a cold/stale entry
//     trigger one probe round, not N.
//
// The cache is deliberately short (2s): the endpoint answers "is it up right
// now", and a stale-forever cache would make an outage invisible.

import (
	"context"
	"os"
	"strconv"
	"sync"
	"time"
)

const (
	// defaultProbeTTL is how long a snapshot is served without re-probing.
	// Two seconds is shorter than a human's patience and longer than the UI's
	// poll interval, so the topology view stays live while staying cheap.
	defaultProbeTTL = 2 * time.Second
	// defaultProbeTimeout bounds ONE downstream check. A healthy hop answers in
	// single-digit milliseconds; 1.5s is a wide margin that still keeps the
	// worst case (every hop hung) inside a browser's request budget.
	defaultProbeTimeout = 1500 * time.Millisecond
)

// probeCheck is one downstream reachability check.
//
// fn receives a context whose deadline is the per-check timeout: a check that
// honours it returns early with a real error, and one that does not is raced
// against it. Either way the probe round is bounded.
type probeCheck struct {
	name string
	fn   func(ctx context.Context) (bool, string)
}

// serviceProber runs the checks, caches the snapshot, and collapses concurrent
// refreshes into one round.
type serviceProber struct {
	checks  []probeCheck
	ttl     time.Duration
	timeout time.Duration
	// now is injectable so a test can age the snapshot without sleeping.
	now func() time.Time

	mu       sync.Mutex
	snapshot []ServiceProbe
	takenAt  time.Time
	allOK    bool
	inflight *probeCall
}

// probeCall is the single in-flight refresh, used for single-flight and for
// tests that need to wait for it.
type probeCall struct{ done chan struct{} }

func newServiceProber(checks []probeCheck) *serviceProber {
	p := &serviceProber{
		checks:  checks,
		ttl:     defaultProbeTTL,
		timeout: defaultProbeTimeout,
		now:     time.Now,
	}
	if raw := os.Getenv("ATF_PROBE_TIMEOUT_MS"); raw != "" {
		if ms, err := strconv.Atoi(raw); err == nil && ms > 0 && ms <= 10000 {
			p.timeout = time.Duration(ms) * time.Millisecond
		}
	}
	if raw := os.Getenv("ATF_PROBE_TTL_MS"); raw != "" {
		// A deployment that wants every request to re-probe sets 0.
		if ms, err := strconv.Atoi(raw); err == nil && ms >= 0 && ms <= 60000 {
			p.ttl = time.Duration(ms) * time.Millisecond
		}
	}
	return p
}

// probeResult is what one check produced, before it is folded into a snapshot.
type probeResult struct {
	online bool
	detail string
	took   time.Duration
}

// probe returns the current snapshot.
//
// probed == true only when THIS call ran the probe round; a cache hit (warm or
// stale-while-revalidate) returns false plus the snapshot's age, so the response
// can say "this is the answer as of 1.4s ago" instead of presenting it as live.
func (p *serviceProber) probe(ctx context.Context) (probes []ServiceProbe, allOK bool, probed bool, age time.Duration) {
	p.mu.Lock()
	if p.snapshot != nil && p.now().Sub(p.takenAt) < p.ttl {
		out, ok := cloneProbes(p.snapshot), p.allOK
		age = p.now().Sub(p.takenAt)
		p.mu.Unlock()
		return out, ok, false, age
	}
	if p.snapshot != nil {
		// Stale but present: answer now, refresh behind the caller. This is what
		// keeps a degraded stack from making the health endpoint itself slow —
		// the exact failure that made it look like the API was the outage.
		age = p.now().Sub(p.takenAt)
		out, ok := cloneProbes(p.snapshot), p.allOK
		p.startRefreshLocked(ctx)
		p.mu.Unlock()
		return out, ok, false, age
	}
	// No snapshot yet: the first request pays for one round, bounded by the
	// checks' own timeouts.
	call := p.startRefreshLocked(ctx)
	p.mu.Unlock()

	select {
	case <-call.done:
	case <-ctx.Done():
		// The client went away; the refresh keeps running and warms the cache
		// for the next caller.
		return nil, false, false, 0
	}

	p.mu.Lock()
	defer p.mu.Unlock()
	return cloneProbes(p.snapshot), p.allOK, true, p.now().Sub(p.takenAt)
}

// startRefreshLocked begins a refresh unless one is already running, and
// returns the running call. The caller must hold p.mu.
func (p *serviceProber) startRefreshLocked(parent context.Context) *probeCall {
	if p.inflight != nil {
		return p.inflight
	}
	call := &probeCall{done: make(chan struct{})}
	p.inflight = call
	// parent's cancellation must not abort a refresh other callers will read;
	// detach, and rely on the per-check timeouts for the bound.
	_ = parent
	go func() {
		results := p.runChecks(context.Background())
		p.mu.Lock()
		p.snapshot = results
		p.takenAt = p.now()
		p.allOK = true
		for _, r := range results {
			if !r.Online {
				p.allOK = false
			}
		}
		p.inflight = nil
		p.mu.Unlock()
		close(call.done)
	}()
	return call
}

// runChecks runs every check concurrently and returns the results in the order
// the checks were declared (core → logger → api → web), so the UI's row order
// does not depend on which service answered first.
func (p *serviceProber) runChecks(parent context.Context) []ServiceProbe {
	out := make([]ServiceProbe, len(p.checks))
	var wg sync.WaitGroup
	wg.Add(len(p.checks))
	for i, check := range p.checks {
		go func(i int, check probeCheck) {
			defer wg.Done()
			result := p.runOne(parent, check)
			out[i] = ServiceProbe{
				Name:      check.name,
				Online:    result.online,
				Detail:    result.detail,
				LatencyMs: result.took.Milliseconds(),
			}
		}(i, check)
	}
	wg.Wait()
	return out
}

// runOne bounds one check. The check runs in its own goroutine so a hop that
// ignores its context (a gRPC call that is not cancellable, an HTTP client
// without a timeout) still cannot hold the round: the select returns at the
// deadline and the goroutine is abandoned. The result channel is buffered so
// the abandoned goroutine can finish without leaking.
func (p *serviceProber) runOne(parent context.Context, check probeCheck) probeResult {
	start := p.now()
	ctx, cancel := context.WithTimeout(parent, p.timeout)
	defer cancel()

	type outcome struct{ ok bool; detail string }
	done := make(chan outcome, 1)
	go func() {
		ok, detail := false, ""
		defer func() {
			// A panicking probe is a broken probe, not a broken API.
			if rec := recover(); rec != nil {
				done <- outcome{false, "probe panicked"}
			}
		}()
		ok, detail = check.fn(ctx)
		done <- outcome{ok, detail}
	}()

	took := func() time.Duration { return p.now().Sub(start) }
	select {
	case res := <-done:
		return probeResult{online: res.ok, detail: res.detail, took: took()}
	case <-ctx.Done():
		return probeResult{
			online: false,
			detail: "probe timeout (" + p.timeout.String() + ")",
			took:   took(),
		}
	}
}

// waitForRefresh blocks until no refresh is in flight. Tests use it to read a
// snapshot the stale path refreshed in the background.
func (p *serviceProber) waitForRefresh() {
	p.mu.Lock()
	call := p.inflight
	p.mu.Unlock()
	if call != nil {
		<-call.done
	}
}

func cloneProbes(in []ServiceProbe) []ServiceProbe {
	if in == nil {
		return nil
	}
	out := make([]ServiceProbe, len(in))
	copy(out, in)
	return out
}
