package handlers

import (
	"encoding/json"
	"net/http"
	"time"

	"autoforward/internal/middleware"
	pb "autoforward/proto"
)

// Control plane for the log collection policy.
//
// The policy is enforced *per producer*, so there is no single "the policy" to
// report. These handlers therefore answer in scopes and label each one: a number
// this process reports is what this process decided, never a system total. Where
// a scope cannot be read (atf-core has no policy RPC yet), it is reported as
// unavailable with the reason, because inventing a zero there would read as
// "nothing was suppressed", which is the opposite of the truth.
//
// Mutations are admin-only and bounded, and every one of them writes durable
// audit evidence through the same policy it is changing.

const (
	// DefaultDiagnosticSeconds is the window length when a caller does not ask
	// for one: long enough to reproduce an intermittent failure, short enough
	// that nobody has to remember to turn it off.
	DefaultDiagnosticSeconds = 600
	// MaxDiagnosticSeconds is the hard bound on a single window. A diagnostic
	// mode with no ceiling is just "logging everything, forever" with a nicer
	// name.
	MaxDiagnosticSeconds = 3600
	// diagnosticRestartGuard rate-limits repeated starts, so a double-clicked
	// button (or a retrying client) cannot keep resetting the expiry and leave a
	// window open indefinitely.
	diagnosticRestartGuard = 5 * time.Second
)

type policyCounters struct {
	Emitted              int64 `json:"emitted"`
	EmittedSecurity      int64 `json:"emitted_security"`
	SuppressedLevel      int64 `json:"suppressed_level"`
	SuppressedRate       int64 `json:"suppressed_rate"`
	DiagnosticActivation int64 `json:"diagnostic_activations"`
}

// policyScope is one process's answer. Every field that is unknown is omitted
// rather than zeroed.
type policyScope struct {
	Available            bool            `json:"available"`
	Scope                string          `json:"scope"`
	Process              string          `json:"process,omitempty"`
	Reason               string          `json:"reason,omitempty"`
	ConfiguredLevels     []string        `json:"configured_levels,omitempty"`
	EffectiveLevels      []string        `json:"effective_levels,omitempty"`
	DiagnosticActive     bool            `json:"diagnostic_active"`
	DiagnosticUntil      int64           `json:"diagnostic_until,omitempty"`
	DiagnosticRemainingS int64           `json:"diagnostic_seconds_remaining,omitempty"`
	VerboseRatePerSecond float64         `json:"verbose_rate_per_second,omitempty"`
	Counters             *policyCounters `json:"counters,omitempty"`
	CounterScope         string          `json:"counter_scope,omitempty"`
	ObservedAt           string          `json:"observed_at"`
}

// LogPolicy reports the effective collection policy for every producer this API
// can see, with the scope of each answer stated explicitly.
func (h *Handlers) LogPolicy(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	scopes := map[string]policyScope{
		"api": h.apiPolicyScope(now),
		// atf-core enforces the same policy in Python, in its own process, from
		// its own environment. Nothing here can read it yet, and a plausible
		// looking zero would be a lie an operator would act on.
		"core": {
			Available:  false,
			Scope:      "atf-core producer policy",
			Process:    "atf-core",
			Reason:     "atf-core does not expose its producer policy over an internal RPC yet; configure it with ATF_LOG_LEVELS/ATF_LOG_DIAGNOSTIC_SECONDS/ATF_LOG_VERBOSE_RATE and restart it",
			ObservedAt: now.Format(time.RFC3339),
		},
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"generated_at": now.Format(time.RFC3339),
		"scopes":       scopes,
		"notes": []string{
			"counters are process-local and reset when the process restarts; they are never a system total",
			"collection is not retention: suppressing a category does not delete, or stop deleting, stored records",
		},
	})
}

func (h *Handlers) apiPolicyScope(now time.Time) policyScope {
	if h.Policy == nil {
		return policyScope{
			Available:  false,
			Scope:      "atf-api producer policy",
			Process:    "atf-api",
			Reason:     "this API instance was started without a collection policy attached",
			ObservedAt: now.Format(time.RFC3339),
		}
	}
	snapshot := h.Policy.Snapshot()
	scope := policyScope{
		Available:            true,
		Scope:                "atf-api producer policy",
		Process:              "atf-api",
		ConfiguredLevels:     snapshot.ConfiguredLevels,
		EffectiveLevels:      snapshot.Levels,
		DiagnosticActive:     snapshot.DiagnosticActive,
		DiagnosticUntil:      snapshot.DiagnosticUntil,
		VerboseRatePerSecond: snapshot.VerboseRatePerSecond,
		CounterScope:         "process-local to atf-api since its last start",
		ObservedAt:           now.Format(time.RFC3339),
		Counters: &policyCounters{
			Emitted:              snapshot.Emitted,
			EmittedSecurity:      snapshot.EmittedSecurity,
			SuppressedLevel:      snapshot.SuppressedLevel,
			SuppressedRate:       snapshot.SuppressedRate,
			DiagnosticActivation: snapshot.DiagnosticActivation,
		},
	}
	if snapshot.DiagnosticActive && snapshot.DiagnosticUntil > 0 {
		if remaining := snapshot.DiagnosticUntil - now.Unix(); remaining > 0 {
			scope.DiagnosticRemainingS = remaining
		}
	}
	return scope
}

// LoggingHealth reports per-service logging reachability and how old the newest
// record is, so "the dashboard is quiet" can be told apart from "the Logger is
// not being fed".
func (h *Handlers) LoggingHealth(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	services := []map[string]any{{
		"service":     "atf-api",
		"status":      "healthy",
		"detail":      "producer policy attached",
		"observed_at": now.Format(time.RFC3339),
	}}
	if h.Policy == nil {
		services[0]["status"] = "degraded"
		services[0]["detail"] = "no collection policy attached to this instance"
	}

	loggerStatus := "healthy"
	loggerDetail := "ingest and query reachable"
	var lastRecord int64
	if h.c == nil || h.c.Logs == nil {
		loggerStatus = "unavailable"
		loggerDetail = "no logger client configured"
	} else {
		ctx, cancel := withTimeout(r)
		defer cancel()
		resp, err := h.c.Logs.LogStats(ctx, &pb.LogStatsRequest{})
		if err != nil {
			loggerStatus = "unavailable"
			loggerDetail = "stats query failed: " + err.Error()
		} else {
			lastRecord = resp.NewestTs
			loggerDetail = "reachable"
		}
	}
	entry := map[string]any{
		"service":     "atf-logger",
		"status":      loggerStatus,
		"detail":      loggerDetail,
		"observed_at": now.Format(time.RFC3339),
	}
	if lastRecord > 0 {
		entry["last_record_at"] = lastRecord
		entry["data_age_seconds"] = now.Unix() - lastRecord
	}
	services = append(services, entry)

	services = append(services, map[string]any{
		"service":     "atf-core",
		"status":      "unknown",
		"detail":      "core exposes no logging telemetry yet; its counters are not readable from here",
		"observed_at": now.Format(time.RFC3339),
	})

	overall := "healthy"
	for _, svc := range services {
		if svc["status"] == "unavailable" {
			overall = "degraded"
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":       overall,
		"generated_at": now.Format(time.RFC3339),
		"services":     services,
	})
}

// LogDiagnosticsStart opens a bounded, self-expiring diagnostic window on this
// API's producer policy.
//
// Semantics, chosen so that repeated presses are harmless: a start always
// produces exactly one window, extending from now, replacing any active one (the
// response says whether it replaced something). Starts inside
// diagnosticRestartGuard of the previous one are refused with 409, so a stuck
// button cannot hold the window open forever.
func (h *Handlers) LogDiagnosticsStart(w http.ResponseWriter, r *http.Request) {
	if h.Policy == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "this API instance has no collection policy attached; diagnostics cannot be enabled",
		})
		return
	}
	seconds, ok := parseDiagnosticSeconds(w, r)
	if !ok {
		return
	}

	h.diagMu.Lock()
	if !h.lastDiagStart.IsZero() && time.Since(h.lastDiagStart) < diagnosticRestartGuard {
		h.diagMu.Unlock()
		w.Header().Set("Retry-After", "5")
		writeJSON(w, http.StatusConflict, map[string]any{
			"error":            "a diagnostic window was started moments ago; repeated starts are ignored to keep the window bounded",
			"minimum_interval": int(diagnosticRestartGuard.Seconds()),
		})
		return
	}
	wasActive := h.Policy.DiagnosticActive()
	h.lastDiagStart = time.Now()
	h.diagMu.Unlock()

	expiresAt := h.Policy.EnableDiagnostics(seconds)
	actor := actorID(r)
	h.auditAction(r, actor, "logging.diagnostics.start", map[string]any{
		"seconds":         seconds,
		"expires_at":      expiresAt,
		"replaced_active": wasActive,
	})

	writeJSON(w, http.StatusOK, map[string]any{
		"started":         true,
		"seconds":         seconds,
		"expires_at":      expiresAt,
		"replaced_active": wasActive,
		"policy":          h.apiPolicyScope(time.Now().UTC()),
	})
}

// LogDiagnosticsStop closes a window early. Stopping when nothing is open is not
// an error (a client retrying a stop must not see a confusing failure), but the
// response says plainly that there was nothing to stop.
func (h *Handlers) LogDiagnosticsStop(w http.ResponseWriter, r *http.Request) {
	if h.Policy == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "this API instance has no collection policy attached",
		})
		return
	}
	wasActive := h.Policy.DiagnosticActive()
	h.Policy.DisableDiagnostics()
	actor := actorID(r)
	h.auditAction(r, actor, "logging.diagnostics.stop", map[string]any{"was_active": wasActive})

	writeJSON(w, http.StatusOK, map[string]any{
		"stopped":    true,
		"was_active": wasActive,
		"policy":     h.apiPolicyScope(time.Now().UTC()),
	})
}

func parseDiagnosticSeconds(w http.ResponseWriter, r *http.Request) (int, bool) {
	seconds := DefaultDiagnosticSeconds
	if r.Body != nil {
		var body struct {
			Seconds *int `json:"seconds"`
		}
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		if err := decoder.Decode(&body); err != nil && err.Error() != "EOF" {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "body must be JSON of the form {\"seconds\": <int>}",
			})
			return 0, false
		}
		if body.Seconds != nil {
			seconds = *body.Seconds
		}
	}
	if seconds < 1 || seconds > MaxDiagnosticSeconds {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error":       "seconds out of range",
			"min_seconds": 1,
			"max_seconds": MaxDiagnosticSeconds,
		})
		return 0, false
	}
	return seconds, true
}

func actorID(r *http.Request) int64 {
	if id := identity(r); id != nil {
		return id.UserID
	}
	return 0
}

// auditAction records an administrative change in the durable log store,
// including the correlation id of the request, so the control plane's own actions
// are part of the trail it configures.
func (h *Handlers) auditAction(r *http.Request, actor int64, action string, detail map[string]any) {
	if h.LogAudit == nil {
		return
	}
	enriched := map[string]any{"action": action, "actor": actor, "reason_code": "ADMIN_REQUIRED"}
	for key, value := range detail {
		enriched[key] = value
	}
	if corr := middleware.CorrelationIDFromContext(r.Context()); corr != "" {
		enriched["correlation_id"] = corr
	}
	h.LogAudit(actor, action, "ADMIN_REQUIRED", enriched)
}
