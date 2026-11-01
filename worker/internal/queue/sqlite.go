// Package queue is the Go side of the durable delivery queue.
//
// This is a *port*, not a redesign: the SQL here re-implements the exact
// claim order, lane-isolation predicate and fence semantics the Python
// SqliteDeliveryQueueRepository / SqliteDeliveryAttemptRepository already
// provide, so the two engines are interchangeable over the same
// delivery_jobs / delivery_attempts tables.
//
// One table set, one claim semantics, two engines — the migration flips which
// engine consumes the queue; it does not add a second queue.
package queue

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "github.com/mattn/go-sqlite3" // registers the sqlite3 driver

	"atf/worker/internal/domain/job"
)

// ErrNotFound is returned when an expected job row is absent.
var ErrNotFound = fmt.Errorf("job not found")

// Store is the delivery queue port the worker consumes.
type Store interface {
	// ClaimBatch claims the next deliverable jobs. See sqliteStore.ClaimBatch
	// for the ordering / lane-isolation contract — it is load-bearing.
	ClaimBatch(ctx context.Context, workerID string, batchSize int, leaseDuration time.Duration) ([]*job.DeliveryJob, error)

	// MarkSent marks one job SENT and records the target message id.
	MarkSent(ctx context.Context, jobID string, targetMessageID int64) error
	// MarkSentBatch marks many jobs SENT in one transaction.
	MarkSentBatch(ctx context.Context, results []SentResult) (int, error)
	// Defer postpones a job WITHOUT spending an attempt (transient blocker).
	Defer(ctx context.Context, jobID, reason string, backoff time.Duration) error
	// MarkRetry spends one attempt and re-queues with backoff, dead-lettering
	// when the budget is exhausted.
	MarkRetry(ctx context.Context, jobID, error string, backoff time.Duration) error
	// MarkFailed terminates a job as FAILED without touching attempts.
	MarkFailed(ctx context.Context, jobID, error string) error
	// MarkUnknown records an ambiguous Telegram outcome (no re-dispatch).
	MarkUnknown(ctx context.Context, jobID, error string) error

	// PendingCount feeds the backpressure guard the Python pipeline applies
	// at enqueue time; the worker reports the same number in metrics.
	PendingCount(ctx context.Context) (int, error)

	// FenceClaim is delivery_attempts.claim_or_check. Only FenceDispatch
	// allows a send; see job.FenceOutcome for the contract.
	FenceClaim(ctx context.Context, key job.AttemptKey, workerID string, leaseUntil float64) (job.FenceOutcome, *job.DeliveryAttempt, error)
	// FenceRecordSent stamps a confirmed Telegram result on the fence row.
	FenceRecordSent(ctx context.Context, key job.AttemptKey, workerID string, targetMessageID int64) error
	// FenceRecordOutcome stamps a FAILED or UNKNOWN outcome on the fence row.
	FenceRecordOutcome(ctx context.Context, key job.AttemptKey, workerID, status, error string) error
	// FenceExpireStale moves a CLAIMED row whose lease passed to UNKNOWN —
	// never back to "dispatch by default", because the send may have landed.
	FenceExpireStale(ctx context.Context, now float64) (int, error)
	// FenceResolveUnknown reopens an UNKNOWN fence after the caller has
	// proved no earlier copy is in the target. The reason is recorded.
	FenceResolveUnknown(ctx context.Context, key job.AttemptKey, workerID, reason string) error

	// NoteMapping records the delivery outcome on the job's source->target
	// message_map row (primary forward only — the Python worker's rule, and
	// the reason keyword-reply jobs never write there).
	NoteMapping(ctx context.Context, j *job.DeliveryJob, status job.Status, targetMessageID int64) error

	// RecordAlbumProgress merges confirmed split-album items into the job's
	// payload so a retry resumes instead of duplicating (audit finding E2E-5).
	RecordAlbumProgress(ctx context.Context, jobID string, sentMap map[string]int64) error

	// Close releases the underlying connection.
	Close() error
}

// SentResult is one entry of a batched SENT commit: job id + target id.
type SentResult struct {
	JobID           string
	TargetMessageID int64
}

// sqliteStore is the SQLite implementation of Store over the existing
// delivery_jobs / delivery_attempts / message_map tables.
type sqliteStore struct {
	db *sql.DB
}

// writable is a startup probe: a store that cannot be written cannot serve
// the queue — the claim update alone is a write. Opening the file and
// *pinging* it is not enough, because a read-only filesystem still answers
// a ping. Acquiring and rolling back a transaction whose DSN carries
// `_txlock=immediate` proves the file is writable and lockable, and leaves
// no schema object and no row behind.
//
// This is what makes the worker honest about a volume its non-root user
// cannot write: the process refuses to start rather than reporting "ready"
// while every claim fails.
func writable(ctx context.Context, db *sql.DB) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("probe write: %w", err)
	}
	// CREATE TABLE IF NOT EXISTS is the cheapest statement that still
	// demands a write lock; rolled back, it leaves nothing behind.
	if _, err := tx.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS atf_worker_probe (id INTEGER PRIMARY KEY)`); err != nil {
		_ = tx.Rollback()
		return fmt.Errorf("probe write: %w", err)
	}
	return tx.Rollback()
}

// NewSQLite opens the existing atf.db and applies the busy/locking behaviour
// the Python side relies on (single writer, serialised by the kernel lock).
func NewSQLite(ctx context.Context, dsn string) (Store, error) {
	conn, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("open delivery db: %w", err)
	}
	// One physical connection: every Go-side mutation is a short critical
	// section under _txlock=immediate, mirroring Python's single-connection
	// _lock. More connections would not add throughput (SQLite serialises
	// writes anyway) and they would add a new way to deadlock.
	conn.SetMaxOpenConns(1)
	if err := conn.PingContext(ctx); err != nil {
		conn.Close()
		return nil, fmt.Errorf("ping delivery db: %w", err)
	}
	if err := writable(ctx, conn); err != nil {
		conn.Close()
		return nil, err
	}
	return &sqliteStore{db: conn}, nil
}

func (s *sqliteStore) Close() error { return s.db.Close() }

// scanJob builds a job.DeliveryJob from a delivery_jobs row.
func scanJob(rows *sql.Rows) (*job.DeliveryJob, error) {
	var (
		j                        job.DeliveryJob
		status, payloadJSON      string
		nextRetry, lease         float64
		workerID, errDetail      sql.NullString
		interChat                sql.NullString
		interMsg                 sql.NullInt64
		stage                    sql.NullString
		kind                     sql.NullString
	)
	cols := []any{
		&j.ID, &j.RuleID, &j.SourceChatID, &j.SourceMessageID, &j.TargetChatID,
		&payloadJSON, &status, &j.Attempts, &j.MaxAttempts, &nextRetry, &lease,
		&workerID, &errDetail, &j.CreatedAt, &j.UpdatedAt,
		&interChat, &interMsg, &stage, &kind,
	}
	if err := rows.Scan(cols...); err != nil {
		return nil, fmt.Errorf("scan delivery job: %w", err)
	}
	j.Status = job.Status(status)
	j.NextRetryAt = nextRetry
	j.LeaseUntil = lease
	j.WorkerID = workerID.String
	j.ErrorDetail = errDetail.String
	j.IntermediateChatID = interChat.String
	j.IntermediateMsgID = interMsg.Int64
	j.DeliveryStage = stage.String
	if kind.String != "" {
		j.JobKind = kind.String
	}
	if j.JobKind == "" {
		j.JobKind = "forward"
	}
	var raw map[string]any
	if payloadJSON != "" {
		_ = json.Unmarshal([]byte(payloadJSON), &raw) // tolerate malformed
	}
	j.Payload = *job.NewPayload(raw)
	return &j, nil
}

var jobColumns = `id, rule_id, source_chat_id, source_message_id, target_chat_id,
	payload_json, status, attempts, max_attempts, next_retry_at, lease_until,
	worker_id, error_detail, created_at, updated_at,
	intermediate_chat_id, intermediate_message_id, delivery_stage, job_kind`

// ClaimBatch claims the next deliverable jobs.
//
// The predicate and ORDER BY are byte-for-byte the Python worker's:
//
//   - a row is claimable when PENDING / RETRY_WAIT / (CLAIMED with an expired
//     lease), its next_retry_at gate has elapsed, and NO earlier unsent job
//     exists for the same target_chat_id. That last clause makes one target
//     a single lane with one in-flight job, so two workers can never deliver
//     to the same target concurrently while different targets stay parallel.
//   - the order is (source_chat_id, source_message_id, created_at, id): the
//     source's own order is the message id; created_at only breaks ties
//     inside one chat. Reordering by created_at alone was a real defect
//     (it let a straggling lower message id overtake) — do not "fix" it.
func (s *sqliteStore) ClaimBatch(ctx context.Context, workerID string, batchSize int, leaseDuration time.Duration) ([]*job.DeliveryJob, error) {
	if batchSize < 1 {
		batchSize = 1
	}
	now := time.Now().Unix()
	leaseUntil := now + int64(leaseDuration.Seconds())

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("claim begin: %w", err)
	}
	defer tx.Rollback()

	query := fmt.Sprintf(`
		SELECT %s FROM delivery_jobs AS j
		WHERE (
			j.status = 'PENDING'
			OR j.status = 'RETRY_WAIT'
			OR (j.status = 'CLAIMED' AND j.lease_until < ?)
		)
		AND j.next_retry_at <= ?
		AND NOT EXISTS (
			SELECT 1 FROM delivery_jobs AS o
			WHERE o.target_chat_id = j.target_chat_id
			  AND o.id <> j.id
			  AND o.status IN ('PENDING', 'RETRY_WAIT', 'CLAIMED')
			  AND (o.source_chat_id, o.source_message_id, o.created_at, o.id)
			      < (j.source_chat_id, j.source_message_id, j.created_at, j.id)
		)
		ORDER BY j.source_chat_id ASC, j.source_message_id ASC,
		         j.created_at ASC, j.id ASC
		LIMIT ?`, jobColumns)

	rows, err := tx.QueryContext(ctx, query, now, now, batchSize)
	if err != nil {
		return nil, fmt.Errorf("claim select: %w", err)
	}
	claimed := make([]*job.DeliveryJob, 0, batchSize)
	for rows.Next() {
		j, err := scanJob(rows)
		if err != nil {
			rows.Close()
			return nil, err
		}
		claimed = append(claimed, j)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, fmt.Errorf("claim rows: %w", err)
	}
	rows.Close()
	if len(claimed) == 0 {
		return nil, nil
	}

	// One UPDATE for the whole batch, under the same transaction: this is the
	// atomic claim. A concurrent claimer either sees the rows as PENDING or
	// does not see them at all.
	ids := make([]any, 0, len(claimed)+3)
	ids = append(ids, workerID, leaseUntil, now)
	for _, c := range claimed {
		ids = append(ids, c.ID)
	}
	marks := ""
	for i := range claimed {
		if i > 0 {
			marks += ","
		}
		marks += "?"
	}
	update := fmt.Sprintf(`
		UPDATE delivery_jobs
		SET status = 'CLAIMED', worker_id = ?, lease_until = ?, updated_at = ?
		WHERE id IN (%s)`, marks)
	if _, err := tx.ExecContext(ctx, update, ids...); err != nil {
		return nil, fmt.Errorf("claim update: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("claim commit: %w", err)
	}

	for _, c := range claimed {
		c.Status = job.StatusClaimed
		c.WorkerID = workerID
		c.LeaseUntil = float64(leaseUntil)
	}
	return claimed, nil
}

// MarkSent marks one job SENT.
func (s *sqliteStore) MarkSent(ctx context.Context, jobID string, targetMessageID int64) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE delivery_jobs SET status='SENT', lease_until=0, updated_at=? WHERE id=?`,
		time.Now().Unix(), jobID)
	return err
}

// MarkSentBatch commits a whole delivered batch with ONE UPDATE + ONE commit
// (commits are what make SQLite slow under load — every commit is an fsync).
func (s *sqliteStore) MarkSentBatch(ctx context.Context, results []SentResult) (int, error) {
	if len(results) == 0 {
		return 0, nil
	}
	now := time.Now().Unix()
	args := make([]any, 0, len(results)+1)
	args = append(args, now)
	for _, r := range results {
		args = append(args, r.JobID)
	}
	marks := ""
	for i := range results {
		if i > 0 {
			marks += ","
		}
		marks += "?"
	}
	res, err := s.db.ExecContext(ctx, fmt.Sprintf(`
		UPDATE delivery_jobs SET status='SENT', lease_until=0, updated_at=?
		WHERE id IN (%s)`, marks), args...)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

// Defer postpones WITHOUT spending an attempt. The blockers this serves — a
// paused task, a rate-limited target, a temporarily-down session, a missing
// client — are states of the environment, not failures of the job. Spending
// an attempt on them is what turns a ten-second outage into a dead-lettered
// message reported as DELIVERY_EXHAUSTED.
func (s *sqliteStore) Defer(ctx context.Context, jobID, reason string, backoff time.Duration) error {
	now := time.Now()
	next := now.Add(backoff).Unix()
	res, err := s.db.ExecContext(ctx, `
		UPDATE delivery_jobs
		SET status='RETRY_WAIT', next_retry_at=?, error_detail=?, lease_until=0, updated_at=?
		WHERE id=? AND status NOT IN ('SENT','FAILED')`,
		next, reason, now.Unix(), jobID)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

// MarkRetry spends one attempt. When the budget is exhausted the job is
// FAILED and a dead-letter row is written, so an operator can replay it.
func (s *sqliteStore) MarkRetry(ctx context.Context, jobID, reason string, backoff time.Duration) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("retry begin: %w", err)
	}
	defer tx.Rollback()

	var attempts, maxAttempts int
	err = tx.QueryRowContext(ctx,
		`SELECT attempts, max_attempts FROM delivery_jobs WHERE id=?`, jobID).
		Scan(&attempts, &maxAttempts)
	if err != nil {
		if err == sql.ErrNoRows {
			return ErrNotFound
		}
		return fmt.Errorf("retry read: %w", err)
	}
	now := time.Now()
	attempts++
	if attempts >= maxAttempts {
		if err := writeDeadLetter(ctx, tx, jobID, reason, "DELIVERY_EXHAUSTED", now, attempts); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE delivery_jobs
			SET status='FAILED', attempts=?, error_detail=?, lease_until=0, updated_at=?
			WHERE id=?`, attempts, reason, now.Unix(), jobID); err != nil {
			return fmt.Errorf("retry fail: %w", err)
		}
		return tx.Commit()
	}
	next := now.Add(backoff).Unix()
	if _, err := tx.ExecContext(ctx, `
		UPDATE delivery_jobs
		SET status='RETRY_WAIT', attempts=?, next_retry_at=?, error_detail=?, lease_until=0, updated_at=?
		WHERE id=?`, attempts, next, reason, now.Unix(), jobID); err != nil {
		return fmt.Errorf("retry update: %w", err)
	}
	return tx.Commit()
}

// MarkFailed terminates a job (the rule was deleted/disabled, or a permanent
// error). The job's spent attempts are left alone.
func (s *sqliteStore) MarkFailed(ctx context.Context, jobID, reason string) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE delivery_jobs SET status='FAILED', error_detail=?, lease_until=0, updated_at=?
		WHERE id=?`, reason, time.Now().Unix(), jobID)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

// MarkUnknown records an ambiguous delivery: Telegram's reply never came
// back, so neither "sent" nor "not sent" is provable. It is never retried
// automatically — reconciliation decides.
func (s *sqliteStore) MarkUnknown(ctx context.Context, jobID, reason string) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE delivery_jobs SET status='UNKNOWN', error_detail=?, lease_until=0, updated_at=?
		WHERE id=?`, reason, time.Now().Unix(), jobID)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

// PendingCount counts the active backlog (PENDING/CLAIMED/RETRY_WAIT).
func (s *sqliteStore) PendingCount(ctx context.Context) (int, error) {
	var count int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM delivery_jobs WHERE status IN ('PENDING','CLAIMED','RETRY_WAIT')`).
		Scan(&count)
	return count, err
}

// writeDeadLetter preserves an exhausted job for replay.
func writeDeadLetter(ctx context.Context, tx *sql.Tx, jobID, reason, eventCode string, now time.Time, attempts int) error {
	var payloadJSON string
	err := tx.QueryRowContext(ctx, `SELECT payload_json FROM delivery_jobs WHERE id=?`, jobID).
		Scan(&payloadJSON)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil // the job row is gone; nothing to dead-letter
		}
		return fmt.Errorf("dead-letter read: %w", err)
	}
	_, err = tx.ExecContext(ctx, `
		INSERT OR REPLACE INTO dead_letter_queue
			(job_id, rule_id, source_chat_id, source_message_id, target_chat_id,
			 payload_json, reason, event_code, attempts, dead_lettered_at)
		SELECT ?, rule_id, source_chat_id, source_message_id, target_chat_id,
		       ?, ?, ?, ?, ?
		FROM delivery_jobs WHERE id=?`,
		jobID, payloadJSON, reason, eventCode, attempts, now.Unix(), jobID)
	if err != nil {
		return fmt.Errorf("dead-letter insert: %w", err)
	}
	return nil
}

// --- message_map --------------------------------------------------------- //

// NoteMapping records the outcome on the job's source->target mapping row.
// Only the PRIMARY forward owns that row — the map is keyed by
// (rule, source_chat, source_message, target), so a keyword reply would
// overwrite the forward's target id and edit/delete sync would act on the
// wrong post. This is the Python worker's rule, kept verbatim.
func (s *sqliteStore) NoteMapping(ctx context.Context, j *job.DeliveryJob, status job.Status, targetMessageID int64) error {
	if j.JobKind != "forward" || j.Payload.IsReply() {
		return nil
	}
	_, err := s.db.ExecContext(ctx, `
		UPDATE message_map SET
			target_message_id = COALESCE(?, target_message_id),
			delivery_status = ?,
			updated_at = ?
		WHERE rule_id=? AND source_chat_id=? AND source_message_id=? AND target_chat_id=?`,
		targetMessageID, string(status), time.Now().Unix(),
		j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID)
	return err
}

// RecordAlbumProgress merges a split album's confirmed items into the job's
// payload. Terminal (SENT) rows are never rewritten, and only confirmed
// entries are merged (audit finding E2E-5: a retry used to re-send every
// item, duplicating the ones Telegram had already accepted).
func (s *sqliteStore) RecordAlbumProgress(ctx context.Context, jobID string, sentMap map[string]int64) error {
	if len(sentMap) == 0 {
		return nil
	}
	var current map[string]any
	var payloadJSON string
	err := s.db.QueryRowContext(ctx,
		`SELECT payload_json FROM delivery_jobs WHERE id=?`, jobID).Scan(&payloadJSON)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil
		}
		return err
	}
	if payloadJSON != "" {
		_ = json.Unmarshal([]byte(payloadJSON), &current)
	}
	if current == nil {
		current = map[string]any{}
	}
	existing := current["album_sent"]
	merged := map[string]any{}
	if m, ok := existing.(map[string]any); ok {
		for k, v := range m {
			merged[k] = v
		}
	}
	for k, v := range sentMap {
		merged[k] = v
	}
	current["album_sent"] = merged
	out, err := json.Marshal(current)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx,
		`UPDATE delivery_jobs SET payload_json=?, updated_at=? WHERE id=? AND status NOT IN ('SENT')`,
		string(out), time.Now().Unix(), jobID)
	return err
}

// --- delivery_attempts: the send-side idempotency fence ------------------ //

// FenceClaim is the atomic check-and-claim of one logical send.
//
// Outcomes (the shared contract with Python's claim_or_check):
//
//   - dispatch — the caller owns this send and may dispatch it. Only a row
//     that does not exist yet, or one whose last outcome was FAILED (a
//     replay), can be claimed. CLAIMED/SENDING rows are never stolen, live
//     or expired.
//   - in_flight — another worker holds a live claim; must not dispatch.
//   - already_sent — a previous attempt recorded SENT; never re-dispatch.
//   - unknown / stale — an earlier send's outcome was never recorded (the
//     crash window, or an expired claim). The caller must NOT dispatch; it
//     reconciles and reopens the fence through FenceResolveUnknown. This is
//     the difference between at-least-once and at-most-once-by-accident.
func (s *sqliteStore) FenceClaim(ctx context.Context, key job.AttemptKey, workerID string, leaseUntil float64) (job.FenceOutcome, *job.DeliveryAttempt, error) {
	now := time.Now().Unix()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", nil, fmt.Errorf("fence begin: %w", err)
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO delivery_attempts (
			id, rule_id, source_chat_id, source_message_id, target_chat_id,
			job_kind, status, worker_id, lease_until, attempts,
			last_error, created_at, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, 'CLAIMED', ?, ?, 0, '', ?, ?)
		ON CONFLICT(id) DO UPDATE SET
			status = 'CLAIMED',
			worker_id = excluded.worker_id,
			lease_until = excluded.lease_until,
			attempts = delivery_attempts.attempts + 1,
			last_error = '',
			updated_at = excluded.updated_at
		WHERE delivery_attempts.status = 'FAILED'`,
		key.String(), key.RuleID, key.SourceChatID, key.SourceMessageID,
		key.TargetChatID, key.JobKind, workerID, leaseUntil, now, now); err != nil {
		return "", nil, fmt.Errorf("fence upsert: %w", err)
	}
	row := tx.QueryRowContext(ctx, `
		SELECT status, worker_id, lease_until, target_message_id, last_error, attempts
		FROM delivery_attempts WHERE id=?`, key.String())
	var a job.DeliveryAttempt
	if err := row.Scan(&a.Status, &a.WorkerID, &a.LeaseUntil, &a.TargetMessageID, &a.LastError, &a.Attempts); err != nil {
		if err == sql.ErrNoRows {
			// The upsert succeeded but the row cannot be read: treat it as
			// unavailable rather than guessing — refuse to dispatch unprotected.
			return "", nil, fmt.Errorf("fence row vanished after upsert for %s", key.String())
		}
		return "", nil, fmt.Errorf("fence read: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return "", nil, fmt.Errorf("fence commit: %w", err)
	}

	switch a.Status {
	case "SENT":
		return job.FenceAlreadySent, &a, nil
	case "UNKNOWN":
		return job.FenceUnknown, &a, nil
	case "FAILED":
		// The UPSERT re-claimed it for this caller.
		return job.FenceDispatch, &a, nil
	}
	if a.WorkerID == workerID {
		return job.FenceDispatch, &a, nil
	}
	if a.LeaseUntil > 0 && a.LeaseUntil >= float64(now) {
		return job.FenceInFlight, &a, nil
	}
	return job.FenceStale, &a, nil
}

// FenceRecordSent stamps a confirmed Telegram result. It is written BEFORE
// the job-row commit on purpose: a crash between the Telegram response and
// the batch commit must leave the fence saying the send landed.
func (s *sqliteStore) FenceRecordSent(ctx context.Context, key job.AttemptKey, workerID string, targetMessageID int64) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE delivery_attempts
		SET status='SENT', worker_id=?, target_message_id=?, updated_at=?
		WHERE id=?`, workerID, targetMessageID, time.Now().Unix(), key.String())
	return err
}

// FenceRecordOutcome stamps a FAILED outcome, releasing the fence for a
// retry. Only call this when the Telegram failure is *unambiguous* — a
// timeout's outcome is UNKNOWN, not FAILED.
func (s *sqliteStore) FenceRecordOutcome(ctx context.Context, key job.AttemptKey, workerID, status, detail string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE delivery_attempts SET status=?, worker_id=?, last_error=?, updated_at=?
		WHERE id=?`, status, workerID, detail, time.Now().Unix(), key.String())
	return err
}

// FenceExpireStale moves a CLAIMED row whose lease passed to UNKNOWN —
// never back to dispatch-by-default: the send may have been accepted.
func (s *sqliteStore) FenceExpireStale(ctx context.Context, now float64) (int, error) {
	res, err := s.db.ExecContext(ctx, `
		UPDATE delivery_attempts SET status='UNKNOWN', updated_at=?
		WHERE status IN ('CLAIMED','SENDING') AND lease_until > 0 AND lease_until < ?`,
		time.Now().Unix(), now)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

// FenceResolveUnknown reopens an UNKNOWN fence after the caller has PROVED
// (a Telegram-side read or a bounded recovery decision) that no earlier copy
// is in the target. The reason is recorded so an operator can audit why a
// possibly-already-sent delivery was re-dispatched.
func (s *sqliteStore) FenceResolveUnknown(ctx context.Context, key job.AttemptKey, workerID, reason string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE delivery_attempts SET status='FAILED', worker_id=?, last_error=?, updated_at=?
		WHERE id=? AND status='UNKNOWN'`, workerID, reason, time.Now().Unix(), key.String())
	return err
}
