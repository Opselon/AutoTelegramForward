package ops

import (
	"net/http"
	"time"
)

// handleControlStatus answers "is the worker paused, and by whom, and what
// did we do about it": the §10 surface the console's Worker Controls page
// paints. Unavailable (nil plane) renders as such — never as a fabricated
// running state.
//
// The pause is NOT a processing state of the /ops "worker" block on
// purpose: the worker is alive and healthy while paused; the claim loop is
// simply not taking new work. Mixing them would make a healthy-but-paused
// worker read as degraded.
func (s *Server) handleControlStatus(w http.ResponseWriter, r *http.Request) {
	if s.controls == nil {
		writeJSONCode(w, http.StatusOK, map[string]any{
			"available": false,
		})
		return
	}
	var pause any
	if p := s.controls.PauseState(); p != nil {
		pause = map[string]any{
			"requested_by":       p.RequestedBy,
			"requested_at":       p.RequestedAt.UTC().Format(time.RFC3339),
			"in_flight_complete": p.InFlightComplete,
		}
	}
	writeJSONCode(w, http.StatusOK, map[string]any{
		"available": true,
		"paused":    s.controls.IsPaused(),
		"pause":     pause,
		"ops":       s.controls.Ops(),
	})
}

// handleControlPause requests a processing pause. POST-only: it mutates
// operational state, so a GET prefetch or a browser navigation must never
// pause a production worker.
//
// The op the worker recorded is returned, so the console's pending spinner
// is the REAL asynchronous status, and a control that did not change state
// (already paused) reports that honestly instead of a fabricated change.
func (s *Server) handleControlPause(w http.ResponseWriter, r *http.Request) {
	if s.controls == nil {
		writeJSONCode(w, http.StatusServiceUnavailable, map[string]any{"error": "control surface unavailable"})
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeJSONCode(w, http.StatusMethodNotAllowed, map[string]any{"error": "POST required"})
		return
	}
	op, already := s.controls.Pause(r.RemoteAddr, nil)
	writeJSONCode(w, http.StatusOK, map[string]any{
		"op":             op,
		"already_paused": already,
	})
}

// handleControlResume requests a processing resume. Same POST-only
// contract; a resume while running completes with an honest "already
// running" reason the console must not render as a state change.
func (s *Server) handleControlResume(w http.ResponseWriter, r *http.Request) {
	if s.controls == nil {
		writeJSONCode(w, http.StatusServiceUnavailable, map[string]any{"error": "control surface unavailable"})
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeJSONCode(w, http.StatusMethodNotAllowed, map[string]any{"error": "POST required"})
		return
	}
	op, wasRunning := s.controls.Resume(r.RemoteAddr)
	writeJSONCode(w, http.StatusOK, map[string]any{
		"op":          op,
		"was_running": wasRunning,
	})
}
