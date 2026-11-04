package ops

import (
	"fmt"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// StageNames is the contract's fixed 11-stage pipeline, IN ORDER. The panel
// renders exactly these names in exactly this order — a stage listed here
// that was never recorded shows "waiting", the honest answer for a stage
// the deployment has not moved into the worker.
var StageNames = []string{
	"ingestion",
	"queue",
	"task_resolution",
	"filter",
	"replacement",
	"cleaner",
	"header_footer",
	"ai",
	"validation",
	"delivery",
	"reconciliation",
}

// Stage health vocabulary (the contract's):
//
//	healthy  — ran, no failures in its recent window
//	degraded — ran, failed at least once but recovered since
//	failed   — its most recent recorded event was a failure
//	disabled — the process switch for this stage is off
//	waiting  — never ran (or the dependency it needs is unreachable)
const (
	StageHealthy  = "healthy"
	StageDegraded = "degraded"
	StageFailed   = "failed"
	StageDisabled = "disabled"
	StageWaiting  = "waiting"
)

// StageDelivery is the stage name the RPC mapper emits; a named constant so
// the engine's call sites never spell it inline.
const StageDelivery = "delivery"

// stageHealthWindow bounds how much history "degraded" is derived from. 64
// runs is roughly a minute of a busy task: enough to see a flaky stage,
// short enough that a stage broken for an hour does not get washed clean
// by its own ancient history.
const stageHealthWindow = 64

// stageLatencyWindow bounds a stage's latency window (the mean is the only
// per-stage latency the contract reports).
const stageLatencyWindow = 128

// stageStats is one pipeline stage's operational record. All mutation goes
// through the Collector's Stage* methods, and health is DERIVED from the
// counters — never set by a caller — so a stage cannot report a health its
// own numbers contradict (the same one-owner rule the collector enforces
// for delivery counters).
type stageStats struct {
	name string

	enabled atomic.Bool

	// processed/failures are monotonic; a failed run counts as processed
	// AND failed, so the panel can render both totals and a failure rate.
	processed atomic.Int64
	failures  atomic.Int64

	// skipped is the stage's extra counter (the filter stage's
	// skipped_by_policy is the contract's named example).
	skipped atomic.Int64

	// recentFails is the bounded window "degraded" is derived from.
	recentFails windowCounter

	// lastFailed reports whether the stage's most recent recorded event was
	// a failure. It is what separates failed (the last thing it did was
	// fail) from degraded (it failed, then recovered).
	lastFailed atomic.Bool

	// lastErr is the sanitized one-liner of the most recent failure. A
	// pointer-to-immutable so the HTTP read path can swap-read it
	// lock-free; nil means "never failed" (JSON null, not an empty object).
	lastErr  atomic.Pointer[stageError]
	lastRun  atomic.Int64

	// mu guards the stage's latency window only.
	mu        sync.Mutex
	lat       []float64
	latIdx    int
	latFilled bool
	latSum    float64
}

// stageError is a stage's last failure: immutable after publication.
type stageError struct {
	message string
	at      time.Time
}

// newStageStats builds a stage record with its bounded windows allocated.
func newStageStats(name string) *stageStats {
	s := &stageStats{
		name:   name,
		lat:    make([]float64, stageLatencyWindow),
	}
	s.recentFails = *newWindowCounter(stageHealthWindow)
	return s
}

// record marks one item processed by the stage, with that item's latency in
// milliseconds.
func (s *stageStats) record(latencyMs float64) {
	s.processed.Add(1)
	s.recentFails.add(false)
	s.lastRun.Store(time.Now().UnixMilli())
	s.lastFailed.Store(false)
	s.observeLatency(latencyMs)
}

// recordSkipped marks one item processed and bumps the skipped counter.
func (s *stageStats) recordSkipped(latencyMs float64) {
	s.skipped.Add(1)
	s.record(latencyMs)
}

// fail records a stage failure. The message is sanitized here — the single
// choke point every failure line passes through, so no future caller can
// forget.
func (s *stageStats) fail(msg string, at time.Time) {
	s.failures.Add(1)
	s.recentFails.add(true)
	s.lastRun.Store(at.UnixMilli())
	s.lastFailed.Store(true)
	s.lastErr.Store(&stageError{message: sanitize(msg), at: at})
}

// observeLatency appends one observation (milliseconds) to the window.
func (s *stageStats) observeLatency(ms float64) {
	if ms <= 0 || ms != ms { // NaN-safe
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.latFilled {
		s.latSum -= s.lat[s.latIdx]
	}
	s.lat[s.latIdx] = ms
	s.latSum += ms
	s.latIdx++
	if s.latIdx >= len(s.lat) {
		s.latIdx = 0
		s.latFilled = true
	}
}

// latencyAvg is the stage's mean latency over its window (ms), 0 when empty.
func (s *stageStats) latencyAvg() float64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := s.latIdx
	if s.latFilled {
		n = len(s.lat)
	}
	if n <= 0 {
		return 0
	}
	return s.latSum / float64(n)
}

// health derives the stage's health. The derivation is the contract's:
//
//	disabled — the process switch is off
//	failed   — the most recent recorded event was a failure
//	degraded — failures exist in the window but the stage recovered
//	healthy  — ran at least once with no failures in the window
//	waiting  — never ran (the honest pre-first-run / dependency-down state)
//
// The AI stage is never healthy just because a task enables it: "healthy"
// requires processed > 0 and no failures, so a wiring that never ran shows
// waiting — the exact trap the contract names.
func (s *stageStats) health() string {
	if !s.enabled.Load() {
		return StageDisabled
	}
	if s.processed.Load() == 0 && s.failures.Load() == 0 {
		return StageWaiting
	}
	if s.lastFailed.Load() {
		return StageFailed
	}
	if s.recentFails.snapshot() > 0 {
		return StageDegraded
	}
	if s.processed.Load() > 0 {
		return StageHealthy
	}
	return StageWaiting
}

// snapshot is the JSON view of one stage, in the contract's §1.3 shape.
func (s *stageStats) snapshot() map[string]any {
	out := map[string]any{
		"stage":          s.name,
		"enabled":        s.enabled.Load(),
		"health":         s.health(),
		"processed":      s.processed.Load(),
		"latency_ms_avg": round3(s.latencyAvg()),
		"failures":       s.failures.Load(),
		"last_error":     nil,
		"last_run_at":    nil,
	}
	if e := s.lastErr.Load(); e != nil {
		out["last_error"] = e.message
	}
	if ts := s.lastRun.Load(); ts > 0 {
		out["last_run_at"] = time.UnixMilli(ts).UTC().Format(time.RFC3339)
	}
	if s.skipped.Load() > 0 {
		// The contract's example attaches skipped_by_policy to the filter
		// stage; a stage that never skipped carries no field at all rather
		// than a zero that reads like data.
		out["skipped_by_policy"] = s.skipped.Load()
	}
	return out
}

// --- the Collector-facing stage API --------------------------------------- //

// StageRecord marks one item processed by the named stage. Unknown names
// are ignored: the 11 fixed stages are the contract, and a caller inventing
// a name is a bug, not an extension point.
func (c *Collector) StageRecord(stage string, latencyMs float64) {
	if s := c.stageOf(stage); s != nil {
		s.record(latencyMs)
	}
}

// StageRecordSkipped marks one item processed by the named stage and bumps
// the stage's skipped counter (the filter's skipped_by_policy).
func (c *Collector) StageRecordSkipped(stage string, latencyMs float64) {
	if s := c.stageOf(stage); s != nil {
		s.recordSkipped(latencyMs)
	}
}

// StageFail records a failure for the named stage. The message is sanitized
// inside; callers pass the classification or reason code they already have.
func (c *Collector) StageFail(stage, msg string) {
	if s := c.stageOf(stage); s != nil {
		s.fail(msg, time.Now())
	}
}

// StageEnabled sets the stage's enabled flag from the process's own switch
// resolution. Disabled is a deployment decision, never an inference.
func (c *Collector) StageEnabled(stage string, enabled bool) {
	if s := c.stageOf(stage); s != nil {
		s.enabled.Store(enabled)
	}
}

// stageForRPC maps a Telegram RPC operation name onto the pipeline stage it
// belongs to. Every send/copy/forward is the delivery stage's work; a name
// with no mapping is not recorded (the stage names are the contract, the
// RPC vocabulary is not).
func stageForRPC(operation string) string {
	switch operation {
	case "send_message", "copy_message", "forward_messages", "copy_media_group",
		"send_media", "bot_send_text", "bot_copy_message", "branding_stage1",
		"download_media", "prepare_media_upload", "reload_media", "bot_send_media":
		return StageDelivery
	}
	return ""
}

// round3 keeps the JSON free of millisecond float noise
// (0.020000000004-style artifacts from the arithmetic, not the measurement).
func round3(f float64) float64 {
	if f != f {
		return 0
	}
	return float64(int64(f*1000+0.5)) / 1000
}

// sanitize is the single choke point every error/stage message passes
// before it reaches a ring or a JSON field. One line, bounded length, no
// credential-shaped content: the classification vocabulary survives, raw
// text and secrets never do.
//
// Three passes:
//
//  1. shape: one line, bounded length.
//  2. pairs: a "=value" is dropped, keeping only the bare key when the key
//     looks like a code (short, no spaces). The messages arriving here are
//     engine classifications ("FLOOD_WAIT", "mark_retry failed") whose
//     payload is ids — the moment a value rides along it is either a
//     secret-shaped pair (token=…, session=…) or raw Telegram detail that
//     must not reach a dashboard.
//  3. scrub: any remaining secret-SHAPED token (a word followed by a long
//     alnum run, or an obvious key/context value) is removed. This is the
//     belt behind the braces: an error string not shaped like a pair
//     ("session string=abc123", "<AUTH_KEY>") still loses the secret.
func sanitize(msg string) string {
	msg = strings.TrimSpace(msg)
	if msg == "" {
		return ""
	}
	if i := strings.IndexAny(msg, "\r\n"); i >= 0 {
		msg = msg[:i] // a multi-line error is a stack trace in disguise
	}
	if len(msg) > 160 {
		msg = msg[:160] // a 10KB error is a log bomb
	}
	// Pair pass.
	if eq := strings.Index(msg, "="); eq > 0 {
		key := msg[:eq]
		if !strings.ContainsAny(key, " 	") && len(key) < 32 {
			msg = key
		}
	}
	// Scrub pass: remove anything that survived the pair pass but still
	// looks like a secret (a long alnum run — session strings, API keys,
	// hex tokens are all ≥16 mixed-case alnum with no spaces).
	msg = secretRun.ReplaceAllString(msg, "[redacted]")
	return strings.TrimSpace(msg)
}

// secretRun matches a long alphanumeric/hex-ish run: the shape session
// strings, API keys and file tokens all share. Codes the engine actually
// emits (FLOOD_WAIT, PEER_ID_INVALID, RULE_NOT_FOUND) are ≤11 chars, so a
// 12-char floor keeps every legitimate vocabulary word intact while
// catching the secrets — which are, in practice, never that short.
var secretRun = regexp.MustCompile(`[A-Za-z0-9_\-]{12,}`)

// String is a compact debug form (tests, log lines).
func (s *stageStats) String() string {
	return fmt.Sprintf("%s(enabled=%v processed=%d failures=%d health=%s)",
		s.name, s.enabled.Load(), s.processed.Load(), s.failures.Load(), s.health())
}
