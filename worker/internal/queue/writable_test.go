package queue

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// TestWritableProbeFailsClosedOnAReadOnlyStore is the deployment guarantee:
// a worker whose store cannot be written must refuse to start rather than
// report "ready" while every claim fails.
//
// The read-only condition is produced with SQLite's own `mode=ro` URI
// rather than with filesystem permission bits, deliberately: file
// permissions are advisory for a root process, so a chmod-based test would
// pass as root and fail in CI's ubuntu runner. `mode=ro` forbids writes
// inside SQLite itself, which is exactly the state an unwritable volume
// produces once the file is open.
func TestWritableProbeFailsClosedOnAReadOnlyStore(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "atf.db")

	// Create a real database first: `mode=ro` refuses to *create* a file,
	// and the point is to prove a write probe fails, not an open probe.
	store, err := NewSQLite(context.Background(), path+"?_busy_timeout=10000&_journal_mode=WAL")
	if err != nil {
		t.Fatalf("seed a writable store: %v", err)
	}
	_ = store.Close()

	// Reopen read-only and run the probe directly: this is the state a
	// non-root container on a root-owned volume reaches at startup.
	conn, err := sql.Open("sqlite3", "file:"+path+"?mode=ro")
	if err != nil {
		t.Fatalf("open read-only connection: %v", err)
	}
	defer conn.Close()
	if err := writable(context.Background(), conn); err == nil {
		t.Fatal("writable() on a read-only store returned no error; the worker would report ready while unable to serve")
	}
}

// TestWritableProbeAcceptsAFreshWritableStore: the happy path must not grow
// a side effect — the probe leaves no schema object behind.
func TestWritableProbeAcceptsAFreshWritableStore(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "atf.db")
	store, err := NewSQLite(context.Background(), path+"?_busy_timeout=10000&_journal_mode=WAL")
	if err != nil {
		t.Fatalf("NewSQLite on a fresh writable store: %v", err)
	}
	defer store.Close()

	// The probe must not leave its scratch table in the database it just
	// proved writable: the store opens a *production* schema, and a stray
	// table there is pollution a future schema check would trip over.
	db := storeDB(t, store)
	var name string
	err = db.QueryRow(
		`SELECT name FROM sqlite_master WHERE type='table' AND name='atf_worker_probe'`,
	).Scan(&name)
	if err == nil {
		t.Errorf("the writable probe left its scratch table behind in %s", path)
	}
}
