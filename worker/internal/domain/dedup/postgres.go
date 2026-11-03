package dedup

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	// pgx's database/sql compatibility layer: the worker uses the standard
	// library abstraction everywhere (one pool, one set of semantics, and no
	// driver-specific API leaking into the store), with pgx underneath because
	// it is pure Go and actively maintained.
	"github.com/jackc/pgx/v5/stdlib"
)

// pgStore is the PostgreSQL implementation of Store over the Core's existing
// processed_messages and content_dedup tables.
//
// The Python Core owns the schema
// (core/infrastructure/persistence/postgres_repositories.py creates both
// tables); this package never creates or migrates them.
//
// Note on processed_messages: the Core's PostgreSQL schema bootstrap does
// not create that table (it is a SQLite-side table created in database.py).
// WasProcessed/MarkProcessed therefore guard the table's existence rather
// than failing startup: on a PostgreSQL deployment without the table, the
// event-level replay marker is unavailable, and every query here returns a
// harmless "does not exist" that the delivery engine treats as "not
// processed" — the Python pipeline's own behavior when the repository is
// not configured (self._processed is None ⇒ the gate is skipped and the
// message is forwarded).
type pgStore struct {
	db *sql.DB
}

// NewPostgres opens the dedup store against the primary PostgreSQL database.
//
// A bounded pool, one connection per in-flight claim plus headroom, matching
// the queue store's sizing rather than opening an unbounded second pool.
func NewPostgres(ctx context.Context, dsn string) (Store, error) {
	conn, err := sql.Open("pgx", dsn)
	if err != nil {
		return nil, fmt.Errorf("open dedup db: %w", err)
	}
	conn.SetMaxOpenConns(8)
	conn.SetMaxIdleConns(4)
	conn.SetConnMaxLifetime(30 * time.Minute)
	conn.SetConnMaxIdleTime(5 * time.Minute)
	if err := conn.PingContext(ctx); err != nil {
		conn.Close()
		return nil, fmt.Errorf("ping dedup db: %w", err)
	}
	return &pgStore{db: conn}, nil
}

func (s *pgStore) Close() error { return s.db.Close() }

// Compile-time guarantees: both implementations satisfy the same Store
// contract, so the delivery engine cannot accidentally grow a
// backend-specific code path; and the pgx driver stays registered without
// the import being flagged unused (sql.Open resolves it by name).
var (
	_ Store = (*pgStore)(nil)
	_       = stdlib.GetDefaultDriver
)

// missingTable reports whether a PostgreSQL error is a "relation does not
// exist" (SQLSTATE 42P01). See the pgStore doc comment for why this is the
// expected condition on a deployment whose schema the Core built without
// processed_messages.
func missingTable(err error) bool {
	// The pgx stdlib error exposes its SQLSTATE through a Code() method;
	// the interface assertion keeps this a tolerance rather than a hard
	// driver dependency, so a driver swap does not silently turn a missing
	// table into a startup failure.
	type codeBearer interface{ Code() string }
	if cb, ok := err.(codeBearer); ok && cb.Code() == "42P01" {
		return true
	}
	// Fall back to the message form (the database/sql wrapping surfaces
	// the driver's text, which carries "does not exist" on 42P01).
	return err != nil && contains(err.Error(), "does not exist")
}

func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}

// WasProcessed is PostgresProcessedMessageRepository.was_processed, guarded
// for the table's absence (see the pgStore doc comment).
func (s *pgStore) WasProcessed(ctx context.Context, ruleID, chatID string, messageID int64) (bool, error) {
	row := s.db.QueryRowContext(ctx,
		"SELECT 1 FROM processed_messages WHERE rule_id = $1 AND chat_id = $2 AND message_id = $3",
		ruleID, chatID, messageID)
	var one int
	err := row.Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		if missingTable(err) {
			return false, nil
		}
		return false, fmt.Errorf("was_processed %s:%s:%d: %w", ruleID, chatID, messageID, err)
	}
	return true, nil
}

// MarkProcessed is the PostgreSQL claim marker: INSERT ... ON CONFLICT DO
// NOTHING over the unique (rule_id, chat_id, message_id) constraint, so a
// replay of an already-processed event is a no-op.
func (s *pgStore) MarkProcessed(ctx context.Context, ruleID, chatID string, messageID int64, mediaGroupID string) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO processed_messages (rule_id, chat_id, message_id, media_group_id, ts)
		 VALUES ($1, $2, $3, $4, $5)
		 ON CONFLICT (rule_id, chat_id, message_id) DO NOTHING`,
		ruleID, chatID, messageID, mediaGroupID, time.Now().Unix())
	if err != nil {
		if missingTable(err) {
			return nil
		}
		return fmt.Errorf("mark_processed %s:%s:%d: %w", ruleID, chatID, messageID, err)
	}
	return nil
}

// ClaimContentHash is PostgresMessageMapRepository.claim_content_hash: the
// ON CONFLICT DO NOTHING ... RETURNING form lets the unique index pick the
// winner — a returned row means THIS caller owns the fingerprint, no row
// means someone else already did.
func (s *pgStore) ClaimContentHash(ctx context.Context, ruleID, contentHash string) (bool, error) {
	row := s.db.QueryRowContext(ctx,
		`INSERT INTO content_dedup (rule_id, content_hash, created_at)
		 VALUES ($1, $2, $3)
		 ON CONFLICT (rule_id, content_hash) DO NOTHING
		 RETURNING 1`,
		ruleID, contentHash, time.Now().Unix())
	var one int
	err := row.Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("claim_content_hash %s: %w", ruleID, err)
	}
	return true, nil
}

// WasContentForwarded is the non-claiming read.
func (s *pgStore) WasContentForwarded(ctx context.Context, ruleID, contentHash string) (bool, error) {
	row := s.db.QueryRowContext(ctx,
		"SELECT 1 FROM content_dedup WHERE rule_id = $1 AND content_hash = $2",
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

// ReleaseContentHash gives a claim back, mirroring the Python release.
func (s *pgStore) ReleaseContentHash(ctx context.Context, ruleID, contentHash string) error {
	if _, err := s.db.ExecContext(ctx,
		"DELETE FROM content_dedup WHERE rule_id = $1 AND content_hash = $2",
		ruleID, contentHash); err != nil {
		return fmt.Errorf("release_content_hash %s: %w", ruleID, err)
	}
	return nil
}
