package ratelimit

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

// Lease is a distributed lock for work that must not run twice.
//
// What it is for: a maintenance loop that must run on exactly ONE replica —
// recovering expired queue leases, a periodic cleanup, a poller that would
// otherwise consume the same stream twice. Those loops are idempotent-ish, but
// running them N times wastes N× the work and makes an incident harder to read.
//
// What it is NOT for: guaranteeing that a Telegram message is delivered once. A
// lease can be lost while its holder is still working (a pause longer than the
// TTL, a network partition, a stop-the-world pause), so work whose repetition is
// harmful — a delivery, a broadcast, a payment — must keep its own durable,
// database-backed idempotency (the delivery queue's UNIQUE(rule, source,
// message, target) plus its claim/lease_until columns). The Redis lease stops
// replicas from STARTING the same job; the database stops a duplicated job from
// having an effect. Both, or neither is trustworthy.
//
// The lock value is a random 128-bit token, and renewal/release are
// token-checked Lua: without that check, a process that stalled past its TTL
// would renew or delete a lease that another process has since acquired.
type Lease struct {
	rdb     redis.UniversalClient
	renew   *redis.Script
	release *redis.Script
	key     string
	token   string
	ttl     time.Duration

	mu      sync.Mutex
	lost    bool
	stopped bool
	stop    chan struct{}
	done    chan struct{}
}

// LeaseOptions configures acquisition.
type LeaseOptions struct {
	// TTL is how long the lease survives without renewal. It must exceed the
	// longest pause the holder can take between renewals, or the holder will
	// lose a lease it is still relying on.
	TTL time.Duration
	// AutoRenew renews at TTL/3 in the background. A holder whose work can
	// outlast the TTL should use it; a holder that keeps its critical section
	// shorter than the TTL does not need the extra traffic.
	AutoRenew bool
}

// ErrLeaseNotHeld means the lease is gone (expired, released, or re-acquired by
// another replica). The holder must treat its work as no longer exclusive.
var ErrLeaseNotHeld = errors.New("ratelimit: lease not held")

// newToken returns a 128-bit random lock value.
func newToken() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// Not recoverable for a lock, but a unique-enough value still prevents
		// the two holders from believing they own the same lease.
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(b[:])
}

// AcquireLease takes a named lease, or reports that someone else holds it.
//
// A refusal to acquire is NOT an error: it is the normal outcome for every
// replica but one, and the caller's job is to skip the work, not to retry in a
// loop that turns into a stampede.
func (l *redisLimiter) AcquireLease(ctx context.Context, name string, opts LeaseOptions) (*Lease, bool, error) {
	if opts.TTL <= 0 {
		opts.TTL = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, l.opts.OpTimeout)
	defer cancel()

	lease := &Lease{
		rdb:     l.rdb,
		renew:   l.scripts[luaScripts.LeaseRenew.Name],
		release: l.scripts[luaScripts.Lease.Name],
		key:     leaseKey(l.opts.Prefix, name),
		token:   newToken(),
		ttl:     opts.TTL,
		stop:    make(chan struct{}),
		done:    make(chan struct{}),
	}
	ok, err := l.rdb.SetNX(ctx, lease.key, lease.token, opts.TTL).Result()
	if err != nil {
		l.stats.Errors.Add(1)
		return nil, false, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	if !ok {
		return nil, false, nil
	}
	if opts.AutoRenew {
		go lease.renewLoop(opts.TTL / 3)
	} else {
		close(lease.done)
	}
	return lease, true, nil
}

// renewLoop keeps a held lease alive until Release or a failed renewal.
func (l *Lease) renewLoop(every time.Duration) {
	if every <= 0 {
		every = time.Second
	}
	defer close(l.done)
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-l.stop:
			return
		case <-t.C:
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			err := l.Renew(ctx)
			cancel()
			if err != nil {
				// The lease is gone. Mark it lost so the holder notices before
				// assuming exclusivity it no longer has.
				l.mu.Lock()
				l.lost = true
				l.mu.Unlock()
				return
			}
		}
	}
}

// Renew extends the lease. It fails with ErrLeaseNotHeld when the token no
// longer matches, which is exactly the case a naive GET+EXPIRE would get wrong.
func (l *Lease) Renew(ctx context.Context) error {
	if l == nil {
		return ErrLeaseNotHeld
	}
	n, err := l.renew.Run(ctx, l.rdb, []string{l.key}, l.token, l.ttl.Milliseconds()).Int64()
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	if n != 1 {
		return ErrLeaseNotHeld
	}
	return nil
}

// Release stops auto-renewal and deletes the lease if this holder still owns
// it. It is safe to call twice.
func (l *Lease) Release(ctx context.Context) error {
	if l == nil {
		return nil
	}
	l.mu.Lock()
	if !l.stopped {
		l.stopped = true
		close(l.stop)
	}
	l.mu.Unlock()
	<-l.done

	if ctx == nil {
		ctx = context.Background()
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	if _, err := l.release.Run(ctx, l.rdb, []string{l.key}, l.token).Int64(); err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	return nil
}

// Lost reports whether the lease was lost while held (renewal failed), i.e.
// whether another replica may now be running the same job.
func (l *Lease) Lost() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lost
}

// Key is the lease key, for diagnostics (never a secret).
func (l *Lease) Key() string { return l.key }

// RunWithLease runs fn only if this process acquires the lease.
//
// It is the shape every caller wants: "if I am the leader, do the thing".
// fn runs while the lease is held and the lease is released afterwards even if
// fn panics, so a crashed iteration cannot wedge the lock for the TTL.
func (l *redisLimiter) RunWithLease(ctx context.Context, name string, opts LeaseOptions, fn func(ctx context.Context) error) (bool, error) {
	lease, ok, err := l.AcquireLease(ctx, name, opts)
	if err != nil || !ok {
		return false, err
	}
	defer func() {
		rctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = lease.Release(rctx)
	}()
	return true, fn(ctx)
}

// leaseKey namespaces a lease under the deployment's prefix, so two deployments
// sharing one Redis can never contend for the same lock.
func leaseKey(prefix, name string) string {
	if prefix == "" {
		prefix = "atf:rl:"
	}
	return prefix + "lock:" + name
}
