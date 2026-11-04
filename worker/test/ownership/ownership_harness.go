// Package ownership is the dedicated Go-ownership contract suite.
//
// The e2e categories (A–J) prove the Go engine decides individual behaviours
// correctly. This suite proves the ENVIRONMENT those decisions happen in:
//
//   - the multi-task isolation matrix (§1): six concurrently-installed tasks
//     with disjoint (filter, replacement, AI, cleaner, header/footer, FEN,
//     media) configurations share one source chat, one queue and one gateway,
//     and each task's delivery is exactly the one its OWN contract predicts —
//     no task's configuration bleeds into another's body, counter or slot;
//   - the ownsTransforms marker contract (§2): the four payload shapes × the
//     two process switches, pinned in BOTH directions so a mid-flight flip is
//     a rollback, not a double-transform;
//   - exactly-once transform proof (§3): redispatch and lease-expiry replays
//     re-run the transform from the durable raw snapshot without staircasing;
//   - the stage-order proof (§4): the OBSERVED execution order of the
//     transform pipeline's stages, read off the decision events the engine
//     itself records — not an assertion about an internal field.
//
// Style: real engines only. queue.NewTestStore (the real SQLite store), the
// real rulestore, delivery.New and fakegateway — no business logic is mocked.
// Every scenario that claims a delivery asserts on the fakegateway's recorded
// call AND the persisted delivery_jobs row, never on a function return.
package ownership

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/engine/counters"
	"atf/worker/internal/engine/delivery"
	"atf/worker/internal/queue"
	"atf/worker/internal/rulestore"
	"atf/worker/internal/telegraph/fakegateway"
)

// ---------------------------------------------------------------- evidence //

// Result is the terminal state of one contract scenario.
type Result string

const (
	ResultPass    Result = "PASS"
	ResultFail    Result = "FAIL"
	ResultBlocked Result = "BLOCKED"
)

// Evidence is the recorded proof one scenario collects. It mirrors the e2e
// harness shape so the two suites' manifests are co-readable.
type Evidence struct {
	ScenarioID    string `json:"scenario_id"`
	Category      string `json:"category"`
	Description   string `json:"description"`
	Expected      string `json:"expected"`
	Observed      string `json:"observed"`
	Correlation   string `json:"correlation_id"`
	JobID         string `json:"job_id,omitempty"`
	TargetMsgID   int64  `json:"target_message_id,omitempty"`
	Decision      string `json:"decision,omitempty"`
	Reason        string `json:"reason,omitempty"`
	DurationMS    int64  `json:"duration_ms"`
	Result        Result `json:"result"`
	FailureReason string `json:"failure_reason,omitempty"`
}

// Recorder is the per-suite manifest writer; concurrency-safe because the
// isolation matrix records from parallel subtests.
type Recorder struct {
	mu      sync.Mutex
	results map[string]Evidence
}

func NewRecorder() *Recorder { return &Recorder{results: make(map[string]Evidence)} }

func (r *Recorder) Record(e Evidence) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.results[e.ScenarioID] = e
}

// Summary returns the total counts and every non-PASS evidence row.
func (r *Recorder) Summary() ([4]int, []Evidence) {
	r.mu.Lock()
	defer r.mu.Unlock()
	var total [4]int
	var fails []Evidence
	for _, e := range r.results {
		total[0]++
		switch e.Result {
		case ResultPass:
			total[1]++
		case ResultFail:
			total[2]++
			fails = append(fails, e)
		case ResultBlocked:
			total[3]++
			fails = append(fails, e)
		}
	}
	sort.Slice(fails, func(i, j int) bool { return fails[i].ScenarioID < fails[j].ScenarioID })
	return total, fails
}

// ManifestJSON renders the full evidence manifest.
func (r *Recorder) ManifestJSON() ([]byte, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Evidence, 0, len(r.results))
	for _, e := range r.results {
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ScenarioID < out[j].ScenarioID })
	return json.MarshalIndent(out, "", "  ")
}

// evt is the shorthand evidence constructor.
func evt(id, cat, desc, expected string) Evidence {
	return Evidence{ScenarioID: id, Category: cat, Description: desc, Expected: expected, Correlation: id}
}

// ---------------------------------------------------------------- harness //

// Harness is one isolated contract environment: its own temp SQLite DB, queue
// store, rule store, gateway and delivery engine. Nothing leaks between
// scenarios — the isolation matrix relies on that for its "one shared source"
// reads, which is why every scenario builds its own.
type Harness struct {
	t       *testing.T
	dbPath  string
	db      *sql.DB
	Store   queue.Store
	Rules   *rulestore.Store
	Gateway *fakegateway.Fake
	Engine  *delivery.Engine
	// Counters is the durable forward-every-N cadence store.
	Counters counters.Store
	// decisions is the in-process DecisionRecorder the engine reports
	// transform decisions through. The stage-order proof (§4) reads the
	// events back out of here — the engine's own observable output, not an
	// internal field.
	decisions *recordingDecisionRecorder
	owner     int64
}

// NewHarness builds an isolated environment for one contract scenario.
//
// The production worker runs with ATF_WORKER_TRANSFORMS=1 (Go owns the
// transform stage and Python defers to it). The harness mirrors that switch so
// every scenario exercises the ownership position the live system runs in; the
// marker-contract scenarios flip it back to prove the rollback direction.
func NewHarness(t *testing.T, ownerUserID int64) *Harness {
	t.Helper()
	delivery.SetWorkerOwnsTransforms(true)

	dir := t.TempDir()
	dbPath := filepath.Join(dir, "ownership.db")

	bootstrapSchema(t, dbPath)
	sq := queue.NewTestStore(t)
	rs, err := rulestore.New(context.Background(), rulestore.Config{
		SQLite: rulestore.DSN{Raw: dbPath, Label: "sqlite:ownership.db", Backend: "sqlite"},
	})
	if err != nil {
		t.Fatalf("rulestore: %v", err)
	}
	gw := fakegateway.New("")
	rawDB, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatalf("open counters db: %v", err)
	}
	cs, err := counters.NewSQLite(rawDB)
	if err != nil {
		t.Fatalf("counters: %v", err)
	}
	rec := &recordingDecisionRecorder{}
	log := &silentLogger{}
	eng := delivery.New(sq, rs, gw, nil, log, &noopMetrics{}, "ownership-worker", dir, delivery.Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})
	eng.SetCounters(cs)
	eng.SetDecisionRecorder(rec)

	return &Harness{
		t: t, dbPath: dbPath, Store: sq, Rules: rs, Gateway: gw, Engine: eng,
		Counters: cs, decisions: rec, owner: ownerUserID,
	}
}

// DB returns the shared SQLite handle for direct state assertions.
func (h *Harness) DB() *sql.DB {
	if h.db == nil {
		db, err := sql.Open("sqlite3", h.dbPath)
		if err != nil {
			h.t.Fatalf("open db: %v", err)
		}
		h.db = db
	}
	return h.db
}

// Owner is the tenant this harness's tasks belong to.
func (h *Harness) Owner() int64 { return h.owner }

// Decisions returns the decision events the engine recorded, in arrival order.
// The stage-order proof reads the OBSERVED stage sequence from these.
func (h *Harness) Decisions() []cleaning.DecisionEvent {
	return h.decisions.snapshot()
}

// Enqueue inserts a job for a task and returns it, ready to Process. The
// payload mirrors the Python enqueue path under ATF_WORKER_TRANSFORMS=1:
// raw_text set (the raw snapshot), transforms_version absent — the exact shape
// durable_queue._transform_boundary_payload writes.
func (h *Harness) Enqueue(taskID, sourceChat string, sourceMsg int64, targetChat, text string, extra ...map[string]any) *job.DeliveryJob {
	payload := map[string]any{
		"text": text, "raw_text": text, "content_mode": "AUTO",
		"forward_mode": "COPY_MESSAGE", "has_media": false, "media_type": "TEXT",
		"session_id": "sess-ownership", "source_chat_id": sourceChat,
		"source_message_id": sourceMsg, "target_chat_id": targetChat,
		"owner_user_id": h.owner,
		"correlation_id": fmt.Sprintf("own-%s-%d", taskID, sourceMsg),
	}
	for _, e := range extra {
		for k, v := range e {
			payload[k] = v
		}
	}
	j := &job.DeliveryJob{
		ID:     fmt.Sprintf("%s:%s:%d:%s", taskID, sourceChat, sourceMsg, targetChat),
		RuleID: taskID, SourceChatID: sourceChat, SourceMessageID: sourceMsg,
		TargetChatID: targetChat, Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *job.NewPayload(payload),
	}
	queue.InsertJob(h.t, h.Store, j)
	return j
}

// EnqueueShape installs a job carrying an EXPLICIT transform-boundary payload
// shape — the pre-4.2 / legacy / no-raw markers the marker contract pins.
//
// deferred:    raw_text + transforms_version=null (Python under the switch)
// transformed: transforms_version="4.2" and no raw snapshot (pre-4.2 Python)
// legacy:      neither key (a job written before Stage 4.2)
// no-raw:      transforms_version=null with no raw snapshot
//
// The text key carries the payload's "already-transformed" body in every
// shape; what changes is which side the boundary says already applied it.
func (h *Harness) EnqueueShape(shape, taskID, sourceChat string, sourceMsg int64, targetChat, text string) *job.DeliveryJob {
	h.t.Helper()
	extra := map[string]any{}
	switch shape {
	case "deferred":
		extra["raw_text"] = text
		extra["transforms_version"] = nil
	case "transformed":
		extra["text"] = text
		extra["transforms_version"] = "4.2"
	case "legacy":
		extra["text"] = text
	case "no-raw":
		extra["text"] = text
		extra["transforms_version"] = nil
	default:
		h.t.Fatalf("unknown payload shape %q", shape)
	}
	j := h.Enqueue(taskID, sourceChat, sourceMsg, targetChat, text)
	raw := j.Payload.Raw()
	// Rebuild the boundary keys to the exact shape: Enqueue wrote both text
	// and raw_text, and the marker contract needs the distinction.
	for _, k := range []string{"raw_text", "transforms_version"} {
		delete(raw, k)
	}
	for k, v := range extra {
		raw[k] = v
	}
	queue.InsertJob(h.t, h.Store, j)
	return j
}

// Run processes one job through the real engine.
func (h *Harness) Run(ctx context.Context, j *job.DeliveryJob) error {
	return h.Engine.Process(ctx, j)
}

// Reclaim simulates a lease-expiry recovery: the job row is put back into the
// claimable set the way the queue's stale-claim sweep does, then re-claimed
// through the REAL claim path so ClaimedFrom carries the recovery state
// (RETRY_WAIT/CLAIMED) the cadence and redispatch gates read.
func (h *Harness) Reclaim(ctx context.Context, j *job.DeliveryJob, from job.Status) *job.DeliveryJob {
	h.t.Helper()
	db := queue.StoreDB(h.t, h.Store)
	if _, err := db.ExecContext(ctx, `UPDATE delivery_jobs SET status=?, next_retry_at=0 WHERE id=?`,
		string(from), j.ID); err != nil {
		h.t.Fatalf("reclaim prep %s: %v", j.ID, err)
	}
	claimed, err := h.Store.ClaimBatch(ctx, "ownership-worker", 5, 30*time.Second)
	if err != nil {
		h.t.Fatalf("reclaim claim: %v", err)
	}
	for _, c := range claimed {
		if c.ID == j.ID {
			return c
		}
	}
	h.t.Fatalf("reclaim: job %s was not re-claimed", j.ID)
	return nil
}

// AssertDelivered is the PASS condition for "the message reached the target":
// a recorded gateway call AND a persisted SENT row. wantSubstring, when
// non-empty, must appear in the text the gateway actually received.
func (h *Harness) AssertDelivered(e *Evidence, j *job.DeliveryJob, wantSubstring string) bool {
	calls := h.Gateway.Recorded()
	if len(calls) == 0 {
		e.Result = ResultFail
		e.FailureReason = "no gateway call recorded — the message never reached the transport"
		return false
	}
	if status := h.jobStatus(j.ID); status != "SENT" {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("job status = %s, want SENT", status)
		return false
	}
	if wantSubstring != "" && !h.gatewaySent(wantSubstring) {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("delivered text does not contain %q (gateway saw %q)",
			wantSubstring, h.gatewayTexts())
		return false
	}
	e.TargetMsgID = calls[0].TargetID
	e.Decision = "DELIVERED"
	return true
}

// AssertDeliveredN is AssertDelivered for multi-task scenarios: exactly n
// deliveries to targetChat, each containing its expectation, in order.
func (h *Harness) AssertDeliveredN(e *Evidence, targetChat string, want []string) bool {
	got := h.bodiesForTarget(targetChat)
	if len(got) != len(want) {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("target %s: %d deliveries %v, want %d %v",
			targetChat, len(got), got, len(want), want)
		return false
	}
	for i := range want {
		if !strings.Contains(got[i], want[i]) {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("target %s delivery %d = %q, want to contain %q",
				targetChat, i, got[i], want[i])
			return false
		}
	}
	e.Decision = "DELIVERED"
	return true
}

// AssertDroppedTarget is AssertDropped scoped to one target chat: no delivery
// may have landed on THAT target, while other tasks' deliveries in the same
// harness are fine — which is exactly the isolation question.
func (h *Harness) AssertDroppedTarget(e *Evidence, j *job.DeliveryJob, targetChat string) bool {
	if len(h.bodiesForTarget(targetChat)) > 0 {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("gateway delivered to %s for a job predicted to drop", targetChat)
		return false
	}
	if st := h.jobStatus(j.ID); st == "SENT" {
		e.Result = ResultFail
		e.FailureReason = "job marked SENT for a message that should have been rejected"
		return false
	}
	e.Decision = "REJECTED"
	e.Reason = h.jobError(j.ID)
	if e.Reason == "" {
		e.Reason = h.jobStatus(j.ID)
	}
	return true
}

// AssertDropped is the PASS condition for "the pipeline must reject with a
// classified reason": no gateway call and no SENT row.
func (h *Harness) AssertDropped(e *Evidence, j *job.DeliveryJob) bool {
	if calls := h.Gateway.Recorded(); len(calls) > 0 {
		e.Result = ResultFail
		e.FailureReason = "gateway was called for a message that should have been rejected"
		return false
	}
	if status := h.jobStatus(j.ID); status == "SENT" {
		e.Result = ResultFail
		e.FailureReason = "job marked SENT for a message that should have been rejected"
		return false
	}
	e.Decision = "REJECTED"
	e.Reason = h.jobError(j.ID)
	if e.Reason == "" {
		e.Reason = h.jobStatus(j.ID)
	}
	return true
}

// gatewaySent checks the text the gateway actually received.
func (h *Harness) gatewaySent(want string) bool {
	for _, c := range h.Gateway.Recorded() {
		if strings.Contains(c.Caption, want) || strings.Contains(c.Text, want) {
			return true
		}
	}
	return false
}

// bodiesForTarget returns the delivered bodies the gateway recorded for one
// target chat, in arrival order. A text-only dispatch lands in Call.Text
// (SendMessage); a copy lands in Call.Caption (CopyMessage/CopyMessages) —
// both shapes are collected so a scenario asserts on the text actually sent
// regardless of the delivery mode the job resolved to.
func (h *Harness) bodiesForTarget(targetChat string) []string {
	var got []string
	for _, c := range h.Gateway.Recorded() {
		if fmt.Sprintf("%d", c.TargetID) != targetChat {
			continue
		}
		if c.Caption != "" {
			got = append(got, c.Caption)
		} else if c.Text != "" {
			got = append(got, c.Text)
		}
	}
	return got
}

// gatewayTexts is the diagnostic form of the gateway's recorded bodies.
func (h *Harness) gatewayTexts() string {
	var parts []string
	for _, c := range h.Gateway.Recorded() {
		if c.Caption != "" {
			parts = append(parts, c.Caption)
		} else if c.Text != "" {
			parts = append(parts, c.Text)
		}
	}
	return strings.Join(parts, " | ")
}

func (h *Harness) jobStatus(id string) string {
	row := queue.StoreDB(h.t, h.Store).QueryRow(`SELECT status FROM delivery_jobs WHERE id = ?`, id)
	var s string
	if err := row.Scan(&s); err != nil {
		return "MISSING"
	}
	return s
}

func (h *Harness) jobError(id string) string {
	row := queue.StoreDB(h.t, h.Store).QueryRow(`SELECT error_detail FROM delivery_jobs WHERE id = ?`, id)
	var s string
	if err := row.Scan(&s); err != nil {
		return ""
	}
	return s
}

// Finish records the timing and stores the evidence.
func (h *Harness) Finish(rec *Recorder, e *Evidence, start time.Time, err error) {
	e.DurationMS = time.Since(start).Milliseconds()
	if err != nil && e.Result == ResultPass {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("engine error: %v", err)
	}
	if e.Result == "" {
		e.Result = ResultFail
		e.FailureReason = "scenario produced no explicit result"
	}
	rec.Record(*e)
}

// CounterAccepted reads a task's cadence counter straight out of the durable
// rule_counters table — the shared bookkeeping both engines would write if the
// ownership gate failed, which is exactly what the isolation matrix watches.
func (h *Harness) CounterAccepted(ctx context.Context, ruleID string) int {
	row, err := h.Counters.Peek(ctx, ruleID)
	if err != nil || row == nil {
		return 0
	}
	return row.Accepted
}

// ------------------------------------------------------------ decision log //

// recordingDecisionRecorder is the in-process DecisionRecorder the harness
// installs. It is the §4 stage-order proof's only instrumentation: the engine
// hands it one cleaning.DecisionEvent per transform, and the event's
// StageNames field is the OBSERVED ordered list of stages that ran.
type recordingDecisionRecorder struct {
	mu     sync.Mutex
	events []cleaning.DecisionEvent
}

func (r *recordingDecisionRecorder) RecordDecision(_ context.Context, event cleaning.DecisionEvent) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, event)
}

func (r *recordingDecisionRecorder) snapshot() []cleaning.DecisionEvent {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]cleaning.DecisionEvent, len(r.events))
	copy(out, r.events)
	return out
}

// StageNamesForRule returns the stage-name list of the last transform
// decision recorded for one task, in observed order.
func (h *Harness) StageNamesForRule(ruleID string) []string {
	var last []string
	for _, e := range h.Decisions() {
		if e.RuleID == ruleID {
			last = e.StageNames
		}
	}
	return last
}

// Lookup reloads a job by ID from the queue store.
func (h *Harness) Lookup(id string) *job.DeliveryJob {
	h.t.Helper()
	row := queue.StoreDB(h.t, h.Store).QueryRow(
		`SELECT id, rule_id, source_chat_id, source_message_id, target_chat_id,
		        status, attempts, max_attempts, payload_json, created_at, updated_at
		 FROM delivery_jobs WHERE id = ?`, id)
	var j job.DeliveryJob
	var status, payload string
	if err := row.Scan(&j.ID, &j.RuleID, &j.SourceChatID, &j.SourceMessageID, &j.TargetChatID,
		&status, &j.Attempts, &j.MaxAttempts, &payload, &j.CreatedAt, &j.UpdatedAt); err != nil {
		h.t.Fatalf("lookup %s: %v", id, err)
	}
	j.Status = job.Status(status)
	j.Payload = *job.NewPayload(mustJSONMap(payload))
	return &j
}

// mustJSONMap decodes a payload_json blob into a map.
func mustJSONMap(raw string) map[string]any {
	out := map[string]any{}
	_ = json.Unmarshal([]byte(raw), &out)
	return out
}

// ResetCalls clears the gateway's recorded call log so a scenario can assert
// on traffic after a mid-flight configuration change.
func (h *Harness) ResetCalls() {
	h.Gateway.Reset()
}

// ------------------------------------------------------------ plumbing ---- //

type noopMetrics struct{}

func (noopMetrics) Outcome(kind string)                          {}
func (noopMetrics) ObserveRPC(operation string, seconds float64) {}
func (noopMetrics) ObserveWait(seconds float64)                  {}

type silentLogger struct{}

func (silentLogger) Debug(msg string, args ...any) {}
func (silentLogger) Info(msg string, args ...any)  {}
func (silentLogger) Warn(msg string, args ...any)  {}
func (silentLogger) Error(msg string, args ...any) {}

// now returns the scenario start instant (Finish computes duration).
func now() time.Time { return time.Now() }
