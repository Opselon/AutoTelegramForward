package scheduler

import (
	"context"
	"fmt"
	"math"
	"strings"
	"sync"
	"time"

	"atf/worker/internal/telegraph"
)

// Logger is the structured logger the engine emits through. Fields carry the
// action id and job id, never message content or credentials.
type Logger interface {
	Info(msg string, args ...any)
	Warn(msg string, args ...any)
	Error(msg string, args ...any)
}

// Metrics is the operational counter surface the scheduler publishes. Every
// name mirrors the delivery engine's own counters so one dashboard holds
// both.
type Metrics interface {
	// Outcome bumps an action outcome counter (deleted|retried|failed).
	Outcome(kind string)
	// ObserveDueLatency records how late an action was executed past its
	// due time (seconds; negative or zero means "on time").
	ObserveDueLatency(seconds float64)
	// ObserveQueueDepth records the pending-action depth.
	ObserveQueueDepth(n int)
	// ObserveRetry records one spent retry attempt.
	ObserveRetry(actionID string, attempt int)
}

// Clock is injectable so the tests never sleep for a real TTL.
type Clock interface {
	Now() time.Time
}

// SystemClock is the production clock.
type SystemClock struct{}

// Now returns the wall clock.
func (SystemClock) Now() time.Time { return time.Now() }

// Options tunes the scheduler loop. Every bound exists so a burst of
// scheduled work cannot create unbounded goroutines, connections or timers.
type Options struct {
	// PollInterval is how often the loop looks for due work. It is the
	// scheduler's latency floor, NOT a per-action timer: no goroutine or
	// timer exists per scheduled message.
	PollInterval time.Duration
	// BatchSize bounds how many actions one claim may pick up.
	BatchSize int
	// MaxConcurrent bounds the worker pool that executes due actions.
	MaxConcurrent int
	// RPCTimeout bounds a single Telegram delete call.
	RPCTimeout time.Duration
	// MaxBackoff is the capped retry backoff for transient failures.
	MaxBackoff time.Duration
}

// Defaults are the production settings. The poll interval is the latency
// floor of a deletion, so it stays small; the batch and concurrency bounds
// keep the worst case constant in memory and connections.
func Defaults() Options {
	return Options{
		PollInterval:  time.Second,
		BatchSize:     32,
		MaxConcurrent: 4,
		RPCTimeout:    30 * time.Second,
		MaxBackoff:    10 * time.Minute,
	}
}

// Engine is the scheduled-action executor. One bounded loop claims due work;
// one bounded pool executes it. The engine owns no per-action goroutine and
// no per-action timer — the schedule is a table row, which is what makes the
// whole thing restart-safe.
type Engine struct {
	store     Store
	gateway   telegraph.Gateway
	log       Logger
	metrics   Metrics
	clock     Clock
	opts      Options
	workerID  string
	sessionID string

	mu      sync.Mutex
	stopped bool
}

// New constructs the engine. sessionID is the MTProto session USER-transport
// deletions go through (actions carry their own transport: a BOT action
// resolves its token by id inside the Runtime).
func New(store Store, gateway telegraph.Gateway, log Logger, metrics Metrics, workerID, sessionID string, opts Options) *Engine {
	if opts.PollInterval <= 0 {
		opts = Defaults()
	}
	if opts.BatchSize < 1 {
		opts.BatchSize = 32
	}
	if opts.MaxConcurrent < 1 {
		opts.MaxConcurrent = 4
	}
	if opts.RPCTimeout <= 0 {
		opts.RPCTimeout = 30 * time.Second
	}
	if opts.MaxBackoff <= 0 {
		opts.MaxBackoff = 10 * time.Minute
	}
	return &Engine{
		store: store, gateway: gateway, log: log, metrics: metrics,
		clock: SystemClock{}, opts: opts, workerID: workerID, sessionID: sessionID,
	}
}

// SetClock replaces the clock (tests only).
func (e *Engine) SetClock(c Clock) { e.clock = c }

// Run is the loop. It returns when ctx is cancelled. Shutdown is graceful:
// the loop stops claiming, and in-flight actions either complete (their
// outcome is recorded) or stay CLAIMED — a CLAIMED row whose worker
// disappeared is reclaimable by the next ClaimDue that observes it, because
// a CLAIMED action that never completed is still a pending action.
func (e *Engine) Run(ctx context.Context) error {
	ticker := time.NewTicker(e.opts.PollInterval)
	defer ticker.Stop()

	// The bounded worker pool: handlers consume claimed actions and execute
	// them with a bounded RPC timeout. Sized to MaxConcurrent so a burst of
	// due work becomes bounded parallelism, never N goroutines.
	work := make(chan Action, e.opts.BatchSize)
	var wg sync.WaitGroup
	for i := 0; i < e.opts.MaxConcurrent; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for a := range work {
				e.execute(ctx, a)
			}
		}()
	}

	for {
		select {
		case <-ctx.Done():
			close(work)
			wg.Wait()
			e.mu.Lock()
			e.stopped = true
			e.mu.Unlock()
			return nil
		case <-ticker.C:
			// Feed one batch through the pool, then report depth. A claimed
			// batch that cannot be delivered to the pool blocks the loop —
			// which is exactly the backpressure the design wants: the loop
			// never claims work faster than the pool can execute it.
			actions, err := e.store.ClaimDue(ctx, e.workerID, e.opts.BatchSize, float64(e.clock.Now().Unix()))
			if err != nil {
				e.log.Warn("scheduled-action claim failed — retrying next tick", "error", err)
				continue
			}
			for _, a := range actions {
				select {
				case work <- a:
				case <-ctx.Done():
					// The batch was claimed but not executed: the rows stay
					// CLAIMED and are reclaimable, because the schedule is
					// durable, not in-memory.
					close(work)
					wg.Wait()
					e.mu.Lock()
					e.stopped = true
					e.mu.Unlock()
					return nil
				}
			}
			if depth, err := e.store.PendingCount(ctx); err == nil {
				e.metrics.ObserveQueueDepth(depth)
			}
		}
	}
}

// execute runs one claimed action to a terminal state.
//
// The outcome policy:
//
//   - a message Telegram proves gone (deleted now, or already absent) is a
//     completed action: deletion is an idempotent cleanup operation;
//   - a FloodWait honors Telegram's own retry_after and re-queues;
//   - a transient failure backs off with a capped exponential;
//   - a permanent failure spends its budget ONCE: it is not retried forever,
//     and when the budget is spent the row is left as FAILED for a human.
func (e *Engine) execute(ctx context.Context, a Action) {
	logger := e.actionLogger(a)

	callCtx, cancel := context.WithTimeout(ctx, e.opts.RPCTimeout)
	defer cancel()

	// Ambiguity check: an ambiguous timeout leaves the action CLAIMED for
	// reconciliation rather than re-queuing it blindly (the same honest
	// UNKNOWN policy the delivery engine applies to sends).
	if ferr := e.delete(callCtx, a); ferr != nil {
		if ferr.IsAmbiguous() {
			// The outcome on Telegram's side is unknown: leave the row
			// CLAIMED and surface it, so a duplicate execution is at least a
			// visible decision rather than an invisible one.
			logger.Error("ambiguous scheduled deletion — leaving CLAIMED for reconciliation",
				"code", ferr.Code, "message", ferr.Message)
			e.metrics.Outcome("unknown")
			return
		}

		// FloodWait: Telegram said exactly how long to wait. The next attempt
		// is scheduled at that point, never sooner.
		if ferr.Code == "FLOOD_WAIT" {
			wait := time.Duration(ferr.RetryAfterSeconds) * time.Second
			if wait <= 0 {
				wait = 5 * time.Second
			}
			logger.Warn("flood wait — re-queuing the action for Telegram's window",
				"retry_after_seconds", ferr.RetryAfterSeconds)
			e.requeue(ctx, a, e.clock.Now().Add(wait), ferr.Message, logger)
			return
		}

		// Permanent permission/auth failures: no retry can help. Spend the
		// budget once and leave the row for a human when it is exhausted.
		if !ferr.IsTransient() {
			if a.Attempts+1 >= a.MaxAttempts {
				if err := e.store.Fail(ctx, a.ID, ferr.Code+": "+ferr.Message); err != nil {
					logger.Warn("could not record the terminal failure", "error", err)
				}
				logger.Error("scheduled action permanently failed — budget exhausted",
					"code", ferr.Code, "attempts", a.Attempts+1, "max_attempts", a.MaxAttempts)
				e.metrics.Outcome("failed")
				return
			}
			// A permanent error still spends the budget once: re-queueing it
			// immediately would loop the same refusal forever.
			e.requeue(ctx, a, e.clock.Now().Add(e.opts.MaxBackoff), ferr.Code+": "+ferr.Message, logger)
			return
		}

		// Transient: capped exponential backoff.
		if a.Attempts+1 >= a.MaxAttempts {
			if err := e.store.Fail(ctx, a.ID, ferr.Code+": "+ferr.Message); err != nil {
				logger.Warn("could not record the terminal failure", "error", err)
			}
			logger.Error("scheduled action exhausted its retry budget",
				"code", ferr.Code, "attempts", a.Attempts+1, "max_attempts", a.MaxAttempts)
			e.metrics.Outcome("failed")
			return
		}
		backoff := backoffFor(a.Attempts, e.opts.MaxBackoff)
		logger.Warn("scheduled action transiently failed — re-queuing with backoff",
			"code", ferr.Code, "attempts", a.Attempts+1, "backoff", backoff.String())
		e.requeue(ctx, a, e.clock.Now().Add(backoff), ferr.Message, logger)
		return
	}

	// Confirmed deletion (or already-deleted, which is the same success).
	if err := e.store.Complete(ctx, a.ID); err != nil {
		logger.Warn("deletion succeeded but the row could not be completed — it stays CLAIMED and is reclaimable", "error", err)
	}
	if dueAt := a.DueAt; dueAt > 0 {
		e.metrics.ObserveDueLatency(math.Max(0, e.clock.Now().Sub(time.Unix(int64(dueAt), 0)).Seconds()))
	}
	logger.Info("scheduled action completed — message deleted",
		"transport", string(a.Transport), "chat_id", a.ChatID, "message_id", a.MessageID)
	e.metrics.Outcome("deleted")
}

// delete executes the Telegram side of one action through the transport the
// delivery actually used — a BOT-delivered message is deleted through the
// same bot, a USER-delivered one through the session.
func (e *Engine) delete(ctx context.Context, a Action) *telegraph.DeliveryFailure {
	chatID := parseInt(a.ChatID)
	ids := []int64{a.MessageID}
	switch a.Transport {
	case TransportBot:
		deleted, ferr := e.gateway.DeleteMessages(ctx, telegraph.TransportBot,
			"", /* session not used on the bot route */
			a.TransportRef, a.OwnerUserID, chatID, ids)
		if ferr != nil {
			return ferr
		}
		if !deleted {
			return &telegraph.DeliveryFailure{
				Code:    "UNKNOWN_DELIVERY_OUTCOME",
				Message: "runtime reported the deletion did not confirm",
			}
		}
		return nil
	default:
		_, ferr := e.gateway.DeleteMessages(ctx, telegraph.TransportUser,
			e.sessionID, "", a.OwnerUserID, chatID, ids)
		return ferr
	}
}

// requeue records one spent attempt and puts the action back in the queue at
// the given time.
func (e *Engine) requeue(ctx context.Context, a Action, at time.Time, detail string, logger Logger) {
	if err := e.store.Retry(ctx, a.ID, float64(at.Unix()), detail); err != nil {
		// A failed re-queue leaves the row CLAIMED: recoverable by the next
		// ClaimDue scan (a CLAIMED row that never completed is still
		// pending), but visibly so.
		logger.Warn("could not re-queue the action — it stays CLAIMED and is reclaimable", "error", err)
		return
	}
	e.metrics.ObserveRetry(a.ID, a.Attempts+1)
	e.metrics.Outcome("retried")
}

// backoffFor is the capped exponential schedule for transient failures.
func backoffFor(attempts int, max time.Duration) time.Duration {
	delay := time.Duration(math.Pow(2, float64(attempts))) * time.Second
	if delay > max {
		return max
	}
	if delay < time.Second {
		return time.Second
	}
	return delay
}

// actionLogger returns a logger pre-loaded with the action's identity
// fields. Message content and credentials never ride along.
func (e *Engine) actionLogger(a Action) Logger {
	return &fieldLogger{base: e.log, fields: []any{
		"action_id", a.ID,
		"job_id", a.JobID,
		"action_type", string(a.Type),
		"owner_user_id", a.OwnerUserID,
		"chat_id", a.ChatID,
		"message_id", a.MessageID,
		"transport", string(a.Transport),
	}}
}

type fieldLogger struct {
	base   Logger
	fields []any
}

func (l *fieldLogger) Info(msg string, args ...any)  { l.base.Info(msg, append(l.fields, args...)...) }
func (l *fieldLogger) Warn(msg string, args ...any)  { l.base.Warn(msg, append(l.fields, args...)...) }
func (l *fieldLogger) Error(msg string, args ...any) { l.base.Error(msg, append(l.fields, args...)...) }

// parseInt coerces a stored chat id (kept as text on both backends, because
// channel ids exceed the 32-bit range and a numeric round-trip is how
// precision is lost) to the number the transport needs.
func parseInt(s string) int64 {
	var out int64
	_, _ = fmt.Sscanf(strings.TrimSpace(s), "%d", &out)
	return out
}
