package queue

// postgres_test_helpers.go — the PostgreSQL test plumbing, in a NON-test
// file so suites OUTSIDE this package (internal/ops) can build a real
// PostgreSQL store without importing test-only identifiers (Go forbids
// importing a package's _test.go symbols from elsewhere).
//
// Every function here is ONLY referenced from test code; the file's
// existence in the build is what makes it linkable, and nothing in the
// production paths touches it.
//
// This file is the complete duplicate-free home of the scratch-database
// dance: create a throwaway database, apply the Core's queue schema, open
// the store, drop the database on cleanup.

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

// pgOpsSchema is the Core's queue schema for the PostgreSQL fixture,
// duplicated from the queue suite's pgSchemaFixture rather than shared
// because that const lives in a _test.go file. The two MUST agree; the
// ops tests' parity assertions are what catches a drift.
const pgOpsSchema = `
CREATE TABLE IF NOT EXISTS delivery_jobs (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    worker_id TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    intermediate_chat_id TEXT NOT NULL DEFAULT '',
    intermediate_message_id BIGINT,
    delivery_stage TEXT NOT NULL DEFAULT 'DIRECT',
    job_kind TEXT NOT NULL DEFAULT 'forward',
    CONSTRAINT delivery_jobs_ops_unique UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id, job_kind)
);
CREATE INDEX IF NOT EXISTS idx_delivery_jobs_ops_claim
    ON delivery_jobs (status, next_retry_at, lease_until, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_jobs_ops_rule ON delivery_jobs (rule_id);
CREATE TABLE IF NOT EXISTS message_map (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    target_message_id BIGINT,
    media_group_id TEXT NOT NULL DEFAULT '',
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    CONSTRAINT message_map_ops_unique UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id)
);
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    error_category TEXT NOT NULL DEFAULT '',
    dead_lettered_at BIGINT NOT NULL,
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dlq_ops_job ON dead_letter_queue (job_id);
CREATE TABLE IF NOT EXISTS delivery_attempts (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    job_kind TEXT NOT NULL DEFAULT 'forward',
    status TEXT NOT NULL DEFAULT 'CLAIMED',
    worker_id TEXT NOT NULL DEFAULT '',
    target_message_id BIGINT,
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    CONSTRAINT delivery_attempts_ops_unique
        UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id, job_kind)
);
CREATE INDEX IF NOT EXISTS idx_delivery_attempts_ops_fence
    ON delivery_attempts (status, lease_until);
`

// NewPostgresTestStore opens a queue store against a throwaway PostgreSQL
// database carrying the Core's queue schema. Skips when
// ATF_TEST_POSTGRES_DSN is unset (a local run without a DB is not a
// failure); the database is dropped on cleanup so a crashed test leaves
// nothing behind. For out-of-package suites (internal/ops) that need a
// REAL postgres — never a SQLite stand-in — for the same reason the queue
// suite needs one: FILTER (WHERE ...) syntax is engine-specific.
func NewPostgresTestStore(t *testing.T) Store {
	t.Helper()
	dsn := os.Getenv(pgTestEnvName)
	if dsn == "" {
		t.Skipf("%s not set; skipping real-PostgreSQL ops tests", pgTestEnvName)
	}
	dbName := pgScratchName(t)
	root, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatalf("open root pg: %v", err)
	}
	for _, stmt := range []string{
		fmt.Sprintf(`DROP DATABASE IF EXISTS %q WITH (FORCE)`, dbName),
		fmt.Sprintf(`CREATE DATABASE %q`, dbName),
	} {
		if _, err := root.ExecContext(context.Background(), stmt); err != nil {
			root.Close()
			t.Fatalf("prepare scratch db (%s): %v", dbName, err)
		}
	}
	root.Close()

	storeDSN := pgSwapDatabase(t, dsn, dbName)
	if err := pgApplyOpsSchema(context.Background(), storeDSN); err != nil {
		t.Fatalf("apply schema: %v", err)
	}
	store, err := NewPostgres(context.Background(), storeDSN)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() {
		_ = store.Close()
		drop, err := sql.Open("pgx", dsn)
		if err == nil {
			_, _ = drop.ExecContext(context.Background(),
				fmt.Sprintf(`DROP DATABASE IF EXISTS %q WITH (FORCE)`, dbName))
			drop.Close()
		}
	})
	return store
}

// pgTestEnvName mirrors the queue suite's gate so one env var covers both.
const pgTestEnvName = "ATF_TEST_POSTGRES_DSN"

// pgScratchName is a validated [a-z0-9_] scratch database name: safe to
// interpolate into the DDL above precisely because it can contain nothing
// else.
func pgScratchName(t *testing.T) string {
	t.Helper()
	name := fmt.Sprintf("atf_ops_%d", time.Now().UnixNano())
	for _, c := range name {
		if !(c >= 'a' && c <= 'z') && !(c >= '0' && c <= '9') && c != '_' {
			t.Fatalf("scratch db name %q contains non [a-z0-9_]", name)
		}
	}
	return name
}

// pgSwapDatabase rewrites the DSN's database through pgx's own parser, so
// a password or query string in the DSN survives the swap intact.
func pgSwapDatabase(t *testing.T, dsn, dbName string) string {
	t.Helper()
	out, err := pgSetDatabase(dsn, dbName)
	if err != nil {
		t.Fatalf("swap database in dsn: %v", err)
	}
	return out
}

// pgApplyOpsSchema applies the fixture schema to a scratch database.
func pgApplyOpsSchema(ctx context.Context, dsn string) error {
	conn, err := sql.Open("pgx", dsn)
	if err != nil {
		return err
	}
	defer conn.Close()
	_, err = conn.ExecContext(ctx, pgOpsSchema)
	return err
}

// pgxParse is the thin seam over pgx's config parser: one place where a
// DSN becomes a config and back, so the swap is a parser round-trip
// rather than string surgery.
func pgxParse(dsn string) (*pgconn.Config, error) {
	return pgconn.ParseConfig(dsn)
}

// pgSetDatabase returns dsn with the target database replaced. Through the
// parser: the DSN may carry a password, sslmode and query parameters, and
// replacing "/postgres" by hand would corrupt them.
func pgSetDatabase(dsn, dbName string) (string, error) {
	u, err := url.Parse(dsn)
	if err != nil {
		return "", err
	}
	u.Path = "/" + dbName
	return u.String(), nil
}
