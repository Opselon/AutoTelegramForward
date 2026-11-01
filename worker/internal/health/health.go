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

// Tracker is a concurrency-safe worker state holder.
type Tracker struct {
	state       atomic.Uint32
	concurrency atomic.Int64
	delivered   atomic.Int64
	retried     atomic.Int64
	failed      atomic.Int64
	unknown     atomic.Int64
	startedAt   time.Time
}

// NewTracker returns a Tracker in StateStarting.
func NewTracker() *Tracker {
	return &Tracker{startedAt: time.Now()}
}

// SetState changes the worker's execution state.
func (t *Tracker) SetState(s State) { t.state.Store(uint32(s)) }

// State returns the current execution state.
func (t *Tracker) State() State { return State(t.state.Load()) }

// ACTIVE is the current in-flight delivery count (for the goroutine limit).
func (t *Tracker) Active() int64 { return t.concurrency.Load() }

// Enter marks one delivery as in-flight, returning the new count.
func (t *Tracker) Enter() int64 { return t.concurrency.Add(1) }

// Leave marks one delivery as complete, returning the new count.
func (t *Tracker) Leave() int64 { return t.concurrency.Add(-1) }

// Outcome records a delivery outcome on the counters.
func (t *Tracker) Outcome(kind string) {
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

// Snapshot returns the operational counters for the /healthz report.
func (t *Tracker) Snapshot() map[string]any {
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
type Server struct {
	tracker *Tracker
	probe   func(ctx context.Context) error
	server  *http.Server
}

// NewServer binds the health surface on addr.
func NewServer(addr string, tracker *Tracker, probe func(ctx context.Context) error) *Server {
	mux := http.NewServeMux()
	s := &Server{tracker: tracker, probe: probe,
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
