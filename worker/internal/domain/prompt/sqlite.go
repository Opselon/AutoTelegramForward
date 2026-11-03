package prompt

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	_ "github.com/mattn/go-sqlite3"
)

type sqliteStore struct {
	db *sql.DB
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

// GetTemplate queries prompt_templates owner-scoped (owner_user_id = ? OR is_system = 1).
func (s *sqliteStore) GetTemplate(ctx context.Context, templateID string, ownerUserID int64) (*PromptTemplate, error) {
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
