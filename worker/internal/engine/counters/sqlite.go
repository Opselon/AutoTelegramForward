package counters

import (
	"context"
	"database/sql"
	"fmt"
)

// sqliteStore is the SQLite implementation of Store over the rule_counters
// table the Python Core's migration v27 creates.
type sqliteStore struct {
	db *sql.DB
}

func (s *sqliteStore) Close() error { return s.db.Close() }

func (s *sqliteStore) schemaCheck(ctx context.Context) error {
	var name string
	err := s.db.QueryRowContext(ctx,
		`SELECT name FROM sqlite_master WHERE type='table' AND name='rule_counters'`).
		Scan(&name)
	if err != nil {
		if err == sql.ErrNoRows {
			return fmt.Errorf("rule_counters is missing: the Python Core owns the schema bootstrap (migration v27)")
		}
		return fmt.Errorf("rule_counters check: %w", err)
	}
	return nil
}

// advanceSQLiteStmt is Python's _ADVANCE verbatim modulo placeholder style.
// The CASE reads the *previous* row (all right-hand sides of an UPSERT see
// the old values), so a changed interval restarts the count at 1 and an
// unchanged one just steps forward.
const advanceSQLiteStmt = `
INSERT INTO rule_counters (rule_id, owner_user_id, every_n, accepted, updated_at)
VALUES (?, ?, ?, 1, ?)
ON CONFLICT(rule_id) DO UPDATE SET
  owner_user_id = excluded.owner_user_id,
  accepted = CASE WHEN rule_counters.every_n = excluded.every_n
                  THEN rule_counters.accepted + 1 ELSE 1 END,
  every_n = excluded.every_n,
  updated_at = excluded.updated_at`

// Advance counts one accepted message and returns the new sequence number.
//
// The UPSERT and the read of the row it just wrote run inside one
// transaction under SQLite's single-writer lock, which is what makes the
// sequence unique under concurrency: two workers handling two messages of
// the same rule cannot both see accepted = 4 and both decide they own slot
// 5. Doing the arithmetic in SQL rather than in Go is the same point — the
// increment and the reset-on-interval-change are one expression, so they
// cannot be split by another writer.
func (s *sqliteStore) Advance(ctx context.Context, ruleID string, everyN int, ownerUserID int64) (int, error) {
	if ruleID == "" {
		return 0, ErrEmptyRuleID
	}
	args := []any{ruleID, ownerUserID, normalize(everyN), nowTS()}
	return advanceSQLite(ctx, s.db, advanceSQLiteStmt, args...)
}

func (s *sqliteStore) Peek(ctx context.Context, ruleID string) (*Row, error) {
	row := s.db.QueryRowContext(ctx, `
	SELECT rule_id, owner_user_id, every_n, accepted, updated_at
	FROM rule_counters WHERE rule_id = ?`, ruleID)
	var r Row
	if err := row.Scan(&r.RuleID, &r.OwnerUserID, &r.EveryN, &r.Accepted, &r.UpdatedAt); err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("counters peek: %w", err)
	}
	return &r, nil
}

func (s *sqliteStore) Reset(ctx context.Context, ruleID string) (bool, error) {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM rule_counters WHERE rule_id = ?`, ruleID)
	if err != nil {
		return false, fmt.Errorf("counters reset: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return false, fmt.Errorf("counters reset rows: %w", err)
	}
	return n > 0, nil
}
