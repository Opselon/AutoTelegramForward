// Package ratelimit is the API boundary's rate-limiting and coordination layer.
//
// Design rules this package exists to enforce:
//
//   - The API is the only place a request can be refused cheaply, so the limit
//     is enforced here, before the request reaches the Python core over gRPC.
//     A refused request must cost one Redis round trip and no core work.
//
//   - The identity a key is derived from is the VERIFIED one (the JWT the core
//     validated), never a client-supplied field: no ?user_id=, no X-User-Id, no
//     body field may move a caller into another tenant's bucket. For traffic
//     that has not been authenticated yet (an unauthenticated caller can still
//     make the API dial the core to validate a bogus token), the key is the
//     peer address — and only a proxy the operator declared trusted may move
//     that peer address via X-Forwarded-For.
//
//   - Redis is an accelerator, never the source of truth. The persistent queue
//     and its database-backed claims keep delivery idempotent on their own; a
//     Redis lease can avoid duplicate WORK but is never the guarantee against
//     duplicate delivery.
//
//   - A Redis failure must not silently disarm a security limit. Each route
//     class declares its outage policy: authentication endpoints fail closed
//     (a refusal with 503), ordinary dashboard traffic falls back to a bounded
//     in-process limiter and is reported as degraded, never as normal.
//
//   - A limit that is enforced is reported: every answer carries the remaining
//     quota and, when refused, a retry-after, so a client never has to guess
//     and never needs a second round trip to find out.
package ratelimit

import (
	"context"
	"errors"
	"sync/atomic"
	"time"
)

// Class is a family of routes that share a limit and an outage policy.
type Class string

const (
	// ClassAuth covers the unauthenticated login/register/exchange endpoints.
	// Brute force lives here, so this class fails CLOSED when Redis is gone.
	ClassAuth Class = "auth"
	// ClassShield covers authenticated-but-unverified traffic: the outer
	// per-address guard that runs before the token is validated, so a flood of
	// bogus tokens cannot make the API hammer the core's ValidateToken.
	ClassShield Class = "shield"
	// ClassRead covers authenticated reads.
	ClassRead Class = "read"
	// ClassWrite covers authenticated mutations.
	ClassWrite Class = "write"
	// ClassAdmin covers the administrator control plane: still limited (an
	// admin token in the wrong hands is the most valuable one), at a higher
	// rate.
	ClassAdmin Class = "admin"
	// ClassProbe covers liveness/version polling. It is not unlimited — a
	// monitor that loops must not become a self-inflicted flood — but the
	// budget is high enough that no honest monitor ever meets it.
	ClassProbe Class = "probe"
)

// Decision is the answer of one limit check. Remaining and Limit are echoed to
// the client, ResetAfter tells a refused client how long to wait.
type Decision struct {
	// Allowed is true when the request may proceed.
	Allowed bool
	// Limit is the bucket capacity / window size that produced this answer.
	Limit int64
	// Remaining is the quota left after this request.
	Remaining int64
	// RetryAfter is the minimum wait before a refused client should try again.
	RetryAfter time.Duration
	// Degraded is true when the answer came from the local fallback because
	// Redis was unavailable: the limit still applied, but per-process, so
	// N replicas allow up to N times the configured rate.
	Degraded bool
	// Unavailable is true when the limit could not be evaluated at all and the
	// class's outage policy produced this answer. A refusal carrying it is a
	// 503 ("cannot enforce right now"), not a 429 ("you are over the limit").
	Unavailable bool
}

// Limiter answers "may this key spend one unit right now?".
//
// Allow must be safe for concurrent use and must respect the context deadline:
// a limiter that blocks longer than the request would have taken is worse than
// no limiter at all.
type Limiter interface {
	Allow(ctx context.Context, key string, cost int64, b Budget) (Decision, error)
	// Name identifies the implementation for logs, headers and benchmarks.
	Name() string
}

// ErrUnavailable is returned when the authoritative limiter (Redis) could not
// answer. The caller applies the route class's outage policy; it is never
// silently treated as "allowed".
var ErrUnavailable = errors.New("ratelimit: backend unavailable")

// Stats are process-local counters, read by the status endpoint and by the QA
// harness. They are deliberately plain atomics: measuring the limiter must not
// itself need a lock, and a counter that is slightly stale is fine.
type Stats struct {
	Allowed     atomic.Int64
	Limited     atomic.Int64
	Degraded    atomic.Int64
	Unavailable atomic.Int64
	Errors      atomic.Int64
	Probes      atomic.Int64
	Bypassed    atomic.Int64
}

// Snapshot is a Stats reading with plain integers, safe to serialise.
type Snapshot struct {
	Allowed     int64 `json:"allowed"`
	Limited     int64 `json:"limited"`
	Degraded    int64 `json:"degraded"`
	Unavailable int64 `json:"unavailable"`
	Errors      int64 `json:"errors"`
	Probes      int64 `json:"probes"`
	Bypassed    int64 `json:"bypassed"`
}

// Snapshot returns the current counters.
func (s *Stats) Snapshot() Snapshot {
	if s == nil {
		return Snapshot{}
	}
	return Snapshot{
		Allowed:     s.Allowed.Load(),
		Limited:     s.Limited.Load(),
		Degraded:    s.Degraded.Load(),
		Unavailable: s.Unavailable.Load(),
		Errors:      s.Errors.Load(),
		Probes:      s.Probes.Load(),
		Bypassed:    s.Bypassed.Load(),
	}
}
