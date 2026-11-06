package prompt

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"sync"

	_ "github.com/mattn/go-sqlite3"
)

type sqliteStore struct {
	db *sql.DB

	// enabledColumn caches the PRAGMA probe for prompt_templates.enabled:
	// a Worker may start beside a database the Python Core has not migrated
	// yet, and a missing column must degrade to "every template enabled"
	// instead of failing every prompt resolution.
	mu               sync.Mutex
	enabledColumnSet bool
	enabledColumn    bool
}

// NewSQLiteStore opens the prompt reader on the SQLite database DSN.
func NewSQLiteStore(ctx context.Context, dsn string) (Store, error) {
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("prompt: open %s: %w", dsn, err)
	}
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("prompt: ping %s: %w", dsn, err)
	}
	return &sqliteStore{db: db}, nil
}

// NewSQLiteStoreFromDB wraps an existing *sql.DB connection.
func NewSQLiteStoreFromDB(db *sql.DB) Store {
	return &sqliteStore{db: db}
}

// hasEnabledColumn reports (once) whether prompt_templates carries the
// library enable/disable switch. A probe error keeps the optimistic default
// (column present): the guarded ALTER that adds the column runs on every
// Core startup, so a transient probe failure is more likely a hiccup than a
// genuinely old schema.
func (s *sqliteStore) hasEnabledColumn(ctx context.Context) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.enabledColumnSet {
		return s.enabledColumn
	}
	rows, err := s.db.QueryContext(ctx, "PRAGMA table_info(prompt_templates)")
	if err == nil {
		defer rows.Close()
		for rows.Next() {
			var cid int
			var name, ctype string
			var notNull, pk int
			var dflt sql.NullString
			if scanErr := rows.Scan(&cid, &name, &ctype, &notNull, &dflt, &pk); scanErr == nil && name == "enabled" {
				s.enabledColumn = true
				break
			}
		}
	}
	s.enabledColumnSet = true
	return s.enabledColumn
}

// GetTemplate queries prompt_templates owner-scoped (owner_user_id = ? OR is_system = 1).
//
// A template switched off in the prompt manager resolves as not found: the
// delivery engine falls back to the task's AI-config prompt, exactly as the
// Python pipeline does when it sees enabled=False
// ([PIPELINE:AI_REWRITE:PROMPT_OFF]). The template itself stays in the
// library — this read path is delivery-only.
func (s *sqliteStore) GetTemplate(ctx context.Context, templateID string, ownerUserID int64) (*PromptTemplate, error) {
	if !s.hasEnabledColumn(ctx) {
		return s.getTemplateLegacy(ctx, templateID, ownerUserID)
	}
	const query = `
		SELECT id, name, description, system_prompt, user_prompt_template,
		       target_language, current_version, is_system, owner_user_id,
		       created_at, updated_at, enabled
		FROM prompt_templates
		WHERE id = ? AND (owner_user_id = ? OR is_system = 1)
	`
	var t PromptTemplate
	var isSys, enabled int
	err := s.db.QueryRowContext(ctx, query, templateID, ownerUserID).Scan(
		&t.ID, &t.Name, &t.Description, &t.SystemPrompt, &t.UserPromptTemplate,
		&t.TargetLanguage, &t.CurrentVersion, &isSys, &t.OwnerUserID,
		&t.CreatedAt, &t.UpdatedAt, &enabled,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("prompt: get template: %w", err)
	}
	t.IsSystem = isSys != 0
	t.Enabled = enabled != 0
	if !t.Enabled {
		// Disabled = not part of the active library, for delivery purposes.
		return nil, nil
	}
	return &t, nil
}

// getTemplateLegacy is the pre-enabled-column read: every template counts as
// enabled, which is exactly what the rows mean when the switch does not
// exist yet.
func (s *sqliteStore) getTemplateLegacy(ctx context.Context, templateID string, ownerUserID int64) (*PromptTemplate, error) {
	const query = `
		SELECT id, name, description, system_prompt, user_prompt_template,
		       target_language, current_version, is_system, owner_user_id,
		       created_at, updated_at
		FROM prompt_templates
		WHERE id = ? AND (owner_user_id = ? OR is_system = 1)
	`
	var t PromptTemplate
	var isSys int
	err := s.db.QueryRowContext(ctx, query, templateID, ownerUserID).Scan(
		&t.ID, &t.Name, &t.Description, &t.SystemPrompt, &t.UserPromptTemplate,
		&t.TargetLanguage, &t.CurrentVersion, &isSys, &t.OwnerUserID,
		&t.CreatedAt, &t.UpdatedAt,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("prompt: get template: %w", err)
	}
	t.IsSystem = isSys != 0
	t.Enabled = true
	return &t, nil
}

// GetVersion queries a specific version from prompt_versions.
func (s *sqliteStore) GetVersion(ctx context.Context, promptID string, version int) (*PromptVersion, error) {
	const query = `
		SELECT id, prompt_id, version, system_prompt, user_prompt_template,
		       change_summary, is_active, created_at
		FROM prompt_versions
		WHERE prompt_id = ? AND version = ?
	`
	var v PromptVersion
	var isActive int
	err := s.db.QueryRowContext(ctx, query, promptID, version).Scan(
		&v.ID, &v.PromptID, &v.Version, &v.SystemPrompt, &v.UserPromptTemplate,
		&v.ChangeSummary, &isActive, &v.CreatedAt,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("prompt: get version: %w", err)
	}
	v.IsActive = isActive != 0
	return &v, nil
}
