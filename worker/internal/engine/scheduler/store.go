// Package scheduler is the Go Worker's durable scheduled-action engine.
//
// It replaces the Python implementation's in-memory delayed deletion
// (an asyncio.sleep task): that schedule lived only in process memory, so a
// Core restart while the task slept lost the deletion forever. This engine
// persists the schedule in the durable store the Worker already owns, and a
// bounded loop — not one goroutine-per-action — claims and executes due
// work.
//
// Properties the design preserves deliberately:
//
//   - Restart-safe. Pending actions survive a Worker restart, a Runtime
//     restart and a replica handover, because the schedule is a table row.
//   - One bounded loop, one bounded worker pool. No goroutine per scheduled
//     action, no sleeping task that holds a connection, no unbounded
//     channel: the loop wakes on a fixed interval, claims what is due, and
//     hands a bounded batch to the pool.
//   - Single-execution per action. The claim is an atomic state
//     transition, so two Worker instances can never execute the same due
//     action concurrently (PostgreSQL: FOR UPDATE SKIP LOCKED; SQLite: the
//     single-writer lock the store already serializes on).
//   - Idempotent execution. Deletion is an idempotent cleanup operation:
//     a message Telegram reports as already absent is a successful action,
//     not a failure.
//   - Bounded retries with Telegram's own backoff. FloodWait's
//     retry_after is authoritative; a transient failure backs off with a
//     capped exponential; a permanent permission failure spends its budget
//     once and surfaces an operational event, it does not retry forever.
//   - No payload storage. The row carries the identifiers an executor
//     needs — never message text, media metadata or credentials.
package scheduler

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"
)

// ActionType is the kind of scheduled side effect. One table serves every
// kind; the scheduler loop is a single engine, not one timer forest per
// action.
type ActionType string

const (
	// ActionAutoDelete deletes a delivered message after its TTL.
	ActionAutoDelete ActionType = "auto_delete"
)

// State is the persisted lifecycle of one scheduled action.
//
// Only three values are written: PENDING (the durable schedule), CLAIMED
// (an executor owns it), and FAILED (the bounded budget is spent — the row
// stays for a human, it is not retried). A completed action is DELETED, not
// archived: the retention policy is minimal by design and a cleanup history
// is not a feature the product has ever needed.
type State string

const (
	StatePending State = "PENDING"
	StateClaimed State = "CLAIMED"
	StateFailed  State = "FAILED"
)

// Transport is the Telegram identity that must execute the action.
type Transport string

const (
	// TransportUser executes through the owner's MTProto session.
	TransportUser Transport = "user"
	// TransportBot executes through the stored bot token.
	TransportBot Transport = "bot"
)

// Action is one persisted scheduled action.
type Action struct {
	ID            string
	JobID         string
	Type          ActionType
	Transport     Transport
	TransportRef  string
	OwnerUserID   int64
	ChatID        string
	MessageID     int64
	DueAt         float64
	NextAttemptAt float64
	Attempts      int
	MaxAttempts   int
	LastError     string
	State         State
	CreatedAt     int64
	UpdatedAt     int64
}

// ErrUnknownDriver is returned when no store implementation matches the
// configured backend.
var ErrUnknownDriver = errors.New("scheduler: unknown store driver")

// Store is the durable persistence contract for scheduled actions.
//
// Both backends implement it so the engine never grows a backend-specific
// code path; the claim is the load-bearing method and its atomicity
// guarantee is what makes multiple Worker instances safe.
type Store interface {
	// Schedule persists a new action, idempotently: the deterministic key
	// (job + type + chat + message) means the same confirmed delivery can
	// never register the same deletion twice.
	Schedule(ctx context.Context, a Action) error
	// ClaimDue atomically transitions N due actions to CLAIMED and returns
	// them. Two callers can never claim the same action.
	ClaimDue(ctx context.Context, workerID string, limit int, now float64) ([]Action, error)
	// Complete removes a successfully executed action (the minimal
	// retention policy: done means gone).
	Complete(ctx context.Context, id string) error
	// Fail records a terminal failure (the retry budget is spent) and
	// leaves the row for a human to see.
	Fail(ctx context.Context, id, detail string) error
	// Retry re-queues the action with the next attempt time and one more
	// spent attempt.
	Retry(ctx context.Context, id string, nextAttemptAt float64, detail string) error
	// PendingCount feeds the scheduler's depth metric.
	PendingCount(ctx context.Context) (int, error)
	// Close releases the underlying connection.
	Close() error
}

// Compile-time guarantee that both implementations satisfy the contract.
var (
	_ Store = (*sqliteStore)(nil)
	_ Store = (*postgresStore)(nil)
)

// --- SQLite --------------------------------------------------------------- //

type sqliteStore struct {
	db *sql.DB
}

// NewSQLite opens the scheduled-action store over the same atf.db the
// delivery queue uses. The table is owned by the Python Core's bootstrap
// (migration 33): the Worker only verifies it exists and uses it, it never
// creates production schema.
func NewSQLite(db *sql.DB) (Store, error) {
	s := &sqliteStore{db: db}
	if err := s.schemaCheck(context.Background()); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *sqliteStore) schemaCheck(ctx context.Context) error {
	var name string
	err := s.db.QueryRowContext(ctx,
		`SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_actions'`).
		Scan(&name)
	if err != nil {
		if err == sql.ErrNoRows {
			return fmt.Errorf("scheduled_actions is missing: the Python Core owns the schema bootstrap (migration 33)")
		}
		return fmt.Errorf("scheduled_actions check: %w", err)
	}
	return nil
}

func (s *sqliteStore) Close() error { return s.db.Close() }

func (s *sqliteStore) Schedule(ctx context.Context, a Action) error {
	now := time.Now().Unix()
	created := a.CreatedAt
	if created == 0 {
		created = now
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT OR IGNORE INTO scheduled_actions
			(id, job_id, action_type, transport, transport_ref, owner_user_id,
			 chat_id, message_id, due_at, next_attempt_at, attempts, max_attempts,
			 last_error, state, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, '', 'PENDING', ?, ?)`,
		a.ID, a.JobID, string(a.Type), string(a.Transport), a.TransportRef,
		a.OwnerUserID, a.ChatID, a.MessageID, a.DueAt, a.NextAttemptAt,
		a.MaxAttempts, created, now)
	return err
}

func (s *sqliteStore) ClaimDue(ctx context.Context, workerID string, limit int, now float64) ([]Action, error) {
	if limit < 1 {
		limit = 1
	}
	// One atomic UPDATE ... RETURNING over a bounded sub-select. SQLite's
	// single-writer lock makes the read-then-update window impossible, so no
	// SKIP LOCKED is needed (and none exists). RETURNING keeps the claim and
	// its read in one statement: a second worker never sees another's claim.
	query := fmt.Sprintf(`
		UPDATE scheduled_actions
		SET state = 'CLAIMED', updated_at = ?
		WHERE id IN (
			SELECT id FROM scheduled_actions
			WHERE state = 'PENDING' AND next_attempt_at <= ?
			ORDER BY due_at ASC, id ASC
			LIMIT %d
		)
		RETURNING id, job_id, action_type, transport, transport_ref, owner_user_id,
		          chat_id, message_id, due_at, next_attempt_at, attempts,
		          max_attempts, last_error, state, created_at, updated_at`, limit)
	rows, err := s.db.QueryContext(ctx, query, time.Now().Unix(), now)
	if err != nil {
		return nil, fmt.Errorf("claim update: %w", err)
	}
	defer rows.Close()
	out := []Action{}
	for rows.Next() {
		a, err := scanAction(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

func (s *sqliteStore) Complete(ctx context.Context, id string) error {
	_, err := s.db.ExecContext(ctx, `DELETE FROM scheduled_actions WHERE id = ?`, id)
	return err
}

func (s *sqliteStore) Fail(ctx context.Context, id, detail string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE scheduled_actions SET state = 'FAILED', last_error = ?, updated_at = ?
		WHERE id = ?`, truncate(detail), time.Now().Unix(), id)
	return err
}

func (s *sqliteStore) Retry(ctx context.Context, id string, nextAttemptAt float64, detail string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE scheduled_actions
		SET state = 'PENDING', next_attempt_at = ?, attempts = attempts + 1,
		    last_error = ?, updated_at = ?
		WHERE id = ?`, nextAttemptAt, truncate(detail), time.Now().Unix(), id)
	return err
}

func (s *sqliteStore) PendingCount(ctx context.Context) (int, error) {
	var count int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM scheduled_actions WHERE state IN ('PENDING','CLAIMED')`).
		Scan(&count)
	return count, err
}

// --- PostgreSQL ----------------------------------------------------------- //

type postgresStore struct {
	db *sql.DB
}

// NewPostgres opens the scheduled-action store over the same PostgreSQL
// database the delivery queue uses.
func NewPostgres(db *sql.DB) (Store, error) {
	s := &postgresStore{db: db}
	if err := s.schemaCheck(context.Background()); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *postgresStore) schemaCheck(ctx context.Context) error {
	var exists bool
	err := s.db.QueryRowContext(ctx,
		`SELECT EXISTS (SELECT 1 FROM information_schema.tables
		   WHERE table_schema = 'public' AND table_name = 'scheduled_actions')`).
		Scan(&exists)
	if err != nil {
		return fmt.Errorf("schema check scheduled_actions: %w", err)
	}
	if !exists {
		return fmt.Errorf("scheduled_actions is missing: the Python Core owns the schema bootstrap (initialize_postgres_schema)")
	}
	return nil
}

func (s *postgresStore) Close() error { return s.db.Close() }

func (s *postgresStore) Schedule(ctx context.Context, a Action) error {
	now := time.Now().Unix()
	created := a.CreatedAt
	if created == 0 {
		created = now
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO scheduled_actions
			(id, job_id, action_type, transport, transport_ref, owner_user_id,
			 chat_id, message_id, due_at, next_attempt_at, attempts, max_attempts,
			 last_error, state, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, $11, '', 'PENDING', $12, $13)
		ON CONFLICT (job_id, action_type, chat_id, message_id) DO NOTHING`,
		a.ID, a.JobID, string(a.Type), string(a.Transport), a.TransportRef,
		a.OwnerUserID, a.ChatID, a.MessageID, a.DueAt, a.NextAttemptAt,
		a.MaxAttempts, created, now)
	return err
}

func (s *postgresStore) ClaimDue(ctx context.Context, workerID string, limit int, now float64) ([]Action, error) {
	if limit < 1 {
		limit = 1
	}
	// One atomic UPDATE ... RETURNING over a FOR UPDATE SKIP LOCKED
	// sub-select, the same shape the delivery queue's claim uses: a
	// concurrent claimer either sees a row as claimable or has it skipped —
	// there is no read-then-write window anywhere, and two Worker instances
	// can never execute the same due action.
	query := fmt.Sprintf(`
		UPDATE scheduled_actions
		SET state = 'CLAIMED', updated_at = $1
		WHERE id IN (
			SELECT id FROM scheduled_actions
			WHERE state = 'PENDING' AND next_attempt_at <= $2
			ORDER BY due_at ASC, id ASC
			LIMIT %d
			FOR UPDATE SKIP LOCKED
		)
		RETURNING id, job_id, action_type, transport, transport_ref, owner_user_id,
		          chat_id, message_id, due_at, next_attempt_at, attempts,
		          max_attempts, last_error, state, created_at, updated_at`, limit)
	rows, err := s.db.QueryContext(ctx, query, time.Now().Unix(), now)
	if err != nil {
		return nil, fmt.Errorf("claim update: %w", err)
	}
	defer rows.Close()
	out := []Action{}
	for rows.Next() {
		a, err := scanAction(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

func (s *postgresStore) Complete(ctx context.Context, id string) error {
	_, err := s.db.ExecContext(ctx, `DELETE FROM scheduled_actions WHERE id = $1`, id)
	return err
}

func (s *postgresStore) Fail(ctx context.Context, id, detail string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE scheduled_actions SET state = 'FAILED', last_error = $1, updated_at = $2
		WHERE id = $3`, truncate(detail), time.Now().Unix(), id)
	return err
}

func (s *postgresStore) Retry(ctx context.Context, id string, nextAttemptAt float64, detail string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE scheduled_actions
		SET state = 'PENDING', next_attempt_at = $1, attempts = attempts + 1,
		    last_error = $2, updated_at = $3
		WHERE id = $4`, nextAttemptAt, truncate(detail), time.Now().Unix(), id)
	return err
}

func (s *postgresStore) PendingCount(ctx context.Context) (int, error) {
	var count int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM scheduled_actions WHERE state IN ('PENDING','CLAIMED')`).
		Scan(&count)
	return count, err
}

// --- shared ---------------------------------------------------------------- //

func scanAction(rows *sql.Rows) (Action, error) {
	var a Action
	var (
		actionType   string
		transport    string
		state        string
		lastError    sql.NullString
		transportRef sql.NullString
	)
	if err := rows.Scan(
		&a.ID, &a.JobID, &actionType, &transport, &transportRef, &a.OwnerUserID,
		&a.ChatID, &a.MessageID, &a.DueAt, &a.NextAttemptAt, &a.Attempts,
		&a.MaxAttempts, &lastError, &state, &a.CreatedAt, &a.UpdatedAt,
	); err != nil {
		return Action{}, fmt.Errorf("scan scheduled action: %w", err)
	}
	a.Type = ActionType(actionType)
	a.Transport = Transport(transport)
	a.State = State(state)
	a.LastError = lastError.String
	a.TransportRef = transportRef.String
	return a, nil
}

// truncate clips an error string to the column's practical length, the same
// bound the delivery store applies.
func truncate(detail string) string {
	if len(detail) > 4096 {
		return detail[:4096]
	}
	return detail
}

// ID builds the deterministic row id for one scheduled action. It is derived
// purely from the delivery identity, so the same confirmed delivery resolves
// to the same row — the second Schedule is a no-op, never a duplicate.
func ID(jobID string, at ActionType, chatID string, messageID int64) string {
	return fmt.Sprintf("sa_%s_%s_%s_%d",
		cleanID(jobID), string(at), cleanID(chatID), messageID)
}

// cleanID keeps the composite id free of characters that would make a logged
// or stored id ambiguous; it is a formatting guard, never a hash (the row
// must stay reconstructible from the identity alone).
func cleanID(s string) string {
	out := strings.Builder{}
	for _, r := range strings.TrimSpace(s) {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z',
			r >= '0' && r <= '9', r == '-' || r == '_' || r == '.' || r == ':':
			out.WriteRune(r)
		default:
			out.WriteRune('_')
		}
	}
	return out.String()
}
