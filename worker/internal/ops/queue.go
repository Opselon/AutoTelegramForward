package ops

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"
)

// QueueStats is the queue-status aggregation the /ops and /ops/queue
// handlers report. It is a READ-ONLY view over the durable delivery_jobs
// table — the only DB reads the ops surface allows, and they happen on the
// request path (bounded, once per poll), never per message and never per
// heartbeat.
type QueueStats struct {
	// Status counts over delivery_jobs.
	Pending      int64
	Processing   int64 // CLAIMED with a live lease
	Delivered    int64
	Failed       int64
	Delayed      int64 // RETRY_WAIT with a future next_retry_at
	RetryPending int64 // RETRY_WAIT whose backoff has elapsed
	DeadLetter   int64 // FAILED with attempts >= max_attempts
	Unknown      int64
	// OldestPendingAgeSeconds is the age of the oldest unsent job; 0 when
	// nothing is waiting. It is the backlog's severity at a glance: a depth
	// of 40 jobs that are 3 seconds old is fine, a depth of 40 that is 40
	// minutes old is stuck.
	OldestPendingAgeSeconds float64
}

// QueueStoreReader is the read-only slice of the queue store the ops
// surface needs. Both queue.Store implementations (sqliteStore, pgStore)
// satisfy it through the methods added in sqlite.go / postgres.go, so /ops
// needs no second connection and no knowledge of which backend is live.
type QueueStoreReader interface {
	QueueStats(ctx context.Context) (*QueueStats, error)
}

// TaskRow is one per-task operational row (the contract's §1.4), aggregated
// from durable delivery_jobs by SQL GROUP BY — the ONLY DB reads the
// per-task surface allows. No new tables, no per-task state in memory.
type TaskRow struct {
	RuleID     string
	Source     string
	Target     string
	Enabled    bool
	Pending    int64
	Processing int64
	Delivered  int64
	Failed     int64
	Retries    int64
	// LastSuccessAt / LastErrorAt are RFC3339 strings ("" = never), read
	// off the delivery_jobs row's updated_at for its newest SENT / failed
	// row. LastError is the sanitized error_detail of that failed row.
	LastSuccessAt string
	LastErrorAt   string
	LastError     string
	// WorkerID is the worker that produced the aggregate.
	WorkerID string
	// ConfigUpdatedAt is the forward_rules updated_at the join read, so an
	// operator can tell a genuinely-stale row from a merely idle one.
	ConfigUpdatedAt string
}

// TaskStoreReader is the read-only per-task aggregation surface.
type TaskStoreReader interface {
	// TaskRows aggregates delivery_jobs by rule_id. limit caps the row count
	// (the panel paginates; an unbounded GROUP BY on a large queue is not
	// cheap).
	TaskRows(ctx context.Context, limit int) ([]*TaskRow, error)
}

// joinRuleInfo is what the per-task aggregation needs from forward_rules.
// Deliberately NOT rule.ForwardRule: the ops read is not the execution
// projection, and coupling them would make the next schema change break the
// panel.
type joinRuleInfo struct {
	id      string
	source  string
	target  string
	enabled bool
	updated int64
	hasRow  bool
}

// placeholder returns the backend's positional placeholder for index n
// (1-based): PostgreSQL $n, SQLite '?'. The same convention rulestore's
// rulesQuery applies to its shared projections.
func placeholder(backend string, n int) string {
	if backend == "postgres" {
		return fmt.Sprintf("$%d", n)
	}
	return "?"
}

// ReadRulesForTasks reads the forward_rules rows the named tasks own, on
// whichever backend the store's reader points at. A task whose rule row is
// gone (deleted while its jobs sat in the queue) is simply absent — the
// caller reports those rows with enabled=false, because their delivery_jobs
// rows are real work the operator must still see.
//
// The ids are bound as parameters, never interpolated: a rule id comes from
// the queue's own rows rather than user input, but parameterization is
// free.
func ReadRulesForTasks(ctx context.Context, db *sql.DB, backend string, ruleIDs []string) (map[string]*joinRuleInfo, error) {
	out := map[string]*joinRuleInfo{}
	if len(ruleIDs) == 0 || db == nil {
		return out, nil
	}
	marks := make([]string, len(ruleIDs))
	for i := range ruleIDs {
		marks[i] = placeholder(backend, i+1)
	}
	query := fmt.Sprintf(`
		SELECT id, is_active, source_chat_id, target_chat_id, updated_at
		FROM forward_rules
		WHERE id IN (%s)`, strings.Join(marks, ","))
	args := make([]any, 0, len(ruleIDs))
	for _, id := range ruleIDs {
		args = append(args, id)
	}
	rows, err := db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("ops task join: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var (
			id       string
			isActive bool
			source   sql.NullString
			target   sql.NullString
			updated  sql.NullInt64
		)
		if err := rows.Scan(&id, &isActive, &source, &target, &updated); err != nil {
			return nil, fmt.Errorf("ops task join scan: %w", err)
		}
		out[id] = &joinRuleInfo{
			id:      id,
			source:  source.String,
			target:  target.String,
			enabled: isActive,
			updated: updated.Int64,
			hasRow:  true,
		}
	}
	return out, rows.Err()
}

// RFC3339 formats a unix timestamp (0 → ""). Exported for the queue store
// adapters, which build per-task rows in their own package.
func RFC3339(unix int64) string {
	if unix <= 0 {
		return ""
	}
	return time.Unix(unix, 0).UTC().Format(time.RFC3339)
}

// SanitizeLast cleans a persisted error_detail before it reaches the
// per-task row. error_detail is engine-written reason text (a code plus
// occasionally a short Telegram detail); one line, bounded.
func SanitizeLast(s string) string { return sanitize(s) }

// BackendOf reports which backend a queue store serves: "sqlite" or
// "postgres". The ops surface uses it to pick the placeholder convention
// for its join queries — and to label /ops's queue_backend, never to log a
// DSN.
func BackendOf(s any) string {
	name := fmt.Sprintf("%T", s)
	if strings.Contains(name, "pgStore") {
		return "postgres"
	}
	return "sqlite"
}
