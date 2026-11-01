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
