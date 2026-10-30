package handlers

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"autoforward/internal/middleware"
	pb "autoforward/proto"
)

// Control plane for the log collection policy.
//
// The policy is enforced *per producer*, so there is no single "the policy" to
// report. These handlers therefore answer in scopes and label each one: a number
// this process reports is what this process decided, never a system total. Where
// a scope cannot be read, it is reported as unavailable with the reason, because
// inventing a zero there would read as "nothing was suppressed", which is the
// opposite of the truth. Both producers are read from their own process: this
// API's own policy in-band, and atf-core's over GetLogPolicy, so what is shown
// is what the process is enforcing rather than what this API assumes it was
// configured with.
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
	// maxLogPolicyBodyBytes caps a policy change body. The policy is a handful of
	// scalars and a seven-name list, so anything larger is a mistake or an
	// attempt; refusing it early keeps the parse bounded.
	maxLogPolicyBodyBytes = 4096
)

// logPolicyUpdateRequest is an operator's change. Every field is optional so one
// thing can be changed without restating the rest, and `nil` means "leave this
// alone" — which is why the handler refuses a body with no fields at all rather
// than answering success to a request that did nothing.
type logPolicyUpdateRequest struct {
	Levels               *[]string `json:"levels"`
	VerboseRatePerSecond *float64  `json:"verbose_rate_per_second"`
	DiagnosticSeconds    *int      `json:"diagnostic_seconds"`
	DisableDiagnostics   *bool     `json:"disable_diagnostics"`
}

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
//
// The engine's admission policy is reported alongside the producer's (issue
// #102): "collected by the producer" and "admitted by the engine" are two
// different gates, and the operator looking at this answer is the one who has
// to notice when they disagree.
func (h *Handlers) LogPolicy(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	scopes := map[string]policyScope{
		"api": h.apiPolicyScope(now),
		// atf-core enforces the same policy in Python, in its own process, from
		// its own environment. Only that process knows what it is actually
		// collecting, so the answer is read from it over GetLogPolicy rather
		// than estimated. Where the channel, the RPC, or the attached policy is
		// missing, the scope stays unavailable with the reason — a plausible
		// looking zero would be a lie an operator would act on.
		"core": h.corePolicyScope(r, now),
		// The engine is the second gate: it can refuse what the producer
		// collected. Its report rides on LogStats, which the API already serves.
		"engine": h.enginePolicyScope(r, now),
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"generated_at": now.Format(time.RFC3339),
		"scopes":       scopes,
		"notes": []string{
			"counters are process-local and reset when the process restarts; they are never a system total",
			"collection is not retention: suppressing a category does not delete, or stop deleting, stored records",
			"`collected` is the producer's verdict and `admitted` the engine's: a level that appears in scope=api/core but not in scope=engine will be counted as filtered, not stored",
		},
	})
}

// enginePolicyScope reports what the logger engine itself admits, so the two
// halves of the collection contract are visible side by side. The producer's
// answer alone says "emitted" for an INFO event the engine refuses, which reads
// to an operator as "queryable" — the exact conflation issue #102 reports.
//
// The engine reports its policy on LogStats (v20). An engine that predates the
// field answers a scope that says so, rather than a zero that would look like
// "the engine admits everything".
func (h *Handlers) enginePolicyScope(r *http.Request, now time.Time) policyScope {
	scope := policyScope{
		Scope:      "logger engine admission policy",
		Process:    "atf-logger",
		ObservedAt: now.Format(time.RFC3339),
	}
	if h.c == nil || h.c.Logs == nil {
		scope.Available = false
		scope.Reason = "no logger gRPC client configured"
		return scope
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Logs.LogStats(ctx, &pb.LogStatsRequest{})
	if err != nil {
		scope.Available = false
		scope.Reason = "engine policy query failed: " + err.Error()
		return scope
	}
	admission := resp.GetAdmission()
	if admission == nil {
		scope.Available = false
		scope.Reason = "engine reports no admission policy (older atf-logger)"
		return scope
	}
	scope.Available = true
	scope.Scope = "logger engine admission policy (queried live from the engine)"
	scope.ConfiguredLevels = admission.GetCollectLevels()
	// `omitted_levels` is reported as the effective set the engine will *not*
	// store, named in the same vocabulary as the producer's levels so the two
	// columns can be diffed by eye.
	scope.EffectiveLevels = admission.GetOmittedLevels()
	scope.CounterScope = "engine admission refusals since the engine last started"
	scope.Counters = &policyCounters{
		// One engine-wide refusal counter exists; the per-level breakdown is
		// carried on the Log Stats surface (`admission.filtered_by_level`) and
		// is deliberately not flattened into the policy answer's shape.
		SuppressedLevel: admission.GetFilteredTotal(),
	}
	return scope
}

// corePolicyScope asks atf-core itself what it is collecting, over the
// GetLogPolicy RPC added for exactly this purpose. Every failure mode keeps the
// scope unavailable with a reason: a channel that is down, an older core that
// predates the RPC, or a core running without a policy are all different
// situations an operator must be able to tell apart, and none of them is "zero".
func (h *Handlers) corePolicyScope(r *http.Request, now time.Time) policyScope {
	scope := policyScope{
		Scope:      "atf-core producer policy",
		Process:    "atf-core",
		ObservedAt: now.Format(time.RFC3339),
	}
	if h.c == nil || h.c.System == nil {
		scope.Available = false
		scope.Reason = "no atf-core gRPC client configured"
		return scope
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.System.GetLogPolicy(ctx, &pb.LogPolicyRequest{})
	if err != nil {
		scope.Available = false
		scope.Reason = "atf-core policy query failed: " + err.Error()
		return scope
	}
	scope.Scope = "atf-core producer policy (queried live from the core process)"
	if !resp.Available {
		scope.Available = false
		scope.Reason = resp.Reason
		if len(scope.Reason) == 0 {
			scope.Reason = "atf-core reports no collection policy attached"
		}
		return scope
	}
	scope.Available = true
	scope.ConfiguredLevels = resp.ConfiguredLevels
	scope.EffectiveLevels = resp.EffectiveLevels
	scope.DiagnosticActive = resp.DiagnosticActive
	scope.DiagnosticUntil = resp.DiagnosticUntil
	scope.VerboseRatePerSecond = resp.VerboseRatePerSecond
	scope.CounterScope = "process-local to atf-core since its last start"
	if scope.DiagnosticActive && scope.DiagnosticUntil > 0 {
		if remaining := scope.DiagnosticUntil - now.Unix(); remaining > 0 {
			scope.DiagnosticRemainingS = remaining
		}
	}
	scope.Counters = &policyCounters{
		Emitted:              resp.Emitted,
		EmittedSecurity:      resp.EmittedSecurity,
		SuppressedLevel:      resp.SuppressedLevel,
		SuppressedRate:       resp.SuppressedRate,
		DiagnosticActivation: resp.DiagnosticActivations,
	}
	return scope
}

// LogPolicyUpdate applies an operator's change to atf-core's collection policy.
//
// The write path is deliberately longer than "forward the request": the change is
// merged onto what the core reports it is *enforcing* (never onto an assumed
// baseline, which is how one change silently reverts another field), the core is
// asked to apply it, and then the policy is read back so the answer describes the
// process rather than the request. A change the core refuses is reported with the
// core's own reason and applied nowhere.
func (h *Handlers) LogPolicyUpdate(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	actor := actorID(r)

	if h.c == nil || h.c.System == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "no atf-core gRPC client configured; the core policy cannot be changed",
		})
		return
	}

	var body logPolicyUpdateRequest
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxLogPolicyBodyBytes))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "body must be JSON of the form " +
				`{"levels":["WARN","ERROR"],"verbose_rate_per_second":20,"diagnostic_seconds":600,"disable_diagnostics":false}`,
		})
		return
	}
	if body.Levels == nil && body.VerboseRatePerSecond == nil &&
		body.DiagnosticSeconds == nil && body.DisableDiagnostics == nil {
		// An empty body that quietly succeeds would let a caller believe it changed
		// something. Say what is missing instead.
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error":  "no policy field supplied",
			"fields": []string{"levels", "verbose_rate_per_second", "diagnostic_seconds", "disable_diagnostics"},
		})
		return
	}
	if body.DiagnosticSeconds != nil && *body.DiagnosticSeconds > 0 &&
		body.DisableDiagnostics != nil && *body.DisableDiagnostics {
		// The core refuses this too; answering before the round trip keeps the
		// contradiction and the refusal in one place for the caller.
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "diagnostic_seconds and disable_diagnostics are mutually exclusive",
		})
		return
	}

	ctx, cancel := withTimeout(r)
	defer cancel()

	// Read first: the change is a merge onto the policy the core is enforcing, so
	// an unreadable baseline must refuse the write rather than guess at it.
	current, err := h.c.System.GetLogPolicy(ctx, &pb.LogPolicyRequest{})
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "could not read the policy atf-core is enforcing: " + err.Error(),
		})
		return
	}
	if !current.Available {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error":  "atf-core reports no collection policy to change",
			"reason": current.Reason,
		})
		return
	}

	// Every field carries an explicit value taken from the enforced policy, so
	// the request describes the complete state the caller wants — not a patch
	// whose omitted fields would let a second writer's change be reverted. The
	// rate is a pointer on the wire precisely so an explicit 0 ("no ceiling")
	// stays distinguishable from "leave it alone".
	currentRate := current.VerboseRatePerSecond
	desired := &pb.SetLogPolicyRequest{
		Levels:               current.ConfiguredLevels,
		VerboseRatePerSecond: &currentRate,
		Actor:                strconv.FormatInt(actor, 10),
		CorrelationId:        middleware.CorrelationIDFromContext(r.Context()),
	}
	if body.Levels != nil {
		desired.Levels = *body.Levels
	}
	if body.VerboseRatePerSecond != nil {
		rate := *body.VerboseRatePerSecond
		desired.VerboseRatePerSecond = &rate
	}
	if body.DiagnosticSeconds != nil {
		desired.DiagnosticSeconds = int32(*body.DiagnosticSeconds)
	}
	if body.DisableDiagnostics != nil {
		desired.DisableDiagnostics = *body.DisableDiagnostics
	}

	before := policyChangeSummary(current)
	resp, err := h.c.System.SetLogPolicy(ctx, desired)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "atf-core could not be asked to change its policy: " + err.Error(),
		})
		return
	}
	if !resp.Available {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error":  "atf-core reports no collection policy to change",
			"reason": resp.Reason,
		})
		return
	}
	if !resp.Applied {
		// The producer refused it, so the policy is unchanged. Recording the
		// refusal is the point: an operator who cannot turn collection off is
		// something the trail should show.
		h.auditAction(r, actor, "logging.policy.update", map[string]any{
			"result": "rejected", "error": resp.Reason, "before": before,
		})
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error":   resp.Reason,
			"applied": false,
			"policy":  h.corePolicyScope(r, now),
		})
		return
	}

	// Read back rather than describing the request: this is what the core is
	// enforcing now, and it is the only answer that can be trusted after a change.
	scope := h.corePolicyScope(r, now)
	h.auditAction(r, actor, "logging.policy.update", map[string]any{
		"result":    "applied",
		"before":    before,
		"after":     policyChangeSummary(resp),
		"persisted": resp.Persisted,
	})

	writeJSON(w, http.StatusOK, map[string]any{
		"ok":        true,
		"applied":   true,
		"persisted": resp.Persisted,
		"before":    before,
		"scope":     scope,
		"notes": []string{
			"the reported policy is read back from the core process after the change",
			"persisted=false means the change is in force now but will not survive a restart",
		},
	})
}

// policyChangeSummary is the part of a policy an operator compares across a
// change: the configured levels, the rate ceiling and the diagnostic window.
func policyChangeSummary(resp *pb.LogPolicyResponse) map[string]any {
	return map[string]any{
		"configured_levels":       resp.ConfiguredLevels,
		"verbose_rate_per_second": resp.VerboseRatePerSecond,
		"diagnostic_active":       resp.DiagnosticActive,
		"diagnostic_until":        resp.DiagnosticUntil,
	}
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

	// atf-core answers for itself. Until GetLogPolicy existed this line read
	// "unknown: core exposes no logging telemetry yet", which told an operator
	// nothing they could act on; a failed probe and a core that is up but has
	// no policy attached are now distinguishable states.
	coreStatus := "unknown"
	coreDetail := "no atf-core gRPC client configured"
	var coreCounters *policyCounters
	var coreDiagActive bool
	if h.c != nil && h.c.System != nil {
		ctx, cancel := withTimeout(r)
		resp, err := h.c.System.GetLogPolicy(ctx, &pb.LogPolicyRequest{})
		cancel()
		switch {
		case err != nil:
			coreStatus = "unavailable"
			coreDetail = "core policy query failed: " + err.Error()
		case !resp.Available:
			coreStatus = "degraded"
			coreDetail = resp.Reason
			if coreDetail == "" {
				coreDetail = "core reports no collection policy attached"
			}
		default:
			coreStatus = "healthy"
			coreDetail = "producer policy readable over GetLogPolicy"
			coreDiagActive = resp.DiagnosticActive
			coreCounters = &policyCounters{
				Emitted:              resp.Emitted,
				EmittedSecurity:      resp.EmittedSecurity,
				SuppressedLevel:      resp.SuppressedLevel,
				SuppressedRate:       resp.SuppressedRate,
				DiagnosticActivation: resp.DiagnosticActivations,
			}
		}
	}
	coreEntry := map[string]any{
		"service":     "atf-core",
		"status":      coreStatus,
		"detail":      coreDetail,
		"observed_at": now.Format(time.RFC3339),
	}
	if coreCounters != nil {
		coreEntry["counters"] = coreCounters
		coreEntry["counter_scope"] = "process-local to atf-core since its last start"
		coreEntry["diagnostic_active"] = coreDiagActive
	}
	services = append(services, coreEntry)

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
