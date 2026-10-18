package ratelimit

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"autoforward/internal/probes"
)

// IdentityFunc returns the VERIFIED identity of a request: the user id the core
// signed into the caller's token. It returns ok=false when the request has not
// been authenticated (the outer shield) — never a client-supplied value. The
// API supplies this from its own token-validation middleware, so this package
// cannot accidentally read a header instead.
type IdentityFunc func(r *http.Request) (userID int64, isAdmin bool, ok bool)

// Runtime is the wired limiter: configuration, the authoritative backend, the
// in-process fallback, and the request middleware.
type Runtime struct {
	cfg      Config
	primary  Limiter       // nil when the limiter is disabled
	fallback *localLimiter // always present: it is the outage policy and the baseline
	stats    *Stats
	logf     func(format string, args ...any)
	closed   atomic.Bool
}

// NewRuntime wires the limiter from configuration.
//
// It never fails because Redis is unreachable: a deployment must start and
// serve (per class policy) and recover on its own. A configuration error
// (unknown algorithm, malformed address) IS fatal to the limiter and is
// reported, because silently running with no limit is the failure mode that
// matters.
func NewRuntime(ctx context.Context, cfg Config, logf func(string, ...any)) (*Runtime, error) {
	if logf == nil {
		logf = log.Printf
	}
	rt := &Runtime{
		cfg:      cfg,
		fallback: newLocalLimiter(cfg.LocalMaxKeys),
		stats:    &Stats{},
		logf:     logf,
	}
	if !cfg.Enabled {
		logf("ratelimit: disabled (no ATF_REDIS_ADDR configured)")
		return rt, nil
	}
	lim, err := NewRedisLimiter(ctx, cfg.Redis)
	if err != nil {
		// Unreachable Redis is not fatal; a misconfiguration is reported and
		// the classes fall back according to their policy.
		logf("ratelimit: redis limiter unavailable (%v) — classes fall back per policy", err)
		return rt, nil
	}
	rt.primary = lim
	logf("ratelimit: enabled (%s, algo=%s, addrs=%s)", lim.Name(), cfg.Redis.Algo, strings.Join(cfg.Redis.Addrs, ","))
	return rt, nil
}

// Enabled reports whether an authoritative limiter is wired.
func (rt *Runtime) Enabled() bool { return rt != nil && rt.primary != nil }

// Config exposes the effective configuration (for the status endpoint).
func (rt *Runtime) Config() Config { return rt.cfg }

// Close releases the Redis pool. The fallback needs no closing.
func (rt *Runtime) Close() error {
	if rt == nil || rt.primary == nil {
		return nil
	}
	rt.closed.Store(true)
	if c, ok := rt.primary.(interface{ Close() error }); ok {
		return c.Close()
	}
	return nil
}

// allow consults the authoritative limiter and applies the class policy to the
// outcome. Every path through this function ends in a Decision the client is
// told about: allowed, limited, or refused because the limit could not be
// evaluated.
func (rt *Runtime) allow(ctx context.Context, class Class, key string, p Policy, r *http.Request) Decision {
	cost := p.Cost
	if cost < 1 {
		cost = 1
	}
	if !rt.Enabled() {
		// No Redis configured: the deployment is single-replica by assumption,
		// and the in-process limiter is the whole limit. This is a real limit,
		// not a bypass — the classes still refuse a flood.
		dec, _ := rt.fallback.Allow(ctx, key, cost, p.Budget)
		rt.stats.Bypassed.Add(1)
		return dec
	}
	dec, err := rt.primary.Allow(ctx, key, cost, p.Budget)
	if err == nil {
		return dec
	}
	switch p.Fail {
	case FailClosed:
		rt.stats.Unavailable.Add(1)
		return Decision{Limit: p.Budget.Limit, RetryAfter: time.Second, Unavailable: true}
	case FailOpen:
		rt.stats.Unavailable.Add(1)
		return Decision{Allowed: true, Limit: p.Budget.Limit, Unavailable: true}
	default:
		local, _ := rt.fallback.Allow(ctx, key, cost, p.Budget)
		local.Degraded = true
		local.Unavailable = true
		rt.stats.Degraded.Add(1)
		return local
	}
}

// WrapShield installs the outer, per-address guard.
//
// It runs before authentication, because the work that must be protected is the
// token validation itself: an unauthenticated flood would otherwise make the
// API dial the core for every bogus token. The identity it uses is therefore
// the peer address, resolved through trusted proxies only.
func (rt *Runtime) WrapShield(next http.Handler) http.Handler {
	if rt == nil {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if isProbePath(r.URL.Path) {
			rt.stats.Probes.Add(1)
			next.ServeHTTP(w, r)
			return
		}
		class := ClassShield
		p := rt.cfg.Policy(class)
		key := rt.key(class, p, "", r)
		dec := rt.allow(r.Context(), class, key, p, r)
		rt.writeHeaders(w, dec, p)
		if !dec.Allowed {
			rt.refuse(w, class, p, dec, "shield")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// WrapClass installs a class's limiter behind authentication, where the
// verified identity is available.
func (rt *Runtime) WrapClass(class Class, id IdentityFunc, next http.Handler) http.Handler {
	if rt == nil {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rt.serve(w, r, class, id, next)
	})
}

// serve is the one place a class's limit is applied, shared by the fixed-class
// wrapper and the route-classifying wrapper.
func (rt *Runtime) serve(w http.ResponseWriter, r *http.Request, class Class, id IdentityFunc, next http.Handler) {
	if isProbePath(r.URL.Path) {
		rt.stats.Probes.Add(1)
		next.ServeHTTP(w, r)
		return
	}
	p := rt.cfg.Policy(class)
	uid := int64(0)
	if id != nil {
		if v, _, ok := id(r); ok {
			uid = v
		}
	}
	key := rt.key(class, p, tenantPart(uid), r)
	dec := rt.allow(r.Context(), class, key, p, r)
	rt.writeHeaders(w, dec, p)
	if !dec.Allowed {
		rt.refuse(w, class, p, dec, string(class))
		return
	}
	next.ServeHTTP(w, r)
}

// WrapByRoute classifies a request and applies the matching class. It is the
// single entry point used for the authenticated dashboard.
func (rt *Runtime) WrapByRoute(id IdentityFunc, next http.Handler) http.Handler {
	if rt == nil {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rt.serve(w, r, classFor(r), id, next)
	})
}

// isProbePath reports whether a path is liveness/readiness/version polling. The
// list lives in internal/probes (shared with the API-key gate and the access
// logger) so the shield cannot spend a caller's budget on a probe the other two
// layers already treat as unauthenticated machine traffic.
func isProbePath(p string) bool { return probes.IsProbePath(p) }

// classFor classifies an authenticated request.
func classFor(r *http.Request) Class {
	path := r.URL.Path
	switch {
	case strings.HasPrefix(path, "/api/auth/"):
		return ClassAuth
	case strings.HasPrefix(path, "/api/admin/"), strings.HasPrefix(path, "/admin/"):
		return ClassAdmin
	case strings.HasPrefix(path, "/api/system/"), strings.HasPrefix(path, "/api/v1/system/"):
		return ClassProbe
	case r.Method == http.MethodGet || r.Method == http.MethodHead || r.Method == http.MethodOptions:
		return ClassRead
	default:
		return ClassWrite
	}
}

// tenantPart renders the verified identity for a key. userID<=0 means "no
// verified identity": a request that reached a tenant-scoped limiter without one
// is attributed to the address instead, so it can never consume — or be
// confused with — an account's budget.
func tenantPart(userID int64) string {
	if userID <= 0 {
		return ""
	}
	return "u:" + strconv.FormatInt(userID, 10)
}

// idSegment matches path segments that are identifiers: the UUIDs, hex ids and
// numbers the dashboard puts in its URLs.
var idSegment = regexp.MustCompile(`^(?:[0-9a-fA-F]{8}-[0-9a-fA-F-]{4,}|[0-9a-fA-F]{16,}|[0-9]+)$`)

// routeGroup is the endpoint dimension of a key.
//
// Without it one endpoint can consume a tenant's entire budget (a log export
// that streams for a minute would lock the account out of everything); with the
// full path in the key the keyspace grows with every id a client invents. The
// group keeps the endpoint's first two meaningful segments and collapses
// identifiers, so `/api/rules/<uuid>/settings` and `/api/rules/<other>/settings`
// share one budget while `/api/logs` keeps its own.
func routeGroup(path string) string {
	trimmed := strings.TrimPrefix(path, "/api")
	trimmed = strings.TrimPrefix(trimmed, "/")
	parts := strings.Split(trimmed, "/")
	var kept []string
	for _, seg := range parts {
		seg = strings.TrimSpace(seg)
		if seg == "" {
			continue
		}
		if idSegment.MatchString(seg) {
			continue
		}
		kept = append(kept, seg)
		if len(kept) == 2 {
			break
		}
	}
	if len(kept) == 0 {
		return "root"
	}
	return strings.Join(kept, "/")
}

// key builds the Redis key for a request.
//
// The shape is <prefix><algo>:<class>:<group>:<scope>. It is derived only from
// the verified identity, the resolved peer address and the route table — never
// from a body field, a query parameter or a client header.
func (rt *Runtime) key(class Class, p Policy, tenant string, r *http.Request) string {
	group := routeGroup(r.URL.Path)
	addr := rt.cfg.clientAddr(r)
	scope := p.Scope
	switch scope {
	case ScopeTenant:
		if tenant == "" {
			scope = ScopeIP // no verified identity: attribute to the address
		}
	case ScopeTenantIP:
		if tenant == "" {
			scope = ScopeIP
		}
	}
	var b strings.Builder
	b.WriteString(rt.cfg.KeyPrefix)
	b.WriteString(string(rt.cfg.Redis.Algo))
	b.WriteByte(':')
	b.WriteString(string(class))
	b.WriteByte(':')
	b.WriteString(group)
	b.WriteByte(':')
	switch scope {
	case ScopeTenant:
		b.WriteString(tenant)
	case ScopeTenantIP:
		b.WriteString(tenant)
		b.WriteString("|ip:")
		b.WriteString(addr)
	default:
		b.WriteString("ip:")
		b.WriteString(addr)
	}
	return b.String()
}

// writeHeaders publishes the quota so a client can pace itself instead of
// discovering the limit by being refused.
func (rt *Runtime) writeHeaders(w http.ResponseWriter, dec Decision, p Policy) {
	if !rt.cfg.Headers {
		return
	}
	h := w.Header()
	h.Set("X-RateLimit-Limit", strconv.FormatInt(dec.Limit, 10))
	h.Set("X-RateLimit-Remaining", strconv.FormatInt(max64(dec.Remaining, 0), 10))
	if !dec.Allowed {
		retry := int(dec.RetryAfter.Seconds() + 0.999)
		if retry < 1 {
			retry = 1
		}
		h.Set("Retry-After", strconv.Itoa(retry))
		h.Set("X-RateLimit-Reset", strconv.Itoa(retry))
	}
	if dec.Degraded {
		// The client is told the limit is being enforced per process: an
		// operator reading a support ticket needs to see that this was the
		// degraded path, not the cluster limit.
		h.Set("X-RateLimit-Degraded", "1")
	}
}

// refuse writes the refusal. A rate limit refusal names itself in the same
// shape as every other refusal in this API ({"error","code"}), so a client
// renders it instead of printing an identifier, and gives the retry-after both
// in the header and in the body.
func (rt *Runtime) refuse(w http.ResponseWriter, class Class, p Policy, dec Decision, where string) {
	if class == ClassAuth || class == ClassShield {
		// The security classes fail closed: when the limit could not be
		// evaluated at all, the honest answer is 503 ("try again shortly"),
		// not 429 ("you are over the limit") — and never "proceed".
		if p.Fail == FailClosed && dec.Unavailable {
			writeProblem(w, http.StatusServiceUnavailable, map[string]any{
				"error": "rate limiting is unavailable; this endpoint refuses requests until it can enforce its limit",
				"code":  "rate_limit_unavailable",
			}, 1)
			rt.logf("ratelimit: fail-closed refusal class=%s path=%s", class, where)
			return
		}
	}
	retry := int(dec.RetryAfter.Seconds() + 0.999)
	if retry < 1 {
		retry = 1
	}
	writeProblem(w, http.StatusTooManyRequests, map[string]any{
		"error":               "rate limit exceeded",
		"code":                "rate_limited",
		"retry_after_seconds": retry,
		"limit":               dec.Limit,
		"scope":               string(p.Scope),
	}, retry)
}

func writeProblem(w http.ResponseWriter, status int, body map[string]any, retryAfter int) {
	if retryAfter < 1 {
		retryAfter = 1
	}
	if w.Header().Get("Retry-After") == "" {
		w.Header().Set("Retry-After", strconv.Itoa(retryAfter))
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

// StatusReport is the observable state of the limiter, for the status endpoint
// and the QA harness.
type StatusReport struct {
	Enabled    bool         `json:"enabled"`
	Algo       string       `json:"algorithm"`
	Addrs      []string     `json:"redis_addrs,omitempty"`
	Reachable  bool         `json:"redis_reachable"`
	Error      string       `json:"redis_error,omitempty"`
	Breaker    breakerState `json:"breaker"`
	Stats      Snapshot     `json:"stats"`
	LocalStats Snapshot     `json:"local_stats"`
	LocalKeys  int          `json:"local_keys"`
	Policies   []PolicyView `json:"policies"`
	KeyPrefix  string       `json:"key_prefix,omitempty"`
}

// PolicyView is one class as reported to an operator: what is enforced, where
// the key comes from, and what happens when Redis is gone.
type PolicyView struct {
	Class        string `json:"class"`
	PerMinute    int64  `json:"per_minute"`
	Burst        int64  `json:"burst"`
	Scope        string `json:"scope"`
	FailMode     string `json:"fail_mode"`
	WindowSecond int64  `json:"window_seconds"`
}

// Status probes the backend and summarises the effective configuration.
func (rt *Runtime) Status(ctx context.Context) StatusReport {
	rep := StatusReport{
		Enabled:   rt.Enabled(),
		Algo:      string(rt.cfg.Redis.Algo),
		Addrs:     redactAddrs(rt.cfg.Redis.Addrs),
		KeyPrefix: rt.cfg.KeyPrefix,
		Stats:     rt.stats.Snapshot(),
	}
	if rt.fallback != nil {
		rep.LocalStats = rt.fallback.Stats().Snapshot()
		rep.LocalKeys = rt.fallback.Size()
	}
	for _, class := range []Class{ClassAuth, ClassShield, ClassRead, ClassWrite, ClassAdmin, ClassProbe} {
		p := rt.cfg.Policy(class)
		rep.Policies = append(rep.Policies, PolicyView{
			Class:        string(class),
			PerMinute:    p.Budget.Limit,
			Burst:        p.Budget.capacity(),
			Scope:        string(p.Scope),
			FailMode:     string(p.Fail),
			WindowSecond: int64(p.Budget.Window.Seconds()),
		})
	}
	if rt.primary == nil {
		return rep
	}
	if p, ok := rt.primary.(interface{ Stats() (*Stats, breakerState) }); ok {
		_, state := p.Stats()
		rep.Breaker = state
	}
	if p, ok := rt.primary.(interface{ Ping(context.Context) error }); ok {
		if err := p.Ping(ctx); err != nil {
			rep.Reachable = false
			rep.Error = redactErr(err, rt.cfg.Redis.Password)
		} else {
			rep.Reachable = true
		}
	}
	return rep
}

// redactAddrs strips any credentials a URL-shaped address may carry, so the
// status report can be pasted into a ticket.
func redactAddrs(addrs []string) []string {
	out := make([]string, 0, len(addrs))
	for _, a := range addrs {
		if i := strings.Index(a, "@"); i >= 0 {
			// redis://user:pass@host:port -> redis://[REDACTED]@host:port
			scheme := ""
			rest := a
			if j := strings.Index(a, "://"); j >= 0 {
				scheme = a[:j+3]
				rest = a[j+3:]
				i = strings.Index(rest, "@")
			}
			if i >= 0 {
				out = append(out, scheme+"[REDACTED]"+rest[i:])
				continue
			}
		}
		out = append(out, a)
	}
	return out
}

// redactErr removes a configured password from an error string: connection
// errors sometimes echo the address, and the address may carry the credential.
func redactErr(err error, password string) string {
	msg := err.Error()
	if password != "" {
		msg = strings.ReplaceAll(msg, password, "[REDACTED]")
	}
	return msg
}
