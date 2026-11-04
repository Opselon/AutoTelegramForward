// Package health is the worker's readiness and liveness surface.
//
// A process being alive is NOT the same as being able to execute Telegram
// work: readiness additionally requires the delivery store to be reachable
// and the fence to be answerable. liveness only requires the run loop to be
// making progress. The split exists so a load balancer never routes to a
// worker whose database is down.
package health

import (
	"context"
	"encoding/json"
	"net/http"
	"sync/atomic"
	"time"
)

// State is the worker's current execution state.
type State uint32

const (
	StateStarting State = iota
	StateReady
	StateDegraded
	StateStopping
)

func (s State) String() string {
	switch s {
	case StateReady:
		return "ready"
	case StateDegraded:
		return "degraded"
	case StateStopping:
		return "stopping"
	}
	return "starting"
}

// Tracker is the worker state holder the health surface reports.
//
// It is an INTERFACE on purpose: the operational collector
// (internal/ops.Collector, through ops.TrackerBridge) is the metrics
// owner, and the health surface must read the counters the delivery
// engine actually increments rather than a second set that counts
// nothing. The in-process default (localTracker) is the pre-ops
// implementation, kept so a health.Server built without a collector still
// works — tests and the worker's own unit surface.
//
// THE DEFECT THE INTERFACE CLOSES: /healthz used to report a Tracker's
// delivered=0 on a worker whose engine had delivered 41 messages, because
// the engine counted elsewhere. Exactly one owner may hold these numbers.
type Tracker interface {
	// SetState changes the worker's execution state.
	SetState(s State)
	// State returns the current execution state.
	State() State
	// Active is the current in-flight delivery count.
	Active() int64
	// Enter marks one delivery as in-flight, returning the new count.
	Enter() int64
	// Leave marks one delivery as complete, returning the new count.
	Leave() int64
	// Outcome records a delivery outcome (sent|retried|failed|unknown).
	Outcome(kind string)
	// Snapshot returns the operational counters for the /healthz report.
	Snapshot() map[string]any
}

// localTracker is the in-process default: a concurrency-safe state holder
// that counts what THIS process did. It is the pre-ops implementation,
// unchanged, and exists so the health surface remains usable standalone.
type localTracker struct {
	state       atomic.Uint32
	concurrency atomic.Int64
	delivered   atomic.Int64
	retried     atomic.Int64
	failed      atomic.Int64
	unknown     atomic.Int64
	startedAt   time.Time
}

// NewTracker returns a Tracker in StateStarting. Callers that own an
// ops.Collector should pass ops.NewTrackerBridge(collector) to NewServer
// instead, so the health surface reports the collector's counters.
func NewTracker() Tracker {
	return &localTracker{startedAt: time.Now()}
}

func (t *localTracker) SetState(s State) { t.state.Store(uint32(s)) }

func (t *localTracker) State() State { return State(t.state.Load()) }

func (t *localTracker) Active() int64 { return t.concurrency.Load() }

func (t *localTracker) Enter() int64 { return t.concurrency.Add(1) }

func (t *localTracker) Leave() int64 { return t.concurrency.Add(-1) }

func (t *localTracker) Outcome(kind string) {
	switch kind {
	case "sent":
		t.delivered.Add(1)
	case "retried":
		t.retried.Add(1)
	case "failed":
		t.failed.Add(1)
	case "unknown":
		t.unknown.Add(1)
	}
}

func (t *localTracker) Snapshot() map[string]any {
	return map[string]any{
		"state":          t.State().String(),
		"uptime_seconds": int(time.Since(t.startedAt).Seconds()),
		"active":         t.Active(),
		"delivered":      t.delivered.Load(),
		"retried":        t.retried.Load(),
		"failed":         t.failed.Load(),
		"unknown":        t.unknown.Load(),
	}
}

// Server serves /healthz (readiness) and /livez (liveness).
//
// readiness checks the delivery store's reachability through the supplied
// probe; liveness only needs the run loop to be advancing.
//
// Mux exposes the HTTP mux so the ops surface can attach /ops to the SAME
// listener — one process, one port, one telemetry owner.
type Server struct {
	tracker Tracker
	probe   func(ctx context.Context) error
	server  *http.Server
	mux     *http.ServeMux
}

// NewServer binds the health surface on addr.
func NewServer(addr string, tracker Tracker, probe func(ctx context.Context) error) *Server {
	mux := http.NewServeMux()
	s := &Server{tracker: tracker, probe: probe,
		mux:    mux,
		server: &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}}

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		// Degraded is still *running* — but readiness is about the ability to
		// execute, and a store that cannot be reached means no execution.
		if err := s.probe(ctx); err != nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{
				"status": "not_ready", "store_error": err.Error(),
				"state": tracker.State().String(),
			})
			return
		}
		body := map[string]any{"status": "ready"}
		for k, v := range tracker.Snapshot() {
			body[k] = v
		}
		writeJSON(w, http.StatusOK, body)
	})
	mux.HandleFunc("/livez", func(w http.ResponseWriter, r *http.Request) {
		// Liveness: the loop is advancing as long as we are not stopping.
		if tracker.State() == StateStopping {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "stopping"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"status": "alive"})
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, tracker.Snapshot())
	})
	return s
}

// Mux returns the mux the health surface is bound to, so the ops surface
// can attach its routes to the same listener instead of opening a second
// port. Nil is never returned from a server built with NewServer.
func (s *Server) Mux() *http.ServeMux { return s.mux }

// Start begins serving.
func (s *Server) Start() error {
	return s.server.ListenAndServe()
}

// Shutdown stops serving gracefully.
func (s *Server) Shutdown(ctx context.Context) error {
	return s.server.Shutdown(ctx)
}

func writeJSON(w http.ResponseWriter, code int, body map[string]any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(body)
}
