package rulestore

import "testing"

// ai_gate_test.go — ForwardRule.ai_rewrite_active (entities.py:1681), the
// single authority for "does this task rewrite text with an LLM?".
//
// Each case was executed against the Python ForwardRule during the Stage 4.4
// port; the gate is pinned to agree byte-for-byte. A divergence here would
// silently rewrite (or silently not rewrite) every message of every task
// with an AI config attached — exactly the bug the Python property was
// written to fix.
func TestAIRewriteActiveMirrorsPython(t *testing.T) {
	cases := []struct {
		name string
		cfg  string
		meta map[string]any
		want bool
	}{
		// No config: no rewrite, whatever the addons say.
		{"no config ignores the addon", "", map[string]any{"paid_addons": map[string]any{"ai_rewrite": true}}, false},
		// A config alone means "rewrite with this config": API clients,
		// backups and the AI menu have always relied on that.
		{"config alone rewrites", "cfg-1", nil, true},
		{"config with empty metadata", "cfg-1", map[string]any{}, true},
		{"empty paid_addons is no veto", "cfg-1", map[string]any{"paid_addons": map[string]any{}}, true},
		// The paid-addon switch is the explicit veto.
		{"addon true", "cfg-1", map[string]any{"paid_addons": map[string]any{"ai_rewrite": true}}, true},
		{"addon false vetoes", "cfg-1", map[string]any{"paid_addons": map[string]any{"ai_rewrite": false}}, false},
		// A malformed (non-dict) addons block is "no veto recorded", and
		// smart_ai_rewrite is NOT the veto (it predates the paid-addon
		// surface and is not consulted by the gate).
		{"non-dict addons is no veto", "cfg-1", map[string]any{"paid_addons": "true"}, true},
		{"smart_ai_rewrite is not the veto", "cfg-1", map[string]any{"smart_ai_rewrite": false}, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := aiRewriteActive(c.cfg, c.meta); got != c.want {
				t.Errorf("aiRewriteActive(%q, %v): got %v, want %v", c.cfg, c.meta, got, c.want)
			}
		})
	}
}
