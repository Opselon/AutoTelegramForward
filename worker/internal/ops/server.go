package ops

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Server serves the /ops surface on the worker's existing health listener.
//
// It is NOT a second HTTP server: Attach() registers these handlers on the
// health mux, so :8090 keeps serving /healthz, /livez, /metrics AND /ops
// from one process, one listener, one collector.
//
// Every response is built from the collector's in-memory state plus, for
// the queue and task surfaces, ONE bounded read-only query against the
// durable queue. No per-message writes, no per-heartbeat writes, no fake
// values: a worker that cannot reach its DB reports db_connected=false and
// the fields it could not read are reported as an error — never zeros
// pretending to be data.
type Server struct {
	collector   *Collector
	queue       QueueStoreReader
	tasks       TaskStoreReader
	rulesDB     *sql.DB
	ruleBackend string
	workerID    string
	// controls is the §10 worker-control surface (pause/resume). May be nil
	// when a Server is constructed without one — the routes then report the
	// section unavailable rather than fabricating any control state.
	controls *ControlPlane

	// snapshotMu serializes only the DB reads inside a request, so two
	// concurrent /ops polls cannot stack two identical GROUP BY queries on
	// the queue's writer connection.
	snapshotMu sync.Mutex
}

// NewServer builds the ops server.
//
// queue and tasks may be nil (the handlers then report what they can and
// mark the DB-backed fields unreachable — honest on a worker whose store is
// a test double). rulesDB is the forward_rules side of the per-task join;
// nil skips the join and the task rows report queue aggregates only.
func NewServer(c *Collector, queue QueueStoreReader, tasks TaskStoreReader, rulesDB *sql.DB, ruleBackend, workerID string, controls *ControlPlane) *Server {
	return &Server{
		collector:   c,
		queue:       queue,
		tasks:       tasks,
		rulesDB:     rulesDB,
		ruleBackend: ruleBackend,
		workerID:    workerID,
		controls:    controls,
	}
}

// Attach registers the /ops routes on an existing mux. The health server
// owns the listener; this adds the routes to its mux so :8090 is one surface.
func (s *Server) Attach(mux *http.ServeMux) {
	mux.HandleFunc("/ops", s.handleOps)
	mux.HandleFunc("/ops/", s.handleOpsRoute)
}

// handleOpsRoute dispatches /ops/queue, /ops/tasks, /ops/errors.
func (s *Server) handleOpsRoute(w http.ResponseWriter, r *http.Request) {
	switch strings.TrimPrefix(r.URL.Path, "/ops/") {
	case "queue":
		s.handleQueue(w, r)
	case "queue/diagnostics":
		s.handleQueueDiagnostics(w, r)
	case "incidents":
		s.handleIncidents(w, r)
	case "incidents/ack":
		s.handleIncidentAck(w, r)
	case "control/status":
		s.handleControlStatus(w, r)
	case "control/pause":
		s.handleControlPause(w, r)
	case "control/resume":
		s.handleControlResume(w, r)
	case "tasks":
		s.handleTasks(w, r)
	case "errors":
		s.handleErrors(w, r)
	default:
		writeJSONCode(w, http.StatusNotFound, map[string]any{"error": "not found"})
	}
}

// handleOps serves the full operational snapshot (the contract's §1.1).
func (s *Server) handleOps(w http.ResponseWriter, r *http.Request) {
	s.collector.Heartbeat()
	body := map[string]any{"worker": s.workerSnapshot()}
	if q, err := s.queueSnapshot(r.Context()); err != nil {
		// A queue that cannot be read is REPORTED, not papered over: the
		// worker object already carries db_connected; this object carries
		// the reason, so the panel shows "queue unreadable" instead of a
		// pile of zeros that look like an empty queue.
		body["queue"] = map[string]any{"error": sanitize(err.Error()), "db_connected": s.collector.DBConnected()}
	} else {
		body["queue"] = q
	}
	body["pipeline"] = s.collector.Stages()
	if tasks, err := s.taskRows(r.Context()); err != nil {
		body["tasks"] = map[string]any{"error": sanitize(err.Error())}
	} else {
		body["tasks"] = tasks
	}
	body["errors"] = s.errorSnapshot(queryLimit(r, 64))
	writeJSONCode(w, http.StatusOK, body)
}

// handleQueue serves the queue depth breakdown + latency percentiles.
func (s *Server) handleQueue(w http.ResponseWriter, r *http.Request) {
	q, err := s.queueSnapshot(r.Context())
	if err != nil {
		writeJSONCode(w, http.StatusServiceUnavailable, map[string]any{
			"error":        sanitize(err.Error()),
			"db_connected": s.collector.DBConnected(),
		})
		return
	}
	writeJSONCode(w, http.StatusOK, q)
}

// handleQueueDiagnostics serves the §5 diagnostics: the signals that
// separate a busy queue from a broken one (stuck claims, starved pending,
// poison jobs, an imminent retry storm). The section is absent when the
// store does not implement QueueDiagnostician — an older backend — rather
// than fabricating zeros.
func (s *Server) handleQueueDiagnostics(w http.ResponseWriter, r *http.Request) {
	diag, err := NewQueueDiagnostics(r.Context(), s.queue, time.Now().Unix())
	if err != nil {
		writeJSONCode(w, http.StatusServiceUnavailable, map[string]any{
			"error":        sanitize(err.Error()),
			"db_connected": s.collector.DBConnected(),
		})
		return
	}
	if diag == nil {
		writeJSONCode(w, http.StatusOK, map[string]any{
			"diagnostics": nil,
			"available":   false,
		})
		return
	}
	writeJSONCode(w, http.StatusOK, map[string]any{
		"diagnostics": diag,
		"available":   true,
	})
}

// handleIncidents serves the §9 error-intelligence view: repeated errors
// grouped into incidents with a lifecycle (open / recovered / recurrent /
// acknowledged / suppressed), affected workers/rules/jobs, and a SUSPECTED
// pattern with an actionable hint when one matched.
//
// State transitions happen on observation and on sweep: the sweep is what
// ages an incident into "recovered" when nobody is reporting new
// occurrences, so the console cannot show a problem as live forever just
// because it once was.
func (s *Server) handleIncidents(w http.ResponseWriter, r *http.Request) {
	s.collector.incidents.sweep()
	state := r.URL.Query().Get("state")
	list := s.collector.incidents.incidents()
	if state != "" {
		filtered := make([]*Incident, 0, len(list))
		for _, inc := range list {
			if inc.State == state {
				filtered = append(filtered, inc)
			}
		}
		list = filtered
	}
	writeJSONCode(w, http.StatusOK, map[string]any{
		"total":     len(list),
		"incidents": list,
	})
}

// handleIncidentAck acknowledges an incident by its stable ID. POST-only,
// admin-gated by the same auth that fronts the whole ops surface (the API
// proxy), with the acker recorded for the audit trail. An ack for an unknown
// ID is an error, never a silent success.
func (s *Server) handleIncidentAck(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeJSONCode(w, http.StatusMethodNotAllowed, map[string]any{"error": "POST required"})
		return
	}
	id := r.URL.Query().Get("id")
	if id == "" {
		writeJSONCode(w, http.StatusBadRequest, map[string]any{"error": "incident id required"})
		return
	}
	if err := s.collector.incidents.ack(id, r.RemoteAddr); err != nil {
		writeJSONCode(w, http.StatusNotFound, map[string]any{"error": sanitize(err.Error())})
		return
	}
	writeJSONCode(w, http.StatusOK, map[string]any{"acknowledged": id})
}

// handleTasks serves the per-task operational rows.
func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	tasks, err := s.taskRows(r.Context())
	if err != nil {
		writeJSONCode(w, http.StatusServiceUnavailable, map[string]any{"error": sanitize(err.Error())})
		return
	}
	writeJSONCode(w, http.StatusOK, map[string]any{"tasks": tasks})
}

// handleErrors serves the bounded, deduped error/warn ring with the
// contract's filters: severity, component, rule_id, since, limit (≤256).
func (s *Server) handleErrors(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	var since time.Time
	if v := q.Get("since"); v != "" {
		if parsed, err := time.Parse(time.RFC3339, v); err == nil {
			since = parsed
		}
	}
	items := s.collector.Errors().Filter(q.Get("severity"), q.Get("component"), q.Get("rule_id"), since, queryLimit(r, 256))
	writeJSONCode(w, http.StatusOK, map[string]any{
		"total": s.collector.Errors().Total(),
		"items": items,
	})
}

// --- snapshot builders ---------------------------------------------------- //

// workerSnapshot is the §1.1 "worker" object. Every field is either
// configuration the process resolved at startup or an atomic the collector
// owns — no DB read, no probe. The probes carry their own last-checked
// timestamps so "false" is distinguishable from "never checked".
func (s *Server) workerSnapshot() map[string]any {
	c := s.collector
	goroutines, memoryBytes := c.runtimeStats()
	sw := c.Switches()
	out := map[string]any{
		"state":              StateName(c.State()),
		"worker_id":          c.WorkerID(),
		"commit_sha":         c.CommitSHA(),
		"version":            c.Version(),
		"uptime_seconds":     int(time.Since(c.StartedAt()).Seconds()),
		"started_at":         c.StartedAt().UTC().Format(time.RFC3339),
		"goroutines":         goroutines,
		"cpu_percent":        c.cpuPercent(),
		"memory_bytes":       memoryBytes,
		"num_workers":        c.NumWorkers(), // CONFIGURED, never live goroutines
		"active":             c.Active(),
		"utilization":        round3(c.Utilization()),
		"processing_mode":    c.ProcessingMode(),
		"queue_backend":      c.QueueBackend(), // label, never a DSN
		"db_connected":       c.DBConnected(),
		"telegram_connected": c.TelegramConnected(),
		"ai_connected":       c.AIConnected(),
		"switches": map[string]any{
			"worker_enabled": sw.WorkerEnabled,
			"transforms":     sw.Transforms,
			"autoreply":      sw.Autoreply,
		},
	}
	if ts := c.dbCheckedAt.Load(); ts > 0 {
		out["db_checked_at"] = time.Unix(ts, 0).UTC().Format(time.RFC3339)
	}
	if ts := c.tgCheckedAt.Load(); ts > 0 {
		out["telegram_checked_at"] = time.Unix(ts, 0).UTC().Format(time.RFC3339)
	}
	// ai_checked_at is the difference between "no provider configured" and
	// "the provider is failing": ai_connected alone cannot tell them apart,
	// and the contract's probe semantics (the LAST provider outcome) leave
	// the flag false until the first call — a timestamp of zero means "no
	// call yet", never "provider down".
	if ts := c.aiCheckedAt.Load(); ts > 0 {
		out["ai_checked_at"] = time.Unix(ts, 0).UTC().Format(time.RFC3339)
	}
	if hb := c.lastHeartbeat.Load(); hb > 0 {
		out["last_heartbeat"] = time.Unix(hb, 0).UTC().Format(time.RFC3339)
		out["heartbeat_age_seconds"] = c.HeartbeatAge()
	}
	// health_state is the DERIVED liveness answer the console paints with:
	//   online  — heartbeat fresh (worker is alive and observable)
	//   offline — heartbeat older than the freshness budget (process dead
	//             or wedged; nothing else can say this — an old DB record
	//             must not keep a worker "online" forever)
	//   degraded — the process reports it is alive but its own state is
	//             degraded, or its DB/Telegram probes are down
	//   starting / stopping / unknown — the execution state when it is not
	//             yet ready or already shutting down.
	// A worker that nobody polls still heartbeats on its own tick, so
	// "nobody is looking" never collides with "worker is down".
	out["health_state"] = derivedHealthState(c, c.HeartbeatAge())
	return out
}

// queueSnapshot is the §1.2 "queue" object: one bounded DB read for the
// status counts plus the in-memory latency rings and rate.
func (s *Server) queueSnapshot(ctx context.Context) (map[string]any, error) {
	c := s.collector
	out := map[string]any{
		"jobs_per_second": round3(c.jobsPerSecond()),
		"latency":         latencyObject(c.E2E()),
		"queue_wait":      latencyObject(c.QueueWait()),
		"capacity":        c.MaxQueueSize(),
		"backlog_trend":   c.backlogTrend(),
		"load_status":     c.loadStatus(),
		"delivered":       c.Delivered(),
	}
	if s.queue == nil {
		out["error"] = "queue reader unavailable"
		return out, nil
	}
	s.snapshotMu.Lock()
	stats, err := s.queue.QueueStats(ctx)
	s.snapshotMu.Unlock()
	if err != nil {
		return out, err
	}
	out["pending"] = stats.Pending
	out["processing"] = stats.Processing
	out["delivered"] = stats.Delivered
	out["failed"] = stats.Failed
	out["delayed"] = stats.Delayed
	out["retry_pending"] = stats.RetryPending
	out["dead_letter"] = stats.DeadLetter
	if stats.Unknown > 0 {
		out["unknown"] = stats.Unknown
	}
	if stats.OldestPendingAgeSeconds > 0 {
		out["oldest_pending_age_seconds"] = round3(stats.OldestPendingAgeSeconds)
	}
	// The §5 diagnostics ride the same snapshot so one /ops/queue poll gets
	// the status counts AND the "busy vs broken" signals in a single
	// request. Unavailable (legacy store) is reported by absence, never by
	// fabricated zeros.
	if diag, derr := NewQueueDiagnostics(ctx, s.queue, time.Now().Unix()); derr == nil && diag != nil {
		out["diagnostics"] = diag
	}
	// §4 aggregation windows: delivered count per window with real
	// timestamps. -1 = not enough history yet (rendered as such, never as
	// zero). Reported only when the history object exists.
	if tw := c.ThroughputWindows(); tw != nil {
		out["throughput_windows"] = tw
		out["throughput_history_minutes"] = c.throughput.historyAgeMinutes(time.Now())
	}
	// The durable count is authoritative whenever the DB is reachable;
	// reconcile in ONE direction (durable ≥ memory) so a restart cannot
	// reset the panel's delivered number while the queue still holds the
	// truth. This is exactly the two-counters-that-never-reconciled defect.
	if stats.Delivered > c.Delivered() {
		c.delivered.Store(stats.Delivered)
	}
	return out, nil
}

// taskRows is the §1.4 per-task list: GROUP BY over delivery_jobs (the only
// allowed DB read for this surface), joined with forward_rules for the
// task's own fields.
func (s *Server) taskRows(ctx context.Context) ([]map[string]any, error) {
	if s.tasks == nil {
		return []map[string]any{}, nil
	}
	s.snapshotMu.Lock()
	rows, err := s.tasks.TaskRows(ctx, 100)
	s.snapshotMu.Unlock()
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(rows))
	for _, r := range rows {
		ids = append(ids, r.RuleID)
	}
	joined := map[string]*joinRuleInfo{}
	if s.rulesDB != nil && len(ids) > 0 {
		if info, jerr := ReadRulesForTasks(ctx, s.rulesDB, s.ruleBackend, ids); jerr == nil {
			joined = info
		}
		// A failed join is not fatal: the rows still report their queue
		// aggregates with the rule fields absent — the honest half-answer.
	}
	out := make([]map[string]any, 0, len(rows))
	for _, r := range rows {
		row := map[string]any{
			"rule_id":    r.RuleID,
			"pending":    r.Pending,
			"processing": r.Processing,
			"delivered":  r.Delivered,
			"failed":     r.Failed,
			"retries":    r.Retries,
			"worker_id":  s.workerID,
		}
		if r.LastSuccessAt != "" {
			row["last_success_at"] = r.LastSuccessAt
		}
		if r.LastError != "" {
			row["last_error"] = r.LastError
		}
		if r.LastErrorAt != "" {
			row["last_error_at"] = r.LastErrorAt
		}
		if info, ok := joined[r.RuleID]; ok && info.hasRow {
			row["source"] = info.source
			row["target"] = info.target
			row["enabled"] = info.enabled
			if info.updated > 0 {
				row["config_updated_at"] = RFC3339(info.updated)
			}
		} else {
			// Jobs exist but no forward_rules row: the rule was deleted
			// while its jobs sat in the queue. enabled=false so the panel
			// shows "rule gone", not "task off".
			row["enabled"] = false
		}
		// Throughput and latency are in-memory rolling views per rule —
		// never DB writes and never per-message aggregates.
		tps, lat := s.collector.TaskStats(r.RuleID)
		row["throughput_per_min"] = round3(tps)
		if lat > 0 {
			row["latency_ms_avg"] = round3(lat)
		}
		out = append(out, row)
	}
	return out, nil
}

// errorSnapshot is the §1.5 errors object.
func (s *Server) errorSnapshot(limit int) map[string]any {
	return map[string]any{
		"total": s.collector.Errors().Total(),
		"items": s.collector.Errors().Filter("", "", "", time.Time{}, limit),
	}
}

// latencyObject is the shared {"p50","p95","p99","units":"ms"} shape.
func latencyObject(r *latencyRing) map[string]any {
	return map[string]any{
		"p50":   round3(r.percentile(50)),
		"p95":   round3(r.percentile(95)),
		"p99":   round3(r.percentile(99)),
		"units": "ms",
		"count": r.countInWindow(),
	}
}

// queryLimit parses ?limit= clipped to the contract's bound (≤256).
func queryLimit(r *http.Request, dflt int) int {
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			if n > 256 {
				return 256
			}
			return n
		}
	}
	return dflt
}

func writeJSONCode(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(body)
}
