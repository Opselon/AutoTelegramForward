package delivery

// This file is the W2 half of the Stage 4.5 delivery preflight: the
// Forward-Every-N cadence gate. It is ONE clearly-marked preflight block,
// independent of the W1 routing block that precedes it (each workstream
// keeps its own additions adjacent and self-contained).
//
// The semantics here are a port of core/application/pipeline.py's step 4c
// (pipeline.py:1065), not a redesign — the same "which message owns a slot"
// arithmetic (internal/domain/forwardinterval, ported from
// core/domain/forward_interval.py) over the same durable rule_counters
// table (internal/engine/counters, ported from
// SqliteRuleCounterRepository.advance / the Postgres equivalent).

import (
	"context"
	"errors"

	"atf/worker/internal/domain/forwardinterval"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/engine/counters"
)

// --- W2: the cadence gate ------------------------------------------------- //

// forwardEveryNGate decides whether the job's message owns its slot in the
// task's forward-every-N cadence, and consumes exactly one sequence number
// when the task has a cadence at all.
//
// The semantics ported from the Python gate (pipeline.py:1065):
//
//   - N = 1 is inert: every message goes, the store is never touched and no
//     rule_counters row is ever written — old tasks stay byte-for-byte
//     alike (Python's test_n1_forward_everything_and_writes_no_counter);
//   - only filter-passing messages consume a slot. The caller gates on the
//     filter outcome, so reaching this point at all means the message
//     passed: Python applies the cadence AFTER every filter, and the Go
//     preflight places this gate after the filter and block_links gates
//     for the same reason;
//   - an album is one slot: the dispatcher aggregated the media group into
//     one job, so one Advance per job counts the album exactly once
//     regardless of how many items Telegram sent;
//   - an edit never counts and never uses a slot (pipeline.py:1068:
//     `not payload.is_edit`): an edit updates a copy that already had its
//     slot, so counting it would both consume a slot and let an edit beat
//     a real message to one. The job payload carries the Python-side
//     edit markers (is_edit_folded / edit_date), and the job-level
//     presentation of an edit is a re-dispatch of the same event, whose
//     slot was consumed on the first pass;
//   - a store error FAILS OPEN, deliberately (pipeline.py:1075): a broken
//     bookkeeping row must not silence a task that was forwarding fine
//     before the feature existed. The message is forwarded (the
//     pre-feature behaviour) and the failure is loud in the log.
//
// A skip is terminal for the job: closing it FAILED records the decision
// without an attempt and without leaving a fence claim behind. Python
// additionally marks the skipped message processed so a redelivery cannot
// consume the next slot — that is W3's table, and the two halves agree
// that a job already marked processed on the ingest side never reaches
// this gate a second time.
func (e *Engine) forwardEveryNGate(ctx context.Context, j *job.DeliveryJob, r *rule.ForwardRule, logger Logger) error {
	// The counter store is optional the same way Python's _counters port
	// is: an embed without the repository forwards everything.
	if e.counters == nil {
		return nil
	}
	// Python: every_n = int(getattr(rule, "forward_every_n", 1) or 1);
	// the projection already clamps, and Clamp's own output is what
	// guards here — 1 is the inert default both engines agree on.
	everyN := r.ForwardEveryN
	if everyN <= forwardinterval.DefaultForwardEveryN {
		return nil
	}
	// An edit never counts and never uses a slot.
	if isEditJob(j) {
		return nil
	}

	// A re-dispatch never consumes a slot either. Python evaluates this
	// gate once per message, at ingest (pipeline.py:1065); a delivery retry
	// or a lease-expiry recovery re-runs the job without re-entering the
	// pipeline, and the re-delivery of an event the counter already counted
	// is absorbed upstream by the event-level dedupe (pipeline.py:867). The
	// Go worker applies the same gate on its side of the boundary, which
	// means it sees every re-claim too — so the gate must be able to tell a
	// first dispatch from a re-dispatch, or a transient failure would eat a
	// second cadence slot for the same message (and, under N=2, eventually
	// skip a message that owned its slot).
	//
	// The claim hands the gate that fact through the job's ClaimedFrom: a
	// row that was PENDING at claim time is a first dispatch, while
	// RETRY_WAIT (or a CLAIMED row whose lease expired) is a job the engine
	// already processed once. Only the first advances the counter.
	if isJobRedispatch(j) {
		return nil
	}

	sequence, err := e.counters.Advance(ctx, j.RuleID, everyN, r.OwnerUserID)
	if err != nil {
		// Fail open, deliberately: a broken bookkeeping row must not
		// silence a task that was forwarding fine before the feature
		// existed. The message is forwarded (the pre-feature behaviour)
		// and the failure is loud in the log.
		logger.Error("forward-every-n counter unavailable — forwarding anyway",
			"error", err,
			"rule_id", j.RuleID,
			"message_id", j.SourceMessageID,
			"every_n", everyN)
		return nil
	}
	if sequence == 0 {
		// The store could not produce a sequence. Python treats a missing
		// row read the same way: fail open and forward, because "no
		// bookkeeping number available" must never become "do not
		// deliver".
		logger.Error("forward-every-n counter returned no sequence — forwarding anyway",
			"rule_id", j.RuleID,
			"message_id", j.SourceMessageID,
			"every_n", everyN)
		return nil
	}
	if forwardinterval.OwnsSlot(sequence, everyN) {
		logger.Info("forward-every-n message owns a slot",
			"rule_id", j.RuleID, "message_id", j.SourceMessageID,
			"sequence", sequence, "every_n", everyN)
		return nil
	}

	logger.Warn("job skipped by forward-every-n cadence — closing job",
		"decision", "DROP", "reason_code", "FORWARD_INTERVAL",
		"rule_id", j.RuleID, "message_id", j.SourceMessageID,
		"sequence", sequence, "every_n", everyN)
	_ = e.store.MarkFailed(ctx, j.ID, "Forward every N: skipped by cadence")
	// A cadence skip is the add-on working exactly as configured — counted
	// as "rejected" (a deliberate non-delivery), never as "failed".
	e.metrics.Outcome("rejected")
	// The sentinel is the gate's terminal signal: Process must stop here so
	// no fence claim and no Telegram call follows a job that will not send.
	return errFenSkipped
}

// errFenSkipped is the gate's terminal "this message does not own a slot"
// decision. It is deliberately not an error the caller reports: the job is
// already closed and the metrics already record the outcome, so Process
// converts it into a clean nil (the same exit the filter and block_links
// drops take) instead of surfacing a delivery failure.
var errFenSkipped = errors.New("forward every n: message does not own a slot")

// isEditJob reports whether the job represents an edit of a message the
// task has already handled, rather than a new message. See
// job.Payload.IsEdit for the three durable markers and why the cadence
// must not consume a slot for an edit.
func isEditJob(j *job.DeliveryJob) bool {
	if j == nil {
		return false
	}
	return j.Payload.IsEdit()
}

// isJobRedispatch reports whether the claim that produced this job read the
// delivery_jobs row in a state other than PENDING — a RETRY_WAIT row the
// backoff released, or a CLAIMED row whose lease expired. Both mean the
// engine already processed this job once; the cadence consumed its slot on
// that pass and must not consume it a second time (Python's
// test_a_redelivered_message_does_not_advance_the_counter).
func isJobRedispatch(j *job.DeliveryJob) bool {
	if j == nil {
		return false
	}
	switch j.ClaimedFrom {
	case job.StatusRetryWait, job.StatusClaimed:
		return true
	}
	return false
}

// --- the counters store the gate consumes --------------------------------- //

// SetCounters installs the durable cadence store. Optional by design: a
// worker (or a test embed) without one forwards every message, exactly as
// Python's DurableMessagePipeline treats a missing counters port.
func (e *Engine) SetCounters(store counters.Store) {
	e.counters = store
}

// CountersStore exposes the store the engine consumes, for tests that
// assert on the persisted cadence without going through the queue.
func (e *Engine) CountersStore() counters.Store { return e.counters }
