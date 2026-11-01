package rulestore

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib" // registers the "pgx" driver for database/sql
	_ "github.com/mattn/go-sqlite3"    // registers the "sqlite3" driver

	"atf/worker/internal/domain/rule"
)

// openPostgres opens the rule/config database with the same pool bounds the
// delivery store uses: bounded, because an unbounded pool is the one part of
// the stack the worker's own concurrency does not already bound.
func openPostgres(ctx context.Context, dsn string) (*sql.DB, error) {
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(30 * time.Minute)
	db.SetConnMaxIdleTime(5 * time.Minute)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

// openSQLite opens the shared atf.db read side. One connection is enough —
// the rule store only reads — and SQLite serializes writes anyway, so more
// connections would add contention without adding throughput.
func openSQLite(ctx context.Context, dsn string) (*sql.DB, error) {
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

// filterProjectionSQLite is the filter_rules read for the SQLite reader.
//
// The projection deliberately EXCLUDES the legacy optional columns
// (regex_block, regex_require, is_enabled): a row written before those
// columns existed makes a plain SELECT that names them fail outright. Reading
// them through the NULL-tolerant optional* helpers instead is exactly the
// Python reader's "if col in keys" compatibility rule — a pre-v30 row keeps
// working, and its filter stays enabled.
const filterProjectionSQLite = `
	SELECT id, whitelist_keywords, blacklist_keywords,
	       regex_patterns, allowed_media_types, blocked_media_types,
	       drop_service_messages, min_message_length, max_message_length
	FROM filter_rules
	WHERE id = ?
`

// rulesQuery adapts a shared projection to the backend's placeholder
// convention — PostgreSQL's $1 versus SQLite's ? — so one verified statement
// serves both engines rather than two near-identical copies that can drift.
//
// The caller's argument is threaded through unchanged: the projection carries
// exactly one bound parameter (the rule id) whichever placeholder spelling the
// backend uses, and dropping it would bind NULL and silently find nothing.
func (s *Store) rulesQuery(text string, arg any) (string, any) {
	if s.rulesBackend == "postgres" {
		return text, arg
	}
	return strings.ReplaceAll(text, "$1", "?"), arg
}

// SchemaLabel describes the store's configuration for a startup log line,
// without any credential: a DSN's raw string never reaches the log.
func (s *Store) SchemaLabel() string {
	if s == nil {
		return "rulestore(unconfigured)"
	}
	if s.filtersShared {
		return fmt.Sprintf("rulestore(forward+filter=%s)", s.rulesBackend)
	}
	return fmt.Sprintf("rulestore(forward=%s, filter=sqlite)", s.rulesBackend)
}

// Compile-time guarantee that the composite store satisfies the engine's
// rule contract, so a backend-specific signature cannot creep in unnoticed.
var _ rule.Store = (*Store)(nil)
