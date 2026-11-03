package aiconfig

import (
	"context"
	"database/sql"
	"fmt"
	"strings"

	_ "github.com/mattn/go-sqlite3" // registers the sqlite3 driver the store opens
)

// sqliteStore is the SQLite-backed ai_configs reader.
//
// ai_configs is an SQLite-only table in the Core: SqliteAIConfigRepository
// is composed unconditionally (core/main.py:183) and no PostgreSQL DDL in
// the repository declares ai_configs. Unlike forward_rules (which
// ATF_POSTGRES_ALL can move) there is no switch to honour, so the store
// reads exactly one backend — mirroring the rule store's treatment of
// filter_rules, which the Core also keeps in SQLite unconditionally.
type sqliteStore struct {
	db     *sql.DB
	crypto *Crypto
}

// NewSQLiteStore opens the ai_configs reader on the shared atf.db path.
// One connection is enough — the store only reads, and SQLite serializes
// writers anyway — matching the rule store's own SQLite pool policy.
func NewSQLiteStore(ctx context.Context, dsn string, crypto *Crypto) (Store, error) {
	if crypto == nil {
		return nil, fmt.Errorf("aiconfig: a store needs the at-rest cipher")
	}
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("aiconfig: open %s: %w", label(dsn), err)
	}
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("aiconfig: ping %s: %w", label(dsn), err)
	}
	return &sqliteStore{db: db, crypto: crypto}, nil
}

// Close releases the handle.
func (s *sqliteStore) Close() error {
	if s == nil {
		return nil
	}
	return s.db.Close()
}

// aiConfigsProjection is the ai_configs read. Every column the execution
// projection consumes is named, so a row with a future column added by the
// Core still scans (SELECT * would too, but an explicit projection is the
// contract the scan order depends on).
const aiConfigsProjection = `
	SELECT id, name, provider, model, api_key_encrypted, base_url,
	       system_prompt, user_prompt_template, temperature, is_enabled,
	       target_language, owner_user_id
	FROM ai_configs
	WHERE id = ?
`

// ownerScopedProjection is the same read, scoped to one owner in SQL so a
// foreign row is never materialised — the Python repository's own contract
// (SqliteAIConfigRepository.get_owned).
const ownerScopedProjection = `
	SELECT id, name, provider, model, api_key_encrypted, base_url,
	       system_prompt, user_prompt_template, temperature, is_enabled,
	       target_language, owner_user_id
	FROM ai_configs
	WHERE id = ? AND owner_user_id = ?
`

// GetOwned returns the config when the given owner owns it, nil when the
// id does not exist under that owner.
func (s *sqliteStore) GetOwned(ctx context.Context, configID string, ownerUserID int64) (*AIConfig, error) {
	if strings.TrimSpace(configID) == "" {
		return nil, nil
	}
	row := s.db.QueryRowContext(ctx, ownerScopedProjection, configID, ownerUserID)
	cfg, err := scanConfig(row, s.crypto)
	if err != nil {
		return nil, err
	}
	return cfg, nil
}

// GetByID returns the config regardless of owner. The rewrite orchestrator
// uses it only to name why an owner-scoped read refused (the Python
// _resolve_config's get_by_id fallthrough), never to execute a rewrite.
func (s *sqliteStore) GetByID(ctx context.Context, configID string) (*AIConfig, error) {
	if strings.TrimSpace(configID) == "" {
		return nil, nil
	}
	row := s.db.QueryRowContext(ctx, aiConfigsProjection, configID)
	cfg, err := scanConfig(row, s.crypto)
	if err != nil {
		return nil, err
	}
	return cfg, nil
}

// scanner is the common contract of sql.Row and sql.Rows for one row.
type scanner interface {
	Scan(dest ...any) error
}

// scanConfig materialises one ai_configs row into the execution
// projection, decrypting the key column. A decryption failure on one row
// is reported, not fatal: the Python repository's _safe_decrypt logs and
// continues with an empty key, and the transformer then refuses an empty
// key as INVALID_CONFIG — the same layered posture, so a single bad row
// never breaks every other task's rewrite.
func scanConfig(row scanner, crypto *Crypto) (*AIConfig, error) {
	var (
		id, name, provider, model   string
		apiKeyEncrypted, baseURL    string
		systemPrompt, userTemplate  string
		temperature                 float64
		isEnabled                   bool
		targetLanguage              string
		ownerUserID                 int64
	)
	if err := row.Scan(
		&id, &name, &provider, &model, &apiKeyEncrypted, &baseURL,
		&systemPrompt, &userTemplate, &temperature, &isEnabled,
		&targetLanguage, &ownerUserID,
	); err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("aiconfig: scan row: %w", err)
	}
	apiKey := ""
	if enc := strings.TrimSpace(apiKeyEncrypted); enc != "" {
		plaintext, err := crypto.Decrypt(enc)
		if err != nil {
			// A row the Worker cannot read must not silently become
			// "no config": it is reported, and the rewrite is refused
			// with the config still resolvable (the orchestrator logs
			// the refusal instead of spending the provider).
			return nil, fmt.Errorf("aiconfig: config %s: %w", id, err)
		}
		apiKey = plaintext
	}
	return &AIConfig{
		ID:                  id,
		Name:                name,
		Provider:            strings.ToLower(strings.TrimSpace(provider)),
		Model:               model,
		APIKey:              apiKey,
		BaseURL:             baseURL,
		SystemPrompt:        systemPrompt,
		UserPromptTemplate:  userTemplate,
		Temperature:         temperature,
		IsEnabled:           isEnabled,
		TargetLanguage:      targetLanguage,
		OwnerUserID:         ownerUserID,
	}, nil
}

// label describes a DSN without credentials, so an error mentioning the
// target never carries the password the DSN may embed.
func label(dsn string) string {
	// SQLite DSNs are file paths (optionally with ?query); they carry no
	// password. Strip the query to keep the log short.
	if i := strings.IndexByte(dsn, '?'); i >= 0 {
		return "sqlite:" + dsn[:i]
	}
	return "sqlite:" + dsn
}
