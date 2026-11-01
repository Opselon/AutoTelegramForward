package queue

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
)

// Test fixtures shared by the queue tests and the engine tests. Kept in a
// normal (non _test.go) file because the delivery engine's tests live in a
// different package and cannot see this package's test-only identifiers; the
// exported wrappers below are the whole public surface.

const schema = `
CREATE TABLE IF NOT EXISTS delivery_jobs (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    target_chat_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    worker_id TEXT NOT NULL DEFAULT '',
    error_detail TEXT,
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    intermediate_chat_id TEXT,
    intermediate_message_id INTEGER,
    delivery_stage TEXT NOT NULL DEFAULT 'DIRECT',
    job_kind TEXT NOT NULL DEFAULT 'forward'
);
CREATE INDEX IF NOT EXISTS idx_delivery_jobs_claim
    ON delivery_jobs(status, next_retry_at, lease_until, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_jobs_rule ON delivery_jobs(rule_id);
CREATE TABLE IF NOT EXISTS delivery_attempts (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    target_chat_id TEXT NOT NULL,
    job_kind TEXT NOT NULL DEFAULT 'forward',
    status TEXT NOT NULL DEFAULT 'CLAIMED',
    worker_id TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    target_message_id INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    resolution_reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_delivery_attempts_fence
    ON delivery_attempts(status, lease_until);
CREATE TABLE IF NOT EXISTS message_map (
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    target_chat_id TEXT NOT NULL,
    target_message_id INTEGER,
    media_group_id TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (rule_id, source_chat_id, source_message_id, target_chat_id)
);
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    target_chat_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    event_code TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    dead_lettered_at INTEGER NOT NULL DEFAULT 0
);
`

// newTestStore opens a fresh temp SQLite with the Core's queue schema.
func newTestStore(t *testing.T) Store {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "atf.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open test db: %v", err)
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		t.Fatalf("apply schema: %v", err)
	}
	db.Close()
	store, err := NewSQLite(context.Background(), path+"?_busy_timeout=10000&_journal_mode=WAL")
	if err != nil {
		t.Fatalf("new sqlite store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// insertJob writes a delivery_jobs row for the test.

// insertJob writes a delivery_jobs row for the test.
func insertJob(t *testing.T, s Store, j *job.DeliveryJob) {
	t.Helper()
	db := storeDB(t, s)
	// The payload is persisted verbatim (the real enqueue path stores the
	// whole decision-shape blob, and the engine's resume paths read
	// structured fields like album_sent back out of it).
	payload := "{}"
	if raw := j.Payload.Raw(); len(raw) > 0 {
		if out, err := json.Marshal(raw); err == nil {
			payload = string(out)
		}
	}
	kind := j.JobKind
	if kind == "" {
		kind = "forward"
	}
	_, err := db.Exec(`INSERT OR REPLACE INTO delivery_jobs
		(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json,
		 status, attempts, max_attempts, next_retry_at, lease_until, worker_id,
		 error_detail, created_at, updated_at, intermediate_chat_id,
		 intermediate_message_id, delivery_stage, job_kind)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		j.ID, j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID, payload,
		string(j.Status), j.Attempts, j.MaxAttempts, j.NextRetryAt, j.LeaseUntil,
		j.WorkerID, j.ErrorDetail, j.CreatedAt, j.UpdatedAt, j.IntermediateChatID,
		j.IntermediateMsgID, j.DeliveryStage, kind)
	if err != nil {
		t.Fatalf("insert job %s: %v", j.ID, err)
	}
}


// storeDB digs the *sql.DB out of the store for direct row assertions.
func storeDB(t *testing.T, s Store) *sql.DB {
	t.Helper()
	if st, ok := s.(*sqliteStore); ok {
		return st.db
	}
	t.Fatalf("store is not a sqliteStore")
	return nil
}

var _ = fmt.Sprintf
var _ = context.Background
var _ = time.Second
var _ = filepath.Join
var _ = sql.Open
