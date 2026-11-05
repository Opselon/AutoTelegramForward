package ops

import (
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
)

// Incident is the §9 error-intelligence object: the dedup ring tells the
// operator WHAT failed; an incident tells them it is STILL failing, whether
// it recovered, how many jobs it touched, and what the likely culprit is.
//
// The incident store is IN-MEMORY, exactly like the error ring it is derived
// from: the contract forbids per-message durable writes, and an incident is
// by definition an aggregation that can be rebuilt from the ring. The
// tradeoff is that a worker restart forgets which incidents were
// acknowledged — /ops reports the restart as a reset rather than pretending
// state survived, and the durable record (delivery_jobs FAILED rows, the
// log DB) is untouched as the single source of truth for audits.
type Incident struct {
	// ID is a stable fingerprint of (component, code, rule_id): the same
	// recurring problem always maps to the same incident across restarts
	// of the tracker, so an ack is meaningful within a worker's lifetime.
	ID string `json:"id"`
	// Fingerprint is the human-readable key the ID hashes: component+code
	// (+ rule when the error is task-scoped). Printed so an operator can
	// see WHY entries were grouped.
	Fingerprint string `json:"fingerprint"`
	Severity    string `json:"severity"`
	Category    string `json:"category"`
	Code        string `json:"code"`
	Component   string `json:"component"`
	Retryable   bool   `json:"retryable"`
	// Message is the latest occurrence's message (sanitized by
	// construction — the ring only ever sees classified codes and ids).
	Message string `json:"message"`
	// Count is total occurrences in this incident's lifetime, including
	// occurrences before the last recovery (recurrences fold into one
	// incident, per the contract's "recurrent incidents" state).
	Count int64 `json:"count"`
	// Recoveries is how many times this incident transitioned
	// open→recovered. ≥1 with state open = "recurrent".
	Recoveries int `json:"recoveries"`
	// FirstSeen/LastSeen are RFC3339 of the first and most recent
	// occurrence the tracker has observed.
	FirstSeen string `json:"first_seen"`
	LastSeen  string `json:"last_seen"`
	// Affected accumulates the distinct workers/rules/jobs the incident
	// touched. Capped: an incident that has touched 10k jobs shows "1000+"
	// rather than growing without bound — the whole point of the bounded
	// telemetry rule.
	AffectedWorkers []string `json:"affected_workers"`
	AffectedRules   []string `json:"affected_rules"`
	RelatedJobs     []string `json:"related_jobs"`
	// Pattern is the DETECTED pattern, if one matched, with the hint the
	// console shows. It is labeled SUGGESTED, never CONFIRMED: the contract
	// forbids presenting a guessed root cause as established — the
	// diagnosis is derived from the error code, which tells us what
	// failed, not why the system around it failed.
	Pattern *IncidentPattern `json:"pattern,omitempty"`
	// State is the derived lifecycle state:
	//   open        occurrences within the recovery window
	//   recovered   no occurrences for the window AND count ≥ threshold
	//               (a single stray log line is noise, not an incident)
	//   recurrent   recovered at least once and open again
	//   acknowledged an operator acked it (sticky while it stays open)
	//   suppressed  occurrences are still coming but the incident is
	//               rate-limited for the console — NEVER for the log,
	//               only for the panel's counter
	State   string `json:"state"`
	AckedBy string `json:"acked_by,omitempty"`
	AckedAt string `json:"acked_at,omitempty"`
}

// IncidentPattern names a detected failure pattern and the actionable hint
// for it. Hints are deliberately imperative and describe what to CHECK, not
// what to believe: the root cause is confirmed only when a human confirms it.
type IncidentPattern struct {
	Name      string `json:"name"`
	Suspected bool   `json:"suspected"`
	Hint      string `json:"hint"`
}

// incidentTracker is the bounded in-memory incident store derived from the
// error ring. One mutex; the ring is itself bounded so the tracker's fan-in
// is bounded by construction.
type incidentTracker struct {
	mu sync.Mutex
	// incidents keyed by fingerprint (not the hashed ID — the tracker
	// collapses on the readable key and emits the hash as the stable ID).
	byFP map[string]*Incident
	cap  int
	// recoveryWindow: no occurrences for this long AND at least
	// minOccurrences = RECOVERED. Generous, because a quiet minute during a
	// retry backoff is not a recovery.
	recoveryWindow time.Duration
	minOccurrences int64
	suppressAfter  int64
	now            func() time.Time
}

func newIncidentTracker(cap int) *incidentTracker {
	if cap <= 0 {
		cap = 256
	}
	return &incidentTracker{
		byFP:           map[string]*Incident{},
		cap:            cap,
		recoveryWindow: recoverWindow,
		minOccurrences: minOccForIncident,
		suppressAfter:  200,
		now:            time.Now,
	}
}

// fingerprintOf is the grouping key. Rule-scoped errors (a specific task's
// delivery failing) must NOT collapse into one incident with every other
// task's delivery failures, so the rule_id participates when present.
func fingerprintOf(component, code, ruleID string) string {
	key := component + "|" + code
	if strings.TrimSpace(ruleID) != "" {
		key += "|" + ruleID
	}
	return key
}

func incidentID(fingerprint string) string {
	sum := sha1.Sum([]byte(fingerprint))
	return hex.EncodeToString(sum[:])[:16]
}

// observe folds a ring dedup event into the tracker. count is the ring
// entry's current occurrence count; first/last are its span; the meta slice
// carries the distinct workers/rules/jobs (already capped by the ring).
func (t *incidentTracker) observe(it ErrorItem, affectedWorkers, affectedRules, relatedJobs []string) {
	t.mu.Lock()
	defer t.mu.Unlock()

	fp := fingerprintOf(it.Component, it.Code, it.RuleID)
	now := t.now()
	stamp := func() string { return now.UTC().Format(time.RFC3339) }

	inc, ok := t.byFP[fp]
	if !ok {
		if len(t.byFP) >= t.cap {
			// Evict the RECOVERED incident with the oldest last_seen: an
			// open incident is never evicted for capacity's sake.
			t.evictOne()
		}
		inc = &Incident{
			ID:          incidentID(fp),
			Fingerprint: fp,
			Severity:    it.Severity,
			Category:    it.Category,
			Code:        it.Code,
			Component:   it.Component,
			Retryable:   it.Retryable,
			FirstSeen:   stamp(),
		}
		t.byFP[fp] = inc
	}
	inc.LastSeen = stamp()
	inc.Message = it.Message
	inc.Count = it.Count
	if inc.Count < it.Count {
		inc.Count = it.Count
	}
	if inc.Severity == "WARN" && it.Severity == "ERROR" {
		inc.Severity = it.Severity // severity escalates, never de-escalates
	}
	inc.AffectedWorkers = unionCapped(inc.AffectedWorkers, affectedWorkers, 8)
	inc.AffectedRules = unionCapped(inc.AffectedRules, affectedRules, 8)
	inc.RelatedJobs = unionCapped(inc.RelatedJobs, relatedJobs, 24)
	inc.Pattern = matchPattern(inc)

	// State transitions. An incident that was recovered and sees a new
	// occurrence becomes recurrent — the whole point of tracking recoveries.
	wasRecovered := inc.Recoveries > 0 && inc.State == "recovered"
	if inc.State != "acknowledged" {
		switch {
		case wasRecovered:
			inc.State = "recurrent"
		case inc.Count >= t.suppressAfter:
			inc.State = "suppressed"
		default:
			inc.State = "open"
		}
	}
	// An acked incident that recovered transitions out of the ack state:
	// the ack acknowledged THE ongoing problem, not the fingerprint forever.
	if inc.State == "acknowledged" && t.isRecovered(inc, now) {
		inc.State = "recovered"
		inc.AckedBy, inc.AckedAt = "", ""
	}
}

// recoverWindow / minOccForIncident are package-level so tests can shrink
// them without depending on a tracker's construction.
var recoverWindow = 10 * time.Minute
var minOccForIncident int64 = 3

// isRecovered reports whether the incident should currently count as
// recovered: no occurrence within the window and enough occurrences to be
// more than noise. Centralized so observe() and sweep() agree.
func (t *incidentTracker) isRecovered(inc *Incident, now time.Time) bool {
	last, err := time.Parse(time.RFC3339, inc.LastSeen)
	if err != nil {
		return false
	}
	return now.Sub(last) > t.recoveryWindow && inc.Count >= t.minOccurrences
}

// sweep recomputes the derived state for incidents whose last occurrence
// has aged out of the window — the recovered/recurrent transition that does
// NOT come from a new observation. Cheap: one pass over a bounded map.
func (t *incidentTracker) sweep() {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.now()
	for _, inc := range t.byFP {
		if inc.State == "acknowledged" || inc.State == "recovered" {
			continue
		}
		last, err := time.Parse(time.RFC3339, inc.LastSeen)
		if err != nil {
			continue
		}
		if now.Sub(last) > t.recoveryWindow && inc.Count >= t.minOccurrences {
			inc.Recoveries++
			inc.State = "recovered"
			if inc.AckedBy != "" {
				inc.AckedBy, inc.AckedAt = "", ""
			}
		}
	}
}

// evictOne drops the recovered incident with the oldest last_seen. If every
// incident is open the tracker is all-live problems; we drop the one with
// the oldest last_seen anyway (bounded storage is a correctness property,
// not a nicety) but that path should be unreachable in practice.
func (t *incidentTracker) evictOne() {
	var (
		oldest  *Incident
		oldestT time.Time
	)
	for _, inc := range t.byFP {
		last, err := time.Parse(time.RFC3339, inc.LastSeen)
		if err != nil {
			last = time.Unix(0, 0)
		}
		if oldest == nil || last.Before(oldestT) {
			oldest, oldestT = inc, last
		}
	}
	if oldest != nil {
		delete(t.byFP, oldest.Fingerprint)
	}
}

// ack marks an incident acknowledged by an operator. Returns an error when
// the incident does not exist (the console must never report success for an
// ack that landed nowhere).
func (t *incidentTracker) ack(id, by string) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, inc := range t.byFP {
		if inc.ID == id {
			inc.State = "acknowledged"
			inc.AckedBy = by
			inc.AckedAt = t.now().UTC().Format(time.RFC3339)
			return nil
		}
	}
	return fmt.Errorf("incident %s not found", id)
}

// incidents returns a snapshot sorted by last_seen descending: the console's
// default view is what is happening NOW.
func (t *incidentTracker) incidents() []*Incident {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := make([]*Incident, 0, len(t.byFP))
	for _, inc := range t.byFP {
		clone := *inc
		out = append(out, &clone)
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].LastSeen > out[j].LastSeen
	})
	return out
}

// unionCapped merges two sorted-distinct slices under a cap, so an incident
// that has been failing all day cannot accumulate 50k job ids.
func unionCapped(a, b []string, cap int) []string {
	seen := make(map[string]struct{}, len(a)+len(b))
	for _, v := range a {
		seen[v] = struct{}{}
	}
	for _, v := range b {
		seen[v] = struct{}{}
	}
	out := make([]string, 0, len(seen))
	for v := range seen {
		out = append(out, v)
	}
	sort.Strings(out)
	if len(out) > cap {
		out = append(out[:cap], fmt.Sprintf("+%d more", len(out)-cap))
	}
	return out
}

// observeRing adapts the errorRing.onRecord callback to the tracker: the
// ring hands an ErrorItem (already deduped, with its current Count), and
// the tracker folds it into the incident for its fingerprint. Affected
// worker/rule/job accumulators seed from the item itself; the unions are
// capped inside observe().
func (t *incidentTracker) observeRing(it ErrorItem) {
	workers := []string{}
	if it.WorkerID != "" {
		workers = append(workers, it.WorkerID)
	}
	rules := []string{}
	if it.RuleID != "" {
		rules = append(rules, it.RuleID)
	}
	jobs := []string{}
	if it.JobID != "" {
		jobs = append(jobs, it.JobID)
	}
	t.observe(it, workers, rules, jobs)
}

// --- pattern detection -------------------------------------------------- //

// matchPattern returns the SUSPECTED pattern for an incident, if any. Each
// detection is a code/category signature plus an imperative hint about what
// to check. The contract is explicit that these are suspected causes, never
// confirmed diagnoses — suspicion is derived from what failed; confirmation
// needs the operator.
func matchPattern(inc *Incident) *IncidentPattern {
	code, cat := inc.Code, inc.Category
	switch {
	case containsAny(code, "FLOOD_WAIT", "flood_wait", "FloodWait"):
		return &IncidentPattern{
			Name: "telegram_flood_wait",
			// FloodWait is Telegram rate limiting: the hint is the action
			// that addresses it, not a claim about why the rate limit hit.
			Hint:      "Telegram is rate-limiting this session. Check the worker's send cadence and concurrent-task count; sustained FloodWait means the configured throughput exceeds the account's limit — reduce concurrency or the per-task rate, do not retry faster.",
			Suspected: true,
		}
	case containsAny(cat, "ai", "AI") && containsAny(code, "timeout", "TIMEOUT", "deadline"):
		return &IncidentPattern{
			Name:      "ai_provider_timeout",
			Hint:      "The AI provider is timing out. Check NineRouter reachability and the provider's own status; if timeouts persist while other calls succeed, the model endpoint (not the network) is the suspect.",
			Suspected: true,
		}
	case containsAny(cat, "database", "db", "Database") || containsAny(code, "CONN", "connection"):
		return &IncidentPattern{
			Name:      "database_connection_loss",
			Hint:      "Database connectivity is failing. Check the Postgres/SQLite health probe on the same worker and the container's network; delivery_jobs writes failing alongside this confirm the store, not the worker, is down.",
			Suspected: true,
		}
	case containsAny(code, "DNS", "dns", "resolve"):
		return &IncidentPattern{
			Name:      "dns_failure",
			Hint:      "Name resolution is failing for an upstream host. Check the container's resolv.conf and whether the target host resolves from the worker container directly.",
			Suspected: true,
		}
	case containsAny(code, "heartbeat", "HEARTBEAT"):
		return &IncidentPattern{
			Name:      "worker_heartbeat_expiration",
			Hint:      "A worker's heartbeat expired while it was expected to be live. Check whether the worker process restarted (restart resets the heartbeat) or is wedged (process alive, no ticks) — /ops heartbeat_age separates the two.",
			Suspected: true,
		}
	case containsAny(code, "QUEUE_FULL", "backlog", "backpressure"):
		return &IncidentPattern{
			Name:      "queue_backlog_growth",
			Hint:      "The queue is growing faster than it is draining. Compare jobs_per_second against delivered rate and check for stuck claims in the queue diagnostics — an unclaimed backlog with a live worker points at the claim loop, not traffic.",
			Suspected: true,
		}
	case containsAny(code, "CRASH", "panic", "PANIC"):
		return &IncidentPattern{
			Name:      "worker_crash_loop",
			Hint:      "The worker is crashing repeatedly. Check the worker container restart count and its logs for the panic stack; a crash loop with a wedge-free health probe points at startup configuration, not runtime load.",
			Suspected: true,
		}
	}
	return nil
}

func containsAny(haystack string, needles ...string) bool {
	for _, n := range needles {
		if strings.Contains(haystack, n) {
			return true
		}
	}
	return false
}
