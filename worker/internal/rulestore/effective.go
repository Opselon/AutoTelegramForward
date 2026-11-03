package rulestore

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"

	"atf/worker/internal/domain/forwardinterval"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/domain/settings"
)

// effective.go wires the per-bot-defaults resolution into the rule-load
// path — the Go counterpart of the Python pipeline's
// `rule = await materialize(rule, self._bot_tokens)` at pipeline.py:787,
// which runs FIRST, before the paused check and every gate.
//
// The integration point is deliberate: resolution happens inside getRule,
// so every consumer of GetByID (delivery preflight, transform stage, every
// gate) sees the bot-defaults-merged task with no second resolution step
// anywhere downstream — the same single-resolution guarantee the Python
// pipeline has.
//
// The read is owner-scoped exactly as the Python one is
// (effective_settings.attached_defaults): a token id that names another
// tenant's bot is simply not found, so this task never inherits that bot's
// defaults and never delivers through that bot's credential. Only a rule
// that knows no owner (a row written before owners existed) falls back to
// the unscoped read.
//
// bot_tokens lives in the SAME database as forward_rules under the current
// Core composition root (core/main.py binds SqliteBotTokenRepository to the
// shared atf.db; the PostgreSQL deployment's bot_tokens table lives in the
// same PostgreSQL database the rule store opens). The column is
// `defaults_json` (TEXT NOT NULL DEFAULT '{}'), added by the v20 additive
// migration — the live schema is (id, label, token_encrypted, is_active,
// created_at, updated_at, owner_user_id, defaults_json).

// botTokenDefaultsQuery is the owner-scoped bot_tokens read, written for
// PostgreSQL ($1, $2) and rewritten to ? for SQLite by tokensQuery. owner
// is part of the WHERE clause, not an if after the read — a transplanted id
// must not even round-trip the secret (the same rule the Python
// repository's get_owned states). Only defaults_json is selected: the token
// row's own identity and secret stay in the Python boundary, and the
// defaults blob is the one column the Worker may read.
const botTokenDefaultsQuery = `
	SELECT defaults_json
	FROM bot_tokens
	WHERE id = $1 AND owner_user_id = $2
`

// botTokenDefaultsUnscopedQuery is the pre-ownership fallback: a row that
// predates owner_user_id belongs to nobody, so it is read by id alone.
// Python's attached_defaults takes the same branch when the rule itself
// knows no owner.
const botTokenDefaultsUnscopedQuery = `
	SELECT defaults_json
	FROM bot_tokens
	WHERE id = $1
`

// attachedDefaults resolves the bot defaults the task may inherit from. It
// returns an empty map (never an error) for every shape that means "run on
// the registry defaults": no attached bot, a missing row, a row that is not
// this task's owner's, or a defaults column that is not a JSON object. A
// failure to read a *convenience* setting must never stop a task from
// forwarding — the exact posture the Python module takes.
func (s *Store) attachedDefaults(ctx context.Context, r *rule.ForwardRule) map[string]any {
	if r == nil {
		return nil
	}
	tokenID := strings.TrimSpace(r.BotTokenID)
	if tokenID == "" {
		return nil
	}
	owner := r.OwnerUserID

	var raw sql.NullString
	var err error
	if owner > 0 {
		query, args := s.tokensQuery(botTokenDefaultsQuery, tokenID, owner)
		err = s.rulesDB.QueryRowContext(ctx, query, args...).Scan(&raw)
	} else {
		// Only a rule that knows no owner falls back to the unscoped
		// read: a row written before owners existed must keep working,
		// and nothing else may take the branch.
		query, arg := s.tokensQueryOne(botTokenDefaultsUnscopedQuery, tokenID)
		err = s.rulesDB.QueryRowContext(ctx, query, arg).Scan(&raw)
	}
	if err != nil {
		// Not found, or a transient read failure: both mean "no
		// defaults" rather than "fail the delivery". A sql.ErrNoRows
		// for a foreign-tenant token id IS the isolation boundary.
		return nil
	}
	if !raw.Valid || strings.TrimSpace(raw.String) == "" {
		return nil
	}
	var defaults map[string]any
	if err := json.Unmarshal([]byte(raw.String), &defaults); err != nil {
		return nil
	}
	if defaults == nil {
		return nil
	}
	return defaults
}

// materializeRule fills the bot's inheritable defaults into the rule the
// store just loaded. The copy-not-mutate rule is the whole point: the
// loaded projection is handed to every gate, and a caller that saved the
// resolved values would turn the bot's defaults into the task's own pins —
// the exact opposite of inheriting them.
//
// The merged values land in the rule's metadata BLOB (not its struct
// fields), because every metadata-backed settings reader — the transform
// projection, the keyword projection, the delivery preflight's flags —
// resolves out of the same blob, exactly as the Python materialised
// entity's properties do. Regenerating the derived struct fields from the
// merged blob afterwards keeps the engine's typed readers on the merged
// view too (a field the projection computed once at load time and never
// re-read from the blob would be a silent divergence).
func (s *Store) materializeRule(ctx context.Context, r *rule.ForwardRule, meta map[string]any, metadataJSON string) *rule.ForwardRule {
	defaults := s.attachedDefaults(ctx, r)
	if len(defaults) == 0 {
		return r
	}
	// Clone the blob BEFORE the resolver can write to it: Materialize
	// applies inherited values by handing the adapter a mutable copy, and
	// the adapter's Set would otherwise write through the SAME map the
	// projection below reads from — the store's own loaded blob, which
	// GetByID's callers and the auto_pause read after it still consult.
	// A shallow copy is not enough: nested maps (content_policy,
	// cleaner_config …) are shared with the original, and an inherited
	// write into one of them would reach the stored view.
	cloned, err := cloneMetadataBlob(meta)
	if err != nil {
		// A blob that cannot be copied is not worth failing a forward
		// over: the task runs on its own values.
		return r
	}
	resolved := settings.Materialize(&ruleSettingsAdapter{rule: r, metadata: cloned}, defaults)
	if !resolved.IsCopy() {
		return r
	}
	merged, ok := resolved.Rule.(*settings.MapSettings)
	if !ok || merged.Metadata == nil {
		return r
	}
	out := *r
	out.Metadata = merged.Metadata
	reprojectInherited(&out, metadataJSON)
	return &out
}

// cloneMetadataBlob deep-copies the decoded metadata blob so the resolver's
// writes can never reach the projection the store loaded. JSON round-trip
// is the same clone settings.Materialize performs internally; doing it here
// as well keeps the adapter's write target independent of the store's map.
func cloneMetadataBlob(meta map[string]any) (map[string]any, error) {
	if meta == nil {
		return map[string]any{}, nil
	}
	raw, err := json.Marshal(meta)
	if err != nil {
		return nil, err
	}
	out := map[string]any{}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// reprojectInherited re-derives the typed struct fields the delivery
// engine reads off the projection, now that the merged metadata blob
// carries values the task never wrote itself.
//
// The fields that need this are the ones the projection computed ONCE at
// load time and carries as typed Go fields rather than reading out of the
// blob at use time: the transform configuration (remove_emojis,
// cleaner_config, replacements … the whole transform projection), the
// link gate (block_links), the cadence gate (forward_every_n) and the AI
// rewrite gate (ai_rewrite_active / paid_addons). A field in that set that
// kept the pre-merge value would silently disagree with the Python engine,
// which runs the inherited one.
//
// metadataJSON is the ORIGINAL blob the store decoded: the merged blob came
// out of a map (so it has no key order), and the transform projection's
// ordered-replacements decoder needs an order to preserve same-priority
// entries in. Merging into the original string's fields through the decoder
// keeps the task's own entries in the order the task stored them; entries
// the bot supplied — replacements is NOT inheritable, so none arrive this
// way — never disturb it.
func reprojectInherited(r *rule.ForwardRule, metadataJSON string) {
	if r.Metadata == nil {
		return
	}
	r.Transform = transformConfigFromMetadata(r.Metadata, metadataJSON)
	r.BlockLinks = metaBool(r.Metadata, "block_links", false)
	r.ForwardEveryN = forwardinterval.Clamp(r.Metadata["forward_every_n"])
	r.AIRewriteActive = aiRewriteActive(r.AIConfigID, r.Metadata)
}
