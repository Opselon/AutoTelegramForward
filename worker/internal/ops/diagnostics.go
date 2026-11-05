package ops

import "context"

// QueueDiagnostics is the §5 "queue diagnostics" read: the signals that
// distinguish a queue that is merely busy from one that is broken.
//
// Every field is derivable from delivery_jobs in ONE bounded query per store;
// no new tables, no per-message writes, no unbounded history. The counts are
// point-in-time and cheap enough to serve on every /ops/queue/diagnostics
// poll, unlike a full text log scan.
type QueueDiagnostics struct {
	// StuckClaimed is CLAIMED rows whose lease has expired but which are
	// still CLAIMED: a worker died mid-job (or its claim loop stalled) and
	// the stale-claim sweep has not reclaimed them yet. A chronic nonzero
	// value means the sweep is not keeping up.
	StuckClaimed int64 `json:"stuck_claimed"`
	// StuckPending is PENDING rows older than the stuck threshold: nobody
	// has claimed them even though they are eligible (their retry gate has
	// elapsed). A chronic nonzero value means the claim loop is not running,
	// is rate-limited by something else, or no workers are configured —
	// the queue is starved, not merely idle.
	StuckPending int64 `json:"stuck_pending"`
	// PoisonJobs is rows whose attempts are at least half of max_attempts
	// and whose last failure is a PERMANENT (non-retryable) Telegram error:
	// the queue is spending its retry budget on a job that can never
	// succeed. These belong in the dead-letter conversation, not in
	// the active backlog.
	PoisonJobs int64 `json:"poison_jobs"`
	// RetryStorm is the count of rows currently in RETRY_WAIT whose next
	// retry is imminent (within the next 60s). A spike here means a burst
	// of failures is about to hit the delivery engine again — the leading
	// indicator of a retry storm.
	RetryStorm int64 `json:"retry_storm"`
	// OldestPendingAgeSeconds is the age of the oldest unsent job; 0 when
	// nothing is waiting. It is the backlog's severity at a glance: a depth
	// of 40 jobs that are 3 seconds old is fine, a depth of 40 that is 40
	// minutes old is stuck.
	OldestPendingAgeSeconds float64 `json:"oldest_pending_age_seconds"`

	// StuckClaimedThresholdSeconds etc. are the thresholds the values above
	// were computed with, serialized so the UI never has to guess what
	// "stuck" meant for this snapshot.
	StuckClaimedThresholdSeconds int64 `json:"stuck_claimed_threshold_seconds"`
	StuckPendingThresholdSeconds int64 `json:"stuck_pending_threshold_seconds"`
	RetryStormWindowSeconds      int64 `json:"retry_storm_window_seconds"`
}

// QueueDiagnostician is the read-only diagnostics slice of the queue store.
// Both store implementations (sqliteStore, pgStore) satisfy it through
// methods added in sqlite.go / postgres.go, the same pattern as
// QueueStoreReader: /ops needs no second connection and no knowledge of
// which backend is live.
type QueueDiagnostician interface {
	QueueDiagnostics(ctx context.Context, now int64) (*QueueDiagnostics, error)
}

// Stuck-thresholds: deliberately conservative — a value that flaps the
// console's "stuck" indicator every poll is worse than one that reacts a
// minute late.
const (
	// StuckClaimedThreshold: lease is 30s, 3x that is a dead worker.
	StuckClaimedThreshold int64 = 90
	// StuckPendingThreshold: 5 minutes with no claim.
	StuckPendingThreshold int64 = 300
	// RetryStormWindow: the next 60 seconds of retries.
	RetryStormWindow int64 = 60
)

// NewQueueDiagnostics computes the diagnostics snapshot for the given now
// (unix seconds) using store when it implements QueueDiagnostician. A store
// that does not (an older backend) returns nil, nil — /ops then reports the
// section as unavailable rather than fabricating zeros.
func NewQueueDiagnostics(ctx context.Context, store any, now int64) (*QueueDiagnostics, error) {
	d, ok := store.(QueueDiagnostician)
	if !ok {
		return nil, nil
	}
	diag, err := d.QueueDiagnostics(ctx, now)
	if err != nil {
		return nil, err
	}
	if diag != nil {
		diag.StuckClaimedThresholdSeconds = StuckClaimedThreshold
		diag.StuckPendingThresholdSeconds = StuckPendingThreshold
		diag.RetryStormWindowSeconds = RetryStormWindow
	}
	return diag, nil
}
