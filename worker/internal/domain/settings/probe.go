package settings

import (
	"encoding/json"
)

// probe.go exports the parity probes the cross-language harness drives
// the registry through. They exist for the same reason ProbeTransform
// does in the rulestore: the parity driver compiles as package main
// outside the internal tree, so it cannot import this package directly —
// but every case must still run the REAL production code, not a
// reimplementation. A probe names the one exported entry point per
// decision the harness pins; nothing here is a second implementation of
// anything.

// ProbeEffective is settings.Effective exposed for cross-language parity
// testing: the complete effective settings for a task, bot defaults
// applied. The harness compares it against
// core.domain.bot_defaults.effective.
func ProbeEffective(meta map[string]any, botDefaults map[string]any) map[string]any {
	return Effective(MapSettings{Metadata: meta}, botDefaults)
}

// ProbePinned is settings.PinnedKeys exposed for cross-language parity
// testing: which settings are the task's OWN choices — the decision every
// inheritance question turns on. The harness compares it against
// core.domain.rule_settings.pinned_keys.
func ProbePinned(meta map[string]any) map[string]bool {
	return PinnedKeys(MapSettings{Metadata: meta})
}

// ProbeMaterializeResult is the materialize outcome a parity case
// inspects: whether a copy was produced, the sorted list of inherited
// keys (the INHERITED_MARK a Python materialised entity carries), and the
// merged values the copy resolves to.
type ProbeMaterializeResult struct {
	// Copied reports whether resolution produced a copy at all (nothing
	// to inherit: no bot, no defaults, all pinned, nothing valid).
	Copied bool
	// Inherited is the sorted key list the INHERITED_MARK carries.
	Inherited []string
	// Values is the merged metadata blob the copy holds, so the harness
	// can ask for any key's resolved value the same way the engine's
	// blob-resolved readers do.
	Values map[string]any
}

// ProbeMaterialize is settings.Materialize exposed for cross-language
// parity testing: the task as the forwarder should see it, bot defaults
// filled in, never mutating the input. The harness compares it against
// core.application.effective_settings.materialize.
func ProbeMaterialize(meta map[string]any, botDefaults map[string]any) ProbeMaterializeResult {
	// The parity harness builds the input map; hand materialize a
	// throwaway clone so the case struct's own map is never touched —
	// the same copy-not-mutate posture the store takes with the row it
	// loaded.
	holder := &MapSettings{Metadata: cloneMapValues(meta)}
	resolved := Materialize(holder, botDefaults)
	if !resolved.IsCopy() {
		return ProbeMaterializeResult{Values: holder.Meta()}
	}
	merged, ok := resolved.Rule.(*MapSettings)
	if !ok || merged.Metadata == nil {
		return ProbeMaterializeResult{Values: holder.Meta()}
	}
	return ProbeMaterializeResult{
		Copied:    true,
		Inherited: resolved.Inherited,
		Values:    merged.Metadata,
	}
}

// ProbeSortedKeys is SortedKeys exposed for the harness's key ordering:
// the order Python's schema() and effective() iterate the registry in.
func ProbeSortedKeys() []string { return SortedKeys() }

// cloneMapValues deep-copies a decoded metadata map through JSON so the
// probe's caller never observes a write the materialize performs.
func cloneMapValues(meta map[string]any) map[string]any {
	if meta == nil {
		return map[string]any{}
	}
	raw, err := jsonMarshal(meta)
	if err != nil {
		return map[string]any{}
	}
	out := map[string]any{}
	_ = jsonUnmarshal(raw, &out)
	return out
}

// jsonMarshal / jsonUnmarshal are thin wrappers so this file stays
// self-contained without importing the encoding/json package twice in one
// file's worth of probe helpers.
func jsonMarshal(value any) ([]byte, error) { return json.Marshal(value) }
func jsonUnmarshal(raw []byte, out any) error {
	return json.Unmarshal(raw, out)
}
