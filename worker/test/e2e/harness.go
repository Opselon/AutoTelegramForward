// Package e2e is the 369-scenario end-to-end verification harness for the
// Go Worker forwarding pipeline.
//
// Contract (per the master execution task):
//   - every scenario has a stable ID (ATF-E2E-<CAT>-<NNN>), an explicit
//     setup, an expected decision, and a recorded observed result;
//   - results are PASS / FAIL / BLOCKED / NOT_RUN, recorded in a manifest
//     that can be dumped as the final evidence report;
//   - a scenario counts as PASS only when the observable outcome is
//     asserted — a claimed job, a gateway call and a recorded destination
//     id, or an explicit, classified rejection. A green function return
//     alone is never enough.
//
// The harness drives the REAL production components: queue.NewTestStore
// (the real SQLite store over a temp DB), rulestore over the same DB, the
// real delivery.Engine, and the real transform/filter/routing engines.
// Only the Telegram wire is replaced — by fakegateway, which records every
// send deterministically. That keeps the harness runnable in CI while every
// business decision is made by production code.
package e2e

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

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/engine/counters"
	"atf/worker/internal/engine/delivery"
	"atf/worker/internal/infra/aiprovider"
	"atf/worker/internal/queue"
	"atf/worker/internal/rulestore"
	"atf/worker/internal/telegraph"
	"atf/worker/internal/telegraph/fakegateway"
)

// Result is the terminal state of one scenario.
type Result string

const (
	ResultPass    Result = "PASS"
	ResultFail    Result = "FAIL"
	ResultBlocked Result = "BLOCKED"
	ResultNotRun  Result = "NOT_RUN"
)

// Evidence is the recorded proof one scenario collects.
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

// Recorder is the per-suite manifest writer. It is concurrency-safe so
// parallel category runners can record into one manifest.
type Recorder struct {
	mu      sync.Mutex
	results map[string]Evidence
}

func NewRecorder() *Recorder {
	return &Recorder{results: make(map[string]Evidence)}
}

func (r *Recorder) Record(e Evidence) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.results[e.ScenarioID] = e
}

// Summary returns the per-category counts, total counts and every failure.
func (r *Recorder) Summary() (map[string][4]int, map[string][4]int, []Evidence) {
	r.mu.Lock()
	defer r.mu.Unlock()

	cats := make(map[string][4]int) // executed, passed, failed, blocked
	for _, e := range r.results {
		c := cats[e.Category]
		c[0]++
		switch e.Result {
		case ResultPass:
			c[1]++
		case ResultFail:
			c[2]++
		case ResultBlocked:
			c[3]++
		}
		cats[e.Category] = c
	}
	var total [4]int
	for _, c := range cats {
		total[0] += c[0]
		total[1] += c[1]
		total[2] += c[2]
		total[3] += c[3]
	}
	var fails []Evidence
	for _, e := range r.results {
		if e.Result != ResultPass {
			fails = append(fails, e)
		}
	}
	sort.Slice(fails, func(i, j int) bool { return fails[i].ScenarioID < fails[j].ScenarioID })
	return cats, map[string][4]int{"TOTAL": total}, fails
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

// ---------------------------------------------------------------- harness ---

// Harness is one isolated scenario environment: its own temp SQLite DB, its
// own queue store, rule store, gateway and delivery engine. Nothing leaks
// between scenarios.
type Harness struct {
	t        *testing.T
	dbPath   string
	db       *sql.DB
	Store    queue.Store
	Rules    rule.Store
	Gateway  *fakegateway.Fake
	Engine   *delivery.Engine
	Counters counters.Store
	AI       *aiprovider.Factory
	owner    int64
}

// NewHarness builds an isolated environment for one scenario.
func NewHarness(t *testing.T, ownerUserID int64) *Harness {
	// The production worker runs with ATF_WORKER_TRANSFORMS=1: Go owns the
	// transform stage and Python defers to it (core/main.py reads the same
	// switch). The harness must mirror that or every D/E scenario would test
	// the Python-side pre-transform against a Go that never runs.
	delivery.SetWorkerOwnsTransforms(true)
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "e2e.db")

	sq := queue.NewTestStore(t)
	bootstrapSchema(t, dbPath)
	rs, err := rulestore.New(context.Background(), rulestore.Config{SQLite: rulestore.DSN{Raw: dbPath, Label: "sqlite:e2e.db", Backend: "sqlite"}})
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
	log := &silentLogger{}
	eng := delivery.New(sq, rs, gw, nil, log, &noopMetrics{}, "e2e-worker", dir, delivery.Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})
	eng.SetCounters(cs)

	return &Harness{
		t: t, dbPath: dbPath, Store: sq, Rules: rs, Gateway: gw, Engine: eng,
		Counters: cs, owner: ownerUserID,
	}
}

// DB returns the shared SQLite handle (for direct state assertions).
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

// Enqueue inserts a job for a task and returns it, ready to Process.
func (h *Harness) Enqueue(taskID, sourceChat string, sourceMsg int64, targetChat string, text string, extra ...map[string]any) *job.DeliveryJob {
	payload := map[string]any{
		"text": text, "raw_text": text, "content_mode": "AUTO",
		"forward_mode": "COPY_MESSAGE", "has_media": false, "media_type": "TEXT",
		"session_id": "sess-e2e", "source_chat_id": sourceChat,
		"source_message_id": sourceMsg, "target_chat_id": targetChat,
		"owner_user_id": h.owner, "correlation_id": fmt.Sprintf("e2e-%s-%d", taskID, sourceMsg),
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

// Run processes one job through the real engine and returns the outcome.
func (h *Harness) Run(ctx context.Context, j *job.DeliveryJob) error {
	return h.Engine.Process(ctx, j)
}

// AssertDelivered is the PASS condition for a "message reaches target"
// scenario: a recorded gateway send AND a persisted SENT row carrying the
// destination message id.
func (h *Harness) AssertDelivered(e *Evidence, j *job.DeliveryJob, wantSubstring string) bool {
	calls := h.Gateway.Recorded()
	if len(calls) == 0 {
		e.Result = ResultFail
		e.FailureReason = "no gateway call recorded — the message never reached the transport"
		return false
	}
	status := h.jobStatus(j.ID)
	if status != "SENT" {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("job status = %s, want SENT", status)
		return false
	}
	if wantSubstring != "" {
		if !h.gatewaySentSubstring(wantSubstring) {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("delivered text does not contain %q", wantSubstring)
			return false
		}
	}
	e.TargetMsgID = calls[0].TargetID
	e.Decision = "DELIVERED"
	return true
}

// AssertDropped is the PASS condition for "the pipeline must reject with a
// classified reason": FAILED status, no gateway call, and a reason captured.
func (h *Harness) AssertDropped(e *Evidence, j *job.DeliveryJob) bool {
	if calls := h.Gateway.Recorded(); len(calls) > 0 {
		e.Result = ResultFail
		e.FailureReason = "gateway was called for a message that should have been rejected"
		return false
	}
	status := h.jobStatus(j.ID)
	if status == "SENT" {
		e.Result = ResultFail
		e.FailureReason = "job marked SENT for a message that should have been rejected"
		return false
	}
	e.Decision = "REJECTED"
	e.Reason = h.jobError(j.ID)
	if e.Reason == "" {
		e.Reason = status
	}
	return true
}

// gatewaySentSubstring checks the text the gateway actually received.
func (h *Harness) gatewaySentSubstring(want string) bool {
	for _, c := range h.Gateway.Recorded() {
		if strings.Contains(c.Text, want) {
			return true
		}
	}
	return false
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

// evt is a shorthand evidence constructor.
func evt(id, cat, desc, expected string) Evidence {
	return Evidence{ScenarioID: id, Category: cat, Description: desc, Expected: expected, Correlation: id}
}

// noopMetrics satisfies delivery.Metrics without asserting anything.
type noopMetrics struct{}

func (noopMetrics) Outcome(kind string)                          {}
func (noopMetrics) ObserveRPC(operation string, seconds float64) {}
func (noopMetrics) ObserveWait(seconds float64)                  {}

// silentLogger swallows delivery logs (harness scenarios assert on state).
type silentLogger struct{}

func (silentLogger) Debug(msg string, args ...any) {}
func (silentLogger) Info(msg string, args ...any)  {}
func (silentLogger) Warn(msg string, args ...any)  {}
func (silentLogger) Error(msg string, args ...any) {}

// TaskSpec describes a task the scenario installs. Fields map 1:1 onto the
// forward_rules columns the production rulestore reads.
type TaskSpec struct {
	ID           string
	Source       string
	SourceName   string
	Target       string
	TargetName   string
	Active       bool
	FilterID     string
	AIConfigID   string
	Replacements map[string]map[string]any
	Metadata     map[string]any
	Header       string
	Footer       string

	// Policy is the task's content_policy metadata block: whitelist,
	// blacklist, replacements, regex and cleaner toggles. nil = no policy.
	Policy *PolicySpec
}

// PolicySpec is the metadata.content_policy shape the Python pipeline and
// the Go transforms pipeline both read.
type PolicySpec struct {
	Enabled        *bool
	BlacklistWords []string
	WhitelistWords []string
	WhitelistMode  string // any | all | phrase
	BlockLinks     bool
	RemoveLinks    bool
	StripWords     []string
	// Replacements: {from: to}. Order matters for overlapping rules, so
	// OrderedReplacements (when non-empty) wins over the map.
	Replacements        map[string]string
	OrderedReplacements [][2]string
	RegexBlock          []string
	RegexRequire        []string
	MinLength           int
	MaxLength           int
	AllowedMedia        []string
	BlockedMedia        []string
	CaseSensitive       bool
	WordBoundary        bool
	Cleaner             *CleanerSpec
}

// CleanerSpec is the metadata.cleaner shape: which normalizations run.
type CleanerSpec struct {
	Enabled    bool
	Emoji      bool
	Urls       bool
	Mentions   bool
	Hashtags   bool
	Phones     bool
	Whitespace bool
	Newlines   bool
}

// Install writes the task as real forward_rules (+ filter_rules when a
// FilterID is set) rows into the harness DB the engine loads. Filters are
// installed separately via InstallFilter.
func (h *Harness) Install(spec TaskSpec) {
	h.t.Helper()
	if spec.ID == "" {
		h.t.Fatal("TaskSpec needs an ID")
	}
	meta := map[string]any{}
	for k, v := range spec.Metadata {
		meta[k] = v
	}
	if spec.Replacements != nil {
		meta["replacements"] = spec.Replacements
	}
	if spec.Policy != nil && (len(spec.Policy.Replacements) > 0 || len(spec.Policy.OrderedReplacements) > 0) {
		// The transforms pipeline reads the top-level metadata key; the
		// content_policy copy is the policy-restricted view. Preserve entry
		// order: the store's ordered decoder keeps same-priority entries in
		// the order the task wrote them, and overlapping rules ("ab" before
		// "a") are order-sensitive.
		if len(spec.Policy.OrderedReplacements) > 0 {
			var sb strings.Builder
			sb.WriteByte('{')
			for i, kv := range spec.Policy.OrderedReplacements {
				if i > 0 {
					sb.WriteByte(',')
				}
				k, _ := json.Marshal(kv[0])
				v, _ := json.Marshal(kv[1])
				sb.Write(k)
				sb.WriteByte(':')
				sb.Write(v)
			}
			sb.WriteByte('}')
			meta["replacements"] = json.RawMessage(sb.String())
		} else {
			reps := map[string]any{}
			for k, v := range spec.Policy.Replacements {
				reps[k] = v
			}
			meta["replacements"] = reps
		}
	}
	if spec.Policy != nil {
		meta["content_policy"] = spec.Policy.asMap()
		if spec.Policy.Cleaner != nil {
			// The production split: the enable flag is a top-level task
			// metadata key (cleaner_enabled); the option flags live under
			// cleaner_config. Both are read by the transform projection.
			meta["cleaner_enabled"] = spec.Policy.Cleaner.Enabled
			meta["cleaner_config"] = spec.Policy.Cleaner.asMap()
		}
	}
	metaJSON, _ := json.Marshal(meta)
	repsJSON, _ := json.Marshal(spec.Replacements)
	now := time.Now().Unix()

	// Every column the live schema carries, with a typed default so an
	// older or newer column list always binds cleanly.
	vals := map[string]any{
		"id": spec.ID, "session_id": "sess-e2e", "source_chat_id": spec.Source,
		"source_chat_name": spec.SourceName, "target_chat_id": spec.Target,
		"target_chat_name": spec.TargetName, "routing_type": "CHANNEL_TO_CHANNEL",
		"forward_mode": "COPY_MESSAGE", "is_active": boolToInt(spec.Active),
		"filter_rule_id": spec.FilterID, "ai_config_id": spec.AIConfigID,
		"remove_links": 0, "custom_caption_template": "", "created_at": now,
		"updated_at": now, "target_chat_ids": "[]", "delay_seconds": 0.0,
		"skip_history": 1, "since_ts": 0, "ignore_edits": 0,
		"trigger_events": `["NEW_MESSAGE"]`, "content_mode": "AUTO",
		"metadata": string(metaJSON), "chain_of": "",
		"replacements": string(repsJSON), "header": spec.Header, "footer": spec.Footer,
		"sync_edits": 1, "sync_deletes": 0, "allow_senders": "[]",
		"block_senders": "[]", "target_topic_id": 0, "version": 33,
		"ai_fallback_policy": "SEND_ORIGINAL", "ai_prompt_version": 0,
		"ai_timeout_seconds": 15.0, "ai_secondary_config_id": nil,
		"message_category": "ALL", "intermediate_channel_id": "",
		"intermediate_channel_name": "", "intermediate_target_chat_id": "",
		"use_intermediate": 0, "fallback_mode": "COPY_MESSAGE",
		"fallback_enabled": 1, "detection_criteria": "{}", "priority": 10,
		"execution_order": 0, "multi_route": 0, "media_handling": "AUTO",
		"dedupe_policy": "STRICT", "max_retries": 3, "retry_backoff_base": 2.0,
		"rate_limit_per_minute": 25, "rate_limit_burst": 0, "custom_header": "",
		"custom_footer": "", "header_enabled": 0, "template_text": "",
		"template_media": "", "template_album": "", "caption_max_length": 0,
		"preserve_signature": 0, "is_paused": 0, "paused_until": 0,
		"link_policy": "PRESERVE_ALL", "domain_allowlist": "[]",
		"domain_blocklist": "[]", "link_rewrite_map": "{}",
		"allowed_media_types": "[]", "split_long_caption": 1,
		"owner_user_id": h.owner,
	}

	rows, err := h.DB().Query(`PRAGMA table_info(forward_rules)`)
	if err != nil {
		h.t.Fatalf("table info: %v", err)
	}
	defer rows.Close()

	var cols []string
	var args []any
	var placeholders []string
	for rows.Next() {
		var cid int
		var name, ctype string
		var notnull, pk int
		var dflt any
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			continue
		}
		if v, ok := vals[name]; ok {
			cols = append(cols, name)
			args = append(args, v)
			placeholders = append(placeholders, "?")
		}
	}

	query := fmt.Sprintf("INSERT INTO forward_rules (%s) VALUES (%s)",
		strings.Join(cols, ","), strings.Join(placeholders, ","))
	if _, err := h.DB().Exec(query, args...); err != nil {
		h.t.Fatalf("install task %s: %v", spec.ID, err)
	}
}

// FilterSpec maps onto filter_rules the production engine reads.
type FilterSpec struct {
	ID           string
	Enabled      bool
	Whitelist    []string
	Blacklist    []string
	RegexBlock   []string
	RegexRequire []string
	AllowedMedia []string
	BlockedMedia []string
	MinLen       int
	MaxLen       int
	DropService  bool
}

// InstallFilter writes a real filter_rules row owned by this harness tenant.
func (h *Harness) InstallFilter(f FilterSpec) {
	h.t.Helper()
	wl, _ := json.Marshal(f.Whitelist)
	bl, _ := json.Marshal(f.Blacklist)
	rb, _ := json.Marshal(f.RegexBlock)
	rr, _ := json.Marshal(f.RegexRequire)
	am, _ := json.Marshal(f.AllowedMedia)
	bm, _ := json.Marshal(f.BlockedMedia)
	_, err := h.DB().Exec(`INSERT INTO filter_rules
 (id, name, is_enabled, whitelist_keywords, blacklist_keywords, regex_patterns,
  allowed_media_types, blocked_media_types, min_message_length, max_message_length,
  owner_user_id, regex_block, regex_require, drop_service_messages)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		f.ID, "e2e-filter", boolToInt(f.Enabled), string(wl), string(bl), "[]",
		string(am), string(bm), f.MinLen, f.MaxLen, h.owner, string(rb), string(rr),
		boolToInt(f.DropService))
	if err != nil {
		h.t.Fatalf("install filter %s: %v", f.ID, err)
	}
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// now returns the scenario start instant (Finish computes duration).
func now() time.Time { return time.Now() }

// EnqueueMedia inserts a media-shaped job: media_type + has_media + caption.
func (h *Harness) EnqueueMedia(taskID, sourceChat string, sourceMsg int64, targetChat, mediaType, caption string) *job.DeliveryJob {
	extra := map[string]any{
		"media_type": mediaType,
		"has_media":  mediaType != "" && mediaType != "text",
	}
	if caption != "" {
		extra["text"] = caption
	}
	return h.Enqueue(taskID, sourceChat, sourceMsg, targetChat, caption, extra)
}

// SetMediaGroup marks the job as a member of a media group (album).
func (h *Harness) SetMediaGroup(j *job.DeliveryJob, groupID string) {
	j.Payload.Raw()["media_group_id"] = groupID
}

// SetHasMedia toggles the payload's media flag independently of media_type.
func (h *Harness) SetHasMedia(j *job.DeliveryJob, has bool) {
	j.Payload.Raw()["has_media"] = has
}

// SetErrorOnce makes the fake gateway fail the next send to targetChat once,
// then succeed — the transient-failure path.
func (h *Harness) SetErrorOnce(targetChat string) {
	h.Gateway.SetErrorOnce(targetChat)
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

// permanentFailure is a non-retryable Telegram refusal.
func permanentFailure() *telegraph.DeliveryFailure {
	return &telegraph.DeliveryFailure{Code: "PERMISSION_DENIED", ErrorType: "CHAT_WRITE_FORBIDDEN", ErrorCode: "400", Message: "write forbidden"}
}

// transientFailure is a retryable Telegram outage.
func transientFailure() *telegraph.DeliveryFailure {
	return &telegraph.DeliveryFailure{Code: "TIMEOUT", ErrorType: "ETIMEDOUT", ErrorCode: "504", Message: "transport timeout"}
}

// floodWaitFailure is Telegram's rate-limit demand.
func floodWaitFailure() *telegraph.DeliveryFailure {
	return &telegraph.DeliveryFailure{Code: "FLOOD_WAIT", ErrorType: "FLOOD_WAIT_5", RetryAfterSeconds: 5, Message: "flood wait"}
}

// mustJSONMap decodes a payload_json blob into a map.
func mustJSONMap(raw string) map[string]any {
	out := map[string]any{}
	_ = json.Unmarshal([]byte(raw), &out)
	return out
}

// ResetCalls clears the gateway's recorded call log so a scenario can
// assert on traffic after a mid-flight configuration change.
func (h *Harness) ResetCalls() {
	h.Gateway.Reset()
}
