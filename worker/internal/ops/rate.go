package ops

import (
	"sync"
	"time"
)

// The /ops surface's derived quantities: jobs_per_second, the backlog
// trend, load_status, and per-task throughput. All are computed from
// SAMPLES of state the collector already owns — never from per-message
// writes, never from per-heartbeat DB reads.
//
// The rate sampler holds a bounded, timestamp-ordered window of (timestamp,
// counter) pairs and answers (last − first) / elapsed. The counter stays
// monotonic and cheap (one atomic add per event); only the sampler pays
// the window cost, once per tick.

// rateWindow is the sliding window a rate is computed over (the contract's
// "1-min rolling").
const rateWindow = time.Minute

// rateSampler holds a bounded, timestamp-ordered window of counter samples.
//
// Mutation is append-mostly under one mutex at the ticker's cadence (1s),
// so contention is not a consideration.
type rateSampler struct {
	mu      sync.Mutex
	samples []sample
	maxAge  time.Duration
}

type sample struct {
	at    time.Time
	value int64
}

func newRateSampler(maxAge time.Duration) *rateSampler {
	return &rateSampler{maxAge: maxAge, samples: make([]sample, 0, 128)}
}

// record appends one (now, value) sample and drops everything older than
// the window. The slice is compacted in place, so at the 1s/1min cadence it
// holds ~60 entries and never grows unboundedly.
func (r *rateSampler) record(value int64, at time.Time) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.samples = append(r.samples, sample{at: at, value: value})
	cutoff := at.Add(-r.maxAge)
	keep := r.samples[:0]
	for _, s := range r.samples {
		if !s.at.Before(cutoff) {
			keep = append(keep, s)
		}
	}
	r.samples = keep
}

// rate returns the per-second rate over the window: (last − first) / the
// time between them. 0 when there are fewer than two samples — the honest
// "no data yet", never a fabricated number. A counter reset (restart) makes
// the difference negative; the rate then reported is the new counter's own
// value over its own span, never a negative spike the panel would render as
// a trough.
func (r *rateSampler) rate(now time.Time) float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.samples) < 2 {
		return 0
	}
	first := r.samples[0]
	last := r.samples[len(r.samples)-1]
	if last.value < first.value {
		if d := now.Sub(last.at).Seconds(); d > 0 {
			return float64(last.value) / d
		}
		return 0
	}
	elapsed := last.at.Sub(first.at).Seconds()
	if elapsed <= 0 {
		return 0
	}
	return float64(last.value-first.value) / elapsed
}

// --- the collector's rate surfaces --------------------------------------- //

// SampleRates records one tick of every rate sampler. Called by the
// process's own ops ticker, so the rates advance even when nobody is
// polling /ops (a worker delivering in the dark still graphs).
func (c *Collector) SampleRates(now time.Time) {
	c.jobsRate.record(c.Delivered(), now)
	c.backlogRate.record(c.pendingDepth.Load(), now)
}

// jobsPerSecond is the 1-minute rolling delivery rate.
func (c *Collector) jobsPerSecond() float64 {
	return c.jobsRate.rate(time.Now())
}

// backlogTrend is the contract's 3-sample window of the pending depth, one
// window apart. All zeros means "no backlog samples yet", not "no backlog".
func (c *Collector) backlogTrend() []int64 {
	c.backlogRate.mu.Lock()
	defer c.backlogRate.mu.Unlock()
	s := c.backlogRate.samples
	if len(s) < 2 {
		return []int64{0, 0, 0}
	}
	// Oldest, middle, newest. Fewer than three samples still returns the
	// shape (padded with the oldest available); [0,0,0] only when never
	// sampled.
	out := make([]int64, 3)
	n := len(s)
	out[0], out[1], out[2] = s[0].value, s[n/2].value, s[n-1].value
	return out
}

// loadStatus is the contract's derivation:
//
//	!db_connected        → unhealthy
//	state == degraded    → degraded
//	utilization < 0.05   → idle
//	utilization < 0.6    → normal
//	utilization < 0.85   → high
//	else                 → saturated
func (c *Collector) loadStatus() string {
	if !c.DBConnected() {
		return "unhealthy"
	}
	if StateName(c.State()) == "degraded" {
		return "degraded"
	}
	switch u := c.Utilization(); {
	case u < 0.05:
		return "idle"
	case u < 0.6:
		return "normal"
	case u < 0.85:
		return "high"
	}
	return "saturated"
}

// --- per-task rolling stats --------------------------------------------- //
//
// The durable half of a task row comes from delivery_jobs; this is the
// "how fast is it going RIGHT NOW" half that must not cost a DB write.

// taskStats holds one task's in-memory rolling throughput and latency.
type taskStats struct {
	delivered *rateSampler
	latency   boundedMean
}

// boundedMean is a bounded arithmetic mean (in ms) over the last N
// observations.
type boundedMean struct {
	mu   sync.Mutex
	vals []float64
	sum  float64
	cap  int
}

func (a *boundedMean) record(v float64) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.vals) >= a.cap {
		a.sum -= a.vals[0]
		a.vals = a.vals[1:]
	}
	a.vals = append(a.vals, v)
	a.sum += v
}

func (a *boundedMean) mean() float64 {
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.vals) == 0 {
		return 0
	}
	return a.sum / float64(len(a.vals))
}

// TaskDelivery records one delivered job for a task: it feeds that task's
// per-minute throughput and mean latency. O(1) per call; the map is TTL'd
// so tasks that disappear stop occupying memory.
func (c *Collector) TaskDelivery(ruleID string, latencyMs float64) {
	if ruleID == "" {
		return
	}
	c.taskMu.Lock()
	defer c.taskMu.Unlock()
	stats, ok := c.tasks[ruleID]
	if !ok {
		stats = &taskStats{
			delivered: newRateSampler(rateWindow),
			latency:   boundedMean{cap: 64},
		}
		c.tasks[ruleID] = stats
	}
	stats.delivered.record(stats.delivered.lastValue()+1, time.Now())
	stats.latency.record(latencyMs)
}

// lastValue is the sampler's newest counter value (0 when empty).
func (r *rateSampler) lastValue() int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.samples) == 0 {
		return 0
	}
	return r.samples[len(r.samples)-1].value
}

// TaskStats returns a task's (throughput_per_min, latency_ms_avg). Both 0
// when the task has never been seen — the honest empty state.
func (c *Collector) TaskStats(ruleID string) (float64, float64) {
	c.taskMu.Lock()
	stats, ok := c.tasks[ruleID]
	c.taskMu.Unlock()
	if !ok {
		return 0, 0
	}
	// per-minute, matching the contract's throughput_per_min.
	return stats.delivered.rate(time.Now()) * 60, stats.latency.mean()
}
