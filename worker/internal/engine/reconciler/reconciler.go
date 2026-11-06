// Package reconciler closes ambiguous deliveries the fence deliberately
// left UNKNOWN.
//
// The delivery contract is: an outcome Telegram never reported (timeout,
// dropped connection) goes UNKNOWN and is "never retried blindly —
// reconciliation decides". But an ambiguity nobody ever inspects is a
// permanently stranded job: production accumulated UNKNOWN rows for a day
// because the decision step simply did not exist. This loop is that step.
//
// It closes only what the recorded evidence PROVES: a gRPC Unimplemented
// outcome means the runtime server received the call, found no method, and
// returned before any handler ran — no Telegram operation can have been
// attempted, so the message cannot be in the target. Everything else
// (timeouts, connection drops after accept, unknown outcomes) stays UNKNOWN
// and visible: guessing here would be exactly the duplicate delivery the
// fence exists to prevent. Operators see what remains through /ops/queue
// (the unknown counter) and the incidents surface.
package reconciler

import (
	"context"
	"strings"
	"time"

	"atf/worker/internal/domain/job"
)

// Store is the reconciliation surface the engine needs from the queue store.
type Store interface {
	UnknownFences(ctx context.Context, olderThan time.Time, limit int) ([]job.UnknownFence, error)
	ReconcileUnknown(ctx context.Context, key job.AttemptKey, workerID, reason string) (bool, error)
}

// Logger is the structured logger contract (satisfied by the worker's logger).
type Logger interface {
	Info(msg string, args ...any)
	Warn(msg string, args ...any)
	Error(msg string, args ...any)
}

// maxBatch bounds one pass. A backlog of thousands of ambiguities resolves
// across consecutive ticks instead of one long-running sweep.
const maxBatch = 100

// Reconciler periodically resolves UNKNOWN fence rows older than the grace
// window when their recorded failure proves no Telegram side effect.
type Reconciler struct {
	store    Store
	workerID string
	grace    time.Duration
	interval time.Duration
	log      Logger
}

// New builds a reconciler. grace should comfortably exceed the claim lease:
// a row still UNKNOWN long after every lease could have expired is one no
// live dispatch can explain.
func New(store Store, workerID string, grace, interval time.Duration, log Logger) *Reconciler {
	return &Reconciler{store: store, workerID: workerID, grace: grace, interval: interval, log: log}
}

// provableMarkers are the recorded failure fragments that PROVE the runtime
// rejected the call before any handler ran. gRPC reports Unimplemented with
// code 12; the Python runtime's server raises NotImplementedError for a
// method it does not register, and the Go client surfaces it as
// `code = Unimplemented desc = Unexpected <class 'NotImplementedError'>…`.
//
// A dial-phase Unavailable is equally safe: when the client's transport
// failed *while dialing* (`transport: Error while dialing: dial tcp …`),
// no HTTP/2 stream ever existed, so no request byte reached the runtime and
// no handler can have run. A bare `code = Unavailable` is NOT on the list —
// a transport that dies mid-call after Telegram accepted the upload reports
// the same code, and closing that would duplicate the delivery.
var provableMarkers = []string{
	"code = Unimplemented",
	"Error while dialing",
}

// provablyUnexecuted reports whether the recorded failure proves the RPC
// never executed a Telegram operation. Anything not on this list stays
// UNKNOWN — ambiguity is resolved by evidence, never by hope.
func provablyUnexecuted(lastError string) bool {
	for _, marker := range provableMarkers {
		if strings.Contains(lastError, marker) {
			return true
		}
	}
	return false
}

// RunOnce performs one reconciliation pass over the aged UNKNOWN fences.
// It returns how many were resolved and how many remain (of those inspected).
func (r *Reconciler) RunOnce(ctx context.Context) (resolved, remaining int, err error) {
	fences, err := r.store.UnknownFences(ctx, time.Now().Add(-r.grace), maxBatch)
	if err != nil {
		return 0, 0, err
	}
	for _, f := range fences {
		if !provablyUnexecuted(f.LastError) {
			remaining++
			continue
		}
		reason := "reconciled: the runtime rejected the RPC before any handler ran (no Telegram " +
			"operation was attempted, so the message cannot be in the target); original: " +
			truncate(f.LastError, 200)
		ok, rerr := r.store.ReconcileUnknown(ctx, f.Key, r.workerID, reason)
		if rerr != nil {
			r.log.Error("reconcile of an ambiguous delivery failed",
				"worker_id", r.workerID,
				"job_id", f.Key.String(),
				"rule_id", f.Key.RuleID,
				"source_message_id", f.Key.SourceMessageID,
				"error", rerr.Error())
			remaining++
			continue
		}
		if ok {
			resolved++
		}
	}
	return resolved, remaining, nil
}

// Start runs the reconciliation loop until ctx is cancelled.
func (r *Reconciler) Start(ctx context.Context) {
	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			resolved, remaining, err := r.RunOnce(ctx)
			if err != nil {
				// A failed pass only delays recovery — the rows stay
				// durable UNKNOWN, never lost.
				r.log.Warn("reconciliation pass failed", "error", err)
				continue
			}
			if resolved > 0 {
				r.log.Warn("reconciler closed ambiguous deliveries",
					"worker_id", r.workerID,
					"component", "worker.reconciler",
					"resolved", resolved, "left_unknown", remaining)
			}
		}
	}
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
