package queue

import (
	"database/sql"
	"testing"

	"atf/worker/internal/domain/job"
)

// Test helpers shared with the other engine packages. The fixtures
// themselves (schema DDL, store construction, row insertion) live in
// sqlite_test.go; these exports are the only surface those packages can
// reach, so the schema stays defined in exactly one place.

// NewTestStore builds a fresh SQLite store carrying the Core's queue schema.
func NewTestStore(t *testing.T) Store {
	t.Helper()
	return newTestStore(t)
}

// InsertJob writes a delivery_jobs row for a test.
func InsertJob(t *testing.T, s Store, j *job.DeliveryJob) {
	t.Helper()
	insertJob(t, s, j)
}

// StoreDB returns the store's underlying *sql.DB for direct row assertions.
func StoreDB(t *testing.T, s Store) *sql.DB {
	t.Helper()
	if st, ok := s.(*sqliteStore); ok {
		return st.db
	}
	t.Fatalf("store is not a sqliteStore")
	return nil
}

// StoreDBForOps digs the *sql.DB out of a store for the ops surface's
// read-only join queries (the per-task forward_rules lookup). It is the
// exported twin of StoreDB, named for what it is FOR rather than what it
// returns, so no future reader mistakes it for a write path.
func StoreDBForOps(s Store) *sql.DB {
	if st, ok := s.(*sqliteStore); ok {
		return st.db
	}
	if st, ok := s.(*pgStore); ok {
		return st.db
	}
	return nil
}

// BackendLabel reports which backend a store serves: "sqlite" or
// "postgres". The ops surface uses it to pick the placeholder convention
// for its join queries (the same convention rulestore's rulesQuery applies
// to its own projections) and to label /ops's queue_backend — never to log
// a DSN.
func BackendLabel(s Store) string {
	if _, ok := s.(*pgStore); ok {
		return "postgres"
	}
	return "sqlite"
}
