package queue

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	// pgx's database/sql compatibility layer: the worker uses the standard
	// library abstraction everywhere (one pool, one set of semantics, and no
	// driver-specific API leaking into the store), with pgx underneath because
	// it is pure Go and actively maintained.
	"github.com/jackc/pgx/v5/stdlib"

	"atf/worker/internal/domain/job"
)

// pgStore is the PostgreSQL implementation of Store over the existing
// delivery_jobs / delivery_attempts / message_map / dead_letter_queue tables.
//
// It is a port, not a redesign: the SQL mirrors
// core/infrastructure/persistence/postgres_repositories.py method for method,
// so one authoritative queue table set serves both engines on both backends.
// The byte-for-byte claim order, the NOT-EXISTS target-lane isolation and
// the guarded fence UPSERT are all the Python implementation's own
// invariants — see the individual methods for why each is load-bearing.
type pgStore struct {
	db *sql.DB
}

// NewPostgres opens the delivery store against an existing PostgreSQL
// database and proves it is actually usable before the worker reports ready.
//
// The DSN is parsed through pgx so TLS/sslmode options the deployment
// already configures are honored exactly as the Python Core honors them.
func NewPostgres(ctx context.Context, dsn string) (Store, error) {
	conn, err := sql.Open("pgx", dsn)
	if err != nil {
		return nil, fmt.Errorf("open delivery db: %w", err)
	}
	// A bounded pool: the worker's concurrency is bounded by its goroutine
	// count, so the pool must not be the unbounded part of the stack. Defaults
	// follow the queue's own claim batch: one connection per in-flight batch,
	// plus headroom for the fence and the sweep loop.
	conn.SetMaxOpenConns(16)
	conn.SetMaxIdleConns(8)
	conn.SetConnMaxLifetime(30 * time.Minute)
	conn.SetConnMaxIdleTime(5 * time.Minute)
	if err := conn.PingContext(ctx); err != nil {
		conn.Close()
		return nil, fmt.Errorf("ping delivery db: %w", err)
	}
	s := &pgStore{db: conn}
	if err := s.SchemaCheck(ctx); err != nil {
		conn.Close()
		return nil, err
	}
	return s, nil
}

func (s *pgStore) Close() error { return s.db.Close() }

// SchemaCheck verifies the delivery tables exist and carry the columns the
// worker depends on. A worker that silently read the wrong database would
// look healthy while doing nothing; this is the fail-closed gate the
// startup validation requires. It never performs a migration of its own —
// the Python Core is the schema owner.
func (s *pgStore) SchemaCheck(ctx context.Context) error {
	for _, table := range []string{"delivery_jobs", "delivery_attempts", "message_map", "dead_letter_queue"} {
		var exists bool
		err := s.db.QueryRowContext(ctx,
			`SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = $1)`,
			table).Scan(&exists)
		if err != nil {
			return fmt.Errorf("schema check %s: %w", table, err)
		}
		if !exists {
			return fmt.Errorf("schema check: required table %q does not exist; the Python Core owns the schema bootstrap (initialize_postgres_schema)", table)
		}
	}

	// The columns specific to the worker's own query paths. Checking a small
	// fixed set (not every column) keeps this a compatibility gate rather
	// than a schema mirror that breaks on every harmless additive change.
	for table, cols := range map[string][]string{
		"delivery_jobs": {
			"id", "rule_id", "source_chat_id", "source_message_id", "target_chat_id",
			"payload_json", "status", "attempts", "max_attempts", "next_retry_at",
			"lease_until", "worker_id", "error_detail", "created_at", "updated_at",
			"job_kind",
		},
		"delivery_attempts": {
			"id", "rule_id", "source_chat_id", "source_message_id", "target_chat_id",
			"job_kind", "status", "worker_id", "lease_until", "attempts",
			"target_message_id", "last_error",
		},
		"message_map": {
			"rule_id", "source_chat_id", "source_message_id", "target_chat_id",
			"target_message_id", "delivery_status", "updated_at",
		},
		"dead_letter_queue": {
			"id", "job_id", "rule_id", "payload_json", "last_error",
			"error_category", "dead_lettered_at", "attempts",
		},
	} {
		for _, col := range cols {
			var present bool
			err := s.db.QueryRowContext(ctx,
				`SELECT EXISTS (SELECT 1 FROM information_schema.columns
				   WHERE table_schema='public' AND table_name=$1 AND column_name=$2)`,
				table, col).Scan(&present)
			if err != nil {
				return fmt.Errorf("schema check %s.%s: %w", table, col, err)
			}
			if !present {
				return fmt.Errorf("schema check: %s.%s is missing; the Python Core owns the schema bootstrap", table, col)
			}
		}
	}
	return nil
}

// scanJobPG builds a job.DeliveryJob from a delivery_jobs row.
//
// delivery_jobs is one table whose *columns* are identical on both backends,
// so this mirrors scanJob in sqlite.go field for field; only the placeholder
// style and the payload decoding dialect differ.
func scanJobPG(rows *sql.Rows) (*job.DeliveryJob, error) {
	var (
		j                   job.DeliveryJob
		status, payloadJSON string
		nextRetry, lease    float64
		workerID, errDetail string
		interChat           string
		interMsg            sql.NullInt64
		stage               string
		kind                string
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
	j.WorkerID = workerID
	j.ErrorDetail = errDetail
	j.IntermediateChatID = interChat
	if interMsg.Valid {
		j.IntermediateMsgID = interMsg.Int64
	}
	j.DeliveryStage = stage
	j.JobKind = kind
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

var jobColumnsPG = `id, rule_id, source_chat_id, source_message_id, target_chat_id,
	payload_json, status, attempts, max_attempts, next_retry_at, lease_until,
	worker_id, error_detail, created_at, updated_at,
	intermediate_chat_id, intermediate_message_id, delivery_stage, job_kind`

// ClaimBatch claims the next deliverable jobs.
//
// The statement is the Python PostgresDeliveryQueueRepository.claim_batch
// ported exactly: a single UPDATE over a FOR UPDATE SKIP LOCKED sub-select,
// returning the claimed rows. Two properties make it load-bearing and are
// preserved deliberately:
//
//   - the lane predicate: a job is only claimable when no *earlier* unsent
//     job exists for the same target_chat_id. That makes one destination a
//     single lane with one in-flight job, so two workers can never deliver
//     to the same target concurrently while different targets stay parallel.
//   - the ORDER BY (source_chat_id, source_message_id, created_at, id): the
//     source's own order is the message id; created_at only breaks ties
//     inside one chat. Reordering by created_at alone was a real defect and
//     must not be "fixed" here.
//
// This implementation FAILS CLOSED on any error. The Python method carries a
// broad-except fallback that degrades to a read-then-update with no SKIP
// LOCKED, no lane isolation and created_at-only ordering — an *unprotected*
// claim under a real production error. That fallback is deliberately not
// ported: a worker that cannot claim safely must not claim at all.
func (s *pgStore) ClaimBatch(ctx context.Context, workerID string, batchSize int, leaseDuration time.Duration) ([]*job.DeliveryJob, error) {
	if batchSize < 1 {
		batchSize = 1
	}
	now := time.Now()
	leaseUntil := now.Add(leaseDuration)

	// One atomic statement: the sub-select locks exactly the rows the UPDATE
	// will claim, so a concurrent claimer either sees a row as claimable or
	// has it skipped — there is no read-then-write window anywhere.
	query := fmt.Sprintf(`
		UPDATE delivery_jobs
		SET status = 'CLAIMED', worker_id = $1, lease_until = $2, updated_at = $3
		WHERE id IN (
			SELECT id FROM delivery_jobs AS j
			WHERE (
				j.status = 'PENDING'
				OR j.status = 'RETRY_WAIT'
				OR (j.status = 'CLAIMED' AND j.lease_until < $4)
			)
			AND j.next_retry_at <= $5
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
			LIMIT $6
			FOR UPDATE SKIP LOCKED
		)
		RETURNING %s`, jobColumnsPG)

	rows, err := s.db.QueryContext(ctx, query,
		workerID, leaseUntil.Unix(), now.Unix(), now.Unix(), now.Unix(), batchSize)
	if err != nil {
		return nil, fmt.Errorf("claim update: %w", err)
	}
	claimed := make([]*job.DeliveryJob, 0, batchSize)
	for rows.Next() {
		j, err := scanJobPG(rows)
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
	return claimed, nil
}

// MarkSent marks one job SENT.
func (s *pgStore) MarkSent(ctx context.Context, jobID string, targetMessageID int64) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE delivery_jobs SET status='SENT', lease_until=0, updated_at=$1 WHERE id=$2`,
		time.Now().Unix(), jobID)
	return err
}

// MarkSentBatch commits a whole delivered batch with ONE UPDATE (commits are
// what make a database slow under load, not the statements). Partial success
// is reported by the affected row count, exactly as the Python version does.
func (s *pgStore) MarkSentBatch(ctx context.Context, results []SentResult) (int, error) {
	if len(results) == 0 {
		return 0, nil
	}
	// A bounded IN list built from parameters only: no interpolation of values.
	args := make([]any, 0, len(results)+1)
	args = append(args, time.Now().Unix())
	for _, r := range results {
		args = append(args, r.JobID)
	}
	marks := strings.Repeat(",$", len(results))[1:] // $2,$3,...
	query := fmt.Sprintf(`
		UPDATE delivery_jobs SET status='SENT', lease_until=0, updated_at=$1
		WHERE id IN (%s)`, marks)
	res, err := s.db.ExecContext(ctx, query, args...)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

// Defer postpones WITHOUT spending an attempt. The blockers this serves — a
// paused task, a rate-limited target, an offline session — are states of the
// environment, not failures of the job: spending an attempt on them is what
// turned a ten-second outage into a dead-lettered healthy forward.
//
// A SENT or FAILED job is never reopened by a late deferral.
func (s *pgStore) Defer(ctx context.Context, jobID, reason string, backoff time.Duration) error {
	now := time.Now()
	next := now.Add(backoff).Unix()
	res, err := s.db.ExecContext(ctx, `
		UPDATE delivery_jobs
		SET status='RETRY_WAIT', next_retry_at=$1, error_detail=$2, lease_until=0, updated_at=$3
		WHERE id=$4 AND status NOT IN ('SENT','FAILED')`,
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
// FAILED and a dead-letter row is written in the SAME transaction, so an
// exhausted delivery never disappears and never reports failure without its
// diagnostic reason.
func (s *pgStore) MarkRetry(ctx context.Context, jobID, reason string, backoff time.Duration) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("retry begin: %w", err)
	}
	defer tx.Rollback()

	var attempts, maxAttempts int
	err = tx.QueryRowContext(ctx,
		`SELECT attempts, max_attempts FROM delivery_jobs WHERE id=$1`, jobID).
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
		if err := writeDeadLetterPG(ctx, tx, jobID, reason, "DELIVERY_EXHAUSTED", now, attempts); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE delivery_jobs
			SET status='FAILED', attempts=$1, error_detail=$2, lease_until=0, updated_at=$3
			WHERE id=$4`, attempts, reason, now.Unix(), jobID); err != nil {
			return fmt.Errorf("retry fail: %w", err)
		}
		return tx.Commit()
	}
	next := now.Add(backoff).Unix()
	if _, err := tx.ExecContext(ctx, `
		UPDATE delivery_jobs
		SET status='RETRY_WAIT', attempts=$1, next_retry_at=$2, error_detail=$3, lease_until=0, updated_at=$4
		WHERE id=$5`, attempts, next, reason, now.Unix(), jobID); err != nil {
		return fmt.Errorf("retry update: %w", err)
	}
	return tx.Commit()
}

// MarkFailed terminates a job (the rule was deleted/disabled, or a permanent
// Telegram error). The job's spent attempts are left alone: a *temporary*
// condition uses Defer and never reaches here.
func (s *pgStore) MarkFailed(ctx context.Context, jobID, reason string) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE delivery_jobs SET status='FAILED', error_detail=$1, lease_until=0, updated_at=$2
		 WHERE id=$3`, reason, time.Now().Unix(), jobID)
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
// automatically — reconciliation owns it.
func (s *pgStore) MarkUnknown(ctx context.Context, jobID, reason string) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE delivery_jobs SET status='UNKNOWN', error_detail=$1, lease_until=0, updated_at=$2
		 WHERE id=$3`, reason, time.Now().Unix(), jobID)
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
func (s *pgStore) PendingCount(ctx context.Context) (int, error) {
	var count int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM delivery_jobs WHERE status IN ('PENDING','CLAIMED','RETRY_WAIT')`).
		Scan(&count)
	return count, err
}

// writeDeadLetterPG preserves an exhausted job for replay, inside the caller's
// transaction. The row id is `dlq_<job_id>` — the Python implementation's own
// convention, so a dead letter written by either engine lands on the same row.
//
// The error-text column is `last_error` on the current schema. The table also
// carries a legacy `error_message` column (the backend's older name, kept so
// no deployed row loses its text) — but the *write* contract is last_error,
// matching PostgresDeliveryQueueRepository, and writing the legacy name would
// leave last_error empty on rows Python later reads.
func writeDeadLetterPG(ctx context.Context, tx *sql.Tx, jobID, reason, eventCode string, now time.Time, attempts int) error {
	var exists bool
	err := tx.QueryRowContext(ctx,
		`SELECT EXISTS (SELECT 1 FROM delivery_jobs WHERE id=$1)`, jobID).Scan(&exists)
	if err != nil {
		return fmt.Errorf("dead-letter read: %w", err)
	}
	if !exists {
		// The job row is gone; nothing to dead-letter and no payload to keep.
		return nil
	}
	// ON CONFLICT (id) keeps the insert idempotent: a replay of the
	// exhaustion transition refreshes the row instead of failing on it.
	_, err = tx.ExecContext(ctx, `
		INSERT INTO dead_letter_queue (
			id, job_id, rule_id, source_chat_id, source_message_id,
			target_chat_id, payload_json, attempts, last_error,
			error_category, dead_lettered_at
		)
		SELECT $1, id, rule_id, source_chat_id, source_message_id,
		       target_chat_id, payload_json, $2, $3, $4, $5
		FROM delivery_jobs WHERE id=$6
		ON CONFLICT (id) DO UPDATE SET
			last_error = EXCLUDED.last_error,
			attempts = EXCLUDED.attempts,
			error_category = EXCLUDED.error_category,
			dead_lettered_at = EXCLUDED.dead_lettered_at`,
		"dlq_"+jobID, attempts, reason, eventCode, now.Unix(), jobID)
	if err != nil {
		return fmt.Errorf("dead-letter insert: %w", err)
	}
	return nil
}

// --- message_map ----------------------------------------------------------- //

// NoteMapping records the outcome on the job's source->target mapping row.
//
// PostgreSQL's message_map carries its own id column, so the update targets
// the composite key directly (the row the Python pipeline inserted at
// enqueue); only the primary forward may update it — a keyword reply shares
// that composite key and must not overwrite the forward's target id, or edit
// and delete sync would later act on the reply's post instead.
func (s *pgStore) NoteMapping(ctx context.Context, j *job.DeliveryJob, status job.Status, targetMessageID int64) error {
	if j.JobKind != "forward" || j.Payload.IsReply() {
		return nil
	}
	_, err := s.db.ExecContext(ctx, `
		UPDATE message_map SET
			target_message_id = COALESCE($1, target_message_id),
			delivery_status = $2,
			updated_at = $3
		WHERE rule_id=$4 AND source_chat_id=$5 AND source_message_id=$6 AND target_chat_id=$7`,
		targetMessageID, string(status), time.Now().Unix(),
		j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID)
	return err
}

// RecordAlbumProgress merges a split album's confirmed items into the job's
// payload. Terminal (SENT) rows are never rewritten, and only confirmed
// entries are merged (audit finding E2E-5: a retry used to re-send every
// item, duplicating the ones Telegram had already accepted).
func (s *pgStore) RecordAlbumProgress(ctx context.Context, jobID string, sentMap map[string]int64) error {
	if len(sentMap) == 0 {
		return nil
	}
	var payloadJSON string
	err := s.db.QueryRowContext(ctx,
		`SELECT payload_json FROM delivery_jobs WHERE id=$1`, jobID).Scan(&payloadJSON)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil
		}
		return err
	}
	var current map[string]any
	if payloadJSON != "" {
		_ = json.Unmarshal([]byte(payloadJSON), &current)
	}
	if current == nil {
		current = map[string]any{}
	}
	merged := map[string]any{}
	if existing, ok := current["album_sent"].(map[string]any); ok {
		for k, v := range existing {
			merged[k] = v
		}
	}
	for k, v := range sentMap {
		merged[k] = v
	}
	current["album_sent"] = merged
	// The column is JSONB: Go's escaped JSON in a text parameter is accepted
	// by PostgreSQL, but marshalling cleanly keeps the payload readable in
	// every tool that inspects it.
	out, err := json.Marshal(current)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx,
		`UPDATE delivery_jobs SET payload_json=$1, updated_at=$2 WHERE id=$3 AND status NOT IN ('SENT')`,
		string(out), time.Now().Unix(), jobID)
	return err
}

// --- delivery_attempts: the send-side idempotency fence --------------------- //

// FenceClaim is the atomic check-and-claim of one logical send.
//
// The guarded UPSERT is the whole atomicity guarantee, exactly as in the
// Python PostgresDeliveryAttemptRepository: only a row that does not exist
// yet, or one whose last outcome was FAILED (a replay), can be claimed;
// CLAIMED rows are never stolen from their holder, live or expired.
//
// Outcomes (the shared contract with Python's claim_or_check):
//
//   - dispatch — absent or FAILED; the caller owns this send and may dispatch
//   - in_flight — another worker holds a live claim; must not dispatch
//   - already_sent — a previous attempt recorded SENT; never re-dispatch
//   - unknown / stale — an earlier send's outcome was never recorded; the
//     caller must NOT dispatch, it reconciles via FenceResolveUnknown
func (s *pgStore) FenceClaim(ctx context.Context, key job.AttemptKey, workerID string, leaseUntil float64) (job.FenceOutcome, *job.DeliveryAttempt, error) {
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
		) VALUES ($1,$2,$3,$4,$5,$6,'CLAIMED',$7,$8,0,'',$9,$10)
		ON CONFLICT (id) DO UPDATE SET
			status = 'CLAIMED',
			worker_id = EXCLUDED.worker_id,
			lease_until = EXCLUDED.lease_until,
			attempts = delivery_attempts.attempts + 1,
			last_error = '',
			updated_at = EXCLUDED.updated_at
		WHERE delivery_attempts.status = 'FAILED'`,
		key.String(), key.RuleID, key.SourceChatID, key.SourceMessageID,
		key.TargetChatID, key.JobKind, workerID, leaseUntil, now, now); err != nil {
		return "", nil, fmt.Errorf("fence upsert: %w", err)
	}
	row := tx.QueryRowContext(ctx, `
		SELECT status, worker_id, lease_until, target_message_id, last_error, attempts
		FROM delivery_attempts WHERE id=$1`, key.String())
	var (
		a        job.DeliveryAttempt
		targetID sql.NullInt64
	)
	if err := row.Scan(&a.Status, &a.WorkerID, &a.LeaseUntil, &targetID, &a.LastError, &a.Attempts); err != nil {
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
	if targetID.Valid {
		a.TargetMessageID = targetID.Int64
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
// the job-row commit on purpose: a crash between Telegram's response and the
// batch commit must leave the fence saying the send landed, and a terminal
// SENT row is never overwritten by a later outcome.
func (s *pgStore) FenceRecordSent(ctx context.Context, key job.AttemptKey, workerID string, targetMessageID int64) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE delivery_attempts
		SET status='SENT', worker_id=$1, target_message_id=$2, lease_until=0, last_error='', updated_at=$3
		WHERE id=$4 AND status <> 'SENT'`, workerID, targetMessageID, time.Now().Unix(), key.String())
	return err
}

// FenceRecordOutcome stamps a FAILED outcome, releasing the fence for a
// retry. Only call this when the Telegram failure is *unambiguous* — a
// timeout's outcome is UNKNOWN, never FAILED.
func (s *pgStore) FenceRecordOutcome(ctx context.Context, key job.AttemptKey, workerID, status, detail string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE delivery_attempts
		SET status=$1, worker_id=$2, last_error=$3, lease_until=0, updated_at=$4
		WHERE id=$5 AND status NOT IN ('SENT')`, status, workerID, truncateDetail(detail), time.Now().Unix(), key.String())
	return err
}

// FenceExpireStale moves a CLAIMED row whose lease passed to UNKNOWN —
// never back to dispatch-by-default: the send may have landed and
// re-dispatching is exactly the duplicate the fence exists to prevent.
// The reason is preserved with a COALESCE so an operator sees why.
func (s *pgStore) FenceExpireStale(ctx context.Context, now float64) (int, error) {
	res, err := s.db.ExecContext(ctx, `
		UPDATE delivery_attempts
		SET status='UNKNOWN',
		    last_error = COALESCE(NULLIF(last_error, ''), 'claim expired before outcome was recorded'),
		    lease_until=0, updated_at=$1
		WHERE status IN ('CLAIMED','SENDING') AND lease_until > 0 AND lease_until < $2`,
		int64(now), now)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

// FenceResolveUnknown reopens an UNKNOWN fence after the caller has PROVED
// (a Telegram-side read or a bounded recovery decision) that no earlier copy
// is in the target. The reason is recorded — prefixed REOPENED, exactly as
// the Python implementation writes it — so an operator can audit why a
// possibly-already-sent delivery was re-dispatched.
func (s *pgStore) FenceResolveUnknown(ctx context.Context, key job.AttemptKey, workerID, reason string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE delivery_attempts
		SET status='FAILED', lease_until=0,
		    last_error = COALESCE($1, last_error), updated_at=$2
		WHERE id=$3 AND status='UNKNOWN'`,
		truncateDetail("REOPENED: "+reason), time.Now().Unix(), key.String())
	return err
}

// truncateDetail clips an error string to the column's practical length,
// mirroring the Python implementation's own [:4096] bound.
func truncateDetail(detail string) string {
	if len(detail) > 4096 {
		return detail[:4096]
	}
	return detail
}

// Compile-time guarantee: the PostgreSQL implementation satisfies the same
// contract as the SQLite one, so the engine cannot accidentally grow a
// backend-specific code path.
var (
	_ Store = (*pgStore)(nil)
	_       = stdlib.GetDefaultDriver
)
