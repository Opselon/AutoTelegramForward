package handlers

import (
	"context"
	"net/http"
	"time"
)

// The question /readyz answers is not "is this process up" (that is /healthz) but
// "can this deployment enforce everything it promises a client".
//
// It exists because of a failure mode observed live: a compose run substituted an
// empty ATF_REDIS_PASSWORD (the env file the deployment was pointed at did not
// carry it, while the redis service still required one). The api started, logged
// "ratelimit: enabled (redis:tb, algo=tb, addrs=redis:6379)", and then refused
// every token exchange with 503 rate_limit_unavailable — core degraded to
// "assume it is the only replica" for the same reason — while GET /healthz
// answered 200 and every container stayed healthy. A deploy that ends in
// "== healthy ==" therefore shipped a stack whose authentication was down, and
// the breakage stayed invisible until a user tried to log in.
//
// Readiness is the missing signal. It is probeable with no session, no
// dashboard and no knowledge of the topology: 200 or 503, with one entry per
// fail-closed dependency and a plain-language detail when one cannot enforce.

// ReadyCheck is one dependency's enforceability as reported by /readyz.
type ReadyCheck struct {
	Name   string `json:"name"`
	OK     bool   `json:"ok"`
	Detail string `json:"detail,omitempty"`
}

// Ready is the HTTP surface registered by main.go.
func (h *Handlers) Ready(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()

	checks := make([]ReadyCheck, 0, 1)
	ready := true

	checks = append(checks, h.limiterReadiness(ctx, &ready))

	// A disabled limiter is a supported mode, not an outage: the in-process
	// limiter is then the whole limit, nothing is failing closed, and declaring
	// such a deployment unready would refuse to deploy working single-node
	// installs. Only a wired limiter that cannot reach its store is unready.
	status := "ready"
	code := http.StatusOK
	if !ready {
		status = "not-ready"
		code = http.StatusServiceUnavailable
	}
	writeJSON(w, code, map[string]any{
		"status": status,
		"ready":  ready,
		"time":   time.Now().UTC().Format(time.RFC3339),
		"checks": checks,
	})
}

// limiterReadiness reports whether the API boundary can actually enforce its
// rate limits, and clears *ready when it cannot.
//
// The check is a live probe of the store (Runtime.Status pings Redis), not a
// configuration reading: an address that is set and a credential that is empty
// look perfectly configured, which is exactly how the failure described at the
// top of this file reached production. It is also why this is not a check of the
// env contract — the question is whether the store answers, not whether someone
// set a variable.
//
// Runtime.Status redacts the credential out of the error it reports, so the
// detail is safe to serve on an unauthenticated endpoint.
func (h *Handlers) limiterReadiness(ctx context.Context, ready *bool) ReadyCheck {
	check := ReadyCheck{Name: "ratelimit", OK: true}
	if h.RateLimit == nil {
		check.Detail = "no shared store configured; limits are per-process"
		return check
	}
	if !h.RateLimit.Enabled() {
		check.Detail = "limiter disabled by configuration; limits are per-process"
		return check
	}
	report := h.RateLimit.Status(ctx)
	if report.Reachable {
		check.Detail = "enforcing through the shared store"
		return check
	}
	check.OK = false
	check.Detail = report.Error
	if check.Detail == "" {
		check.Detail = "the shared store did not answer; limits cannot be enforced"
	}
	*ready = false
	return check
}
