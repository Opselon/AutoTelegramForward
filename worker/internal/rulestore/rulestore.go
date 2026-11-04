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
	"log/slog"
	"strconv"
	"strings"
	"time"

	"atf/worker/internal/domain/forwardinterval"
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
//
// The concrete *Store (not the rule.Store interface) is returned so callers
// that own the process lifecycle can Close it; the compile-time assertion in
// drivers.go still guarantees the interface is satisfied.
func New(ctx context.Context, cfg Config) (*Store, error) {
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
	f, err := s.getFilter(ctx, ext.FilterRuleID, ext.ForwardRule.OwnerUserID)
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
// Stage 3 note: several fields the hop/transport decision needs
// (delivery_transport, bot_token_id, album_mode, topic_id,
// inline_buttons_raw) are NOT columns — the Core's ForwardRule entity
// resolves them out of the metadata JSON blob, so the worker must do the
// same or it would be reading columns that do not exist on either backend.
//
// Stage 5 (W4) note: the AI execution pair is the mirror case. The four
// fields ai_fallback_policy, ai_prompt_version, ai_timeout_seconds and
// ai_secondary_config_id ARE real forward_rules columns on both backends
// (SQLite v1.1 migration, database.py:333-336; the PostgreSQL loader
// reads them at postgres_repositories.py:671-678), so they are read as
// columns. The prompt_id / ai_creativity / ai_max_tokens triple is NOT a
// column anywhere: it lives in the task's metadata blob (entities.py:1629
// / 1644 read it there), so those are projected from the blob through
// the same accessor the Python entity uses — the same split the
// transport fields above already follow.
//
// The placeholders are written for PostgreSQL ($1); the SQLite reader rewrites
// them through rulesQuery before the statement ever executes, so one verified
// projection serves both engines.
//
// Stage 4.5 W1: source_chat_id / source_chat_name / target_chat_id /
// target_chat_ids are the routing columns. source_chat_id, source_chat_name
// and target_chat_id are NOT NULL on both backends (the SQLite v1 DDL and
// the PostgreSQL DDL_SCHEMA declare all three); target_chat_ids is a
// nullable JSONB list on PostgreSQL and a NOT NULL DEFAULT '[]' TEXT list
// on SQLite, so it is read through a NullString and an empty/invalid blob
// means "no extra targets" — the same answer the Python _safe_list
// fallback gives. The extra *sources* are NOT a column: they live in
// detection_criteria["source_chat_ids"], which the projection already
// decodes.
const forwardProjection = `
	SELECT id, session_id, is_active, is_paused, paused_until,
	       forward_mode, content_mode, filter_rule_id, metadata,
	       owner_user_id, fallback_enabled, intermediate_channel_id,
	       use_intermediate, message_category, detection_criteria,
	       split_long_caption, ai_config_id,
	       source_chat_id, source_chat_name, target_chat_id, target_chat_ids,
	       ai_secondary_config_id, ai_fallback_policy,
	       ai_prompt_version, ai_timeout_seconds
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
	// MetadataJSON is the raw metadata blob, kept verbatim because JSON
	// object key order is part of the replacements contract (Python dict
	// insertion order decides tie-breaks) and Go's map drops it.
	MetadataJSON string
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

		// Stage 3: transport + branding hop columns. All nullable through
		// Null* types because an older database (or a PostgreSQL schema that
		// predates the column) must still read, not crash: a missing column
		// is the "feature off" default, exactly as the Python side treats it.
		ownerUserID        sql.NullInt64
		fallbackEnabled    sql.NullBool
		intermediateChatID sql.NullString
		useIntermediate    sql.NullBool
		messageCategory    sql.NullString
		detectionCriteria  sql.NullString
		splitLongCaption   sql.NullBool
		aiConfigID         sql.NullString

		// Stage 4.5 W1: the routing columns. source_chat_id and
		// target_chat_id are NOT NULL on both backends but are still read
		// through NullString: a row written before the columns existed
		// (SQLite ALTER TABLE adds them with a default, PostgreSQL has
		// them since the v1 DDL) would otherwise break the whole
		// projection instead of degrading to the "no routing configured"
		// default.
		sourceChatID   sql.NullString
		sourceChatName sql.NullString
		targetChatID   sql.NullString
		targetChatIDs  sql.NullString

		// Stage 5 (W4): the AI execution pair. All four are real
		// forward_rules columns on both backends (database.py:333-336,
		// and the PostgreSQL DDL the loader reads them from), so they
		// are read as columns and not out of the metadata blob. All are
		// nullable-or-defaulted: ai_secondary_config_id is nullable,
		// and the other three carry NOT NULL DEFAULTs ('DROP', 0, 15.0)
		// that an older or hand-edited row may still have as NULL, so
		// the Null* readers degrade to the feature's own defaults
		// rather than failing the whole projection.
		aiSecondaryID    sql.NullString
		aiFallbackPolicy sql.NullString
		aiPromptVersion  sql.NullInt64
		aiTimeoutSeconds sql.NullFloat64
	)
	if err := rows.Scan(
		&id, &sessionID, &isActive, &isPaused, &pausedUntil,
		&forwardMode, &contentMode, &filterRuleID, &metadataJSON,
		&ownerUserID, &fallbackEnabled, &intermediateChatID,
		&useIntermediate, &messageCategory, &detectionCriteria,
		&splitLongCaption, &aiConfigID,
		&sourceChatID, &sourceChatName, &targetChatID, &targetChatIDs,
		&aiSecondaryID, &aiFallbackPolicy, &aiPromptVersion, &aiTimeoutSeconds,
	); err != nil {
		return nil, fmt.Errorf("scan forward_rules %s: %w", ruleID, err)
	}

	// FallbackEnabled defaults ON: the Python payload accessor treats a
	// missing key as "older job, bypass allowed", and the rule read must
	// agree or the two engines diverge on the same task.
	fallback := true
	if fallbackEnabled.Valid {
		fallback = fallbackEnabled.Bool
	}

	extra := &extendedForwardRule{ForwardRule: &rule.ForwardRule{
		ID:              id,
		SessionID:       sessionID,
		IsActive:        isActive,
		IsPaused:        isPaused,
		PausedUntil:     pausedUntil,
		ForwardMode:     forwardMode.String,
		ContentMode:     contentMode.String,
		OwnerUserID:     ownerUserID.Int64,
		FallbackEnabled: fallback,
		// Branding hop columns. Every one degrades to the pre-feature default
		// when the column is absent/empty, so an older database behaves
		// exactly as before.
		IntermediateChannelID: strings.TrimSpace(intermediateChatID.String),
		UseIntermediate:       useIntermediate.Bool,
		MessageCategory:       strings.ToUpper(strings.TrimSpace(messageCategory.String)),
		SplitLongCaption:      splitLongCaption.Bool,
		// Stage 4.4: AI transform. ai_config_id is a real column; a NULL or
		// empty value means "no AI transform configured".
		AIConfigID: strings.TrimSpace(aiConfigID.String),
		// Stage 4.5 W1: routing columns. The Python ForwardRule builds
		// these same values out of the same columns
		// (sqlite_repositories.py:617, postgres_repositories.py:682):
		// source_chat_id/target_chat_id from their NOT NULL columns and
		// target_chat_ids from the JSON list column. source_chat_name is
		// a real column on both backends — NOT a metadata key, unlike
		// name/description — so it is read from the column the Python
		// loader reads it from, never out of the blob.
		SourceChatID:   strings.TrimSpace(sourceChatID.String),
		SourceChatName: strings.TrimSpace(sourceChatName.String),
		TargetChatID:   strings.TrimSpace(targetChatID.String),
		TargetChatIDs:  decodeTargetChatIDs(targetChatIDs.String),

		// Stage 5 (W4): the AI execution pair, read from the columns the
		// Python loader reads (postgres_repositories.py:671-678,
		// sqlite_repositories.py:420-423). The fallback policy goes
		// through the resolver's fail-closed default, so an illegible
		// value can never become "forward the untransformed text". The
		// timeout degrades to the Python entity's own default
		// (entities.py:429: 15.0), never 0 — a zero budget would make
		// every rewrite a TIMEOUT.
		AISecondaryConfigID: strings.TrimSpace(aiSecondaryID.String),
		AIFallbackPolicy:    rule.ResolveAIFallbackPolicy(aiFallbackPolicy.String),
		AIPromptVersion:     int(aiPromptVersion.Int64),
		AITimeoutSeconds:    aiTimeoutSeconds.Float64,
	}, FilterRuleID: filterRuleID.String}

	if metadataJSON.Valid && metadataJSON.String != "" {
		var meta map[string]any
		if err := json.Unmarshal([]byte(metadataJSON.String), &meta); err == nil {
			extra.Metadata = meta
			// Stage 4.2: the raw blob is kept because JSON object key
			// order is load-bearing for the replacements map (Python's
			// dict preserves insertion order and text_replace applies
			// same-priority entries in it). Go's map[string]any loses
			// that order at decode time, so the ordered re-decode in
			// transformConfigFromMetadata reads this string instead.
			extra.MetadataJSON = metadataJSON.String
		}
	}

	// detection_criteria is a JSONB column: the hop decision's VIP/origin
	// gate reads it as a map. A missing/invalid blob means "no criteria
	// configured", the same answer Python's vip_criteria_configured gives.
	if detectionCriteria.Valid && detectionCriteria.String != "" {
		var criteria map[string]any
		if err := json.Unmarshal([]byte(detectionCriteria.String), &criteria); err == nil {
			extra.DetectionCriteria = criteria
		}
	}

	// The transport and per-task shape fields are NOT columns: the Core's
	// ForwardRule entity resolves them out of the metadata JSON blob
	// (delivery_transport, bot_token_id, album_mode, topic_id,
	// intermediate_mode, custom_caption_template). Querying them as columns
	// would read names neither backend's schema defines, so the projection
	// reads the blob the Python entity reads.
	if extra.Metadata != nil {
		extra.DeliveryTransport = strings.ToUpper(strings.TrimSpace(metaStr(extra.Metadata, "delivery_transport", "USER")))
		extra.BotTokenID = metaStr(extra.Metadata, "bot_token_id", "")
		extra.IntermediateMode = strings.ToUpper(strings.TrimSpace(metaStr(extra.Metadata, "intermediate_mode", "NONE")))
		extra.AlbumMode = strings.ToLower(strings.TrimSpace(metaStr(extra.Metadata, "album_mode", "album")))
		extra.TopicID = metaInt64(extra.Metadata, "topic_id", 0)
		extra.CustomCaptionTemplate = metaStr(extra.Metadata, "custom_caption_template", "")
		// block_links is a task-level flag with no column on either table:
		// the Python ForwardRule entity resolves it out of the metadata blob
		// (entities.py: block_links = bool(metadata.get("block_links", False))),
		// and the pipeline gates on it via media_filter.link_block_reason
		// (pipeline.py:957) whether or not the task has an attached filter.
		// Projecting it here is what lets the delivery preflight enforce the
		// same gate the Python pipeline already enforced at ingest.
		extra.BlockLinks = metaBool(extra.Metadata, "block_links", false)

		// Stage 4.4: ai_rewrite_active (entities.py:1681) — the single
		// authority for "does this task rewrite text with an LLM?".
		extra.ForwardRule.AIRewriteActive = aiRewriteActive(extra.AIConfigID, extra.Metadata)

		// Stage 4.4: the task's display name lives in the metadata blob
		// (sqlite_repositories.py loads it as name=str(meta.get("name"))).
		// The header placeholder renderer reads it for {rule_name}.
		extra.Name = metaStr(extra.Metadata, "name", "")

		// Stage 4.2: the transform configuration the Python pipeline resolves
		// out of the same blob in _apply_transforms. The Worker re-runs the
		// transform pipeline on the RAW payload text when it owns the
		// transform stage (ATF_WORKER_TRANSFORMS), so it must read the exact
		// view the Python ForwardRule exposes — including the merged
		// cleaner_config + legacy flag resolution Python's build_cleaner()
		// performs, which is why this call is not a plain key lookup.
		extra.Transform = transformConfigFromMetadata(extra.Metadata, extra.MetadataJSON)

		// Stage 4.5 (W2): forward_every_n is resolved out of the same blob
		// the Python entity reads (entities.py::forward_every_n), through
		// the SAME clamp and not a raw key lookup, so a hand-edited row or a
		// float from JSON can never become a 0 or negative stride on the Go
		// side while Python reads 1. The value is 1 (inert) for every task
		// that never configured the feature, which keeps old rows
		// byte-for-byte alike.
		extra.ForwardRule.ForwardEveryN = forwardinterval.Clamp(extra.Metadata["forward_every_n"])

		// Stage 5 (W4): the AI prompt Tailoring triple — prompt_id,
		// ai_creativity, ai_max_tokens — is metadata-only on both backends
		// (entities.py:1629/1644 read ai_creativity and ai_max_tokens out
		// of the blob; prompt_id is the prompt-template reference
		// pipeline.py:1724 reads out of the same blob). Never a column, so
		// never read as one: a SELECT that named them would fail on both
		// backends. The Worker has no prompt-template service, so prompt_id
		// is carried for attribution alone and the prompt pair itself is
		// read off the AI config row, exactly as the Python pipeline does
		// when no prompt_id is configured.
		extra.ForwardRule.AIPromptID = metaStr(extra.Metadata, "prompt_id", "")
		policyBlob, _ := extra.Metadata["content_policy"].(map[string]any)
		extra.ForwardRule.TargetPromptOverrides = buildTargetPromptOverrides(policyBlob, extra.Metadata)
		extra.ForwardRule.AICreativity = metaFloatDefault(extra.Metadata, "ai_creativity", 0.7)
		extra.ForwardRule.AIMaxTokens = metaIntDefault(extra.Metadata, "ai_max_tokens", 600)
	}

	// Stage 5 (W2): the task's effective settings are resolved here, at the
	// rule-load path — the Go counterpart of the Python pipeline's
	// `rule = await materialize(rule, self._bot_tokens)` (pipeline.py:787),
	// which runs FIRST, before the paused check and every gate. Every
	// consumer of GetByID (delivery preflight, transform stage, every
	// gate) therefore sees the bot-defaults-merged task; there is no
	// second resolution step anywhere downstream.
	//
	// It sits AFTER the blob-resolved fields above for one reason:
	// resolution needs the bot_token_id/owner pair the projection
	// resolves out of the metadata blob, so it cannot run before the blob
	// block does. It runs before anything else sees the projection, which
	// is the ordering that actually matters — GetByID returns the merged
	// task, and no later stage re-resolves.
	//
	// materializeRule returns the SAME projection when there is nothing
	// to inherit (no attached bot, empty defaults_json, every candidate
	// key pinned, or the only candidate keys non-inheritable), so the
	// common path allocates nothing and no caller can tell resolution
	// ran. When it does inherit, the stored rule is untouched: the merged
	// values live on a copy (the copy-not-mutate rule — a caller that
	// saved the resolved values would turn the bot's defaults into the
	// task's own pins).
	if extra.Metadata != nil {
		if merged := s.materializeRule(ctx, extra.ForwardRule, extra.Metadata, extra.MetadataJSON); merged != extra.ForwardRule {
			extra.ForwardRule = merged
		}
	}

	// auto_pause is not a column: the engine records it inside the task's
	// metadata JSON, so the projection reads it out of the same blob. It is
	// NOT a registry setting (it is engine bookkeeping, never inheritable,
	// never written by a bot default), so the original blob is the right
	// place to read it from: resolution never touches the key, and the
	// merged copy carries it forward verbatim.
	if reason, ok := extra.Metadata["auto_pause"].(map[string]any); ok {
		if r, ok := reason["reason"].(string); ok && r != "" {
			extra.AutoPauseReason = r
		}
	}
	return extra, nil
}

// metaStr reads a string field from a decoded metadata blob with the
// pre-feature default applied, mirroring the Python entity's own accessors
// (str(self.metadata.get(key) or default)).
func metaStr(meta map[string]any, key, def string) string {
	if v, ok := meta[key]; ok {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return def
}

// decodeTargetChatIDs reads the target_chat_ids column blob into the
// target list, mirroring the Python loader's _safe_list(col, []):
// a missing, empty or invalid blob means "no extra targets", never an
// error — a row whose list cannot be parsed still delivers to its primary
// target, exactly as it does on the Python side. Non-string entries are
// stringified (the column is written by Python as json.dumps of a list of
// str, but an API client can store numbers).
func decodeTargetChatIDs(blob string) []string {
	trimmed := strings.TrimSpace(blob)
	if trimmed == "" {
		return nil
	}
	var list []any
	if err := json.Unmarshal([]byte(trimmed), &list); err != nil {
		return nil
	}
	out := make([]string, 0, len(list))
	for _, item := range list {
		switch v := item.(type) {
		case string:
			if s := strings.TrimSpace(v); s != "" {
				out = append(out, s)
			}
		case float64:
			// json.dumps writes Telegram ids as ints; a JSON-decoded
			// -100123.0 must read back as "-100123", matching _chat_key.
			out = append(out, strconv.FormatInt(int64(v), 10))
		}
	}
	return out
}

// metaInt64 reads a numeric metadata field. JSON numbers decode as float64,
// so the conversion goes through that, exactly like the payload accessor.
func metaInt64(meta map[string]any, key string, def int64) int64 {
	if v, ok := meta[key]; ok {
		switch n := v.(type) {
		case float64:
			return int64(n)
		case int:
			return int64(n)
		case int64:
			return n
		}
	}
	return def
}

// metaFloatDefault reads a floating-point metadata field with the
// feature's own default (Python: float(self.metadata.get(key, default))).
// Used for ai_creativity, whose entity default is 0.7 (entities.py:1633):
// a missing or uninterpretable value must keep the configured behaviour,
// never become a 0 that would pin the provider's sampling.
func metaFloatDefault(meta map[string]any, key string, def float64) float64 {
	if v, ok := meta[key]; ok {
		switch n := v.(type) {
		case float64:
			return n
		case int:
			return float64(n)
		case int64:
			return float64(n)
		}
	}
	return def
}

// metaIntDefault reads an integer metadata field with the feature's own
// default (Python: int(self.metadata.get(key, default))). Used for
// ai_max_tokens, whose entity default is 600 (entities.py:1648): a
// missing value must not become a 0-token request, which every provider
// would reject.
func metaIntDefault(meta map[string]any, key string, def int) int {
	return int(metaInt64(meta, key, int64(def)))
}

// metaBool reads a boolean field from a decoded metadata blob, mirroring the
// Python entity's own accessor (bool(self.metadata.get(key, def))). JSON
// decodes booleans as Go bool, but the blob is free-form: a 0/1 integer or
// the string "true" still means what the writer meant, and a value the
// reader cannot interpret as a boolean falls back to the default rather than
// silently switching the flag off.
func metaBool(meta map[string]any, key string, def bool) bool {
	v, ok := meta[key]
	if !ok {
		return def
	}
	switch b := v.(type) {
	case bool:
		return b
	case float64:
		return b != 0
	case int:
		return b != 0
	case int64:
		return b != 0
	case string:
		s := strings.ToLower(strings.TrimSpace(b))
		if s == "true" {
			return true
		}
		if s == "false" {
			return false
		}
	}
	return def
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
// returns (nil, nil): the task attached a filter, the filter is gone, and
// the delivery proceeds *unfiltered* — the Python pipeline's own behaviour when
// a filter load fails (it logs and evaluates no filter), never a drop.
//
// Tenant isolation (pipeline.py:930-943): the filter id comes from the task's
// own row, but the row it points at is read BY ID ALONE — so a task must never
// be filtered by (or inherit the configuration of) a filter owned by another
// tenant. A filter whose owner_user_id is neither 0 (a pre-ownership row,
// still the task's own configuration) nor the task's owner is IGNORED, exactly
// as the Python pipeline ignores it: the delivery proceeds unfiltered, and the
// violation is visible in the log rather than silently applied.
func (s *Store) getFilter(ctx context.Context, filterID string, ruleOwnerUserID int64) (*rule.FilterRule, error) {
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
		filterOwner       sql.NullInt64
	)
	if err := rows.Scan(&id, &whitelistJSON, &blacklistJSON,
		&regexPatternsJSON, &allowedJSON, &blockedJSON, &dropService, &minLen, &maxLen, &filterOwner); err != nil {
		return nil, fmt.Errorf("scan filter_rules %s: %w", filterID, err)
	}
	// Release the cursor NOW, not on return. The optional* helpers below run
	// fresh single-row queries through this same *sql.DB, which runs with
	// SetMaxOpenConns(1) — a still-open cursor owns that one connection and
	// the helpers would block on it until queryTimeout fires and the column
	// (is_enabled in particular) is never read, silently leaving the filter
	// at its pre-v30 default of ENABLED. An owner who toggled a filter off
	// would get a filter that still drops messages.
	rows.Close()

	// Tenant isolation: the task may only be filtered by its own filter. An
	// owner_user_id of 0 is a pre-ownership row and stays the task's own
	// configuration, exactly as the Python pipeline allows.
	if filterOwner.Valid && filterOwner.Int64 != 0 && filterOwner.Int64 != ruleOwnerUserID {
		slog.Warn("filter ignored: owned by another tenant",
			"filter_id", id, "rule_owner", ruleOwnerUserID, "filter_owner", filterOwner.Int64,
			"reason", "FILTER_NOT_OWNED")
		return nil, nil
	}

	f := &rule.FilterRule{
		ID:                id,
		OwnerUserID:       filterOwner.Int64,
		IsEnabled:         true, // pre-v30 default; overridden when the column exists AND is false
		DropService:       dropService,
		MinMessageLength:  minLen,
		MaxMessageLength:  maxLen,
		WhitelistKeywords: decodeStringList(whitelistJSON),
		BlacklistKeywords: decodeStringList(blacklistJSON),
		RegexPatterns:     decodeStringList(regexPatternsJSON),
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

// aiRewriteActive is ForwardRule.ai_rewrite_active (entities.py:1681): the
// single authority for "does this task rewrite text with an LLM?".
//
// The semantics are deliberately asymmetric: a config id alone keeps meaning
// "rewrite with this config", because API clients, backups and the AI menu
// have always relied on that; the task panel's paid-addon switch is the one
// thing that can veto it. An absent or non-dict paid_addons block is "no veto
// recorded", not "vetoed".
func aiRewriteActive(aiConfigID string, meta map[string]any) bool {
	if strings.TrimSpace(aiConfigID) == "" {
		return false
	}
	addons, ok := meta["paid_addons"].(map[string]any)
	if !ok {
		return true
	}
	if _, present := addons["ai_rewrite"]; !present {
		return true
	}
	return metaBool(addons, "ai_rewrite", false)
}
