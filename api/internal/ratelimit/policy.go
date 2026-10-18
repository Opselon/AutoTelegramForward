package ratelimit

import (
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// FailMode is a route class's outage policy: what happens to a request when the
// authoritative limiter cannot answer.
type FailMode string

const (
	// FailClosed refuses the request. Used where the limit IS the security
	// control — an authentication endpoint whose brute-force budget disappears
	// with Redis is an invitation, so a refusal with 503 is the safe answer.
	FailClosed FailMode = "closed"
	// FailLocal applies the same budget from the in-process fallback. Used for
	// ordinary authenticated traffic, where refusing every request because an
	// accelerator is down would be a self-inflicted outage.
	FailLocal FailMode = "local"
	// FailOpen lets the request through unmeasured. It is available but not a
	// default anywhere in this package: a limiter class that can be disarmed by
	// breaking Redis is not a limit.
	FailOpen FailMode = "open"
)

// Policy is one route class's configuration.
type Policy struct {
	Class Class
	// Budget is the allowance per scope.
	Budget Budget
	// Fail decides the outage behaviour.
	Fail FailMode
	// Scope selects the key.
	Scope Scope
	// Cost is how many units a request in this class spends.
	Cost int64
}

// Scope names the identity a key is derived from. It is an enum on purpose: the
// key must never be derived from anything the caller can choose.
type Scope string

const (
	// ScopeIP keys on the peer address (after trusted-proxy resolution).
	ScopeIP Scope = "ip"
	// ScopeTenant keys on the VERIFIED user id from the JWT.
	ScopeTenant Scope = "u"
	// ScopeTenantIP keys on verified user id AND peer address: a stolen token
	// used from a second address cannot consume the owner's whole budget, and
	// one address cannot exhaust several tenants' budgets at once.
	ScopeTenantIP Scope = "uip"
)

// Config is everything the limiter needs from the environment.
type Config struct {
	Enabled bool
	Redis   RedisOptions
	// Policies maps the class to its configuration.
	Policies map[Class]Policy
	// LocalMaxKeys bounds the in-process fallback.
	LocalMaxKeys int
	// TrustedProxies are the networks whose X-Forwarded-For may be believed.
	TrustedProxies []*net.IPNet
	// Prefix prepended to every key; it lets several deployments share one
	// Redis without ever resolving to the same key.
	KeyPrefix string
	// Header controls whether X-RateLimit-* headers are emitted.
	Headers bool
}

// DefaultBudgetFor returns the budget a class gets when the operator configures
// nothing. The numbers are deliberately generous for reads and tight for the
// unauthenticated surfaces, because that is where abuse is cheap.
func DefaultBudgetFor(c Class) Budget {
	switch c {
	case ClassAuth:
		// 30/minute per address, burst 10: a human logging in from a phone
		// never notices; an offline guesser against a one-time code cannot
		// make progress.
		return Budget{Limit: 30, Window: time.Minute, Burst: 10}
	case ClassShield:
		// The outer guard runs before token validation, so it must absorb a
		// scraper loop while never inconveniencing a busy dashboard: 600
		// requests/minute per address with a 120 burst.
		return Budget{Limit: 600, Window: time.Minute, Burst: 120}
	case ClassRead:
		return Budget{Limit: 600, Window: time.Minute, Burst: 120}
	case ClassWrite:
		// Writes reach the core and Telegram; they are the expensive ones and
		// legitimately rare (a rule edit, a settings change).
		return Budget{Limit: 120, Window: time.Minute, Burst: 30}
	case ClassAdmin:
		return Budget{Limit: 300, Window: time.Minute, Burst: 60}
	case ClassProbe:
		// A monitor polling every second uses 60/minute; ten monitors on one
		// address still fit.
		return Budget{Limit: 1200, Window: time.Minute, Burst: 300}
	default:
		return Budget{Limit: 300, Window: time.Minute, Burst: 60}
	}
}

// defaultScopeFor returns the key identity a class uses by default.
func defaultScopeFor(c Class) Scope {
	switch c {
	case ClassAuth, ClassShield:
		return ScopeIP
	case ClassRead, ClassWrite:
		return ScopeTenant
	case ClassAdmin:
		return ScopeTenant
	default:
		return ScopeIP
	}
}

// defaultFailFor returns the outage policy a class uses by default.
func defaultFailFor(c Class) FailMode {
	switch c {
	case ClassAuth, ClassShield:
		return FailClosed
	case ClassProbe:
		// A liveness probe must never be refused because an accelerator is
		// down: a monitor that sees 503 reports the deployment as broken.
		return FailLocal
	default:
		return FailLocal
	}
}

// ConfigFromEnv reads the deployment's configuration.
//
// Names follow the deployment's existing convention: ATF_* for anything an
// operator sets in .env, with the Redis names matching the ones the Python core
// reads, so one .env configures both.
func ConfigFromEnv(getenv func(string) string) Config {
	if getenv == nil {
		getenv = os.Getenv
	}
	opts := DefaultRedisOptions()
	addr := firstNonEmpty(getenv("ATF_REDIS_ADDR"), getenv("ATF_REDIS_URL"))
	opts.Addrs = splitAddrs(addr)
	opts.Username = getenv("ATF_REDIS_USERNAME")
	opts.Password = getenv("ATF_REDIS_PASSWORD")
	opts.DB = envInt(getenv, "ATF_REDIS_DB", 0)
	opts.TLS = envBool(getenv, "ATF_REDIS_TLS", false)
	if p := getenv("ATF_RATELIMIT_PREFIX"); p != "" {
		opts.Prefix = p
	}
	if a := getenv("ATF_RATELIMIT_ALGO"); a != "" {
		opts.Algo = Algo(strings.ToLower(strings.TrimSpace(a)))
	}
	if v := envInt(getenv, "ATF_REDIS_POOL_SIZE", 0); v > 0 {
		opts.PoolSize = v
	}
	if v := envInt(getenv, "ATF_REDIS_TIMEOUT_MS", 0); v > 0 {
		d := time.Duration(v) * time.Millisecond
		opts.OpTimeout, opts.ReadTimeout, opts.WriteTimeout = d, d, d
	}
	if v := envInt(getenv, "ATF_REDIS_POOL_TIMEOUT_MS", 0); v > 0 {
		opts.PoolTimeout = time.Duration(v) * time.Millisecond
	}
	if p := envInt(getenv, "ATF_RATELIMIT_PROTOCOL", 0); p == 2 || p == 3 {
		opts.Protocol = p
	}

	// Enabled by default when an address is configured: a deployment that
	// wires Redis and forgets a flag should get the protection, not silence.
	enabled := len(opts.Addrs) > 0
	if raw := strings.TrimSpace(getenv("ATF_RATELIMIT_ENABLED")); raw != "" {
		enabled = parseBool(raw) && len(opts.Addrs) > 0
	}

	cfg := Config{
		Enabled:      enabled,
		Redis:        opts,
		LocalMaxKeys: envInt(getenv, "ATF_RATELIMIT_LOCAL_MAX_KEYS", 20000),
		KeyPrefix:    opts.Prefix,
		Headers:      envBool(getenv, "ATF_RATELIMIT_HEADERS", true),
		Policies:     map[Class]Policy{},
	}
	cfg.TrustedProxies = parseCIDRs(getenv("ATF_TRUSTED_PROXIES"))

	for _, class := range []Class{ClassAuth, ClassShield, ClassRead, ClassWrite, ClassAdmin, ClassProbe} {
		budget := DefaultBudgetFor(class)
		if v := envInt(getenv, envNameForBudget(class), 0); v > 0 {
			budget.Limit = int64(v)
		}
		if v := envInt(getenv, envNameForBurst(class), 0); v > 0 {
			budget.Burst = int64(v)
		}
		if v := envInt(getenv, envNameForWindow(class), 0); v > 0 {
			budget.Window = time.Duration(v) * time.Second
		}
		fail := defaultFailFor(class)
		if raw := strings.TrimSpace(getenv(envNameForFail(class))); raw != "" {
			fail = FailMode(strings.ToLower(raw))
		}
		scope := defaultScopeFor(class)
		if raw := strings.TrimSpace(getenv(envNameForScope(class))); raw != "" {
			scope = Scope(strings.ToLower(raw))
		}
		cfg.Policies[class] = Policy{Class: class, Budget: budget, Fail: fail, Scope: scope, Cost: 1}
	}
	return cfg
}

// envNameForBudget is the operator-visible name of a class's rate. The name
// spells the window out (PER_MINUTE) so a value cannot be misread as
// "per second" — the most expensive misreading in a rate limiter.
func envNameForBudget(c Class) string {
	return "ATF_RATELIMIT_" + strings.ToUpper(string(c)) + "_PER_MINUTE"
}

func envNameForBurst(c Class) string {
	return "ATF_RATELIMIT_" + strings.ToUpper(string(c)) + "_BURST"
}

func envNameForWindow(c Class) string {
	return "ATF_RATELIMIT_" + strings.ToUpper(string(c)) + "_WINDOW_SECONDS"
}

func envNameForFail(c Class) string {
	return "ATF_RATELIMIT_" + strings.ToUpper(string(c)) + "_FAIL_MODE"
}

func envNameForScope(c Class) string {
	return "ATF_RATELIMIT_" + strings.ToUpper(string(c)) + "_SCOPE"
}

// Policy returns one class's effective configuration.
func (c Config) Policy(class Class) Policy {
	if p, ok := c.Policies[class]; ok {
		return p
	}
	return Policy{Class: class, Budget: DefaultBudgetFor(class), Fail: defaultFailFor(class), Scope: defaultScopeFor(class), Cost: 1}
}

// firstNonEmpty returns the first non-blank value.
func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

// splitAddrs accepts a comma-separated list (the shape every other ATF address
// variable uses).
func splitAddrs(raw string) []string {
	var out []string
	for _, part := range strings.Split(raw, ",") {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func envInt(getenv func(string) string, name string, def int) int {
	raw := strings.TrimSpace(getenv(name))
	if raw == "" {
		return def
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		return def
	}
	return v
}

func envBool(getenv func(string) string, name string, def bool) bool {
	raw := strings.TrimSpace(getenv(name))
	if raw == "" {
		return def
	}
	return parseBool(raw)
}

func parseBool(raw string) bool {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "1", "true", "yes", "on", "y":
		return true
	default:
		return false
	}
}

// parseCIDRs parses the trusted-proxy list. An entry without a prefix length is
// read as a single host (/32 or /128), which is what an operator pasting a
// proxy's address means.
func parseCIDRs(raw string) []*net.IPNet {
	var out []*net.IPNet
	for _, part := range strings.Split(raw, ",") {
		p := strings.TrimSpace(part)
		if p == "" {
			continue
		}
		if !strings.Contains(p, "/") {
			if ip := net.ParseIP(p); ip != nil {
				if ip.To4() != nil {
					p += "/32"
				} else {
					p += "/128"
				}
			}
		}
		if _, network, err := net.ParseCIDR(p); err == nil {
			out = append(out, network)
		}
	}
	return out
}

// clientAddr resolves the address a request is attributed to.
//
// X-Forwarded-For is believed ONLY when the immediate peer is a declared
// trusted proxy. Without that check any client could mint a fresh rate-limit
// identity per request by sending a header, which turns a per-address limit
// into no limit at all.
func (c Config) clientAddr(r *http.Request) string {
	host, _, err := net.SplitHostPort(strings.TrimSpace(r.RemoteAddr))
	if err != nil {
		host = strings.TrimSpace(r.RemoteAddr)
	}
	if len(c.TrustedProxies) == 0 || host == "" {
		return host
	}
	peer := net.ParseIP(host)
	if peer == nil || !c.trusted(peer) {
		return host
	}
	xf := r.Header.Get("X-Forwarded-For")
	if xf == "" {
		if fwd := r.Header.Get("X-Real-IP"); fwd != "" {
			xf = fwd
		}
	}
	if xf == "" {
		return host
	}
	// The left-most entry is the original client; everything after it is
	// proxies. Only the left-most matters for attribution.
	first := strings.TrimSpace(strings.Split(xf, ",")[0])
	if ip := net.ParseIP(first); ip != nil {
		return ip.String()
	}
	return host
}

func (c Config) trusted(ip net.IP) bool {
	for _, n := range c.TrustedProxies {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}
