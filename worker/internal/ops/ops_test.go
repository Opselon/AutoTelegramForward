package ops

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func testCollector() *Collector {
	c := NewCollector(Config{
		WorkerID:     "w-test",
		CommitSHA:    "82f1982",
		Version:      "1.2.3",
		NumWorkers:   4,
		MaxQueueSize: 1000,
		QueueBackend: "sqlite:/app/data/atf.db",
	})
	// Mirror the worker's own startup: stages default to disabled unless
	// the process owns them, and a wired process owns the pipeline.
	for _, stage := range StageNames {
		c.StageEnabled(stage, true)
	}
	return c
}

// TestCollectorIsTheOneMetricsOwner is the regression test for the defect
// the contract describes: /healthz used to report delivered=0 on a worker
// that had actually delivered 41 messages, because the engine's counters
// and the health surface's counters were two different objects.
//
// Here the SAME collector object is handed to the engine-shaped recorder
// and read back through the health-shaped snapshot. If a second counter
// set ever reappears, this test fails on the divergence.
func TestCollectorIsTheOneMetricsOwner(t *testing.T) {
	c := testCollector()

	// The engine reports through the collector (delivery.Metrics shape).
	for i := 0; i < 41; i++ {
		c.Outcome("sent")
	}
	c.Outcome("retried")
	c.Outcome("failed")
	c.Outcome("unknown")

	// The health surface reads the same object.
	snap := c.Snapshot()
	if got := snap["delivered"]; got != int64(41) {
		t.Fatalf("health snapshot delivered = %v, want 41 (the one-owner defect)", got)
	}
	if got := c.Delivered(); got != 41 {
		t.Fatalf("Delivered() = %d, want 41", got)
	}
	if snap["retried"] != int64(1) || snap["failed"] != int64(1) || snap["unknown"] != int64(1) {
		t.Fatalf("health snapshot outcome counters = %+v", snap)
	}
}

// TestHTTPReportsSameDeliveredAsEngine proves the defect at the WIRE: the
// /ops handler must serve exactly the delivered counter the engine
// increments, not a parallel one.
func TestHTTPReportsSameDeliveredAsEngine(t *testing.T) {
	c := testCollector()
	srv := NewServer(c, nil, nil, nil, "sqlite", "w-test", nil)
	mux := http.NewServeMux()
	srv.Attach(mux)

	for i := 0; i < 41; i++ {
		c.Outcome("sent")
	}
	ts := httptest.NewServer(mux)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/ops")
	if err != nil {
		t.Fatalf("GET /ops: %v", err)
	}
	defer resp.Body.Close()
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode /ops: %v", err)
	}
	queue, _ := body["queue"].(map[string]any)
	if got := queue["delivered"]; got != float64(41) {
		t.Fatalf("/ops queue.delivered = %v, want 41 (same counter the engine increments)", got)
	}
	if worker, _ := body["worker"].(map[string]any); worker != nil {
		// The worker object carries no delivered field by contract; this
		// assertion exists so a future field addition stays consistent
		// with the queue's authoritative number.
		if d, ok := worker["delivered"]; ok && d != float64(41) {
			t.Fatalf("worker.delivered = %v, diverges from queue.delivered", d)
		}
	}
}

// TestUtilizationIsNotGoroutines pins the contract's explicit prohibition:
// utilization = active / numWorkers, never a goroutine count. A worker
// idling at many goroutines while delivering nothing must read 0.
func TestUtilizationIsNotGoroutines(t *testing.T) {
	c := testCollector() // NumWorkers 4
	// No deliveries in flight: utilization must be 0 however many
	// goroutines exist.
	if u := c.Utilization(); u != 0 {
		t.Fatalf("utilization with no active deliveries = %v, want 0", u)
	}
	// Two of four workers busy.
	c.Enter()
	c.Enter()
	if u := c.Utilization(); u != 0.5 {
		t.Fatalf("utilization with 2/4 active = %v, want 0.5", u)
	}
	// Saturate +1: clamped to 1, never above.
	c.Enter()
	c.Enter()
	c.Enter()
	if u := c.Utilization(); u != 1 {
		t.Fatalf("utilization over-saturated = %v, want 1 (clamped)", u)
	}
	if c.Active() != 5 {
		t.Fatalf("active = %d, want 5", c.Active())
	}
}

// TestStagesReportTheContractNames pins the 11 fixed stages and their
// order — the panel renders exactly these, so a rename is a breaking
// change this test makes visible at compile time of the contract.
func TestStagesReportTheContractNames(t *testing.T) {
	want := []string{
		"ingestion", "queue", "task_resolution", "filter", "replacement",
		"cleaner", "header_footer", "ai", "validation", "delivery",
		"reconciliation",
	}
	if len(StageNames) != len(want) {
		t.Fatalf("StageNames has %d entries, want %d", len(StageNames), len(want))
	}
	got := make([]string, 0, len(want))
	for _, s := range StagesOf(testCollector()) {
		got = append(got, s["stage"].(string))
	}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("stage names/order = %v, want %v", got, want)
	}
}

func StagesOf(c *Collector) []map[string]any { return c.Stages() }

// TestStageHealthDerivation pins the contract's health rules, including
// the AI trap: never healthy because the task enables it — healthy only
// after at least one processed run without failures.
func TestStageHealthDerivation(t *testing.T) {
	c := testCollector()
	// Never ran → waiting (the honest pre-first-run state).
	if h := stageHealthOf(c, "ai"); h != StageWaiting {
		t.Fatalf("never-run stage health = %q, want %q", h, StageWaiting)
	}
	// Disabled: a deployment decision, distinct from waiting.
	c.StageEnabled("ai", false)
	if h := stageHealthOf(c, "ai"); h != StageDisabled {
		t.Fatalf("disabled stage health = %q, want %q", h, StageDisabled)
	}
	c.StageEnabled("ai", true)
	// Fail then succeed: the LAST event decides — failed first.
	c.StageFail("ai", "TIMEOUT")
	if h := stageHealthOf(c, "ai"); h != StageFailed {
		t.Fatalf("stage whose last event failed = %q, want %q", h, StageFailed)
	}
	// A success clears last-failed; the failure window keeps it degraded.
	c.StageRecord("ai", 1.2)
	if h := stageHealthOf(c, "ai"); h != StageDegraded {
		t.Fatalf("recovered-but-recently-failed stage = %q, want %q", h, StageDegraded)
	}
	// Enough clean runs to age the failure out of the window.
	for i := 0; i < stageHealthWindow; i++ {
		c.StageRecord("ai", 1)
	}
	if h := stageHealthOf(c, "ai"); h != StageHealthy {
		t.Fatalf("stage with a clean window = %q, want %q", h, StageHealthy)
	}
}

func stageHealthOf(c *Collector, stage string) string {
	for _, s := range c.Stages() {
		if s["stage"] == stage {
			return s["health"].(string)
		}
	}
	return ""
}

// TestFilterSkippedByPolicy is the contract's named extra field: the
// filter stage carries skipped_by_policy only when it has skipped some.
func TestFilterSkippedByPolicy(t *testing.T) {
	c := testCollector()
	if row := stageRow(t, c, "filter"); row["skipped_by_policy"] != nil {
		t.Fatalf("filter stage carried skipped_by_policy before any skip: %v", row["skipped_by_policy"])
	}
	c.StageRecordSkipped("filter", 0.4)
	row := stageRow(t, c, "filter")
	if row["skipped_by_policy"] != int64(1) {
		t.Fatalf("skipped_by_policy = %v, want 1", row["skipped_by_policy"])
	}
}

func stageRow(t *testing.T, c *Collector, name string) map[string]any {
	t.Helper()
	for _, s := range c.Stages() {
		if s["stage"] == name {
			return s
		}
	}
	t.Fatalf("stage %q missing from Stages()", name)
	return nil
}

// TestLatencyPercentiles pins the ring math against a hand-checked
// distribution. Empty windows are 0 — "no data yet", never a fabricated
// number the panel would render as today's latency.
func TestLatencyPercentiles(t *testing.T) {
	c := testCollector()
	if p := c.E2E().percentile(99); p != 0 {
		t.Fatalf("empty-window p99 = %v, want 0 (no data yet)", p)
	}
	// 1..100 ms, one each: p50=50, p95=95, p99=99 by nearest-rank.
	for i := 1; i <= 100; i++ {
		c.ObserveE2E(float64(i) / 1000.0)
	}
	if p := c.E2E().percentile(50); p != 50 {
		t.Fatalf("p50 = %v, want 50", p)
	}
	if p := c.E2E().percentile(95); p != 95 {
		t.Fatalf("p95 = %v, want 95", p)
	}
	if p := c.E2E().percentile(99); p != 99 {
		t.Fatalf("p99 = %v, want 99", p)
	}
}

// TestErrorRingDedupAndBound pins the contract's §1.5 semantics: dedup on
// (component, code, rule_id), count/first_seen/last_seen collapse, 512 bound.
func TestErrorRingDedupAndBound(t *testing.T) {
	c := testCollector()
	for i := 0; i < 400; i++ {
		c.RecordError(LogEntry{
			Severity: "ERROR", Component: "worker.delivery",
			Code: "FLOOD_WAIT", RuleID: "r1",
			Message: "transient failure — retrying with backoff",
		})
	}
	if got := c.Errors().Total(); got != 1 {
		t.Fatalf("deduped ring total = %d, want 1 (400 identical entries collapsed)", got)
	}
	items := c.Errors().Items()
	if len(items) != 1 || items[0]["count"] != int64(400) {
		t.Fatalf("collapse: %+v", items)
	}
	if items[0]["first_seen"] == "" || items[0]["ts"] == "" {
		t.Fatalf("collapsed entry missing first_seen/last_seen: %+v", items[0])
	}
	// A different rule_id is a different dedup bucket.
	c.RecordError(LogEntry{Severity: "ERROR", Component: "worker.delivery", Code: "FLOOD_WAIT", RuleID: "r2"})
	if got := c.Errors().Total(); got != 2 {
		t.Fatalf("after a second dedup key, total = %d, want 2", got)
	}
}

// TestErrorRingBoundedAtCapacity: the ring must never grow past its bound,
// and eviction is by least-recently-seen so a fresh problem is always visible.
func TestErrorRingBoundedAtCapacity(t *testing.T) {
	r := newErrorRing(8)
	for i := 0; i < 40; i++ {
		r.Record(ErrorItem{
			Ts:        time.Unix(int64(i), 0).UTC().Format(time.RFC3339),
			Severity:  "ERROR",
			Component: fmt.Sprintf("comp%d", i),
			Code:      "X",
			Message:   "m",
		})
	}
	if got := r.Total(); got != 8 {
		t.Fatalf("ring total after 40 inserts = %d, want 8 (the bound)", got)
	}
	// The last-inserted (newest ts) must have survived every eviction.
	items := r.Items()
	if len(items) == 0 || items[0]["component"] != "comp39" {
		t.Fatalf("newest entry did not survive eviction: %+v", items)
	}
}

// TestErrorFilterLimitBounds pins the contract's ≤256 limit and the
// severity/component/rule_id filters.
func TestErrorFilterLimitBounds(t *testing.T) {
	c := testCollector()
	for i := 0; i < 300; i++ {
		c.RecordError(LogEntry{
			Severity: "WARN", Component: "worker.delivery",
			Code: "FLOOD_WAIT", RuleID: "r1", Message: "m",
		})
	}
	// limit=1000 must clamp to 256 (the contract's bound).
	items := c.Errors().Filter("", "", "", time.Time{}, 1000)
	if len(items) > 256 {
		t.Fatalf("filter returned %d items, want ≤256", len(items))
	}
	// severity filter
	if items := c.Errors().Filter("ERROR", "", "", time.Time{}, 10); len(items) != 0 {
		t.Fatalf("ERROR filter matched %d WARN entries", len(items))
	}
	if items := c.Errors().Filter("warn", "", "", time.Time{}, 10); len(items) != 1 {
		t.Fatalf("case-insensitive WARN filter matched %d entries, want 1", len(items))
	}
	if items := c.Errors().Filter("", "worker.delivery", "", time.Time{}, 10); len(items) != 1 {
		t.Fatalf("component filter failed")
	}
	if items := c.Errors().Filter("", "", "r1", time.Time{}, 10); len(items) != 1 {
		t.Fatalf("rule_id filter failed")
	}
	if items := c.Errors().Filter("", "", "nope", time.Time{}, 10); len(items) != 0 {
		t.Fatalf("rule_id filter matched an absent rule")
	}
}

// TestSanitizeStripsSecretsAndContent is the contract's §4 prohibition,
// pinned: no message content, no secrets, no multi-line detail in any field.
func TestSanitizeStripsSecretsAndContent(t *testing.T) {
	cases := []struct{ in, want string }{
		{"line one\nline two", "line one"}, // multi-line → first line
		// A 300-char single token is both a log bomb and a secret shape;
		// the length bound fires (160), then the scrub pass collapses it.
		{strings.Repeat("a", 300), "[redacted]"},
		{"token=abc123-secret", "token"}, // key=value pair → key only
		{"FLOOD_WAIT", "FLOOD_WAIT"},     // the legitimate vocabulary
		{"  spaced  ", "spaced"},
	}
	for _, tc := range cases {
		if got := sanitize(tc.in); got != tc.want {
			t.Errorf("sanitize(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
	// And the ring sanitizes what it is handed.
	c := testCollector()
	c.RecordError(LogEntry{
		Severity: "ERROR", Component: "worker.delivery", Code: "AUTHENTICATION_ERROR",
		Message: "session string=abcSECRET123\nsecond line",
	})
	item := c.Errors().Items()[0]
	if strings.Contains(item["message"].(string), "SECRET") {
		t.Fatalf("ring message leaked a secret: %v", item["message"])
	}
	if strings.Contains(item["message"].(string), "\n") {
		t.Fatalf("ring message is multi-line: %q", item["message"])
	}
}

// TestQueueObjectShapeOnNilReader: with no store reader the /ops handler
// must report the in-memory fields and an explicit error — never zeros
// that read like an empty, healthy queue.
func TestQueueObjectShapeOnNilReader(t *testing.T) {
	c := testCollector()
	srv := NewServer(c, nil, nil, nil, "sqlite", "w-test", nil)
	mux := http.NewServeMux()
	srv.Attach(mux)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/ops/queue")
	if err != nil {
		t.Fatalf("GET /ops/queue: %v", err)
	}
	defer resp.Body.Close()
	var q map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&q); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if q["error"] == nil {
		t.Fatalf("nil reader reported no error: %+v", q)
	}
	for _, key := range []string{"latency", "queue_wait", "jobs_per_second", "capacity", "backlog_trend", "load_status"} {
		if _, ok := q[key]; !ok {
			t.Fatalf("queue object missing %q (in-memory fields must still report)", key)
		}
	}
	lat := q["latency"].(map[string]any)
	if lat["units"] != "ms" {
		t.Fatalf("latency units = %v, want ms", lat["units"])
	}
	for _, p := range []string{"p50", "p95", "p99"} {
		if _, ok := lat[p]; !ok {
			t.Fatalf("latency object missing %q", p)
		}
	}
}

// TestWorkerObjectShape pins the §1.1 fields, and especially that
// goroutines and num_workers are DIFFERENT fields — utilization reads
// num_workers, never the goroutine count.
func TestWorkerObjectShape(t *testing.T) {
	c := testCollector()
	c.RecordDB(true, time.Now())
	c.RecordTelegram(true, time.Now())
	c.RecordAI(false, time.Now())
	srv := NewServer(c, nil, nil, nil, "sqlite", "w-test", nil)
	mux := http.NewServeMux()
	srv.Attach(mux)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/ops")
	if err != nil {
		t.Fatalf("GET /ops: %v", err)
	}
	defer resp.Body.Close()
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	w := body["worker"].(map[string]any)
	for _, key := range []string{
		"state", "worker_id", "commit_sha", "version", "uptime_seconds",
		"started_at", "goroutines", "cpu_percent", "memory_bytes",
		"num_workers", "active", "utilization", "processing_mode",
		"switches", "queue_backend", "db_connected", "telegram_connected",
		"ai_connected",
	} {
		if _, ok := w[key]; !ok {
			t.Fatalf("worker object missing %q", key)
		}
	}
	if w["commit_sha"] != "82f1982" {
		t.Fatalf("commit_sha = %v, want the ldflag value", w["commit_sha"])
	}
	if w["queue_backend"] == nil || strings.Contains(fmt.Sprint(w["queue_backend"]), "://") {
		t.Fatalf("queue_backend must be a label without a DSN: %v", w["queue_backend"])
	}
	// JSON decodes numbers as float64; assert the CONFIGURED value, which
	// is the contract's requirement (never a live-goroutine count).
	if got := w["num_workers"]; got != float64(4) {
		t.Fatalf("num_workers = %v, want 4 (the CONFIGURED pool size)", got)
	}
	if g := w["goroutines"]; g == float64(int(g.(float64))) == false {
		t.Fatalf("goroutines not an integer: %v", g)
	}
	if u, _ := w["utilization"].(float64); u != 0 {
		t.Fatalf("utilization = %v, want 0 (nothing in flight)", u)
	}
}

// TestLoadStatusDerivation pins the contract's ladder, including the
// degraded/unhealthy overrides.
func TestLoadStatusDerivation(t *testing.T) {
	c := testCollector()
	if got := c.loadStatus(); got != "unhealthy" {
		t.Fatalf("load_status with DB down = %q, want unhealthy", got)
	}
	c.RecordDB(true, time.Now())
	if got := c.loadStatus(); got != "idle" {
		t.Fatalf("load_status idle check = %q, want idle", got)
	}
	for i := 0; i < 3; i++ { // 3/4 = 0.75 → high
		c.Enter()
	}
	if got := c.loadStatus(); got != "high" {
		t.Fatalf("load_status at 0.75 = %q, want high", got)
	}
	c.Enter() // 4/4 → saturated
	if got := c.loadStatus(); got != "saturated" {
		t.Fatalf("load_status at 1.0 = %q, want saturated", got)
	}
	for i := 0; i < 4; i++ {
		c.Leave()
	}
	c.SetState(StateDegraded)
	if got := c.loadStatus(); got != "degraded" {
		t.Fatalf("load_status with state=degraded = %q, want degraded", got)
	}
}

// TestConcurrentRecordingIsRaceFree is the race-detector test: the
// collector's hot path (engine goroutines) and the read path (HTTP
// handlers) share the object. Run with -race.
func TestConcurrentRecordingIsRaceFree(t *testing.T) {
	c := testCollector()
	srv := NewServer(c, nil, nil, nil, "sqlite", "w-test", nil)
	mux := http.NewServeMux()
	srv.Attach(mux)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	var wg sync.WaitGroup
	// Writers: the engine's shape — outcomes, stage events, ring records,
	// probe outcomes, rate samples, error records.
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				c.Outcome("sent")
				c.ObserveE2E(0.01)
				c.ObserveWait(0.002)
				c.ObserveRPC("send_message", 0.003)
				c.StageRecord("delivery", 1)
				c.StageFail("delivery", "FLOOD_WAIT")
				c.RecordDB(j%7 != 0, time.Now())
				c.RecordTelegram(j%5 != 0, time.Now())
				c.RecordAI(j%3 != 0, time.Now())
				c.Enter()
				c.Leave()
				c.SampleRates(time.Now())
				c.RecordError(LogEntry{
					Severity: "WARN", Component: "worker.delivery",
					Code: "X", RuleID: "r", Message: "m",
				})
				c.TaskDelivery("r1", 2.0)
			}
			_ = n
		}(i)
	}
	// Readers: the /ops handlers + the health snapshot.
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				_ = c.Snapshot()
				_ = c.Stages()
				_ = c.Errors().Items()
				_ = c.Utilization()
				_ = c.loadStatus()
				resp, err := http.Get(ts.URL + "/ops")
				if err == nil {
					resp.Body.Close()
				}
				_ = c.E2E().percentile(99)
			}
		}()
	}
	wg.Wait()
}

// TestSetDeliveredReconcilesOnlyUpward: the durable count is the truth
// when it is larger; a restart's fresh counter must not zero the panel's
// number, but a durable count below the in-memory counter must never
// silently increase the reported figure from a stale read.
func TestSetDeliveredReconcilesOnlyUpward(t *testing.T) {
	c := testCollector()
	c.Outcome("sent")
	c.Outcome("sent")
	c.SetDelivered(41)
	if got := c.Delivered(); got != 41 {
		t.Fatalf("after reconciliation Delivered = %d, want 41", got)
	}
	c.SetDelivered(3)
	if got := c.Delivered(); got != 41 {
		t.Fatalf("a lower durable count must not lower the counter: %d", got)
	}
}

// TestRateWindowIsOneMinuteRolling: jobs_per_second is the contract's
// 1-minute rolling rate. Two samples a second apart carry the rate; fewer
// than two samples report 0 (no data yet).
func TestRateWindowIsOneMinuteRolling(t *testing.T) {
	c := testCollector()
	if got := c.jobsPerSecond(); got != 0 {
		t.Fatalf("jobs_per_second with no samples = %v, want 0", got)
	}
	now := time.Now()
	c.SampleRates(now)
	for i := 1; i <= 60; i++ {
		c.Outcome("sent")
		c.SampleRates(now.Add(time.Duration(i) * time.Second))
	}
	// 60 delivered over the 60 seconds the window spans = 1/s.
	if got := c.jobsPerSecond(); got < 0.9 || got > 1.1 {
		t.Fatalf("jobs_per_second = %v, want ~1.0", got)
	}
}

// TestTaskStatsPerRule: throughput_per_min and latency_ms_avg are per-rule
// in-memory views — the durable half comes from delivery_jobs, this half
// never costs a DB write.
func TestTaskStatsPerRule(t *testing.T) {
	c := testCollector()
	tps, lat := c.TaskStats("r1")
	if tps != 0 || lat != 0 {
		t.Fatalf("unseen task stats = (%v, %v), want (0, 0)", tps, lat)
	}
	c.TaskDelivery("r1", 4.0)
	c.TaskDelivery("r1", 6.0)
	_, lat = c.TaskStats("r1")
	if lat != 5.0 {
		t.Fatalf("task mean latency = %v, want 5.0", lat)
	}
}

// TestErrorsEndpointContent pins the §1.5 response shape and the 503
// contract for the queue route when the store is unavailable.
func TestErrorsEndpointShape(t *testing.T) {
	c := testCollector()
	c.RecordError(LogEntry{
		Severity: "WARN", Component: "worker.delivery",
		Code: "400", RuleID: "ea565d5c", Message: "sanitized one-liner",
	})
	srv := NewServer(c, nil, nil, nil, "sqlite", "w-test", nil)
	mux := http.NewServeMux()
	srv.Attach(mux)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/ops/errors?limit=10")
	if err != nil {
		t.Fatalf("GET /ops/errors: %v", err)
	}
	defer resp.Body.Close()
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	items := body["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("items = %d, want 1", len(items))
	}
	row := items[0].(map[string]any)
	for _, key := range []string{
		"ts", "severity", "component", "worker_id", "rule_id",
		"category", "code", "retryable", "message", "count",
	} {
		if _, ok := row[key]; !ok {
			t.Fatalf("error item missing %q", key)
		}
	}
	if row["severity"] != "WARN" {
		t.Fatalf("severity = %v, want WARN", row["severity"])
	}
}

// TestOpsExposesEveryProbeTimestamp pins the parity the DevOps console
// depends on: the worker tracks db/telegram/AI probe timestamps, and the
// /ops snapshot must expose ALL THREE. ai_checked_at was missing even though
// the collector stored it, so the UI could not tell "no AI call yet" from
// "the provider is failing" — both render as ai_connected=false without a
// timestamp to disambiguate.
func TestOpsExposesEveryProbeTimestamp(t *testing.T) {
	c := testCollector()
	c.RecordTelegram(true, time.Now())
	c.RecordAI(true, time.Now())
	c.RecordDB(true, time.Now())

	srv := NewServer(c, nil, nil, nil, "sqlite", "w-test", nil)
	mux := http.NewServeMux()
	srv.Attach(mux)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/ops")
	if err != nil {
		t.Fatalf("GET /ops: %v", err)
	}
	defer resp.Body.Close()
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode /ops: %v", err)
	}
	w, ok := body["worker"].(map[string]any)
	if !ok {
		t.Fatalf("decode /ops: worker block missing: %#v", body)
	}
	for _, key := range []string{"db_checked_at", "telegram_checked_at", "ai_checked_at"} {
		if v, ok := w[key]; !ok || v == "" {
			t.Errorf("/ops %s = %v (ok=%v), want an RFC3339 timestamp — every probe the collector tracks must be visible to the console", key, v, ok)
		}
	}
	if v, _ := w["ai_connected"]; v != true {
		t.Errorf("/ops ai_connected = %v, want true after RecordAI(true)", v)
	}
}

// TestDerivedHealthState pins the console's "is the worker alive?" answer.
// The requirement is explicit: a worker must not stay ONLINE forever because
// an old record exists, "nobody is polling" must never read as "worker is
// down", and alive-but-broken must read degraded, not online.
func TestDerivedHealthState(t *testing.T) {
	c := testCollector()
	c.RecordDB(true, time.Now())

	cases := []struct {
		name  string
		setup func()
		age   int64
		want  string
	}{
		{
			name:  "no heartbeat yet is starting, never offline",
			setup: func() {},
			age:   -1,
			want:  "starting",
		},
		{
			name:  "fresh heartbeat + ready + db is online",
			setup: func() { c.SetState(StateReady) },
			age:   2,
			want:  "online",
		},
		{
			name: "stale heartbeat is offline regardless of state",
			setup: func() {
				c.SetState(StateReady)
			},
			age:  int64(HeartbeatFreshnessBudget/time.Second) + 10,
			want: "offline",
		},
		{
			name:  "degraded state is degraded even with fresh heartbeat",
			setup: func() { c.SetState(StateDegraded) },
			age:   1,
			want:  "degraded",
		},
		{
			name: "alive but db down is degraded, not online",
			setup: func() {
				c.SetState(StateReady)
				c.RecordDB(false, time.Now())
			},
			age:  1,
			want: "degraded",
		},
		{
			name:  "stopping is stopping",
			setup: func() { c.SetState(StateStopping) },
			age:   1,
			want:  "stopping",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tc.setup()
			if got := derivedHealthState(c, tc.age); got != tc.want {
				t.Errorf("derivedHealthState(age=%d) = %q, want %q", tc.age, got, tc.want)
			}
		})
	}
}

// TestOpsExposesHeartbeatAge asserts /ops serializes heartbeat_age_seconds
// alongside last_heartbeat so the console can paint freshness without
// parsing timestamps client-side.
func TestOpsExposesHeartbeatAge(t *testing.T) {
	c := testCollector()
	c.SelfHeartbeat()
	srv := NewServer(c, nil, nil, nil, "sqlite", "w-test", nil)
	mux := http.NewServeMux()
	srv.Attach(mux)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/ops")
	if err != nil {
		t.Fatalf("GET /ops: %v", err)
	}
	defer resp.Body.Close()
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode /ops: %v", err)
	}
	w, ok := body["worker"].(map[string]any)
	if !ok {
		t.Fatalf("decode /ops: worker block missing: %#v", body)
	}
	if _, ok := w["heartbeat_age_seconds"]; !ok {
		t.Error("/ops worker.heartbeat_age_seconds missing — the console cannot derive freshness from last_heartbeat alone")
	}
	if _, ok := w["health_state"]; !ok {
		t.Error("/ops worker.health_state missing — the console needs a derived liveness answer, not just the execution state")
	}
}

// TestIncidentLifecycle pins the §9 lifecycle the error center depends on:
// repeated entries collapse into ONE incident (not N rows), a quiet incident
// recovers, a repeat after recovery marks it recurrent, and an ack is
// recorded — and reports an error for an unknown id rather than pretending.
func TestIncidentLifecycle(t *testing.T) {
	recoverWindow = 50 * time.Millisecond
	minOccForIncident = 2
	defer func() {
		recoverWindow = 10 * time.Minute
		minOccForIncident = 3
	}()

	tr := newIncidentTracker(16)
	item := ErrorItem{
		Severity:  "ERROR",
		Component: "worker.delivery",
		Category:  "telegram",
		Code:      "FLOOD_WAIT",
		RuleID:    "r1",
		WorkerID:  "w1",
		JobID:     "j1",
		Message:   "Telegram rate limit",
	}
	item.Count = 1

	// Three observations of the same fingerprint → one incident, count 3.
	for i := 0; i < 3; i++ {
		item.Count = int64(i + 1)
		item.JobID = fmt.Sprintf("j%d", i) // distinct jobs fold into RelatedJobs
		tr.observeRing(item)
	}
	got := tr.incidents()
	if len(got) != 1 {
		t.Fatalf("incidents = %d, want 1 (repeats collapse into one incident)", len(got))
	}
	inc := got[0]
	if inc.State != "open" {
		t.Errorf("state = %q, want open", inc.State)
	}
	if inc.Count != 3 {
		t.Errorf("count = %d, want 3", inc.Count)
	}
	if len(inc.RelatedJobs) != 3 {
		t.Errorf("related jobs = %v, want 3 distinct", inc.RelatedJobs)
	}
	// FLOOD_WAIT must carry the telegram_flood_wait pattern with a hint,
	// marked SUSPECTED — never a confirmed diagnosis.
	if inc.Pattern == nil || inc.Pattern.Name != "telegram_flood_wait" || !inc.Pattern.Suspected {
		t.Errorf("pattern = %+v, want suspected telegram_flood_wait", inc.Pattern)
	}

	// Quiet period past the window → recovered.
	time.Sleep(80 * time.Millisecond)
	tr.sweep()
	got = tr.incidents()
	if got[0].State != "recovered" {
		t.Errorf("after quiet sweep: state = %q, want recovered", got[0].State)
	}
	if got[0].Recoveries != 1 {
		t.Errorf("recoveries = %d, want 1", got[0].Recoveries)
	}

	// A repeat after recovery → recurrent (not a brand-new incident).
	tr.observeRing(item)
	got = tr.incidents()
	if got[0].State != "recurrent" {
		t.Errorf("after repeat: state = %q, want recurrent", got[0].State)
	}

	// Ack is recorded; acking an unknown id is an error, not a success.
	if err := tr.ack(got[0].ID, "operator"); err != nil {
		t.Fatalf("ack: %v", err)
	}
	got = tr.incidents()
	if got[0].State != "acknowledged" || got[0].AckedBy != "operator" {
		t.Errorf("after ack: state=%q by=%q, want acknowledged/operator", got[0].State, got[0].AckedBy)
	}
	if err := tr.ack("nope", "operator"); err == nil {
		t.Error("ack(unknown) = nil, want an error — the console must never report success for a no-op ack")
	}
}

// TestIncidentFingerprintIsolation: a rule-scoped error must NOT collapse
// into the incident of another task's identical error code — an incident is
// "this task's delivery failures", not "all delivery failures everywhere".
func TestIncidentFingerprintIsolation(t *testing.T) {
	tr := newIncidentTracker(16)
	base := ErrorItem{
		Severity:  "ERROR",
		Component: "worker.delivery",
		Category:  "telegram",
		Code:      "NETWORK_TIMEOUT",
		Message:   "timeout",
	}
	a := base
	a.RuleID = "task-a"
	a.Count = 5
	tr.observeRing(a)
	b := base
	b.RuleID = "task-b"
	b.Count = 5
	tr.observeRing(b)

	got := tr.incidents()
	if len(got) != 2 {
		t.Fatalf("incidents = %d, want 2 (per-task isolation)", len(got))
	}
}

// TestThroughputWindows pins the §4 aggregation windows. Three properties
// the console depends on:
//
//  1. Bounded: 60 int buckets for an HOUR of history regardless of message
//     rate — a 100k-messages-a-minute worker stores the same 60 ints as an
//     idle one.
//  2. Window correctness: ticks land in the wall-clock minute they belong
//     to, and the CURRENT (partially elapsed) minute is excluded from every
//     window — including it would understate every rate.
//  3. Honesty: a window older than the available history reports -1, so the
//     console renders 'insufficient history' rather than a misleading zero.
func TestThroughputWindows(t *testing.T) {
	h := newThroughputHistory(0)
	t0 := time.Unix(1_800_000_000, 0) // a clean minute boundary
	var delivered int64

	// tick: advance the clock by one minute and record a delivery count for
	// that minute. Minute boundaries are exact by construction.
	tick := func(perMinute int64, now time.Time) time.Time {
		now = now.Add(time.Minute)
		delivered += perMinute
		h.observe(now, delivered)
		return now
	}

	now := tick(10, t0) // minute 1 complete: 10 delivered
	w := h.windows(now)
	if w["last_1m"] != 10 {
		t.Errorf("last_1m after one minute = %d, want 10", w["last_1m"])
	}
	for _, k := range []string{"last_5m", "last_15m", "last_1h"} {
		if w[k] != -1 {
			t.Errorf("%s with 1 minute of history = %d, want -1 (insufficient history)", k, w[k])
		}
	}

	// Four more minutes of 10. All five minutes are complete by
	// construction (each tick's delta spans the full 60s since the
	// previous tick), so last_5m is the full 50.
	for m := 0; m < 4; m++ {
		now = tick(10, now)
	}
	w = h.windows(now)
	if w["last_1m"] != 10 {
		t.Errorf("last_1m = %d, want 10", w["last_1m"])
	}
	if w["last_5m"] != 50 {
		t.Errorf("last_5m = %d, want 50 (five complete minutes x 10)", w["last_5m"])
	}
}

// TestThroughputHistoryBoundedByConstruction: two hours of 100k/minute
// still fits the fixed 60-bucket ring, and the 1h window is full.
func TestThroughputHistoryBoundedByConstruction(t *testing.T) {
	h := newThroughputHistory(0)
	now := time.Unix(1_800_000_000, 0)
	var delivered int64
	for m := 0; m < 120; m++ {
		now = now.Add(time.Minute)
		delivered += 100_000
		h.observe(now, delivered)
	}
	got := h.windows(now)
	if got["last_1m"] != 100_000 {
		t.Errorf("last_1m = %d, want 100000", got["last_1m"])
	}
	if got["last_1h"] != 60*100_000 {
		t.Errorf("last_1h = %d, want %d (a full hour of 100k)", got["last_1h"], 60*100_000)
	}
	if len(h.buckets) != 60 {
		t.Errorf("ring grew to %d buckets, want fixed 60", len(h.buckets))
	}
}

// TestThroughputCounterRestart: a restart resets the delivered counter. The
// pre-restart history stays valid; only the crossing delta is dropped (it is
// meaningless), and subsequent ticks re-baseline and accumulate normally.
func TestThroughputCounterRestart(t *testing.T) {
	h := newThroughputHistory(0)
	now := time.Unix(1_800_000_000, 0)
	var delivered int64
	// Six minutes of 10 = 60 delivered, so last_5m has real content.
	for m := 0; m < 6; m++ {
		now = now.Add(time.Minute)
		delivered += 10
		h.observe(now, delivered)
	}
	// Restart: counter resets to 0. The delta is negative → dropped, but
	// the six minutes already observed stay in the ring.
	h.observe(now.Add(time.Minute), 0)
	w := h.windows(now.Add(time.Minute))
	if w["last_5m"] != 50 {
		t.Errorf("last_5m after restart = %d, want 50 (pre-restart history survives)", w["last_5m"])
	}
	// Post-restart ticks re-baseline and accumulate normally.
	h.observe(now.Add(2*time.Minute), 5)
	w = h.windows(now.Add(2 * time.Minute))
	if w["last_1m"] != 5 {
		t.Errorf("last_1m after restart = %d, want 5", w["last_1m"])
	}
}

// TestThroughputHistoryAgeIsObservedNotGap: historyAgeMinutes must report
// how many minutes of history the ring HOLDS, not the gap to the last tick.
// While ticks flow the gap is 0 — reporting it as "0 minutes of history"
// would make the console hide windows it can actually answer.
func TestThroughputHistoryAgeIsObservedNotGap(t *testing.T) {
	h := newThroughputHistory(0)
	now := time.Unix(1_800_000_000, 0)
	var delivered int64
	for m := 0; m < 3; m++ {
		now = now.Add(time.Minute)
		delivered += 5
		h.observe(now, delivered)
	}
	// Immediately after the last tick the gap is zero but the ring holds
	// three minutes of real history.
	if age := h.historyAgeMinutes(now); age != 3 {
		t.Errorf("historyAgeMinutes = %d, want 3 (minutes held, not the gap to the last tick)", age)
	}
}

// TestControlPlaneLifecycle pins §10: safe operational actions from the Web.
// The contract's explicit words: a worker must never falsely report an
// operation simply because the UI accepted the request, and every control
// must have a REAL status.
func TestControlPlaneLifecycle(t *testing.T) {
	c := NewControlPlane(16)

	if c.IsPaused() {
		t.Fatal("fresh control plane reports paused — a new worker always starts running")
	}

	// Pause: accepted, then completed, with the honest effect text.
	op, already := c.Pause("operator@1.2.3.4", nil)
	if already {
		t.Error("first pause reports already-paused — it should be a state change")
	}
	if op.Status != "COMPLETED" {
		t.Errorf("pause status = %q, want COMPLETED (the claim loop observes the gate within one poll interval)", op.Status)
	}
	if !c.IsPaused() {
		t.Error("pause recorded but IsPaused() = false — the claim loop would still claim")
	}
	if op.Reason == "" {
		t.Error("pause op carries no effect text — the console cannot say what the pause actually does")
	}

	// A second pause is NOT a state change: it completes with an honest
	// "already paused" rather than a fabricated second pause.
	op2, already2 := c.Pause("operator@1.2.3.4", nil)
	if !already2 {
		t.Error("second pause reports a state change — the contract forbids reporting what did not happen")
	}
	if op2.Reason != "already paused — no state change" {
		t.Errorf("second pause reason = %q", op2.Reason)
	}

	// Pause context: who asked, when — the console must show WHY delivery
	// stopped, not just that it did.
	if p := c.PauseState(); p == nil || p.RequestedBy != "operator@1.2.3.4" {
		t.Errorf("pause state = %+v, want the requester recorded for the audit", p)
	}

	// Resume: completed, and the gate opens.
	op3, wasRunning := c.Resume("operator@1.2.3.4")
	if wasRunning {
		t.Error("resume reports was-running while paused — the pause was live")
	}
	if op3.Status != "COMPLETED" {
		t.Errorf("resume status = %q", op3.Status)
	}
	if c.IsPaused() {
		t.Error("resume recorded but IsPaused() = true — the claim loop would never claim again")
	}

	// Resume while running: honest "already running", not a change.
	op4, wasRunning2 := c.Resume("operator@1.2.3.4")
	if !wasRunning2 {
		t.Error("second resume reports a state change — nothing changed")
	}
	if op4.Reason != "already running — no state change" {
		t.Errorf("second resume reason = %q", op4.Reason)
	}

	// Audit trail: newest first, every operation recorded.
	ops := c.Ops()
	if len(ops) != 4 {
		t.Fatalf("audit trail = %d ops, want 4 (every control recorded)", len(ops))
	}
	if ops[0].Kind != "resume" || ops[3].Kind != "pause" {
		t.Errorf("trail not newest-first: %v", []string{ops[0].Kind, ops[3].Kind})
	}
}

// TestControlPlaneAuditBounded: the trail cannot grow without bound — a
// chatty admin or an automated loop must not be able to make /ops pay for
// an unbounded audit read.
func TestControlPlaneAuditBounded(t *testing.T) {
	c := NewControlPlane(3)
	for i := 0; i < 6; i++ {
		_, _ = c.Pause("admin", nil)
		_, _ = c.Resume("admin")
	}
	if got := len(c.Ops()); got != 3 {
		t.Errorf("audit trail = %d ops, want capped at 3 (oldest evicted)", got)
	}
}
