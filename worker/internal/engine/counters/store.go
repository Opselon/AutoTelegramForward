// Package counters is the Go Worker's durable "Forward Every N Messages"
// cadence store: the rule_counters table the Python Core owns, read and
// advanced atomically from both engines over the same rows.
//
// One row per task, holding how many filtered messages the task accepted.
// It is deliberately not derived from anything else: the dedupe store
// forgets messages on a retention sweep, the delivery jobs are per-target,
// and processed_messages is keyed by message rather than by count. Losing
// this row would restart the cadence at message 1 — forwarding a message
// the owner had configured away — so it is written once per accepted
// message and never trimmed.
//
// The single method is one atomic statement on purpose. A read-then-write
// pair lets two workers that process two messages at the same time both
// read the same count and both decide they own the slot, which delivers two
// messages where one was asked for. The store, not the caller, is the
// serialisation point — exactly the contract IRuleCounterRepository states.
//
// Both PostgreSQL and SQLite implement it so the engine never grows a
// backend-specific code path; the INSERT … ON CONFLICT(rule_id) DO UPDATE
// is Python's own statement, adapted only for placeholder spelling.
package counters

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// ErrUnknownDriver is returned when no store implementation matches the
// configured backend.
var ErrUnknownDriver = errors.New("counters: unknown store driver")

// ErrEmptyRuleID mirrors Python's ValueError("rule_id is required"): the
// row's primary key cannot be empty, and a caller that passes one has a bug
// the store must not paper over by writing a nonsense row.
var ErrEmptyRuleID = errors.New("counters: rule_id is required")

// Row is the stored cadence position without advancing it, for UI reads
// and tests (Python's peek()).
type Row struct {
	RuleID      string
	OwnerUserID int64
	EveryN      int
	Accepted    int
	UpdatedAt   int64
}

// Store is the persisted-position contract for the forward-every-N cadence.
//
// Advance is the load-bearing method: it counts one accepted message and
// returns the new 1-based sequence number, uniquely per task under
// concurrency. When everyN differs from the interval the row was last
// advanced with, the count restarts at 1 — an owner who changes the
// setting starts a fresh cycle, and the first message of the new cycle is
// forwarded.
type Store interface {
	// Advance counts one accepted message and returns the new sequence.
	// Concurrent callers get distinct numbers; the store is the only
	// serialisation point.
	Advance(ctx context.Context, ruleID string, everyN int, ownerUserID int64) (int, error)
	// Peek returns the stored row without advancing it (or nil).
	Peek(ctx context.Context, ruleID string) (*Row, error)
	// Reset drops the row so the next accepted message is the first of a
	// fresh cycle. Reports whether a row existed.
	Reset(ctx context.Context, ruleID string) (bool, error)
	// Close releases the underlying connection.
	Close() error
}

// Compile-time guarantee that both implementations satisfy the contract.
var (
	_ Store = (*sqliteStore)(nil)
	_ Store = (*postgresStore)(nil)
)

// nowTS is the updated_at the UPSERT carries. Python writes int(time.time());
// the same wall clock, so an operator looking at either engine's row sees
// the same column meaning the same thing.
func nowTS() int64 { return time.Now().Unix() }

// normalize applies Python's `every_n = max(1, int(every_n or 1))` guard
// from advance(): the value the caller passes may be NaN/0/negative and
// must never reach the row, because a 0 or negative stride would break the
// cadence arithmetic that reads it back.
func normalize(everyN int) int {
	if everyN < 1 {
		return 1
	}
	return everyN
}

// NewSQLite opens the cadence store over the same atf.db the Python Core
// owns. The table comes from migration v27 — the Worker verifies it exists
// and uses it, it never creates production schema (the Core is the schema
// owner on both backends).
func NewSQLite(db *sql.DB) (Store, error) {
	s := &sqliteStore{db: db}
	if err := s.schemaCheck(context.Background()); err != nil {
		return nil, err
	}
	return s, nil
}

// NewPostgres opens the cadence store over the same PostgreSQL database
// the durable queue uses. The table comes from initialize_postgres_schema;
// the Worker only verifies it.
func NewPostgres(db *sql.DB) (Store, error) {
	s := &postgresStore{db: db}
	if err := s.schemaCheck(context.Background()); err != nil {
		return nil, err
	}
	return s, nil
}

// advanceSQLite runs the atomic UPSERT plus its read inside one SQLite
// transaction, the exact unit() shape Python's SqliteRuleCounterRepository
// uses: SQLite's single-writer lock makes the read-then-write window
// impossible, so no SKIP LOCKED is needed (and none exists).
func advanceSQLite(ctx context.Context, db *sql.DB, q string, args ...any) (int, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("counters advance begin: %w", err)
	}
	// Commit explicitly and only report success once the row is durable:
	// the deferred Rollback is a no-op after a successful Commit and the
	// safety net for every early return.
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, q, args...); err != nil {
		return 0, fmt.Errorf("counters advance: %w", err)
	}
	var accepted int
	if err := tx.QueryRowContext(ctx,
		`SELECT accepted FROM rule_counters WHERE rule_id = ?`, args[0]).Scan(&accepted); err != nil {
		if err == sql.ErrNoRows {
			// The INSERT arm failed to produce a row and the UPSERT did not
			// raise: the only reachable cause is a schema the store did not
			// validate. Fail closed with 0, which OwnsSlot treats as
			// "no slot" — the same answer Python gives when the row read is
			// somehow absent.
			return 0, nil
		}
		return 0, fmt.Errorf("counters advance read: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("counters advance commit: %w", err)
	}
	return accepted, nil
}
