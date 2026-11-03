package store

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"

	_ "github.com/mattn/go-sqlite3" // registers the sqlite3 driver

	"atf/worker/internal/domain/autoreply"
)

// LeaseSeconds is how long a claimed job stays claimed before another worker may take it over.
const LeaseSeconds = 60

// MaxContextRows bounds the rolling conversation window per chat.
const MaxContextRows = 40

// MaxMemoryRows bounds remembered facts per chat.
const MaxMemoryRows = 60

// SQLiteStore is the SQLite implementation of the owner-scoped auto-reply store.
type SQLiteStore struct {
	db *sql.DB
}

// NewSQLiteStore opens the auto-reply tables on the given SQLite DSN.
func NewSQLiteStore(ctx context.Context, dsn string) (Store, error) {
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("autoreply: open %s: %w", dsn, err)
	}
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("autoreply: ping %s: %w", dsn, err)
	}
	s := &SQLiteStore{db: db}
	if _, err := db.ExecContext(ctx, SQLiteSchema); err != nil {
		db.Close()
		return nil, fmt.Errorf("autoreply: schema: %w", err)
	}
	return s, nil
}

// NewSQLiteStoreFromDB binds an existing *sql.DB (used by tests).
func NewSQLiteStoreFromDB(ctx context.Context, db *sql.DB) (Store, error) {
	if _, err := db.ExecContext(ctx, SQLiteSchema); err != nil {
		return nil, fmt.Errorf("autoreply: schema: %w", err)
	}
	return &SQLiteStore{db: db}, nil
}

func (s *SQLiteStore) Close() error {
	if s == nil || s.db == nil {
		return nil
	}
	return s.db.Close()
}

func requireOwner(ownerUserID int64) error {
	if ownerUserID <= 0 {
		return ErrOwnerRequired
	}
	return nil
}

func toJSON(v any) string {
	if v == nil {
		return "{}"
	}
	raw, err := json.Marshal(v)
	if err != nil {
		return "{}"
	}
	return string(raw)
}

func fromJSON(raw sql.NullString, def any) any {
	if !raw.Valid || strings.TrimSpace(raw.String) == "" {
		return def
	}
	var out any
	if err := json.Unmarshal([]byte(raw.String), &out); err != nil {
		return def
	}
	return out
}

func jsonIntList(raw sql.NullString) []int {
	var out []int
	if !raw.Valid {
		return out
	}
	if err := json.Unmarshal([]byte(raw.String), &out); err != nil {
		return nil
	}
	return out
}

func jsonStrList(raw sql.NullString) []string {
	var out []string
	if !raw.Valid {
		return out
	}
	if err := json.Unmarshal([]byte(raw.String), &out); err != nil {
		return nil
	}
	return out
}

func memoryHash(ownerUserID int64, chatID, content string) string {
	raw := fmt.Sprintf("%d|%s|%s", ownerUserID, autoreply.NormalizeChatID(chatID), strings.ToLower(strings.TrimSpace(content)))
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])[:32]
}

func newHexID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// --- settings ---------------------------------------------------------------

func (s *SQLiteStore) GetSettings(ctx context.Context, ownerUserID int64) (*autoreply.AutoReplySettings, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	row := s.db.QueryRowContext(ctx, `SELECT
        owner_user_id, enabled, ai_config_id, prompt_id, target_mode, language, tone,
        response_length, temperature, max_tokens, context_messages, memory_enabled,
        memory_max_facts, extra_instructions, max_reply_chars, typing_simulation,
        typing_delay_min, typing_delay_max, cooldown_seconds, daily_limit_per_chat,
        rate_limit_per_minute, max_concurrency, max_attempts, retry_backoff_seconds,
        quiet_hours_start, quiet_hours_end, timezone_offset_minutes, active_days,
        fallback_text, ignore_bots, version, created_at, updated_at
        FROM autoreply_settings WHERE owner_user_id=?`, ownerUserID)
	var st autoreply.AutoReplySettings
	var activeDays sql.NullString
	err := row.Scan(
		&st.OwnerUserID, &st.Enabled, &st.AIConfigID, &st.PromptID, &st.TargetMode, &st.Language, &st.Tone,
		&st.ResponseLength, &st.Temperature, &st.MaxTokens, &st.ContextMessages, &st.MemoryEnabled,
		&st.MemoryMaxFacts, &st.ExtraInstructions, &st.MaxReplyChars, &st.TypingSimulation,
		&st.TypingDelayMin, &st.TypingDelayMax, &st.CooldownSeconds, &st.DailyLimitPerChat,
		&st.RateLimitPerMinute, &st.MaxConcurrency, &st.MaxAttempts, &st.RetryBackoffSeconds,
		&st.QuietHoursStart, &st.QuietHoursEnd, &st.TimezoneOffsetMinutes, &activeDays,
		&st.FallbackText, &st.IgnoreBots, &st.Version, &st.CreatedAt, &st.UpdatedAt,
	)
	if err == sql.ErrNoRows {
		return autoreply.DefaultSettings(ownerUserID), nil
	}
	if err != nil {
		return nil, err
	}
	st.ActiveDays = jsonIntList(activeDays)
	return st.Normalize(), nil
}

func (s *SQLiteStore) SaveSettings(ctx context.Context, settings *autoreply.AutoReplySettings) (*autoreply.AutoReplySettings, error) {
	if settings == nil {
		return nil, ErrOwnerRequired
	}
	if err := requireOwner(settings.OwnerUserID); err != nil {
		return nil, err
	}
	settings.Normalize()
	now := autoreply.NowUnix()
	settings.UpdatedAt = now
	if settings.CreatedAt == 0 {
		settings.CreatedAt = now
	}
	_, err := s.db.ExecContext(ctx, `INSERT INTO autoreply_settings (
        owner_user_id, enabled, ai_config_id, prompt_id, target_mode, language, tone,
        response_length, temperature, max_tokens, context_messages, memory_enabled,
        memory_max_facts, extra_instructions, max_reply_chars, typing_simulation,
        typing_delay_min, typing_delay_max, cooldown_seconds, daily_limit_per_chat,
        rate_limit_per_minute, max_concurrency, max_attempts, retry_backoff_seconds,
        quiet_hours_start, quiet_hours_end, timezone_offset_minutes, active_days,
        fallback_text, ignore_bots, version, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(owner_user_id) DO UPDATE SET
        enabled=excluded.enabled, ai_config_id=excluded.ai_config_id, prompt_id=excluded.prompt_id,
        target_mode=excluded.target_mode, language=excluded.language, tone=excluded.tone,
        response_length=excluded.response_length, temperature=excluded.temperature,
        max_tokens=excluded.max_tokens, context_messages=excluded.context_messages,
        memory_enabled=excluded.memory_enabled, memory_max_facts=excluded.memory_max_facts,
        extra_instructions=excluded.extra_instructions, max_reply_chars=excluded.max_reply_chars,
        typing_simulation=excluded.typing_simulation, typing_delay_min=excluded.typing_delay_min,
        typing_delay_max=excluded.typing_delay_max, cooldown_seconds=excluded.cooldown_seconds,
        daily_limit_per_chat=excluded.daily_limit_per_chat,
        rate_limit_per_minute=excluded.rate_limit_per_minute,
        max_concurrency=excluded.max_concurrency, max_attempts=excluded.max_attempts,
        retry_backoff_seconds=excluded.retry_backoff_seconds,
        quiet_hours_start=excluded.quiet_hours_start, quiet_hours_end=excluded.quiet_hours_end,
        timezone_offset_minutes=excluded.timezone_offset_minutes,
        active_days=excluded.active_days, fallback_text=excluded.fallback_text,
        ignore_bots=excluded.ignore_bots, version=excluded.version, updated_at=excluded.updated_at`,
		settings.OwnerUserID, settings.Enabled, settings.AIConfigID, settings.PromptID,
		settings.TargetMode, settings.Language, settings.Tone,
		settings.ResponseLength, settings.Temperature, settings.MaxTokens, settings.ContextMessages,
		settings.MemoryEnabled, settings.MemoryMaxFacts, settings.ExtraInstructions, settings.MaxReplyChars,
		settings.TypingSimulation, settings.TypingDelayMin, settings.TypingDelayMax,
		settings.CooldownSeconds, settings.DailyLimitPerChat, settings.RateLimitPerMinute,
		settings.MaxConcurrency, settings.MaxAttempts, settings.RetryBackoffSeconds,
		settings.QuietHoursStart, settings.QuietHoursEnd, settings.TimezoneOffsetMinutes,
		toJSON(settings.ActiveDays), settings.FallbackText, settings.IgnoreBots,
		settings.Version, settings.CreatedAt, settings.UpdatedAt,
	)
	if err != nil {
		return nil, err
	}
	return settings, nil
}

// --- prompts ----------------------------------------------------------------

func (s *SQLiteStore) GetPrompt(ctx context.Context, promptID string, ownerUserID int64) (*autoreply.AutoReplyPrompt, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	row := s.db.QueryRowContext(ctx, `SELECT
        id, owner_user_id, name, description, system_prompt, user_prompt_template,
        tone, language, response_length, category, tags, is_default, is_archived,
        use_count, last_used_at, version, created_at, updated_at
        FROM autoreply_prompts WHERE id=? AND owner_user_id=?`, promptID, ownerUserID)
	return scanPrompt(row)
}

func scanPrompt(row *sql.Row) (*autoreply.AutoReplyPrompt, error) {
	var p autoreply.AutoReplyPrompt
	var tone, respLen, tags sql.NullString
	err := row.Scan(
		&p.ID, &p.OwnerUserID, &p.Name, &p.Description, &p.SystemPrompt, &p.UserPromptTemplate,
		&tone, &p.Language, &respLen, &p.Category, &tags, &p.IsDefault, &p.IsArchived,
		&p.UseCount, &p.LastUsedAt, &p.Version, &p.CreatedAt, &p.UpdatedAt,
	)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	p.Tone = tone.String
	p.ResponseLength = respLen.String
	p.Tags = jsonStrList(tags)
	return p.Normalize(), nil
}

func (s *SQLiteStore) ListPrompts(ctx context.Context, ownerUserID int64, includeArchived bool, search, category string) ([]*autoreply.AutoReplyPrompt, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	query := `SELECT id, owner_user_id, name, description, system_prompt, user_prompt_template,
        tone, language, response_length, category, tags, is_default, is_archived,
        use_count, last_used_at, version, created_at, updated_at
        FROM autoreply_prompts WHERE owner_user_id=?`
	args := []any{ownerUserID}
	if !includeArchived {
		query += " AND is_archived=0"
	}
	if strings.TrimSpace(category) != "" {
		query += " AND category=?"
		args = append(args, strings.TrimSpace(category))
	}
	if strings.TrimSpace(search) != "" {
		query += " AND (name LIKE ? OR description LIKE ? OR system_prompt LIKE ?)"
		like := "%" + strings.TrimSpace(search) + "%"
		args = append(args, like, like, like)
	}
	query += " ORDER BY is_default DESC, updated_at DESC"
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*autoreply.AutoReplyPrompt
	for rows.Next() {
		var p autoreply.AutoReplyPrompt
		var tone, respLen, tags sql.NullString
		if err := rows.Scan(
			&p.ID, &p.OwnerUserID, &p.Name, &p.Description, &p.SystemPrompt, &p.UserPromptTemplate,
			&tone, &p.Language, &respLen, &p.Category, &tags, &p.IsDefault, &p.IsArchived,
			&p.UseCount, &p.LastUsedAt, &p.Version, &p.CreatedAt, &p.UpdatedAt,
		); err != nil {
			return nil, err
		}
		p.Tone = tone.String
		p.ResponseLength = respLen.String
		p.Tags = jsonStrList(tags)
		out = append(out, p.Normalize())
	}
	return out, rows.Err()
}

func (s *SQLiteStore) SavePrompt(ctx context.Context, prompt *autoreply.AutoReplyPrompt) (*autoreply.AutoReplyPrompt, error) {
	if prompt == nil {
		return nil, ErrNameRequired
	}
	if err := requireOwner(prompt.OwnerUserID); err != nil {
		return nil, err
	}
	prompt.Normalize()
	if errs := prompt.Validate(); len(errs) > 0 {
		return nil, fmt.Errorf("%s", strings.Join(errs, ","))
	}
	now := autoreply.NowUnix()
	prompt.UpdatedAt = now
	if prompt.CreatedAt == 0 {
		prompt.CreatedAt = now
	}
	var existing string
	err := s.db.QueryRowContext(ctx, "SELECT id FROM autoreply_prompts WHERE id=? AND owner_user_id=?", prompt.ID, prompt.OwnerUserID).Scan(&existing)
	if err != nil && err != sql.ErrNoRows {
		return nil, err
	}
	if existing != "" {
		_, err = s.db.ExecContext(ctx, `UPDATE autoreply_prompts SET name=?, description=?, system_prompt=?,
            user_prompt_template=?, tone=?, language=?, response_length=?, category=?, tags=?,
            is_default=?, is_archived=?, use_count=?, last_used_at=?, version=?, updated_at=?
            WHERE id=? AND owner_user_id=?`,
			prompt.Name, prompt.Description, prompt.SystemPrompt, prompt.UserPromptTemplate,
			prompt.Tone, prompt.Language, prompt.ResponseLength, prompt.Category, toJSON(prompt.Tags),
			prompt.IsDefault, prompt.IsArchived, prompt.UseCount, prompt.LastUsedAt, prompt.Version,
			prompt.UpdatedAt, prompt.ID, prompt.OwnerUserID)
	} else {
		_, err = s.db.ExecContext(ctx, `INSERT INTO autoreply_prompts (id, owner_user_id, name, description,
            system_prompt, user_prompt_template, tone, language, response_length, category, tags,
            is_default, is_archived, use_count, last_used_at, version, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			prompt.ID, prompt.OwnerUserID, prompt.Name, prompt.Description,
			prompt.SystemPrompt, prompt.UserPromptTemplate, prompt.Tone, prompt.Language,
			prompt.ResponseLength, prompt.Category, toJSON(prompt.Tags),
			prompt.IsDefault, prompt.IsArchived, prompt.UseCount, prompt.LastUsedAt,
			prompt.Version, prompt.CreatedAt, prompt.UpdatedAt)
	}
	if err != nil {
		if strings.Contains(strings.ToUpper(err.Error()), "UNIQUE") {
			return nil, ErrNameTaken
		}
		return nil, err
	}
	if prompt.IsDefault {
		_, err = s.db.ExecContext(ctx, "UPDATE autoreply_prompts SET is_default=0, updated_at=? WHERE owner_user_id=? AND id<>?",
			now, prompt.OwnerUserID, prompt.ID)
		if err != nil {
			return nil, err
		}
	}
	return prompt, nil
}

func (s *SQLiteStore) DuplicatePrompt(ctx context.Context, promptID string, ownerUserID int64, name string) (*autoreply.AutoReplyPrompt, error) {
	source, err := s.GetPrompt(ctx, promptID, ownerUserID)
	if err != nil {
		return nil, err
	}
	if source == nil {
		return nil, nil
	}
	clone := source.CopyFor(ownerUserID, name)
	base := clone.Name
	suffix := 1
	for {
		var exists string
		err := s.db.QueryRowContext(ctx, "SELECT id FROM autoreply_prompts WHERE owner_user_id=? AND name=?", clone.OwnerUserID, clone.Name).Scan(&exists)
		if err == sql.ErrNoRows || (err == nil && exists == "") {
			break
		}
		if err != nil && err != sql.ErrNoRows {
			return nil, err
		}
		suffix++
		clone.Name = fmt.Sprintf("%s (%d)", base, suffix)
		if len(clone.Name) > 80 {
			clone.Name = clone.Name[:80]
		}
	}
	return s.SavePrompt(ctx, clone)
}

func (s *SQLiteStore) DeletePrompt(ctx context.Context, promptID string, ownerUserID int64) (bool, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return false, err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	var exists string
	err = tx.QueryRowContext(ctx, "SELECT id FROM autoreply_prompts WHERE id=? AND owner_user_id=?", promptID, ownerUserID).Scan(&exists)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	now := autoreply.NowUnix()
	if _, err := tx.ExecContext(ctx, "DELETE FROM autoreply_prompts WHERE id=? AND owner_user_id=?", promptID, ownerUserID); err != nil {
		return false, err
	}
	if _, err := tx.ExecContext(ctx, "UPDATE autoreply_targets SET prompt_id='', updated_at=? WHERE owner_user_id=? AND prompt_id=?", now, ownerUserID, promptID); err != nil {
		return false, err
	}
	if _, err := tx.ExecContext(ctx, "UPDATE autoreply_rules SET prompt_id='', updated_at=? WHERE owner_user_id=? AND prompt_id=?", now, ownerUserID, promptID); err != nil {
		return false, err
	}
	if _, err := tx.ExecContext(ctx, "UPDATE autoreply_settings SET prompt_id='' WHERE owner_user_id=? AND prompt_id=?", ownerUserID, promptID); err != nil {
		return false, err
	}
	return true, tx.Commit()
}

func (s *SQLiteStore) CountPrompts(ctx context.Context, ownerUserID int64) (int, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return 0, err
	}
	var count int
	err := s.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM autoreply_prompts WHERE owner_user_id=?", ownerUserID).Scan(&count)
	return count, err
}

func (s *SQLiteStore) TouchPromptUsage(ctx context.Context, promptID string, ownerUserID int64) error {
	_, err := s.db.ExecContext(ctx, "UPDATE autoreply_prompts SET use_count=use_count+1, last_used_at=? WHERE id=? AND owner_user_id=?",
		autoreply.NowUnix(), promptID, ownerUserID)
	return err
}

// --- targets ----------------------------------------------------------------

func scanTarget(row *sql.Row) (*autoreply.ContactTarget, error) {
	var t autoreply.ContactTarget
	var promptID sql.NullString
	err := row.Scan(&t.ID, &t.OwnerUserID, &t.ChatID, &t.ChatType, &t.Title, &t.Username,
		&t.IsEnabled, &promptID, &t.Note, &t.Priority, &t.CreatedAt, &t.UpdatedAt)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	t.PromptID = strings.TrimSpace(promptID.String)
	return t.Normalize(), nil
}

func (s *SQLiteStore) GetTargetByChat(ctx context.Context, chatID string, ownerUserID int64) (*autoreply.ContactTarget, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	row := s.db.QueryRowContext(ctx, `SELECT id, owner_user_id, chat_id, chat_type, title, username,
        is_enabled, prompt_id, note, priority, created_at, updated_at
        FROM autoreply_targets WHERE owner_user_id=? AND chat_id=?`,
		ownerUserID, autoreply.NormalizeChatID(chatID))
	return scanTarget(row)
}

func (s *SQLiteStore) ListTargets(ctx context.Context, ownerUserID int64, search string, enabledOnly bool) ([]*autoreply.ContactTarget, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	query := `SELECT id, owner_user_id, chat_id, chat_type, title, username,
        is_enabled, prompt_id, note, priority, created_at, updated_at
        FROM autoreply_targets WHERE owner_user_id=?`
	args := []any{ownerUserID}
	if enabledOnly {
		query += " AND is_enabled=1"
	}
	if strings.TrimSpace(search) != "" {
		query += " AND (title LIKE ? OR username LIKE ? OR chat_id LIKE ? OR note LIKE ?)"
		like := "%" + strings.TrimSpace(search) + "%"
		args = append(args, like, like, like, like)
	}
	query += " ORDER BY is_enabled DESC, priority DESC, title ASC"
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*autoreply.ContactTarget
	for rows.Next() {
		var t autoreply.ContactTarget
		var promptID sql.NullString
		if err := rows.Scan(&t.ID, &t.OwnerUserID, &t.ChatID, &t.ChatType, &t.Title, &t.Username,
			&t.IsEnabled, &promptID, &t.Note, &t.Priority, &t.CreatedAt, &t.UpdatedAt); err != nil {
			return nil, err
		}
		t.PromptID = strings.TrimSpace(promptID.String)
		out = append(out, t.Normalize())
	}
	return out, rows.Err()
}

func (s *SQLiteStore) UpsertTarget(ctx context.Context, target *autoreply.ContactTarget) (*autoreply.ContactTarget, error) {
	if target == nil {
		return nil, ErrChatIDRequired
	}
	if err := requireOwner(target.OwnerUserID); err != nil {
		return nil, err
	}
	target.Normalize()
	if target.ChatID == "" {
		return nil, ErrChatIDRequired
	}
	now := autoreply.NowUnix()
	target.UpdatedAt = now
	if target.CreatedAt == 0 {
		target.CreatedAt = now
	}
	_, err := s.db.ExecContext(ctx, `INSERT INTO autoreply_targets (id, owner_user_id, chat_id, chat_type, title,
        username, is_enabled, prompt_id, note, priority, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(owner_user_id, chat_id) DO UPDATE SET
        title=excluded.title, username=excluded.username, is_enabled=excluded.is_enabled,
        prompt_id=excluded.prompt_id, note=excluded.note, priority=excluded.priority,
        updated_at=excluded.updated_at`,
		target.ID, target.OwnerUserID, target.ChatID, "private", target.Title,
		target.Username, target.IsEnabled, target.PromptID, target.Note, target.Priority,
		target.CreatedAt, target.UpdatedAt)
	if err != nil {
		return nil, err
	}
	return target, nil
}

func (s *SQLiteStore) SetTargetEnabled(ctx context.Context, chatID string, ownerUserID int64, enabled bool) (bool, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return false, err
	}
	res, err := s.db.ExecContext(ctx, "UPDATE autoreply_targets SET is_enabled=?, updated_at=? WHERE owner_user_id=? AND chat_id=?",
		enabled, autoreply.NowUnix(), ownerUserID, autoreply.NormalizeChatID(chatID))
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func (s *SQLiteStore) DeleteTarget(ctx context.Context, chatID string, ownerUserID int64) (bool, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return false, err
	}
	res, err := s.db.ExecContext(ctx, "DELETE FROM autoreply_targets WHERE owner_user_id=? AND chat_id=?",
		ownerUserID, autoreply.NormalizeChatID(chatID))
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func (s *SQLiteStore) BulkSetTargets(ctx context.Context, ownerUserID int64, targets []*autoreply.ContactTarget, replace bool) ([]*autoreply.ContactTarget, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	now := autoreply.NowUnix()
	if replace {
		if _, err := tx.ExecContext(ctx, "DELETE FROM autoreply_targets WHERE owner_user_id=?", ownerUserID); err != nil {
			return nil, err
		}
	}
	for _, target := range targets {
		if target == nil {
			continue
		}
		target.OwnerUserID = ownerUserID
		target.Normalize()
		if target.ChatID == "" {
			continue
		}
		if target.ID == "" {
			target.ID = newHexID()
		}
		if target.CreatedAt == 0 {
			target.CreatedAt = now
		}
		target.UpdatedAt = now
		if _, err := tx.ExecContext(ctx, `INSERT INTO autoreply_targets (id, owner_user_id, chat_id, chat_type,
            title, username, is_enabled, prompt_id, note, priority, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(owner_user_id, chat_id) DO UPDATE SET
            title=excluded.title, username=excluded.username, is_enabled=excluded.is_enabled,
            prompt_id=excluded.prompt_id, note=excluded.note, priority=excluded.priority,
            updated_at=excluded.updated_at`,
			target.ID, ownerUserID, target.ChatID, "private", target.Title, target.Username,
			target.IsEnabled, target.PromptID, target.Note, target.Priority, target.CreatedAt, target.UpdatedAt); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return s.ListTargets(ctx, ownerUserID, "", false)
}

// --- rules ------------------------------------------------------------------

func scanRule(row *sql.Row) (*autoreply.AutoReplyRule, error) {
	var r autoreply.AutoReplyRule
	var tone, respLen, promptID, aiConfigID sql.NullString
	var conditions, exclusions sql.NullString
	err := row.Scan(&r.ID, &r.OwnerUserID, &r.Name, &r.IsEnabled, &r.Priority, &r.Action,
		&r.CooldownSeconds, &promptID, &aiConfigID, &tone, &r.Language, &respLen,
		&conditions, &exclusions, &r.CreatedAt, &r.UpdatedAt)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	r.PromptID = strings.TrimSpace(promptID.String)
	r.AIConfigID = strings.TrimSpace(aiConfigID.String)
	r.Tone = tone.String
	r.ResponseLength = respLen.String
	r.Conditions = mapFromJSON(conditions)
	r.Exclusions = mapFromJSON(exclusions)
	return r.Normalize()
}

func mapFromJSON(raw sql.NullString) map[string]any {
	if !raw.Valid || strings.TrimSpace(raw.String) == "" {
		return make(map[string]any)
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(raw.String), &out); err != nil {
		return make(map[string]any)
	}
	if out == nil {
		return make(map[string]any)
	}
	return out
}

func (s *SQLiteStore) GetRule(ctx context.Context, ruleID string, ownerUserID int64) (*autoreply.AutoReplyRule, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	row := s.db.QueryRowContext(ctx, `SELECT id, owner_user_id, name, is_enabled, priority, action,
        cooldown_seconds, prompt_id, ai_config_id, tone, language, response_length,
        conditions_json, exclusions_json, created_at, updated_at
        FROM autoreply_rules WHERE id=? AND owner_user_id=?`, ruleID, ownerUserID)
	return scanRule(row)
}

func (s *SQLiteStore) ListRules(ctx context.Context, ownerUserID int64) ([]*autoreply.AutoReplyRule, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `SELECT id, owner_user_id, name, is_enabled, priority, action,
        cooldown_seconds, prompt_id, ai_config_id, tone, language, response_length,
        conditions_json, exclusions_json, created_at, updated_at
        FROM autoreply_rules WHERE owner_user_id=? ORDER BY priority DESC, created_at`, ownerUserID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*autoreply.AutoReplyRule
	for rows.Next() {
		var r autoreply.AutoReplyRule
		var tone, respLen, promptID, aiConfigID sql.NullString
		var conditions, exclusions sql.NullString
		if err := rows.Scan(&r.ID, &r.OwnerUserID, &r.Name, &r.IsEnabled, &r.Priority, &r.Action,
			&r.CooldownSeconds, &promptID, &aiConfigID, &tone, &r.Language, &respLen,
			&conditions, &exclusions, &r.CreatedAt, &r.UpdatedAt); err != nil {
			return nil, err
		}
		r.PromptID = strings.TrimSpace(promptID.String)
		r.AIConfigID = strings.TrimSpace(aiConfigID.String)
		r.Tone = tone.String
		r.ResponseLength = respLen.String
		r.Conditions = mapFromJSON(conditions)
		r.Exclusions = mapFromJSON(exclusions)
		out = append(out, &r)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) SaveRule(ctx context.Context, rule *autoreply.AutoReplyRule) (*autoreply.AutoReplyRule, error) {
	if rule == nil {
		return nil, ErrNameRequired
	}
	if err := requireOwner(rule.OwnerUserID); err != nil {
		return nil, err
	}
	rule, err := rule.Normalize()
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(rule.Name) == "" {
		return nil, ErrNameRequired
	}
	now := autoreply.NowUnix()
	rule.UpdatedAt = now
	if rule.CreatedAt == 0 {
		rule.CreatedAt = now
	}
	_, err = s.db.ExecContext(ctx, `INSERT INTO autoreply_rules (id, owner_user_id, name, is_enabled, priority,
        action, cooldown_seconds, prompt_id, ai_config_id, tone, language, response_length,
        conditions_json, exclusions_json, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
        name=excluded.name, is_enabled=excluded.is_enabled, priority=excluded.priority,
        action=excluded.action, cooldown_seconds=excluded.cooldown_seconds,
        prompt_id=excluded.prompt_id, ai_config_id=excluded.ai_config_id, tone=excluded.tone,
        language=excluded.language, response_length=excluded.response_length,
        conditions_json=excluded.conditions_json, exclusions_json=excluded.exclusions_json,
        updated_at=excluded.updated_at`,
		rule.ID, rule.OwnerUserID, rule.Name, rule.IsEnabled, rule.Priority,
		rule.Action, rule.CooldownSeconds, rule.PromptID, rule.AIConfigID, rule.Tone,
		rule.Language, rule.ResponseLength, toJSON(rule.Conditions), toJSON(rule.Exclusions),
		rule.CreatedAt, rule.UpdatedAt)
	if err != nil {
		return nil, err
	}
	return rule, nil
}

func (s *SQLiteStore) DeleteRule(ctx context.Context, ruleID string, ownerUserID int64) (bool, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return false, err
	}
	res, err := s.db.ExecContext(ctx, "DELETE FROM autoreply_rules WHERE id=? AND owner_user_id=?", ruleID, ownerUserID)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

// --- conversation context ---------------------------------------------------

func (s *SQLiteStore) AppendMessage(ctx context.Context, ownerUserID int64, chatID, role, content string, messageID int64) error {
	if err := requireOwner(ownerUserID); err != nil {
		return err
	}
	chat := autoreply.NormalizeChatID(chatID)
	if chat == "" {
		return nil
	}
	now := autoreply.NowUnix()
	roleLower := strings.ToLower(strings.TrimSpace(role))
	if len(roleLower) > 16 {
		roleLower = roleLower[:16]
	}
	if roleLower == "" {
		roleLower = "user"
	}
	if len(content) > autoreply.MaxTextLength {
		content = content[:autoreply.MaxTextLength]
	}
	_, err := s.db.ExecContext(ctx, `INSERT INTO autoreply_messages (id, owner_user_id, chat_id, role, content, message_id, created_at)
        VALUES (?,?,?,?,?,?,?)`,
		newHexID(), ownerUserID, chat, roleLower, content, messageID, now)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `DELETE FROM autoreply_messages WHERE owner_user_id=? AND chat_id=? AND id NOT IN
        (SELECT id FROM autoreply_messages WHERE owner_user_id=? AND chat_id=?
         ORDER BY created_at DESC, rowid DESC LIMIT ?)`,
		ownerUserID, chat, ownerUserID, chat, MaxContextRows)
	return err
}

func (s *SQLiteStore) ListMessages(ctx context.Context, ownerUserID int64, chatID string, limit int) ([]autoreply.MessageHistory, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	chat := autoreply.NormalizeChatID(chatID)
	if chat == "" {
		return nil, nil
	}
	if limit < 1 {
		limit = 10
	}
	if limit > MaxContextRows {
		limit = MaxContextRows
	}
	rows, err := s.db.QueryContext(ctx, `SELECT role, content FROM autoreply_messages
        WHERE owner_user_id=? AND chat_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?`,
		ownerUserID, chat, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []autoreply.MessageHistory
	for rows.Next() {
		var item autoreply.MessageHistory
		if err := rows.Scan(&item.Role, &item.Content); err != nil {
			return nil, err
		}
		out = append(out, item)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// reverse to oldest-first
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out, nil
}

func (s *SQLiteStore) CountMessages(ctx context.Context, ownerUserID int64, chatID string) (int, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return 0, err
	}
	chat := autoreply.NormalizeChatID(chatID)
	if chat == "" {
		return 0, nil
	}
	var count int
	err := s.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM autoreply_messages WHERE owner_user_id=? AND chat_id=?",
		ownerUserID, chat).Scan(&count)
	return count, err
}

func (s *SQLiteStore) ClearMessages(ctx context.Context, ownerUserID int64, chatID string) (int, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return 0, err
	}
	chat := autoreply.NormalizeChatID(chatID)
	if chat == "" {
		return 0, nil
	}
	res, err := s.db.ExecContext(ctx, "DELETE FROM autoreply_messages WHERE owner_user_id=? AND chat_id=?",
		ownerUserID, chat)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

func (s *SQLiteStore) CleanupMessages(ctx context.Context, maxAgeSeconds int64) (int, error) {
	if maxAgeSeconds <= 0 {
		return 0, nil
	}
	cutoff := autoreply.NowUnix() - maxAgeSeconds
	res, err := s.db.ExecContext(ctx, "DELETE FROM autoreply_messages WHERE created_at<?", cutoff)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

// --- memory -----------------------------------------------------------------

func (s *SQLiteStore) Remember(ctx context.Context, ownerUserID int64, chatID, content string, kind autoreply.MemoryKind, ttlSeconds int64) (string, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return "", err
	}
	chat := autoreply.NormalizeChatID(chatID)
	text := strings.TrimSpace(content)
	if chat == "" || text == "" {
		return "", nil
	}
	if len(text) > 1200 {
		text = text[:1200]
	}
	kindStr := string(kind)
	if kindStr == "" {
		kindStr = string(autoreply.MemoryKindFact)
	}
	if len(kindStr) > 16 {
		kindStr = kindStr[:16]
	}
	now := autoreply.NowUnix()
	expiresAt := int64(0)
	if ttlSeconds > 0 {
		expiresAt = now + ttlSeconds
	}
	rowID := newHexID()
	_, err := s.db.ExecContext(ctx, `INSERT INTO autoreply_memory (id, owner_user_id, chat_id, kind, content,
        content_hash, created_at, updated_at, expires_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(owner_user_id, chat_id, content_hash) DO UPDATE SET
        updated_at=excluded.updated_at, expires_at=excluded.expires_at, kind=excluded.kind`,
		rowID, ownerUserID, chat, kindStr, text, memoryHash(ownerUserID, chat, text), now, now, expiresAt)
	if err != nil {
		return "", err
	}
	_, err = s.db.ExecContext(ctx, `DELETE FROM autoreply_memory WHERE owner_user_id=? AND chat_id=? AND id NOT IN
        (SELECT id FROM autoreply_memory WHERE owner_user_id=? AND chat_id=?
         ORDER BY updated_at DESC LIMIT ?)`,
		ownerUserID, chat, ownerUserID, chat, MaxMemoryRows)
	if err != nil {
		return "", err
	}
	return rowID, nil
}

func (s *SQLiteStore) ListMemory(ctx context.Context, ownerUserID int64, chatID string, limit int) ([]autoreply.MemoryFact, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	chat := autoreply.NormalizeChatID(chatID)
	if chat == "" {
		return nil, nil
	}
	if limit < 1 {
		limit = 12
	}
	if limit > MaxMemoryRows {
		limit = MaxMemoryRows
	}
	now := autoreply.NowUnix()
	rows, err := s.db.QueryContext(ctx, `SELECT id, kind, content, updated_at, expires_at FROM autoreply_memory
        WHERE owner_user_id=? AND chat_id=? AND (expires_at=0 OR expires_at>?)
        ORDER BY updated_at DESC LIMIT ?`, ownerUserID, chat, now, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []autoreply.MemoryFact
	for rows.Next() {
		var fact autoreply.MemoryFact
		if err := rows.Scan(&fact.ID, &fact.Kind, &fact.Content, &fact.UpdatedAt, &fact.ExpiresAt); err != nil {
			return nil, err
		}
		out = append(out, fact)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) Forget(ctx context.Context, ownerUserID int64, chatID, memoryID string) (int, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return 0, err
	}
	chat := autoreply.NormalizeChatID(chatID)
	if chat == "" {
		return 0, nil
	}
	var res sql.Result
	var err error
	if memoryID != "" {
		res, err = s.db.ExecContext(ctx, "DELETE FROM autoreply_memory WHERE owner_user_id=? AND chat_id=? AND id=?",
			ownerUserID, chat, memoryID)
	} else {
		res, err = s.db.ExecContext(ctx, "DELETE FROM autoreply_memory WHERE owner_user_id=? AND chat_id=?",
			ownerUserID, chat)
	}
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

func (s *SQLiteStore) CleanupExpiredMemory(ctx context.Context, now int64) (int, error) {
	res, err := s.db.ExecContext(ctx, "DELETE FROM autoreply_memory WHERE expires_at>0 AND expires_at<=?", now)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

// --- durable queue jobs ----------------------------------------------------

func scanJob(scanner interface {
	Scan(dest ...any) error
}) (*autoreply.AutoReplyJob, error) {
	var j autoreply.AutoReplyJob
	var promptID, ruleID, aiConfigID sql.NullString
	var sentMsgID sql.NullInt64
	var statusStr string
	err := scanner.Scan(
		&j.ID, &j.OwnerUserID, &j.SessionID, &j.ChatID, &j.ChatTitle,
		&j.SourceMessageID, &statusStr, &j.Attempt, &j.MaxAttempts,
		&j.NextAttemptAt, &j.LeaseToken, &j.LeaseUntil, &promptID,
		&ruleID, &aiConfigID, &j.InboundText, &j.ReplyText, &j.Error,
		&j.ErrorCode, &j.CorrelationID, &j.LatencyMS, &sentMsgID,
		&j.CreatedAt, &j.UpdatedAt, &j.SentAt,
	)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	j.Status = autoreply.JobStatus(statusStr)
	j.PromptID = strings.TrimSpace(promptID.String)
	j.RuleID = strings.TrimSpace(ruleID.String)
	j.AIConfigID = strings.TrimSpace(aiConfigID.String)
	if sentMsgID.Valid {
		j.SentMessageID = sentMsgID.Int64
	}
	return &j, nil
}

func (s *SQLiteStore) Enqueue(ctx context.Context, job *autoreply.AutoReplyJob) (bool, *autoreply.AutoReplyJob, error) {
	if job == nil {
		return false, nil, ErrChatIDRequired
	}
	if err := requireOwner(job.OwnerUserID); err != nil {
		return false, nil, err
	}
	job.ChatID = autoreply.NormalizeChatID(job.ChatID)
	if job.ChatID == "" {
		return false, nil, ErrChatIDRequired
	}
	if job.ID == "" {
		job.ID = autoreply.NewID()
	}
	now := autoreply.NowUnix()
	if job.CreatedAt == 0 {
		job.CreatedAt = now
	}
	job.UpdatedAt = now
	if job.Status == "" {
		job.Status = autoreply.JobStatusPending
	}
	if job.MaxAttempts <= 0 {
		job.MaxAttempts = 3
	}

	_, err := s.db.ExecContext(ctx, `INSERT INTO autoreply_jobs (
        id, owner_user_id, session_id, chat_id, chat_title,
        source_message_id, status, attempt, max_attempts,
        next_attempt_at, lease_token, lease_until, prompt_id,
        rule_id, ai_config_id, inbound_text, reply_text, error,
        error_code, correlation_id, latency_ms, sent_message_id,
        created_at, updated_at, sent_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		job.ID, job.OwnerUserID, job.SessionID, job.ChatID, job.ChatTitle,
		job.SourceMessageID, string(job.Status), job.Attempt, job.MaxAttempts,
		job.NextAttemptAt, job.LeaseToken, job.LeaseUntil, job.PromptID,
		job.RuleID, job.AIConfigID, job.InboundText, job.ReplyText, job.Error,
		job.ErrorCode, job.CorrelationID, job.LatencyMS, job.SentMessageID,
		job.CreatedAt, job.UpdatedAt, job.SentAt,
	)
	if err != nil {
		if strings.Contains(strings.ToUpper(err.Error()), "UNIQUE") {
			row := s.db.QueryRowContext(ctx, `SELECT
                id, owner_user_id, session_id, chat_id, chat_title,
                source_message_id, status, attempt, max_attempts,
                next_attempt_at, lease_token, lease_until, prompt_id,
                rule_id, ai_config_id, inbound_text, reply_text, error,
                error_code, correlation_id, latency_ms, sent_message_id,
                created_at, updated_at, sent_at
                FROM autoreply_jobs WHERE owner_user_id=? AND session_id=? AND chat_id=? AND source_message_id=?`,
				job.OwnerUserID, job.SessionID, job.ChatID, job.SourceMessageID)
			existing, scanErr := scanJob(row)
			if scanErr == nil && existing != nil {
				return false, existing, nil
			}
			return false, nil, nil
		}
		return false, nil, err
	}
	return true, job, nil
}

func (s *SQLiteStore) ClaimNext(ctx context.Context, ownerUserID int64, workerID string, now int64, leaseSeconds int) (*autoreply.AutoReplyJob, error) {
	if leaseSeconds <= 0 {
		leaseSeconds = LeaseSeconds
	}
	token := newHexID() + newHexID()
	query := `SELECT j.id FROM autoreply_jobs j
        WHERE j.status IN ('PENDING','RETRY_WAIT')
        AND j.next_attempt_at<=? AND (j.lease_until=0 OR j.lease_until<?)
        AND NOT EXISTS (
            SELECT 1 FROM autoreply_jobs e
            WHERE e.owner_user_id=j.owner_user_id AND e.session_id=j.session_id
              AND e.chat_id=j.chat_id AND e.status IN ('PENDING','RETRY_WAIT','RUNNING')
              AND (e.created_at<j.created_at OR (e.created_at=j.created_at AND e.source_message_id<j.source_message_id))
        )`
	args := []any{now, now}
	if ownerUserID > 0 {
		query += " AND j.owner_user_id=?"
		args = append(args, ownerUserID)
	}
	query += " ORDER BY j.created_at, j.source_message_id LIMIT 1"

	var jobID string
	err := s.db.QueryRowContext(ctx, query, args...).Scan(&jobID)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	res, err := s.db.ExecContext(ctx, `UPDATE autoreply_jobs SET status='RUNNING', lease_token=?, lease_until=?, updated_at=?
        WHERE id=? AND status IN ('PENDING','RETRY_WAIT')`,
		token, now+int64(leaseSeconds), now, jobID)
	if err != nil {
		return nil, err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return nil, nil // lost race
	}

	row := s.db.QueryRowContext(ctx, `SELECT
        id, owner_user_id, session_id, chat_id, chat_title,
        source_message_id, status, attempt, max_attempts,
        next_attempt_at, lease_token, lease_until, prompt_id,
        rule_id, ai_config_id, inbound_text, reply_text, error,
        error_code, correlation_id, latency_ms, sent_message_id,
        created_at, updated_at, sent_at
        FROM autoreply_jobs WHERE id=?`, jobID)
	job, err := scanJob(row)
	if err != nil {
		return nil, err
	}
	if job != nil {
		job.LeaseToken = token
	}
	return job, nil
}

func (s *SQLiteStore) CompleteSent(ctx context.Context, jobID, leaseToken string, sentMessageID int64, replyText string, latencyMS int) (bool, error) {
	now := autoreply.NowUnix()
	if len(replyText) > 4000 {
		replyText = replyText[:4000]
	}
	res, err := s.db.ExecContext(ctx, `UPDATE autoreply_jobs SET status='SENT', sent_message_id=?, reply_text=?,
        latency_ms=?, sent_at=?, updated_at=?, error='', error_code=''
        WHERE id=? AND lease_token=? AND status='RUNNING'`,
		sentMessageID, replyText, latencyMS, now, now, jobID, leaseToken)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func (s *SQLiteStore) MarkRetry(ctx context.Context, jobID, leaseToken, errStr, errCode string, backoffSeconds float64, attempt int) (bool, error) {
	now := autoreply.NowUnix()
	nextAt := now + int64(clampFloat(backoffSeconds, 1.0, 86400.0, 20.0))
	if len(errStr) > 500 {
		errStr = errStr[:500]
	}
	if len(errCode) > 64 {
		errCode = errCode[:64]
	}
	res, err := s.db.ExecContext(ctx, `UPDATE autoreply_jobs SET status='RETRY_WAIT', attempt=?, error=?, error_code=?,
        next_attempt_at=?, lease_token='', lease_until=0, updated_at=?
        WHERE id=? AND lease_token=? AND status='RUNNING'`,
		attempt, errStr, errCode, nextAt, now, jobID, leaseToken)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func (s *SQLiteStore) MarkFailed(ctx context.Context, jobID, leaseToken, errStr, errCode string, deadLetter bool) (bool, error) {
	status := string(autoreply.JobStatusFailed)
	if deadLetter {
		status = string(autoreply.JobStatusDeadLetter)
	}
	if len(errStr) > 500 {
		errStr = errStr[:500]
	}
	if len(errCode) > 64 {
		errCode = errCode[:64]
	}
	res, err := s.db.ExecContext(ctx, `UPDATE autoreply_jobs SET status=?, error=?, error_code=?, lease_token='',
        lease_until=0, updated_at=? WHERE id=? AND lease_token=? AND status='RUNNING'`,
		status, errStr, errCode, autoreply.NowUnix(), jobID, leaseToken)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func (s *SQLiteStore) MarkSkipped(ctx context.Context, jobID, reason string) (bool, error) {
	if len(reason) > 300 {
		reason = reason[:300]
	}
	res, err := s.db.ExecContext(ctx, `UPDATE autoreply_jobs SET status='SKIPPED', error=?, updated_at=?
        WHERE id=? AND status IN ('PENDING','RETRY_WAIT')`,
		reason, autoreply.NowUnix(), jobID)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func (s *SQLiteStore) Cancel(ctx context.Context, jobID string, ownerUserID int64) (bool, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return false, err
	}
	res, err := s.db.ExecContext(ctx, `UPDATE autoreply_jobs SET status='CANCELLED', updated_at=?
        WHERE id=? AND owner_user_id=? AND status IN ('PENDING','RETRY_WAIT','RUNNING')`,
		autoreply.NowUnix(), jobID, ownerUserID)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func (s *SQLiteStore) Replay(ctx context.Context, jobID string, ownerUserID int64) (bool, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return false, err
	}
	now := autoreply.NowUnix()
	res, err := s.db.ExecContext(ctx, `UPDATE autoreply_jobs SET status='PENDING', attempt=0, error='', error_code='',
        next_attempt_at=?, lease_token='', lease_until=0, updated_at=?
        WHERE id=? AND owner_user_id=? AND status IN ('DEAD_LETTER','FAILED')`,
		now, now, jobID, ownerUserID)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func (s *SQLiteStore) RecoverExpiredLeases(ctx context.Context, now int64) (int, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	if _, err := tx.ExecContext(ctx, `UPDATE autoreply_jobs SET attempt=attempt+1, updated_at=?
        WHERE status='RUNNING' AND lease_until>0 AND lease_until<?`, now, now); err != nil {
		return 0, err
	}

	if _, err := tx.ExecContext(ctx, `UPDATE autoreply_jobs SET status='DEAD_LETTER', lease_token='', lease_until=0,
        error='worker_lost', error_code='lease_expired', updated_at=?
        WHERE status='RUNNING' AND lease_until>0 AND lease_until<? AND attempt>=max_attempts`, now, now); err != nil {
		return 0, err
	}

	res, err := tx.ExecContext(ctx, `UPDATE autoreply_jobs SET status='RETRY_WAIT', lease_token='', lease_until=0,
        next_attempt_at=?, updated_at=?
        WHERE status='RUNNING' AND lease_until>0 AND lease_until<?`, now, now, now)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return int(n), nil
}

func (s *SQLiteStore) ListJobs(ctx context.Context, ownerUserID int64, status, chatID string, limit, offset int) ([]*autoreply.AutoReplyJob, int, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, 0, err
	}
	if limit < 1 {
		limit = 50
	}
	if limit > 200 {
		limit = 200
	}
	if offset < 0 {
		offset = 0
	}
	where := "WHERE owner_user_id=?"
	args := []any{ownerUserID}
	if status != "" {
		where += " AND status=?"
		args = append(args, strings.ToUpper(status))
	}
	if chatID != "" {
		where += " AND chat_id=?"
		args = append(args, autoreply.NormalizeChatID(chatID))
	}

	var total int
	countQuery := "SELECT COUNT(*) FROM autoreply_jobs " + where
	if err := s.db.QueryRowContext(ctx, countQuery, args...).Scan(&total); err != nil {
		return nil, 0, err
	}

	query := `SELECT id, owner_user_id, session_id, chat_id, chat_title,
        source_message_id, status, attempt, max_attempts,
        next_attempt_at, lease_token, lease_until, prompt_id,
        rule_id, ai_config_id, inbound_text, reply_text, error,
        error_code, correlation_id, latency_ms, sent_message_id,
        created_at, updated_at, sent_at FROM autoreply_jobs ` + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
	listArgs := append(args, limit, offset)
	rows, err := s.db.QueryContext(ctx, query, listArgs...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()

	var out []*autoreply.AutoReplyJob
	for rows.Next() {
		job, err := scanJob(rows)
		if err != nil {
			return nil, 0, err
		}
		out = append(out, job)
	}
	return out, total, rows.Err()
}

func (s *SQLiteStore) ListDeadLetter(ctx context.Context, ownerUserID int64, limit, offset int) ([]*autoreply.AutoReplyJob, int, error) {
	return s.ListJobs(ctx, ownerUserID, string(autoreply.JobStatusDeadLetter), "", limit, offset)
}

func (s *SQLiteStore) GetJob(ctx context.Context, jobID string, ownerUserID int64) (*autoreply.AutoReplyJob, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	row := s.db.QueryRowContext(ctx, `SELECT
        id, owner_user_id, session_id, chat_id, chat_title,
        source_message_id, status, attempt, max_attempts,
        next_attempt_at, lease_token, lease_until, prompt_id,
        rule_id, ai_config_id, inbound_text, reply_text, error,
        error_code, correlation_id, latency_ms, sent_message_id,
        created_at, updated_at, sent_at
        FROM autoreply_jobs WHERE id=? AND owner_user_id=?`, jobID, ownerUserID)
	return scanJob(row)
}

func (s *SQLiteStore) PendingCounts(ctx context.Context, ownerUserID int64) (map[string]int, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, "SELECT status, COUNT(*) FROM autoreply_jobs WHERE owner_user_id=? GROUP BY status", ownerUserID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]int)
	total := 0
	for rows.Next() {
		var status string
		var count int
		if err := rows.Scan(&status, &count); err != nil {
			return nil, err
		}
		out[status] = count
		total += count
	}
	out["TOTAL"] = total
	return out, rows.Err()
}

func (s *SQLiteStore) RecentSends(ctx context.Context, ownerUserID int64, chatID string, sinceTS int64) (int, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return 0, err
	}
	chat := autoreply.NormalizeChatID(chatID)
	if chat == "" {
		return 0, nil
	}
	var count int
	err := s.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM autoreply_jobs WHERE owner_user_id=? AND chat_id=? AND status='SENT' AND sent_at>=?",
		ownerUserID, chat, sinceTS).Scan(&count)
	return count, err
}

func (s *SQLiteStore) LastSentAt(ctx context.Context, ownerUserID int64, chatID string) (int64, error) {
	if err := requireOwner(ownerUserID); err != nil {
		return 0, err
	}
	chat := autoreply.NormalizeChatID(chatID)
	if chat == "" {
		return 0, nil
	}
	var ts sql.NullInt64
	err := s.db.QueryRowContext(ctx, "SELECT MAX(sent_at) FROM autoreply_jobs WHERE owner_user_id=? AND chat_id=? AND status='SENT'",
		ownerUserID, chat).Scan(&ts)
	if err != nil {
		return 0, err
	}
	return ts.Int64, nil
}

func (s *SQLiteStore) CleanupTerminal(ctx context.Context, retentionSeconds int64) (int, error) {
	if retentionSeconds <= 0 {
		return 0, nil
	}
	cutoff := autoreply.NowUnix() - retentionSeconds
	res, err := s.db.ExecContext(ctx, "DELETE FROM autoreply_jobs WHERE status IN ('SENT','FAILED','SKIPPED','CANCELLED') AND updated_at<?", cutoff)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

var _ = fromJSON

