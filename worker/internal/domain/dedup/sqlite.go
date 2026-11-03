package dedup

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	_ "github.com/mattn/go-sqlite3" // registers the sqlite3 driver
)

// sqliteStore is the SQLite implementation of Store over the Core's existing
// processed_messages and content_dedup tables.
//
// The Python Core owns the schema (core/infrastructure/persistence/
// database.py creates both tables); this package never creates or migrates
// them, it only reads and writes what the Core already laid down.
type sqliteStore struct {
	db *sql.DB
}

// NewSQLite opens the dedup store against the same atf.db the queue uses.
//
// One physical connection, exactly like the queue store: SQLite serializes
// writes at the kernel level anyway, and a second connection would only add
// a new way to deadlock with the writer the Core already holds.
func NewSQLite(ctx context.Context, dsn string) (Store, error) {
	conn, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("open dedup db: %w", err)
	}
	conn.SetMaxOpenConns(1)
	if err := conn.PingContext(ctx); err != nil {
		conn.Close()
		return nil, fmt.Errorf("ping dedup db: %w", err)
	}
	return &sqliteStore{db: conn}, nil
}

func (s *sqliteStore) Close() error { return s.db.Close() }

// Compile-time guarantee: the SQLite implementation satisfies the same Store
// contract as the PostgreSQL one, so the delivery engine cannot accidentally
// grow a backend-specific code path.
var _ Store = (*sqliteStore)(nil)

// WasProcessed is SqliteProcessedMessageRepository.was_processed verbatim:
// the existence of one row decides, no target dimension, no timestamps.
func (s *sqliteStore) WasProcessed(ctx context.Context, ruleID, chatID string, messageID int64) (bool, error) {
	row := s.db.QueryRowContext(ctx,
		"SELECT 1 FROM processed_messages WHERE rule_id=? AND chat_id=? AND message_id=?",
		ruleID, chatID, messageID)
	var one int
	err := row.Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("was_processed %s:%s:%d: %w", ruleID, chatID, messageID, err)
	}
	return true, nil
}

// MarkProcessed is SqliteProcessedMessageRepository.mark_processed verbatim:
// INSERT OR IGNORE over the UNIQUE(rule_id, chat_id, message_id) index, so a
// replay of an already-processed event is a no-op rather than a violation.
func (s *sqliteStore) MarkProcessed(ctx context.Context, ruleID, chatID string, messageID int64, mediaGroupID string) error {
	if _, err := s.db.ExecContext(ctx,
		"INSERT OR IGNORE INTO processed_messages (rule_id, chat_id, message_id,"+
			" media_group_id, ts) VALUES (?, ?, ?, ?, ?)",
		ruleID, chatID, messageID, mediaGroupID, time.Now().Unix()); err != nil {
		return fmt.Errorf("mark_processed %s:%s:%d: %w", ruleID, chatID, messageID, err)
	}
	return nil
}

// ClaimContentHash is SqliteMessageMapRepository.claim_content_hash:
// INSERT OR IGNORE plus rowcount lets the UNIQUE(rule_id, content_hash)
// index pick the winner, so two workers racing on identical content cannot
// both forward it. One statement, no read-then-write window.
func (s *sqliteStore) ClaimContentHash(ctx context.Context, ruleID, contentHash string) (bool, error) {
	res, err := s.db.ExecContext(ctx,
		"INSERT OR IGNORE INTO content_dedup (rule_id, content_hash, created_at)"+
			" VALUES (?, ?, ?)",
		ruleID, contentHash, time.Now().Unix())
	if err != nil {
		return false, fmt.Errorf("claim_content_hash %s: %w", ruleID, err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return false, fmt.Errorf("claim_content_hash %s rows: %w", ruleID, err)
	}
	return n == 1, nil
}

// WasContentForwarded is the non-claiming read the Python pipeline falls
// back to for stores predating the atomic claim.
func (s *sqliteStore) WasContentForwarded(ctx context.Context, ruleID, contentHash string) (bool, error) {
	row := s.db.QueryRowContext(ctx,
		"SELECT 1 FROM content_dedup WHERE rule_id=? AND content_hash=?",
		ruleID, contentHash)
	var one int
	err := row.Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("was_content_forwarded %s: %w", ruleID, err)
	}
	return true, nil
}

// ReleaseContentHash is SqliteMessageMapRepository.release_content_hash:
// give the claim back so a message that never reached a target does not
// poison the next identical post.
func (s *sqliteStore) ReleaseContentHash(ctx context.Context, ruleID, contentHash string) error {
	if _, err := s.db.ExecContext(ctx,
		"DELETE FROM content_dedup WHERE rule_id=? AND content_hash=?",
		ruleID, contentHash); err != nil {
		return fmt.Errorf("release_content_hash %s: %w", ruleID, err)
	}
	return nil
}
