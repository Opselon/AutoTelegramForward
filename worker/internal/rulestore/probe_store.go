package rulestore

import (
	"context"
	"database/sql"
	"encoding/json"

	"atf/worker/internal/domain/rule"
	"atf/worker/internal/domain/settings"
)

// probe_store.go exposes the composite rule store to the cross-language
// parity harness. The harness compiles as package main outside the
// internal tree, so it cannot construct a Store itself; the probe takes
// the already-open *sql.DB the harness prepared (a temp SQLite file with
// the Core's own DDL applied) and answers through GetByID — the call the
// delivery engine makes.
//
// Nothing here is a reimplementation: the probe wires the SAME composite
// Store the worker opens (same struct shape, same backend selection) and
// answers through GetByID. The settings probes alongside it exist for the
// same reason ProbeTransform does: one import boundary for the harness,
// the real production functions behind it.

// ProbeStoreResult is the outcome of one store-backed parity case.
type ProbeStoreResult struct {
	// Rule is the resolved projection the delivery engine would see:
	// bot defaults merged in when the task's owner owns the named bot,
	// untouched otherwise.
	Rule *rule.ForwardRule
	// StoredMetadata is the row's metadata blob read back from the
	// database AFTER resolution, so the harness can assert the stored
	// rule was never written through (the copy-not-mutate rule).
	StoredMetadata map[string]any
	// Error carries a harness-readable failure description when the case
	// could not run at all (a broken fixture, not a divergence).
	Error string
}

// OpenParityStore builds a composite rule store over the harness's
// already-open SQLite database — the same composite construction the
// worker's own openRuleStore performs for the single-database case.
func OpenParityStore(ctx context.Context, db *sql.DB) (*Store, error) {
	store := &Store{
		rulesDB:       db,
		rulesBackend:  "sqlite",
		filtersDB:     db,
		filtersShared: true,
	}
	_ = ctx // the composite store opens no new connections; ctx stays in
	// the signature for symmetry with New so callers read the same shape.
	return store, nil
}

// ProbeEffectiveStore resolves one rule through the real composite store:
// GetByID with the bot-defaults materialize wired in, then a fresh read of
// the stored row so the harness can see what resolution left behind.
//
// The harness owns the database (its DDL, its seeded bot_tokens rows and
// its forward_rules row); the probe owns the resolution. The rule returned
// carries the MERGED blob the projection hands the delivery engine, and
// StoredMetadata is the row as it sits in the database after resolution
// ran — the two halves of the copy-not-mutate contract.
func ProbeEffectiveStore(ctx context.Context, db *sql.DB, ruleID string) (ProbeStoreResult, error) {
	store, err := OpenParityStore(ctx, db)
	if err != nil {
		return ProbeStoreResult{}, err
	}
	r, _, err := store.GetByID(ruleID)
	if err != nil {
		return ProbeStoreResult{}, err
	}
	var raw string
	if err := db.QueryRow(`SELECT metadata FROM forward_rules WHERE id = ?`, ruleID).Scan(&raw); err != nil {
		return ProbeStoreResult{}, err
	}
	stored := map[string]any{}
	_ = json.Unmarshal([]byte(raw), &stored)
	return ProbeStoreResult{Rule: r, StoredMetadata: stored}, nil
}

// ProbeSettingsEffective exposes settings.Effective for the parity harness
// through the rulestore — the internal package the harness can reach — so
// one import boundary serves every settings probe.
func ProbeSettingsEffective(meta map[string]any, botDefaults map[string]any) map[string]any {
	return settings.Effective(settings.MapSettings{Metadata: meta}, botDefaults)
}

// ProbeSettingsPinned exposes settings.PinnedKeys for the parity harness.
func ProbeSettingsPinned(meta map[string]any) map[string]bool {
	return settings.PinnedKeys(settings.MapSettings{Metadata: meta})
}

// ProbeSettingsMaterialized exposes settings.Materialize for the parity
// harness, returning the merged values, the inherited key list, and
// whether a copy was produced at all.
//
// The values are the task's settings READ THROUGH the registry — the way
// the Python adapter's getattr(resolved, key) resolves through the entity
// property — so a key the task never set and no bot supplied answers with
// the registry default rather than "absent". Both engines must answer the
// same value for the same key, and "absent" is not a value.
func ProbeSettingsMaterialized(meta map[string]any, botDefaults map[string]any) (map[string]any, []string, bool) {
	resolved := settings.Materialize(&settings.MapSettings{Metadata: meta}, botDefaults)
	if !resolved.IsCopy() {
		return settings.ReadSettings(&settings.MapSettings{Metadata: meta}), nil, false
	}
	merged, ok := resolved.Rule.(*settings.MapSettings)
	if !ok || merged.Metadata == nil {
		return settings.ReadSettings(&settings.MapSettings{Metadata: meta}), nil, false
	}
	return settings.ReadSettings(merged), resolved.Inherited, true
}

// ProbeRuleSettings reads every registered setting off a loaded rule
// projection through the registry — the exact view the Python adapter's
// getattr(resolved, key) produces, where a key the task never set answers
// with the registry default instead of "absent". Used by the parity
// harness so a store-backed case compares VALUES, not key presence.
func ProbeRuleSettings(r *rule.ForwardRule) map[string]any {
	if r == nil {
		return settings.ReadSettings(nil)
	}
	if r.Metadata == nil {
		return settings.ReadSettings(&settings.MapSettings{})
	}
	return settings.ReadSettings(&ruleSettingsAdapter{rule: r, metadata: r.Metadata})
}

// ProbeSortedSettingsKeys exposes settings.SortedKeys for the parity
// harness's key ordering (the order Python's schema() iterates in).
func ProbeSortedSettingsKeys() []string {
	return settings.SortedKeys()
}
