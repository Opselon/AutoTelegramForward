package prompt

import (
	"context"
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

func setupTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	schema := `
	CREATE TABLE prompt_templates (
		id TEXT PRIMARY KEY,
		name TEXT NOT NULL,
		description TEXT NOT NULL DEFAULT '',
		system_prompt TEXT NOT NULL,
		user_prompt_template TEXT NOT NULL DEFAULT '{text}',
		target_language TEXT NOT NULL DEFAULT 'en',
		current_version INTEGER NOT NULL DEFAULT 1,
		is_system INTEGER NOT NULL DEFAULT 0,
		created_at INTEGER NOT NULL,
		updated_at INTEGER NOT NULL,
		owner_user_id INTEGER NOT NULL DEFAULT 0
	);

	CREATE TABLE prompt_versions (
		id TEXT PRIMARY KEY,
		prompt_id TEXT NOT NULL,
		version INTEGER NOT NULL,
		system_prompt TEXT NOT NULL,
		user_prompt_template TEXT NOT NULL,
		change_summary TEXT NOT NULL DEFAULT '',
		is_active INTEGER NOT NULL DEFAULT 1,
		created_at INTEGER NOT NULL,
		UNIQUE(prompt_id, version)
	);
	`
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("create tables: %v", err)
	}
	return db
}

func TestPromptResolutionAndVersionPinning(t *testing.T) {
	db := setupTestDB(t)
	defer db.Close()
	ctx := context.Background()

	// Seed templates:
	// 1. User template owned by 42, current_version=2
	_, err := db.Exec(`
		INSERT INTO prompt_templates (id, name, system_prompt, user_prompt_template, target_language, current_version, is_system, owner_user_id, created_at, updated_at)
		VALUES ('p-user', 'User Prompt', 'System v2', 'Translate v2: {text}', 'fa', 2, 0, 42, 100, 200)
	`)
	if err != nil {
		t.Fatalf("insert template: %v", err)
	}
	_, err = db.Exec(`
		INSERT INTO prompt_versions (id, prompt_id, version, system_prompt, user_prompt_template, created_at)
		VALUES
			('v1', 'p-user', 1, 'System v1', 'Translate v1: {text}', 100),
			('v2', 'p-user', 2, 'System v2', 'Translate v2: {text}', 200)
	`)
	if err != nil {
		t.Fatalf("insert versions: %v", err)
	}

	// 2. System template (is_system=1, owner=0)
	_, err = db.Exec(`
		INSERT INTO prompt_templates (id, name, system_prompt, user_prompt_template, target_language, current_version, is_system, owner_user_id, created_at, updated_at)
		VALUES ('p-sys', 'System Prompt', 'System Base', 'Clean: {text}', 'en', 1, 1, 0, 100, 100)
	`)
	if err != nil {
		t.Fatalf("insert sys template: %v", err)
	}

	// 3. Foreign user template (owned by 99)
	_, err = db.Exec(`
		INSERT INTO prompt_templates (id, name, system_prompt, user_prompt_template, target_language, current_version, is_system, owner_user_id, created_at, updated_at)
		VALUES ('p-foreign', 'Foreign Prompt', 'Secret', '{text}', 'en', 1, 0, 99, 100, 100)
	`)
	if err != nil {
		t.Fatalf("insert foreign template: %v", err)
	}

	store := NewSQLiteStoreFromDB(db)

	// Case A: Version 0 (latest) resolves current_version (v2)
	res, err := PreviewPrompt(ctx, store, "p-user", "hello world", 0, 42)
	if err != nil {
		t.Fatalf("preview p-user latest: %v", err)
	}
	if res.Version != 2 {
		t.Errorf("version: got %d, want 2", res.Version)
	}
	if res.SystemPrompt != "System v2" {
		t.Errorf("system_prompt: got %q, want 'System v2'", res.SystemPrompt)
	}
	if res.RenderedUserPrompt != "Translate v2: hello world" {
		t.Errorf("rendered_user_prompt: got %q", res.RenderedUserPrompt)
	}

	// Case B: Version 1 pinned resolves v1
	res, err = PreviewPrompt(ctx, store, "p-user", "hello world", 1, 42)
	if err != nil {
		t.Fatalf("preview p-user v1: %v", err)
	}
	if res.Version != 1 {
		t.Errorf("version: got %d, want 1", res.Version)
	}
	if res.SystemPrompt != "System v1" {
		t.Errorf("system_prompt: got %q, want 'System v1'", res.SystemPrompt)
	}
	if res.RenderedUserPrompt != "Translate v1: hello world" {
		t.Errorf("rendered_user_prompt: got %q", res.RenderedUserPrompt)
	}

	// Case C: System prompt accessible by any owner (e.g. 42)
	res, err = PreviewPrompt(ctx, store, "p-sys", "test", 0, 42)
	if err != nil {
		t.Fatalf("preview p-sys: %v", err)
	}
	if res.SystemPrompt != "System Base" {
		t.Errorf("sys prompt: got %q", res.SystemPrompt)
	}

	// Case D: Foreign prompt refused for owner 42
	_, err = PreviewPrompt(ctx, store, "p-foreign", "test", 0, 42)
	if err == nil {
		t.Error("expected error for foreign template, got nil")
	}

	// Case E: Non-existent prompt refused
	_, err = PreviewPrompt(ctx, store, "p-unknown", "test", 0, 42)
	if err == nil {
		t.Error("expected error for unknown template, got nil")
	}

	// Case F: Template without {text} placeholder prepends
	_, err = db.Exec(`
		INSERT INTO prompt_templates (id, name, system_prompt, user_prompt_template, current_version, is_system, owner_user_id, created_at, updated_at)
		VALUES ('p-no-placeholder', 'No Placeholder', 'sys', 'prefix only', 1, 0, 42, 100, 100)
	`)
	if err != nil {
		t.Fatalf("insert no-placeholder template: %v", err)
	}
	res, err = PreviewPrompt(ctx, store, "p-no-placeholder", "msg", 0, 42)
	if err != nil {
		t.Fatalf("preview p-no-placeholder: %v", err)
	}
	if res.RenderedUserPrompt != "prefix only\n\nmsg" {
		t.Errorf("rendered: got %q, want 'prefix only\\n\\nmsg'", res.RenderedUserPrompt)
	}
}
