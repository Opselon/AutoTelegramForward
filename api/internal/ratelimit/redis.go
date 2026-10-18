package ratelimit

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// Algo selects the algorithm. It is part of the key prefix, so switching the
// algorithm does not inherit the other one's counters.
type Algo string

const (
	// AlgoBucket is the token bucket (default): one key per scope, two fields,
	// constant memory, smooth refill.
	AlgoBucket Algo = "tb"
	// AlgoWindow is the bucketed sliding-window counter: one key per scope,
	// three fields, better burst behaviour against a fixed window.
	AlgoWindow Algo = "sw"
	// AlgoExactWindow is the sorted-set reference implementation. It is not a
	// production default (memory grows with admitted traffic) but it is
	// selectable so the accuracy claim can be reproduced on a live deployment.
	AlgoExactWindow Algo = "swe"
)

// Valid reports whether the algorithm name is one this package implements.
func (a Algo) Valid() bool {
	return a == AlgoBucket || a == AlgoWindow || a == AlgoExactWindow
}

// Budget is the allowance one class grants one scope.
type Budget struct {
	// Limit is the sustained rate: requests per Window.
	Limit int64
	// Window is the period Limit is expressed over.
	Window time.Duration
	// Burst is the token-bucket capacity, i.e. how much of the allowance may be
	// spent at once. Zero means "Limit" (spend the whole minute's budget in one
	// second, then wait). A value below Limit is clamped up to Limit: a bucket
	// that cannot hold one window's allowance would refuse traffic that the
	// stated rate allows.
	Burst int64
}

// capacity is the bucket size actually used.
func (b Budget) capacity() int64 {
	if b.Burst > b.Limit {
		return b.Burst
	}
	return b.Limit
}

// ratePerSecond converts the budget to the refill rate the script wants.
func (b Budget) ratePerSecond() float64 {
	if b.Window <= 0 {
		return float64(b.Limit)
	}
	return float64(b.Limit) / b.Window.Seconds()
}

// ttl is how long one key may live.
//
// A bucket dies once it would have refilled (plus slack), a window once neither
// bucket can still influence a decision. This is what keeps a hostile client
// that walks a million distinct scopes from growing the keyspace: every key it
// creates expires by itself, without a sweeper.
func (b Budget) ttl(algo Algo, slack time.Duration) time.Duration {
	switch algo {
	case AlgoWindow, AlgoExactWindow:
		return 2*b.Window + slack
	default:
		// From empty, a bucket is full again after Window*capacity/Limit.
		fill := time.Duration(float64(b.Window) * (float64(b.capacity()) / float64(max64(b.Limit, 1))))
		return fill + slack
	}
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

// RedisOptions configures the shared Redis connection.
//
// Every field has a bounded default, because an unbounded pool or an unbounded
// command timeout turns a Redis hiccup into an API outage: requests queue on a
// saturated pool instead of failing fast into the documented fallback.
type RedisOptions struct {
	Addrs             []string
	Username          string
	Password          string
	DB                int
	TLS               bool
	Prefix            string
	Algo              Algo
	Protocol          int
	PoolSize          int
	MinIdle           int
	DialTimeout       time.Duration
	ReadTimeout       time.Duration
	WriteTimeout      time.Duration
	PoolTimeout       time.Duration
	OpTimeout         time.Duration
	KeyTTLSlack       time.Duration
	MaxRetries        int
	BreakerOpensAfter int
	BreakerCooldown   time.Duration
}

// DefaultRedisOptions returns the bounded defaults. Values here are the ones a
// deployment that sets nothing but ATF_REDIS_ADDR gets.
func DefaultRedisOptions() RedisOptions {
	return RedisOptions{
		Prefix:       "atf:rl:",
		Algo:         AlgoBucket,
		Protocol:     3,
		PoolSize:     0, // 0 => derived from GOMAXPROCS in connect()
		MinIdle:      4,
		DialTimeout:  500 * time.Millisecond,
		ReadTimeout:  200 * time.Millisecond,
		WriteTimeout: 200 * time.Millisecond,
		PoolTimeout:  150 * time.Millisecond,
		OpTimeout:    250 * time.Millisecond,
		KeyTTLSlack:  10 * time.Second,
		// A consume is not idempotent: a retry after an ambiguous failure could
		// spend a second token. go-redis retries network errors by default, so
		// the client is built with MaxRetries=0 and the ONLY retry this package
		// performs is the bounded, jittered reconnect in redisLimiter.do.
		MaxRetries:        0,
		BreakerOpensAfter: 5,
		BreakerCooldown:   2 * time.Second,
	}
}

// redisLimiter is the authoritative limiter: one shared pool, one EVALSHA per
// check, Lua doing the read-modify-write so N replicas cannot overshoot.
type redisLimiter struct {
	rdb     redis.UniversalClient
	scripts map[string]*redis.Script
	opts    RedisOptions
	stats   *Stats
	breaker *breaker
	// clock is injectable so the tests can move time instead of sleeping: a
	// refill, a window roll and a TTL expiry are all "what happens later", and
	// a test suite that waits for them is a suite nobody runs.
	clock func() time.Time
}

// NewRedisLimiter connects and loads the scripts. It does NOT require Redis to
// be reachable: a deployment whose Redis is briefly down must still start and
// serve (per class policy), and must recover on its own when Redis returns.
func NewRedisLimiter(ctx context.Context, opts RedisOptions) (Limiter, error) {
	if len(opts.Addrs) == 0 {
		return nil, errors.New("ratelimit: no redis address configured")
	}
	if opts.Algo == "" {
		opts.Algo = AlgoBucket
	}
	if !opts.Algo.Valid() {
		return nil, fmt.Errorf("ratelimit: unknown algorithm %q", opts.Algo)
	}
	rdb := redis.NewUniversalClient(&redis.UniversalOptions{
		Addrs:           opts.Addrs,
		Username:        opts.Username,
		Password:        opts.Password,
		DB:              opts.DB,
		TLSConfig:       tlsConfigIf(opts.TLS),
		Protocol:        opts.Protocol,
		PoolSize:        opts.PoolSize,
		MinIdleConns:    opts.MinIdle,
		DialTimeout:     opts.DialTimeout,
		ReadTimeout:     opts.ReadTimeout,
		WriteTimeout:    opts.WriteTimeout,
		PoolTimeout:     opts.PoolTimeout,
		MaxRetries:      opts.MaxRetries,
		ConnMaxIdleTime: 5 * time.Minute,
		ConnMaxLifetime: 30 * time.Minute,
		ClientName:      "atf-api",
	})
	l := newRedisLimiter(rdb, opts)
	// Best-effort warm-up: a first request should not pay SCRIPT LOAD for four
	// scripts. A failure here is not fatal (the fallback policy covers it).
	warmCtx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err := l.warm(warmCtx); err != nil {
		return l, nil
	}
	return l, nil
}

func tlsConfigIf(use bool) *tls.Config {
	if !use {
		return nil
	}
	return &tls.Config{MinVersion: tls.VersionTLS12}
}

// newRedisLimiter builds the limiter around an already-constructed client (used
// by the tests with miniredis).
func newRedisLimiter(rdb redis.UniversalClient, opts RedisOptions) *redisLimiter {
	if opts.OpTimeout <= 0 {
		opts.OpTimeout = 250 * time.Millisecond
	}
	scripts := map[string]*redis.Script{
		luaScripts.Bucket.Name:      redis.NewScript(luaScripts.Bucket.Body),
		luaScripts.Window.Name:      redis.NewScript(luaScripts.Window.Body),
		luaScripts.ExactWindow.Name: redis.NewScript(luaScripts.ExactWindow.Body),
		luaScripts.LeaseRenew.Name:  redis.NewScript(luaScripts.LeaseRenew.Body),
		luaScripts.Lease.Name:       redis.NewScript(luaScripts.Lease.Body),
	}
	return &redisLimiter{
		rdb:     rdb,
		scripts: scripts,
		opts:    opts,
		stats:   &Stats{},
		breaker: newBreaker(opts.BreakerOpensAfter, opts.BreakerCooldown),
		clock:   time.Now,
	}
}

// warm loads every script and pings, so the first real request is a plain
// EVALSHA and a broken deployment is noticed at startup rather than at the
// first login.
func (l *redisLimiter) warm(ctx context.Context) error {
	if err := l.rdb.Ping(ctx).Err(); err != nil {
		l.stats.Errors.Add(1)
		return err
	}
	for _, s := range []string{luaScripts.Bucket.Name, luaScripts.Window.Name, luaScripts.ExactWindow.Name, luaScripts.LeaseRenew.Name, luaScripts.Lease.Name} {
		if err := l.scripts[s].Load(ctx, l.rdb).Err(); err != nil {
			l.stats.Errors.Add(1)
			return err
		}
	}
	return nil
}

// Name implements Limiter.
func (l *redisLimiter) Name() string { return "redis:" + string(l.opts.Algo) }

// Stats exposes the counters (and the breaker state) for the status endpoint.
func (l *redisLimiter) Stats() (*Stats, breakerState) { return l.stats, l.breaker.state() }

// Ping reports whether Redis answers, and arms/disarms the breaker
// accordingly. It is what the readiness probe and the recovery test use.
func (l *redisLimiter) Ping(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, l.opts.OpTimeout)
	defer cancel()
	l.stats.Probes.Add(1)
	if err := l.rdb.Ping(ctx).Err(); err != nil {
		l.breaker.failure()
		l.stats.Errors.Add(1)
		return err
	}
	l.breaker.success()
	return nil
}

// Allow implements Limiter. One round trip, one key.
func (l *redisLimiter) Allow(ctx context.Context, key string, cost int64, b Budget) (Decision, error) {
	key = l.prefixKey(key)
	dec, err := l.allow(ctx, key, cost, b)
	if err != nil {
		l.stats.Errors.Add(1)
		l.stats.Unavailable.Add(1)
		return Decision{Allowed: false, Limit: b.Limit}, err
	}
	dec.Limit = b.capacity()
	if dec.Allowed {
		l.stats.Allowed.Add(1)
	} else {
		l.stats.Limited.Add(1)
	}
	return dec, nil
}

// AllowAll evaluates several independent limits for one request.
//
// The checks are pipelined into a single network round trip: for a request that
// is subject to both a per-address and a per-tenant limit, two sequential
// EVALSHAs would double the latency the limiter adds, and the two decisions are
// independent (they are compared, not combined inside Redis).
func (l *redisLimiter) AllowAll(ctx context.Context, checks []Check) ([]Decision, error) {
	out := make([]Decision, len(checks))
	if len(checks) == 0 {
		return out, nil
	}
	if l.breaker.open() {
		l.stats.Unavailable.Add(int64(len(checks)))
		return out, ErrUnavailable
	}
	ctx, cancel := context.WithTimeout(ctx, l.opts.OpTimeout)
	defer cancel()

	pipe := l.rdb.Pipeline()
	cmds := make([]*redis.Cmd, len(checks))
	keys := make([]string, len(checks))
	for i, c := range checks {
		keys[i] = l.prefixKey(c.Key)
		name, args := l.scriptAndArgs(keys[i], c.Cost, c.Budget)
		cmds[i] = pipe.EvalSha(ctx, l.scripts[name].Hash(), []string{keys[i]}, args...)
	}
	if _, err := pipe.Exec(ctx); err != nil {
		// A pipeline fails as a unit; the individual command errors are below.
		allFailed := true
		for _, cmd := range cmds {
			if cmd.Err() == nil {
				allFailed = false
				break
			}
		}
		if allFailed {
			l.breaker.failure()
			l.stats.Errors.Add(int64(len(checks)))
			l.stats.Unavailable.Add(int64(len(checks)))
			return out, ErrUnavailable
		}
	}
	l.breaker.success()
	for i, cmd := range cmds {
		dec, err := l.decode(cmd, checks[i].Budget)
		if err != nil {
			l.stats.Errors.Add(1)
			l.stats.Unavailable.Add(1)
			out[i] = Decision{Limit: checks[i].Budget.capacity()}
			continue
		}
		if dec.Allowed {
			l.stats.Allowed.Add(1)
		} else {
			l.stats.Limited.Add(1)
		}
		out[i] = dec
	}
	return out, nil
}

// prefixKey puts a key inside this limiter's namespace.
//
// The prefix belongs to the limiter, not to the caller: a caller that composes
// its own scope must not be able to leave the namespace (or, worse, land in
// another deployment's keys on a shared Redis). A key that already carries the
// prefix is left alone, so the wrapper does not double it.
func (l *redisLimiter) prefixKey(key string) string {
	if l.opts.Prefix == "" || strings.HasPrefix(key, l.opts.Prefix) {
		return key
	}
	return l.opts.Prefix + key
}

// Check is one limit inside a batched decision.
type Check struct {
	Key    string
	Budget Budget
	Cost   int64
}

// allow is the single-check path.
func (l *redisLimiter) allow(ctx context.Context, key string, cost int64, b Budget) (Decision, error) {
	if l.breaker.open() {
		return Decision{}, ErrUnavailable
	}
	ctx, cancel := context.WithTimeout(ctx, l.opts.OpTimeout)
	defer cancel()

	name, args := l.scriptAndArgs(key, cost, b)
	cmd := l.scripts[name].Run(ctx, l.rdb, []string{key}, args...)
	if err := cmd.Err(); err != nil {
		// One bounded, jittered retry ONLY when the connection never carried
		// the command. go-redis reports a refused/failed dial distinguishably;
		// anything else may have spent a token already, and spending a second
		// one would make the client pay twice for one request.
		if isDialFailure(err) && l.retryOnce(ctx, name, key, args) == nil {
			l.breaker.success()
			return l.decode(cmd, b)
		}
		l.breaker.failure()
		return Decision{}, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	l.breaker.success()
	return l.decode(cmd, b)
}

func (l *redisLimiter) retryOnce(ctx context.Context, name, key string, args []interface{}) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	sleep := jitter(20*time.Millisecond, 40*time.Millisecond)
	timer := time.NewTimer(sleep)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
	}
	return l.scripts[name].Run(ctx, l.rdb, []string{key}, args...).Err()
}

// scriptAndArgs maps a budget onto the selected algorithm.
func (l *redisLimiter) scriptAndArgs(key string, cost int64, b Budget) (string, []interface{}) {
	if cost < 1 {
		cost = 1
	}
	now := l.clock().UnixMilli()
	ttl := b.ttl(l.opts.Algo, l.opts.KeyTTLSlack).Milliseconds()
	switch l.opts.Algo {
	case AlgoWindow:
		return luaScripts.Window.Name, []interface{}{b.Limit, b.Window.Milliseconds(), now, cost, ttl}
	case AlgoExactWindow:
		return luaScripts.ExactWindow.Name, []interface{}{b.Limit, b.Window.Milliseconds(), now, cost, ttl, key}
	default:
		return luaScripts.Bucket.Name, []interface{}{b.capacity(), b.ratePerSecond(), now, cost, ttl}
	}
}

// decode turns a script reply into a Decision.
func (l *redisLimiter) decode(cmd *redis.Cmd, b Budget) (Decision, error) {
	raw, err := cmd.Result()
	if err != nil {
		return Decision{Limit: b.capacity()}, err
	}
	vals, ok := raw.([]interface{})
	if !ok || len(vals) < 3 {
		return Decision{Limit: b.capacity()}, fmt.Errorf("ratelimit: unexpected reply %T", raw)
	}
	allowed := asInt(vals[0]) == 1
	remaining := asInt(vals[1])
	retryMs := asInt(vals[2])
	return Decision{
		Allowed:    allowed,
		Limit:      b.capacity(),
		Remaining:  remaining,
		RetryAfter: time.Duration(retryMs) * time.Millisecond,
	}, nil
}

func asInt(v interface{}) int64 {
	switch t := v.(type) {
	case int64:
		return t
	case string:
		var n int64
		fmt.Sscanf(t, "%d", &n)
		return n
	case nil:
		return 0
	default:
		return 0
	}
}

// isDialFailure reports whether an error means the command never reached Redis.
func isDialFailure(err error) bool {
	if err == nil {
		return false
	}
	var netErr *net.OpError
	if errors.As(err, &netErr) {
		return true
	}
	msg := err.Error()
	return strings.Contains(msg, "connection refused") ||
		strings.Contains(msg, "connect: connection refused") ||
		strings.Contains(msg, "dial tcp") ||
		strings.Contains(msg, "i/o timeout")
}

// Close releases the pool. Called on shutdown, after the listener stops
// accepting, so in-flight requests finish their decisions.
func (l *redisLimiter) Close() error { return l.rdb.Close() }
