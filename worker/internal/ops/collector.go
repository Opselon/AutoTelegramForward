// Package ops is the worker's single operational telemetry owner.
//
// THE INVARIANT (the defect this package closes). Before this package the
// worker had TWO competing metrics owners: the delivery engine held a
// private metricsCounters and the health surface held its own
// health.Tracker. The engine's counters were the ones that actually counted
// deliveries; the tracker's were the ones /healthz reported — so /healthz
// could show delivered=0 on a worker that had delivered 41 messages. Two
// counters for one fact is a lie waiting to happen.
//
// Here there is exactly one: the Collector. It satisfies delivery.Metrics
// (so the engine reports through it) and it IS the source the /healthz,
// /livez, /metrics and /ops endpoints read. No second chain exists.
//
// EVERYTHING here is in-memory: atomics, bounded ring buffers and (for the
// queue/task surfaces) one read-only SQL view over the durable queue. There
// is no per-message DB write and no per-heartbeat DB write — the contract
// forbids both, and the durability cost would be paid on the hot path of
// every delivery.
//
// The collector is concurrency-safe by construction: hot-path recording is
// lock-free atomics plus bounded appends under short mutexes, and the HTTP
// read paths take the same mutexes only for the ring snapshots.
package ops

import (
	"sync"
	"sync/atomic"
	"time"
)

// Collector is the worker's one and only operational telemetry owner.
type Collector struct {
	// Static identity, set once at construction.
	workerID       string
	commitSHA      string
	version        string
	startedAt      time.Time
	numWorkers     int
	queueBackend   string
	maxQueueSize   int
	processingMode string
	switches       Switches

	// --- delivery counters: the /healthz vocabulary --------------------- //
	state     atomic.Uint32
	delivered atomic.Int64
	retried   atomic.Int64
	failed    atomic.Int64
	unknown   atomic.Int64

	// ACTIVE: in-flight deliveries. utilization = active / numWorkers. NOT
	// goroutines: 34 idle goroutines on a worker delivering nothing is
	// exactly the misread the contract forbids.
	active atomic.Int64

	// --- connectivity probes (set by the process's probe goroutine) ----- //
	dbConnected atomic.Bool
	dbCheckedAt atomic.Int64
	tgConnected atomic.Bool
	tgCheckedAt atomic.Int64
	aiConnected atomic.Bool
	aiCheckedAt atomic.Int64

	// --- latency ---------------------------------------------------------- //
	e2e  *latencyRing // end-to-end processing latency
	wait *latencyRing // queue-wait latency (enqueue → claim)

	// --- pipeline stages --------------------------------------------------- //
	stages    map[string]*stageStats
	stageLock sync.RWMutex

	// --- error ring -------------------------------------------------------- //
	errors    *errorRing
	incidents *incidentTracker
	// throughputHistory: the §4 aggregation windows (1m/5m/15m/1h), fed
	// from the delivered counter as a per-tick delta. Bounded by
	// construction: 60 ints for an hour of history, rate-independent.
	throughput *throughputHistory

	// --- CPU: process time-on-CPU, baselined at construction so the
	// reported figure is the average since start (see cpuPercent).
	cpuStart time.Time
	cpuUser  time.Duration
	cpuSys   time.Duration

	// --- derived rates (rate.go): sampled from counters, never written
	// per message. lastHeartbeat is the /ops last_heartbeat.
	jobsRate      *rateSampler
	backlogRate   *rateSampler
	lastHeartbeat atomic.Int64

	// --- per-task rolling stats: throughput per rule over a 1-minute
	// window plus a bounded mean latency. Bounded + TTL'd so a churning
	// task list cannot grow this map forever.
	taskMu  sync.Mutex
	tasks   map[string]*taskStats
	taskTTL time.Duration

	// pendingDepth feeds backlogRate without a DB read on the sampler
	// tick: the claim loop already knows the depth it claimed against.
	pendingDepth atomic.Int64
}

// Switches are the process-level feature switches /ops reports verbatim:
// whatever the deployment flipped, the panel shows — never an opinion about
// what "should" be on.
type Switches struct {
	WorkerEnabled bool
	Transforms    bool
	Autoreply     bool
}

// Config is everything the collector cannot discover on its own. All of it
// is configuration the process already resolved; nothing here is probed.
type Config struct {
	WorkerID       string
	CommitSHA      string
	Version        string
	NumWorkers     int
	MaxQueueSize   int
	QueueBackend   string // label only ("sqlite:/app/data/atf.db"), never a DSN
	ProcessingMode string // "worker" | "legacy"
	Switches       Switches
}

// latencyCapacity bounds a latency ring. 2048 observations is ~17 minutes
// of sustained 2 deliveries/second; percentiles are computed from a sorted
// snapshot, so the ring holds the recent window the operator actually
// cares about rather than a process-lifetime distribution a long-lived
// worker renders meaningless.
const latencyCapacity = 2048

// errorRingCapacity is the contract's 512-entry bound.
const errorRingCapacity = 512

// NewCollector returns a Collector in StateStarting with empty rings.
func NewCollector(cfg Config) *Collector {
	if cfg.NumWorkers < 1 {
		cfg.NumWorkers = 1
	}
	if cfg.MaxQueueSize < 1 {
		cfg.MaxQueueSize = 1
	}
	c := &Collector{
		workerID:       cfg.WorkerID,
		commitSHA:      cfg.CommitSHA,
		version:        cfg.Version,
		startedAt:      time.Now(),
		numWorkers:     cfg.NumWorkers,
		queueBackend:   cfg.QueueBackend,
		maxQueueSize:   cfg.MaxQueueSize,
		processingMode: cfg.ProcessingMode,
		switches:       cfg.Switches,
		e2e:            newLatencyRing(latencyCapacity),
		wait:           newLatencyRing(latencyCapacity),
		stages:         map[string]*stageStats{},
		errors:         newErrorRing(errorRingCapacity),
		incidents:      newIncidentTracker(256),
		throughput:     newThroughputHistory(0),
		cpuStart:       time.Now(),
		jobsRate:       newRateSampler(rateWindow),
		backlogRate:    newRateSampler(rateWindow),
		tasks:          map[string]*taskStats{},
		taskTTL:        10 * time.Minute,
	}
	// Wire the ring into the incident tracker: every deduped ring event
	// becomes an observation the tracker aggregates into an incident. One
	// closure per process — the incident surface costs nothing per
	// successful message and no DB write per failure.
	c.errors.onRecord = c.incidents.observeRing
	// Baseline CPU now so cpuPercent averages over the process lifetime
	// rather than from the first /ops read onward.
	c.cpuUser, c.cpuSys = c.cpuStats()
	for _, name := range StageNames {
		c.stages[name] = newStageStats(name)
	}
	return c
}

// --- identity -------------------------------------------------------------- //

// WorkerID is this process's queue identity.
func (c *Collector) WorkerID() string { return c.workerID }

// CommitSHA is the revision baked in at build time ("" = built without the
// ldflag; the surface reports that instead of inventing a value).
func (c *Collector) CommitSHA() string { return c.commitSHA }

// Version is the product version baked in at build time.
func (c *Collector) Version() string { return c.version }

// StartedAt is when the process started (the /ops started_at field).
func (c *Collector) StartedAt() time.Time { return c.startedAt }

// NumWorkers is the CONFIGURED pool size (never a live-goroutine count).
func (c *Collector) NumWorkers() int { return c.numWorkers }

// MaxQueueSize is the backpressure ceiling (config.MaxQueueSize).
func (c *Collector) MaxQueueSize() int { return c.maxQueueSize }

// QueueBackend is the queue's label — the backend, never the DSN.
func (c *Collector) QueueBackend() string { return c.queueBackend }

// ProcessingMode is "worker" when this process owns the queue, "legacy"
// when the Python Core does.
func (c *Collector) ProcessingMode() string {
	if c.processingMode != "" {
		return c.processingMode
	}
	return "worker"
}

// Switches reports the process-level feature switches.
func (c *Collector) Switches() Switches { return c.switches }

// --- state ----------------------------------------------------------------- //

// SetState records the worker's execution state (starting|ready|degraded|
// stopping).
func (c *Collector) SetState(s uint32) { c.state.Store(s) }

// State returns the last state SetState recorded.
func (c *Collector) State() uint32 { return c.state.Load() }

// Enter marks one delivery as in-flight.
func (c *Collector) Enter() int64 { return c.active.Add(1) }

// Leave marks one delivery as complete.
func (c *Collector) Leave() int64 { return c.active.Add(-1) }

// Active is the current in-flight delivery count.
func (c *Collector) Active() int64 { return c.active.Load() }

// Utilization is active / numWorkers, clamped to [0,1]. NaN-safe by
// construction (numWorkers >= 1 always).
func (c *Collector) Utilization() float64 {
	if c.numWorkers <= 0 {
		return 0
	}
	u := float64(c.active.Load()) / float64(c.numWorkers)
	switch {
	case u < 0:
		return 0
	case u > 1:
		return 1
	}
	return u
}

// --- connectivity ---------------------------------------------------------- //

// RecordDB is called by the DB probe (the same PendingCount call /healthz
// already made). ok reports reachability; at is when the probe ran.
func (c *Collector) RecordDB(ok bool, at time.Time) {
	c.dbConnected.Store(ok)
	c.dbCheckedAt.Store(at.Unix())
}

// RecordTelegram is called by the Telegram gateway probe.
func (c *Collector) RecordTelegram(ok bool, at time.Time) {
	c.tgConnected.Store(ok)
	c.tgCheckedAt.Store(at.Unix())
}

// RecordAI is called with the last provider outcome: the contract's "last
// provider probe ok (9router reachability)".
func (c *Collector) RecordAI(ok bool, at time.Time) {
	c.aiConnected.Store(ok)
	c.aiCheckedAt.Store(at.Unix())
}

// DBConnected reports the last DB probe outcome.
func (c *Collector) DBConnected() bool { return c.dbConnected.Load() }

// TelegramConnected reports the last Telegram probe outcome.
func (c *Collector) TelegramConnected() bool { return c.tgConnected.Load() }

// AIConnected reports the last AI provider outcome.
func (c *Collector) AIConnected() bool { return c.aiConnected.Load() }

// Heartbeat marks the /ops last_heartbeat: any /ops request bumps it, so
// "stale panel data" is distinguishable from "worker alive but nobody is
// looking".
func (c *Collector) Heartbeat() { c.lastHeartbeat.Store(time.Now().Unix()) }

// HeartbeatAge is how long ago the last heartbeat (self or poll) landed.
// /ops serializes it as heartbeat_age_seconds so the console can render
// freshness without time-math on a string timestamp.
func (c *Collector) HeartbeatAge() int64 {
	hb := c.lastHeartbeat.Load()
	if hb <= 0 {
		return -1 // never: distinguish from a fresh heartbeat
	}
	return time.Now().Unix() - hb
}

// SelfHeartbeat is the worker's OWN liveness mark, independent of anyone
// polling /ops. Without it last_heartbeat only advanced when a client
// looked, so a healthy-but-unobserved worker read as offline. The run loop
// ticks this on a fixed cadence; it costs one atomic store, no DB write.
func (c *Collector) SelfHeartbeat() { c.lastHeartbeat.Store(time.Now().Unix()) }

// RecordPendingDepth feeds the backlog sampler from the depth the claim
// loop already measured — no extra DB read, no per-heartbeat write.
func (c *Collector) RecordPendingDepth(n int64) { c.pendingDepth.Store(n) }

// --- the delivery.Metrics surface (the engine's only metrics owner) -------- //

// Outcome bumps a delivery outcome counter (sent|retried|failed|unknown).
// This is the single place a delivery outcome is counted anywhere in the
// worker — the one owner the contract demands.
func (c *Collector) Outcome(kind string) {
	switch kind {
	case "sent":
		c.delivered.Add(1)
	case "retried":
		c.retried.Add(1)
	case "failed":
		c.failed.Add(1)
	case "unknown":
		c.unknown.Add(1)
	}
}

// Delivered is the confirmed-send counter. The old defect was /healthz
// reporting one "delivered" while the engine incremented another; this is
// the one and only read of the one and only counter.
func (c *Collector) Delivered() int64 { return c.delivered.Load() }

// Retried returns the retry counter.
func (c *Collector) Retried() int64 { return c.retried.Load() }

// Failed returns the failure counter.
func (c *Collector) Failed() int64 { return c.failed.Load() }

// Unknown returns the ambiguous-outcome counter.
func (c *Collector) Unknown() int64 { return c.unknown.Load() }

// SetDelivered reconciles the in-memory counter with the durable count.
// The queue read is authoritative whenever the DB is reachable, and
// reconciling in ONE direction (durable ≥ memory) is what keeps a restart
// from resetting the panel's delivered number to zero while the durable
// queue still shows the truth.
func (c *Collector) SetDelivered(n int64) {
	if n > c.delivered.Load() {
		c.delivered.Store(n)
	}
}

// ObserveRPC records a Telegram RPC latency observation. The engine names
// the operation (send_message, copy_message, ...); every named op belongs
// to the delivery stage. Non-positive values are the engine's "the op ran"
// markers (its call sites do not time themselves — the gateway does), so
// they are never latency samples.
func (c *Collector) ObserveRPC(operation string, seconds float64) {
	if seconds <= 0 || seconds != seconds {
		return
	}
	if stage := stageForRPC(operation); stage != "" {
		c.StageRecord(stage, seconds*float64(time.Second/time.Millisecond))
	}
}

// ObserveWait records how long a job waited in the queue before this worker
// processed it (claim time minus enqueue time), in seconds.
func (c *Collector) ObserveWait(seconds float64) {
	if seconds <= 0 || seconds != seconds {
		return
	}
	c.wait.observe(seconds * float64(time.Second/time.Millisecond))
}

// ObserveE2E records one job's end-to-end processing latency in seconds
// (the elapsed time the worker owned the job: claim → outcome).
func (c *Collector) ObserveE2E(seconds float64) {
	if seconds <= 0 || seconds != seconds {
		return
	}
	c.e2e.observe(seconds * float64(time.Second/time.Millisecond))
}

// E2E is the end-to-end processing latency ring (ms).
func (c *Collector) E2E() *latencyRing { return c.e2e }

// QueueWait is the queue-wait latency ring (ms).
func (c *Collector) QueueWait() *latencyRing { return c.wait }

// E2EPercentile is the p-th percentile of the end-to-end window (ms),
// exported so out-of-package tests can read the ring without the
// package's unexported ring type.
func (c *Collector) E2EPercentile(p float64) float64 { return c.e2e.percentile(p) }

// QueueWaitPercentile is the p-th percentile of the queue-wait window (ms).
func (c *Collector) QueueWaitPercentile(p float64) float64 { return c.wait.percentile(p) }

// E2ECount is the number of observations in the end-to-end window.
func (c *Collector) E2ECount() int64 { return c.e2e.countInWindow() }

// QueueWaitCount is the number of observations in the queue-wait window.
func (c *Collector) QueueWaitCount() int64 { return c.wait.countInWindow() }

// Errors is the deduped error ring.
func (c *Collector) Errors() *errorRing { return c.errors }

// Snapshot returns the operational counters for /healthz, /livez and
// /metrics — the shape the existing /healthz contract already served, so a
// monitor that reads /healthz today keeps reading it.
func (c *Collector) Snapshot() map[string]any {
	return map[string]any{
		"state":          StateName(c.State()),
		"uptime_seconds": int(time.Since(c.startedAt).Seconds()),
		"active":         c.Active(),
		"delivered":      c.Delivered(),
		"retried":        c.Retried(),
		"failed":         c.Failed(),
		"unknown":        c.Unknown(),
	}
}

// Stages returns the 11 stage snapshots in the contract's fixed order.
func (c *Collector) Stages() []map[string]any {
	out := make([]map[string]any, 0, len(StageNames))
	for _, name := range StageNames {
		if s := c.stageOf(name); s != nil {
			out = append(out, s.snapshot())
		}
	}
	return out
}

// StageClearFailure clears a stage's last-failure flag — the recovery half
// of the health derivation. A stage that failed and then succeeded reads
// degraded (visible, not hidden); clearing on a success keeps the flag
// from outliving the recovery the operator already sees.
func (c *Collector) StageClearFailure(stage string) {
	if s := c.stageOf(stage); s != nil {
		s.lastFailed.Store(false)
	}
}

func (c *Collector) stageOf(name string) *stageStats {
	c.stageLock.RLock()
	defer c.stageLock.RUnlock()
	return c.stages[name]
}
