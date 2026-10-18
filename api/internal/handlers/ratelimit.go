package handlers

import (
	"context"
	"net/http"
	"time"
)

// This file exposes the rate limiter's own state, because a limit an operator
// cannot inspect is a limit they will end up disabling. Two audiences:
//
//   - the administrator control plane (GET /api/admin/ratelimit): the full
//     report — effective policy per route class, which identity each class is
//     keyed on, the outage policy, the circuit-breaker state and the counters.
//   - the setup/env contract (see system.go): a boolean summary an operator or
//     a smoke test can assert on without a token.
//
// Nothing here reports a credential: addresses are redacted, the password is
// never read, and the counters are plain integers.

// RateLimitStatus is the administrator view of the limiter.
func (h *Handlers) RateLimitStatus(w http.ResponseWriter, r *http.Request) {
	if h.RateLimit == nil {
		writeJSON(w, http.StatusOK, map[string]any{
			"enabled": false,
			"detail":  "no rate limiter is wired into this process",
		})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	writeJSON(w, http.StatusOK, h.RateLimit.Status(ctx))
}

// rateLimitEnvChecks is the Redis part of the configuration contract.
//
// Redis is "recommended", not "required": a deployment without it still serves,
// with the in-process limit and without cross-replica coordination. Reporting it
// as required would tell a working single-node install that it is broken.
func rateLimitEnvChecks() []EnvCheck {
	return []EnvCheck{
		{
			Key:    "ATF_REDIS_ADDR",
			Level:  "recommended",
			Set:    envAnySet("ATF_REDIS_ADDR", "ATF_REDIS_URL"),
			Reason: "shared rate-limit store and worker-coordination leases (Redis); without it limits are per-process only",
			Hint:   "ATF_REDIS_ADDR=redis:6379 (set by docker-compose.yml when the redis service is enabled)",
		},
		{
			Key:    "ATF_RATELIMIT_ENABLED",
			Level:  "optional",
			Set:    envAnySet("ATF_RATELIMIT_ENABLED"),
			Reason: "explicit switch; the limiter is enabled automatically when ATF_REDIS_ADDR is set",
			Hint:   "leave unset to enable whenever a Redis address is configured; set 0 to disable",
		},
		{
			Key:    "ATF_REDIS_PASSWORD",
			Level:  "optional",
			Set:    envAnySet("ATF_REDIS_PASSWORD"),
			Reason: "authentication for the Redis instance (recommended when Redis is not on a private network)",
			Hint:   "openssl rand -hex 24 > ATF_REDIS_PASSWORD=... (never commit the value)",
		},
	}
}

// rateLimitSummary is the boolean view embedded in the setup report: enough to
// answer "is the limiter enforcing, and can it reach its store?" without
// leaking the deployment's topology into an unauthenticated response.
func (h *Handlers) rateLimitSummary(ctx context.Context) map[string]any {
	if h.RateLimit == nil {
		return map[string]any{"enabled": false}
	}
	rep := h.RateLimit.Status(ctx)
	out := map[string]any{
		"enabled":  rep.Enabled,
		"mode":     "cluster",
		"degraded": false,
	}
	if !rep.Enabled {
		out["mode"] = "per-process"
		return out
	}
	if !rep.Reachable {
		out["degraded"] = true
		out["breaker"] = rep.Breaker.State
	}
	return out
}
