package rulestore

import (
	"strings"

	"atf/worker/internal/domain/rule"
	"atf/worker/internal/domain/settings"
)

// tokens_query.go adapts the bot_tokens statements to the placeholder
// convention of whichever backend the rule store opened.
//
// The shared projections elsewhere in this package use rulesQuery for the
// same job; it exists separately here only because the bot_tokens read
// carries TWO bound parameters (id, owner) where the rule projections carry
// one, and the rewrite has to know the count to keep the mapping exact.

// tokensQuery adapts a two-parameter PostgreSQL statement (id, owner) to
// the backend's placeholder convention. PostgreSQL keeps $1/$2; SQLite
// needs ?, one per parameter, in the same order.
func (s *Store) tokensQuery(text string, args ...any) (string, []any) {
	if s.rulesBackend == "postgres" {
		return text, args
	}
	return rewritePlaceholders(text, len(args)), args
}

// tokensQueryOne adapts the single-parameter unscoped read.
func (s *Store) tokensQueryOne(text string, arg any) (string, any) {
	if s.rulesBackend == "postgres" {
		return text, arg
	}
	return rewritePlaceholders(text, 1), arg
}

// rewritePlaceholders rewrites $1..$n into SQLite's positional ? marks.
// SQLite counts ? positionally, so every $k — whatever its number — becomes
// the next ?, and the arguments keep their order.
func rewritePlaceholders(text string, count int) string {
	out := text
	for i := 1; i <= count; i++ {
		out = strings.ReplaceAll(out, placeholder(i), "?")
	}
	return out
}

// placeholder spells one numbered PostgreSQL parameter; the count is small
// and spelled out because a fmt.Sprintf here would allocate on every query.
func placeholder(i int) string {
	if i >= 1 && i <= len(numberedPlaceholders) {
		return numberedPlaceholders[i-1]
	}
	return "?"
}

// numberedPlaceholders covers every parameter count a bot_tokens statement
// carries (one for the unscoped read, two for the owner-scoped one).
var numberedPlaceholders = []string{"$1", "$2", "$3", "$4"}

// ruleSettingsAdapter is the settings.SettingsHolder view of a loaded rule
// projection: every registered key is resolved the way the Python
// ForwardRule entity's own properties resolve it, so the registry's notion
// of "the task's current settings" and the engine's are the same notion.
//
// The adapter reads the metadata blob the store just decoded, which is the
// same blob every metadata-backed reader (the transform projection, the
// keyword projection) resolves from — the Python entity's properties read
// the same blob through the entity, so the two sides answer the same
// question with the same value.
type ruleSettingsAdapter struct {
	rule     *rule.ForwardRule
	metadata map[string]any
}

// Meta returns the task's metadata blob.
func (a *ruleSettingsAdapter) Meta() map[string]any {
	if a == nil {
		return nil
	}
	if a.metadata == nil {
		return map[string]any{}
	}
	return a.metadata
}

// Get returns the task's current value for one registered key. The
// metadata-backed settings are read straight out of the blob; the handful
// of settings the projection carries as typed struct fields are answered
// from those fields, because that IS where the projection resolved them at
// load time (the store never carries a column-backed setting the blob also
// holds — the Python entity resolves both through the same property).
func (a *ruleSettingsAdapter) Get(key string) any {
	if a == nil || a.rule == nil {
		return nil
	}
	switch key {
	case "bot_token_id":
		return a.rule.BotTokenID
	case "delivery_transport":
		return a.rule.DeliveryTransport
	case "forward_every_n":
		if a.rule.ForwardEveryN > 0 {
			return a.rule.ForwardEveryN
		}
	case "block_links":
		return a.rule.BlockLinks
	}
	return a.Meta()[key]
}

// Set is the write side of the adapter: applying an inherited value writes
// it into the metadata blob, exactly as the Python entity's property
// setter writes the key into the blob (or the column it maps to). The
// copy Materialize hands back is a fresh adapter over a cloned blob, so
// the write never reaches the projection the store loaded.
func (a *ruleSettingsAdapter) Set(key string, value any) {
	if a == nil || a.metadata == nil {
		return
	}
	a.metadata[key] = value
}

// compile-time guarantee that the adapter satisfies the registry's read
// contract.
var _ settings.SettingsHolder = (*ruleSettingsAdapter)(nil)
