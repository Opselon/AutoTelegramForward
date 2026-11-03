package counters

import (
	"context"
	"database/sql"
	"fmt"
)

// postgresStore is the PostgreSQL implementation of Store over the
// rule_counters table initialize_postgres_schema creates.
//
// PostgreSQL has no equivalent of SQLite's implicit single-writer lock, so
// the atomicity story is the ON CONFLICT … DO UPDATE itself: the increment
// and the reset-on-interval-change are one expression over one row, and
// PostgreSQL's row-level lock on the conflicting tuple serializes
// concurrent advancers. A read-then-write in Go would see the same
// pre-increment count twice; the UPSERT cannot. The read-back then runs on
// the same connection inside the same transaction, so it observes the row
// this caller just wrote — never another advancer's.
type postgresStore struct {
	db *sql.DB
}

func (s *postgresStore) Close() error { return s.db.Close() }

func (s *postgresStore) schemaCheck(ctx context.Context) error {
	var exists bool
	err := s.db.QueryRowContext(ctx,
		`SELECT EXISTS (SELECT 1 FROM information_schema.tables
		   WHERE table_schema = 'public' AND table_name = 'rule_counters')`).
		Scan(&exists)
	if err != nil {
		return fmt.Errorf("schema check rule_counters: %w", err)
	}
	if !exists {
		return fmt.Errorf("rule_counters is missing: the Python Core owns the schema bootstrap (initialize_postgres_schema)")
	}
	return nil
}

// advancePostgresStmt is the PostgreSQL spelling of Python's _ADVANCE.
// PostgreSQL's excluded pseudo-table and ON CONFLICT DO UPDATE are the
// direct equivalents of SQLite's UPSERT; the CASE reads the pre-conflict
// row so an interval change restarts the cycle at 1.
const advancePostgresStmt = `
INSERT INTO rule_counters (rule_id, owner_user_id, every_n, accepted, updated_at)
VALUES ($1, $2, $3, 1, $4)
ON CONFLICT (rule_id) DO UPDATE SET
  owner_user_id = excluded.owner_user_id,
  accepted = CASE WHEN rule_counters.every_n = excluded.every_n
                  THEN rule_counters.accepted + 1 ELSE 1 END,
  every_n = excluded.every_n,
  updated_at = excluded.updated_at`

// Advance counts one accepted message and returns the new sequence number.
//
// The UPSERT and the read-back run in one transaction. On conflict the
// row-level lock held by the ON CONFLICT DO UPDATE serializes concurrent
// advancers of the same rule_id, so each caller's read-back observes its
// own increment — two workers never both own slot N.
func (s *postgresStore) Advance(ctx context.Context, ruleID string, everyN int, ownerUserID int64) (int, error) {
	if ruleID == "" {
		return 0, ErrEmptyRuleID
	}
	everyN = normalize(everyN)
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("counters advance begin: %w", err)
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, advancePostgresStmt,
		ruleID, ownerUserID, everyN, nowTS()); err != nil {
		return 0, fmt.Errorf("counters advance: %w", err)
	}
	var accepted int
	if err := tx.QueryRowContext(ctx,
		`SELECT accepted FROM rule_counters WHERE rule_id = $1`, ruleID).Scan(&accepted); err != nil {
		if err == sql.ErrNoRows {
			// Symmetric with the SQLite path: an absent row after a
			// successful UPSERT is "no slot", never an error that could
			// silence a task.
			return 0, nil
		}
		return 0, fmt.Errorf("counters advance read: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("counters advance commit: %w", err)
	}
	return accepted, nil
}

func (s *postgresStore) Peek(ctx context.Context, ruleID string) (*Row, error) {
	row := s.db.QueryRowContext(ctx, `
	SELECT rule_id, owner_user_id, every_n, accepted, updated_at
	FROM rule_counters WHERE rule_id = $1`, ruleID)
	var r Row
	if err := row.Scan(&r.RuleID, &r.OwnerUserID, &r.EveryN, &r.Accepted, &r.UpdatedAt); err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("counters peek: %w", err)
	}
	return &r, nil
}

func (s *postgresStore) Reset(ctx context.Context, ruleID string) (bool, error) {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM rule_counters WHERE rule_id = $1`, ruleID)
	if err != nil {
		return false, fmt.Errorf("counters reset: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return false, fmt.Errorf("counters reset rows: %w", err)
	}
	return n > 0, nil
}

var (
	_ Store = (*sqliteStore)(nil)
	_ Store = (*postgresStore)(nil)
)
