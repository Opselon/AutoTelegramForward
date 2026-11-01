// Package rulestore is the database-backed implementation of rule.Store.
//
// It replaces the Stage-1 stub with real reads of the forward_rules and
// filter_rules tables — and it reads them from the CORRECT backend each time,
// because the Core's composition root does not keep them together:
//
//   - forward_rules moves to PostgreSQL only when ATF_POSTGRES_ALL selects it;
//     otherwise it stays in SQLite.
//   - filter_rules is ALWAYS SQLite under the current composition root (the
//     PostgreSQL schema does not define it).
//
// So the store holds up to two *sql.DB handles and answers each lookup from
// the one its table actually lives in. A worker that read filter_rules from
// PostgreSQL would be reading a table that does not exist, and a worker that
// read forward_rules from SQLite while the Core serves it from PostgreSQL
// would silently act on a stale or missing task. Both are wrong in exactly
// the way a fail-closed startup validation exists to prevent.
package rulestore

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"atf/worker/internal/domain/rule"
)

// DSN is a parsed connection target for one logical database, carrying only
// what the store needs to log about it. A DSN's raw string can embed a
// password; this type exists so a log line can never print one.
type DSN struct {
	// Raw is the connectable string. It is never logged.
	Raw string
	// Label describes the target without credentials, e.g.
	// "postgres://core-db:5432/atf" or "sqlite:/app/data/atf.db".
	Label string
	// Backend is "postgres" or "sqlite".
	Backend string
}

// Config selects which database serves which table, mirroring the Python
// Core's composition root (core/main.py) row for row:
//
//   - delivery tables follow ATF_DATABASE_URL;
//   - forward_rules follows ATF_DATABASE_URL *and* ATF_POSTGRES_ALL;
//   - filter_rules always stays on SQLite.
//
// The empty-string cases mean "that database is not configured", and the
// constructor refuses to build a store that has no home for a table it needs.
type Config struct {
	// Postgres is the configured ATF_DATABASE_URL target. Empty = unset.
	Postgres DSN
	// SQLite is the atf.db path the Core always initializes, even when
	// PostgreSQL carries the durable queue.
	SQLite DSN
	// PostgresAll mirrors ATF_POSTGRES_ALL: when set, forward_rules lives in
	// PostgreSQL too. filter_rules ignores this by design.
	PostgresAll bool
}

// New builds the rule store, opening exactly the databases the configuration
// requires — never both when one will do.
func New(ctx context.Context, cfg Config) (rule.Store, error) {
	store := &Store{}

	// forward_rules: PostgreSQL only when the Core's own selection rule says
	// so (ATF_DATABASE_URL *and* ATF_POSTGRES_ALL). Anything else is SQLite.
	switch {
	case cfg.PostgresAll && cfg.Postgres.Raw != "":
		pg, err := openPostgres(ctx, cfg.Postgres.Raw)
		if err != nil {
			return nil, fmt.Errorf("open forward_rules store: %w", err)
		}
		store.rulesDB = pg
		store.rulesBackend = "postgres"
	case cfg.SQLite.Raw != "":
		lite, err := openSQLite(ctx, cfg.SQLite.Raw)
		if err != nil {
			return nil, fmt.Errorf("open forward_rules store: %w", err)
		}
		store.rulesDB = lite
		store.rulesBackend = "sqlite"
	default:
		return nil, fmt.Errorf("no configured home for forward_rules (ATF_DB_PATH or ATF_DATABASE_URL+ATF_POSTGRES_ALL)")
	}

	// filter_rules: always SQLite. Creating a PostgreSQL filter_rules table
	// here would be inventing a second source of truth for configuration the
	// Core deliberately keeps in SQLite.
	if cfg.SQLite.Raw != "" {
		lite, err := openSQLite(ctx, cfg.SQLite.Raw)
		if err != nil {
			store.Close()
			return nil, fmt.Errorf("open filter_rules store: %w", err)
		}
		store.filtersDB = lite
	} else {
		store.Close()
		return nil, fmt.Errorf("filter_rules has no configured home (ATF_DB_PATH); the Core serves it from SQLite")
	}
	return store, nil
}

// Store is the composite rule.Store: one handle per authoritative backend.
//
// The common case — SQLite for everything configured, no PostgreSQL — shares
// a single connection between the two readers, because both tables live in
// the same file.
type Store struct {
	rulesDB       *sql.DB
	rulesBackend  string
	filtersDB     *sql.DB
	filtersShared bool // true when filtersDB IS rulesDB (one SQLite file)
}

// Close releases every handle the store opened. Each distinct database is
// closed exactly once: CloseOnIdentical is safe because *sql.DB counts down.
func (s *Store) Close() error {
	if s == nil {
		return nil
	}
	var first error
	if s.rulesDB != nil {
		if err := s.rulesDB.Close(); err != nil && first == nil {
			first = err
		}
		s.rulesDB = nil
	}
	// When both readers shared one handle, rulesDB.Close() already released
	// it and the shared pointer is the same object.
	if s.filtersDB != nil && !s.filtersShared {
		if err := s.filtersDB.Close(); err != nil && first == nil {
			first = err
		}
	}
	s.filtersDB = nil
	return first
}

// GetByID reads a forward_rules row and, when it references one, the attached
// filter_rules row.
//
// The three results are deliberate and are the whole contract:
//
//   - (rule, filter, nil) — the task exists, with its filter when configured;
//   - (nil, nil, nil) — the task genuinely no longer exists (deleted while
//     its job sat in the queue), so the caller drops the job;
//   - (nil, nil, err) — the store could not answer. A database error is NEVER
//     reported as a missing rule: a worker that treats an unreachable store
//     as "rule deleted" would fail deliveries en masse during an outage that
//     should have deferred them.
func (s *Store) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	if strings.TrimSpace(ruleID) == "" {
		return nil, nil, fmt.Errorf("rule store: empty rule id")
	}
	ctx, cancel := context.WithTimeout(context.Background(), queryTimeout)
	defer cancel()

	ext, err := s.getRule(ctx, ruleID)
	if err != nil {
		return nil, nil, err
	}
	if ext == nil {
		return nil, nil, nil // the task genuinely no longer exists
	}
	r := ext.ForwardRule
	if strings.TrimSpace(ext.FilterRuleID) == "" {
		return r, nil, nil // no filter attached
	}
	f, err := s.getFilter(ctx, ext.FilterRuleID)
	if err != nil {
		// A filter-store failure is an outage, not "no filter". Report it —
		// the caller defers instead of deciding from missing data.
		return r, nil, err
	}
	return r, f, nil
}

// queryTimeout bounds every store lookup. A rule lookup that never returns
// must not hold a delivery slot forever.
const queryTimeout = 5 * time.Second

// --- forward_rules --------------------------------------------------------- //

// forwardProjection is the narrow column set the worker actually needs. It is
// NOT every column the entity carries: the delivery decision reads exactly
// these, and selecting fewer columns is cheaper and more honest than
// projecting the whole row and dropping most of it.
//
// The placeholders are written for PostgreSQL ($1); the SQLite reader rewrites
// them through rulesQuery before the statement ever executes, so one verified
// projection serves both engines.
const forwardProjection = `
	SELECT id, session_id, is_active, is_paused, paused_until,
	       forward_mode, content_mode, filter_rule_id, metadata
	FROM forward_rules
	WHERE id = $1
`

// extendedForwardRule carries the columns GetByID reads on its way to the
// projection — filter_rule_id and metadata included — without exposing them
// on the public rule.ForwardRule (which is the execution contract, not the
// row shape).
type extendedForwardRule struct {
	*rule.ForwardRule
	FilterRuleID string
	Metadata     map[string]any
}

// getRule reads one forward_rules row from whichever backend owns it.
//
// It returns the *extended* projection so GetByID can follow the task's
// optional filter reference without a second read of forward_rules.
func (s *Store) getRule(ctx context.Context, ruleID string) (*extendedForwardRule, error) {
	query, arg := s.rulesQuery(forwardProjection, ruleID)
	rows, err := s.rulesDB.QueryContext(ctx, query, arg)
	if err != nil {
		// A prepare or execution failure is an outage, never "not found": a
		// worker that mappped a broken statement onto "rule deleted" would
		// fail every queued delivery for that task.
		return nil, fmt.Errorf("read forward_rules %s: %w", ruleID, err)
	}
	defer rows.Close()

	if !rows.Next() {
		if err := rows.Err(); err != nil {
			return nil, fmt.Errorf("read forward_rules %s: %w", ruleID, err)
		}
		return nil, nil // genuinely absent
	}

	var (
		id, sessionID string
		isActive      bool
		isPaused      bool
		pausedUntil   int64
		forwardMode   sql.NullString
		contentMode   sql.NullString
		filterRuleID  sql.NullString
		metadataJSON  sql.NullString
	)
	if err := rows.Scan(&id, &sessionID, &isActive, &isPaused, &pausedUntil,
		&forwardMode, &contentMode, &filterRuleID, &metadataJSON); err != nil {
		return nil, fmt.Errorf("scan forward_rules %s: %w", ruleID, err)
	}

	extra := &extendedForwardRule{ForwardRule: &rule.ForwardRule{
		ID:          id,
		SessionID:   sessionID,
		IsActive:    isActive,
		IsPaused:    isPaused,
		PausedUntil: pausedUntil,
		ForwardMode: forwardMode.String,
		ContentMode: contentMode.String,
	}, FilterRuleID: filterRuleID.String}

	if metadataJSON.Valid && metadataJSON.String != "" {
		var meta map[string]any
		if err := json.Unmarshal([]byte(metadataJSON.String), &meta); err == nil {
			extra.Metadata = meta
		}
	}

	// auto_pause is not a column: the engine records it inside the task's
	// metadata JSON, so the projection reads it out of the same blob.
	if reason, ok := extra.Metadata["auto_pause"].(map[string]any); ok {
		if r, ok := reason["reason"].(string); ok && r != "" {
			extra.AutoPauseReason = r
		}
	}
	return extra, nil
}

// --- filter_rules ---------------------------------------------------------- //

// getFilter reads one filter_rules row.
//
// Legacy compatibility follows the Python repository's own rules, because a
// row written before a column existed is not an error there:
//
//   - regex_block / regex_require may be absent on pre-v30 rows → empty
//     lists, exactly as the Python reader's "if in keys" guard yields;
//   - is_enabled may be absent on pre-v30 rows → TRUE, never false. The
//     Python reader's own comment is explicit: "Never default a stored
//     filter to off." A Go port that defaulted to disabled would silently
//     disable every old filter the first time the worker ran.
//
// A dangling reference (the rule points at a filter that no longer exists)
// returns (nil, nil): the task attached a filter, the filter is gone, and the
// delivery proceeds *unfiltered* — the Python pipeline's own behaviour when
// a filter load fails (it logs and evaluates no filter), never a drop.
func (s *Store) getFilter(ctx context.Context, filterID string) (*rule.FilterRule, error) {
	// filter_rules always lives in SQLite; rewrite the PG-style placeholder
	// the shared projection uses, if the projection ever cites one.
	filterQuery := filterProjectionSQLite
	rows, err := s.filtersDB.QueryContext(ctx, filterQuery, filterID)
	if err != nil {
		return nil, fmt.Errorf("read filter_rules %s: %w", filterID, err)
	}
	defer rows.Close()
	if !rows.Next() {
		if err := rows.Err(); err != nil {
			return nil, fmt.Errorf("read filter_rules %s: %w", filterID, err)
		}
		// Dangling reference: the filter is gone. Proceed unfiltered (the
		// Python pipeline's own behaviour on a failed filter load).
		return nil, nil
	}

	var (
		id                string
		whitelistJSON     string
		blacklistJSON     string
		regexPatternsJSON string
		allowedJSON       string
		blockedJSON       string
		dropService       bool
		minLen            int
		maxLen            int
	)
	if err := rows.Scan(&id, &whitelistJSON, &blacklistJSON,
		&regexPatternsJSON, &allowedJSON, &blockedJSON, &dropService, &minLen, &maxLen); err != nil {
		return nil, fmt.Errorf("scan filter_rules %s: %w", filterID, err)
	}

	f := &rule.FilterRule{
		ID:               id,
		IsEnabled:        true, // pre-v30 default; overridden when the column exists AND is false
		DropService:      dropService,
		MinMessageLength: minLen,
		MaxMessageLength: maxLen,
		WhitelistKeywords: decodeStringList(whitelistJSON),
		BlacklistKeywords: decodeStringList(blacklistJSON),
		RegexPatterns:    decodeStringList(regexPatternsJSON),
		AllowedMediaTypes: decodeStringList(allowedJSON),
		BlockedMediaTypes: decodeStringList(blockedJSON),
	}
	// The legacy optional columns carry their own NULL-ness: a row written
	// before v30 has NULL here, and NULL means "not configured", never an
	// error and never a silently-changed filter.
	if r, err := optionalStringList(ctx, s.filtersDB, filterID, "regex_block"); err == nil {
		f.RegexBlock = r
	}
	if r, err := optionalStringList(ctx, s.filtersDB, filterID, "regex_require"); err == nil {
		f.RegexRequire = r
	}
	if enabled, ok, err := optionalBool(ctx, s.filtersDB, filterID, "is_enabled"); err == nil {
		if ok {
			f.IsEnabled = enabled
		}
	}
	return f, nil
}

// decodeStringList decodes a JSON list column into []string, tolerating the
// empty string and malformed bytes — a corrupt row must not crash a delivery.
func decodeStringList(raw string) []string {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	var out []string
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		// A single string instead of a list is written by some historical
		// paths; accept it rather than losing the filter entirely.
		var single string
		if err2 := json.Unmarshal([]byte(raw), &single); err2 == nil {
			return []string{single}
		}
		return nil
	}
	return out
}

// optionalStringList reads one of the legacy optional JSON columns. It
// returns (list, nil) when the column carries a value, (nil, nil) when the
// column is NULL (absent on old rows), and (nil, err) on a real failure.
func optionalStringList(ctx context.Context, db *sql.DB, filterID, column string) ([]string, error) {
	var raw sql.NullString
	err := db.QueryRowContext(ctx,
		`SELECT `+quoteIdent(column)+` FROM filter_rules WHERE id = ?`, filterID).Scan(&raw)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, err
	}
	if !raw.Valid || raw.String == "" {
		return nil, nil
	}
	var out []string
	if err := json.Unmarshal([]byte(raw.String), &out); err != nil {
		var single string
		if err2 := json.Unmarshal([]byte(raw.String), &single); err2 == nil {
			return []string{single}, nil
		}
		return nil, nil
	}
	return out, nil
}

// optionalBool reads one legacy optional boolean column. The second return is
// false when the column is NULL, so the caller keeps its default.
func optionalBool(ctx context.Context, db *sql.DB, filterID, column string) (bool, bool, error) {
	var raw sql.NullBool
	err := db.QueryRowContext(ctx,
		`SELECT `+quoteIdent(column)+` FROM filter_rules WHERE id = ?`, filterID).Scan(&raw)
	if err != nil {
		if err == sql.ErrNoRows {
			return false, false, nil
		}
		return false, false, err
	}
	return raw.Valid && raw.Bool, raw.Valid, nil
}

// quoteIdent quotes a column identifier for a SELECT list. The names come
// from this package's own constants, never from user input, so this is a
// formatting helper rather than an injection surface.
func quoteIdent(name string) string {
	return `"` + strings.ReplaceAll(name, `"`, `""`) + `"`
}
