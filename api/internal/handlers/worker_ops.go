package handlers

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"time"
)

// workerOps exposes the Go Worker's operational surface (/ops on :8090) to
// administrators. The WORKER is the single telemetry owner: the API proxies
// its snapshot and never collects a second copy. An unreachable worker is
// reported as unreachable — never a zeroed-out snapshot that would look like
// an idle, healthy worker.
//
// Contract: /root/.hermes/cache/scratch/devops-contract.md sections 1-2.

// workerOpsURL is the base the worker's :8090 listener is reachable on.
// The worker deliberately publishes no ports to the host (the health and
// ops surface is internal-only), so the API reaches it over the shared
// compose network.
const (
	defaultWorkerOpsURL = "http://worker:8090"
	opsTimeout          = 3 * time.Second
	maxErrorLimit       = 256
)

func workerOpsBase() string {
	if v := os.Getenv("ATF_WORKER_OPS_URL"); v != "" {
		return v
	}
	return defaultWorkerOpsURL
}

// fetchOps proxies one worker /ops* path, returning the decoded JSON body
// and the HTTP status the worker returned. A network failure is distinct
// from a worker-side 5xx so the handler can tell "unreachable" from "sick".
func fetchOps(ctx context.Context, path string, query url.Values) (map[string]any, int, error) {
	u := workerOpsBase() + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	ctx, cancel := context.WithTimeout(ctx, opsTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, 0, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20)) // 1MiB ceiling: a full ops snapshot + a bounded error ring
	if err != nil {
		return nil, resp.StatusCode, err
	}
	var out map[string]any
	if len(body) > 0 {
		if err := json.Unmarshal(body, &out); err != nil {
			return nil, resp.StatusCode, err
		}
	}
	return out, resp.StatusCode, nil
}

// WorkerStatus proxies GET /api/admin/worker/status — the worker overview:
// state, SHA, uptime, goroutines, utilization (active/configured pool, never
// goroutines), connectivity chips and the worker's switches.
func (h *Handlers) WorkerStatus(w http.ResponseWriter, r *http.Request) {
	body, status, err := fetchOps(r.Context(), "/ops", nil)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"worker_reachable": false,
			"error":            "worker ops endpoint unreachable",
		})
		return
	}
	out := map[string]any{"worker_reachable": true}
	for k, v := range body {
		out[k] = v
	}
	writeJSON(w, status, out)
}

// WorkerQueue proxies GET /api/admin/worker/queue — queue depth, latency
// percentiles, backlog trend and the derived load status.
func (h *Handlers) WorkerQueue(w http.ResponseWriter, r *http.Request) {
	body, status, err := fetchOps(r.Context(), "/ops/queue", nil)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"worker_reachable": false,
			"error":            "worker ops endpoint unreachable",
		})
		return
	}
	writeJSON(w, status, body)
}

// WorkerTasks proxies GET /api/admin/worker/tasks — the per-task operational
// rows the worker derives from its durable delivery_jobs (no new tables).
func (h *Handlers) WorkerTasks(w http.ResponseWriter, r *http.Request) {
	body, status, err := fetchOps(r.Context(), "/ops/tasks", nil)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"worker_reachable": false,
			"error":            "worker ops endpoint unreachable",
		})
		return
	}
	writeJSON(w, status, body)
}

// WorkerErrors proxies GET /api/admin/worker/errors with the bounded query
// surface: severity, component, rule_id, since (epoch seconds) and a limit
// capped at 256. The worker owns the dedup ring; the API only filters.
func (h *Handlers) WorkerErrors(w http.ResponseWriter, r *http.Request) {
	q := url.Values{}
	if s := r.URL.Query().Get("severity"); s != "" {
		q.Set("severity", s)
	}
	if c := r.URL.Query().Get("component"); c != "" {
		q.Set("component", c)
	}
	if rid := r.URL.Query().Get("rule_id"); rid != "" {
		q.Set("rule_id", rid)
	}
	if since := r.URL.Query().Get("since"); since != "" {
		if _, err := strconv.ParseInt(since, 10, 64); err == nil {
			q.Set("since", since)
		}
	}
	if lim := r.URL.Query().Get("limit"); lim != "" {
		if n, err := strconv.Atoi(lim); err == nil && n > 0 {
			if n > maxErrorLimit {
				n = maxErrorLimit
			}
			q.Set("limit", strconv.Itoa(n))
		}
	}
	body, status, err := fetchOps(r.Context(), "/ops/errors", q)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"worker_reachable": false,
			"error":            "worker ops endpoint unreachable",
		})
		return
	}
	writeJSON(w, status, body)
}

// WorkerIncidents proxies GET /api/admin/worker/incidents — the worker's
// error-intelligence view: repeated failures grouped into incidents with a
// lifecycle (open/recovered/recurrent/acknowledged/suppressed), affected
// workers/rules/jobs, and a SUSPECTED pattern with an actionable hint.
// The worker owns the aggregation; the API only passes the state filter
// through after validating it against the lifecycle vocabulary.
func (h *Handlers) WorkerIncidents(w http.ResponseWriter, r *http.Request) {
	q := url.Values{}
	if st := r.URL.Query().Get("state"); st != "" {
		switch st {
		case "open", "recovered", "recurrent", "acknowledged", "suppressed":
			q.Set("state", st)
		default:
			writeJSON(w, http.StatusBadRequest, map[string]any{
				"error": "invalid state filter",
			})
			return
		}
	}
	body, status, err := fetchOps(r.Context(), "/ops/incidents", q)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"worker_reachable": false,
			"error":            "worker ops endpoint unreachable",
		})
		return
	}
	writeJSON(w, status, body)
}

// WorkerIncidentAck proxies POST /api/admin/worker/incidents/ack — an
// operator acknowledging an incident by its stable id. POST-only: the ack
// mutates operational state, so a GET prefetch or a browser navigational
// request can never ackidentally acknowledge anything.
func (h *Handlers) WorkerIncidentAck(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"error": "POST required"})
		return
	}
	id := r.URL.Query().Get("id")
	if id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "incident id required"})
		return
	}
	q := url.Values{}
	q.Set("id", id)
	body, status, err := fetchOps(r.Context(), "/ops/incidents/ack", q)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"worker_reachable": false,
			"error":            "worker ops endpoint unreachable",
		})
		return
	}
	writeJSON(w, status, body)
}
