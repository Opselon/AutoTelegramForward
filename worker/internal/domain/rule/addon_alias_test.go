package rule

// The paid-addon entitlement gate must honor BOTH spellings: the canonical
// engine key and the short form the catalogue clients (bot rows, web
// dashboard) actually record. Production shipped three paid add-ons that
// silently never fired from any real client because the gate read a key
// nothing wrote — the worst outcome for a feature a user paid for.

import (
	"reflect"
	"testing"
)

func TestRuleAddonEnabledHonoursCatalogueAliases(t *testing.T) {
	cases := []struct {
		name string
		meta map[string]any
		key  string
		want bool
	}{
		{
			"canonical key still works",
			map[string]any{"paid_addons": map[string]any{"keyword_reply": true}},
			"keyword_reply", true,
		},
		{
			"bot catalogue spelling (kw_reply) enables the gate",
			map[string]any{"paid_addons": map[string]any{"kw_reply": true}},
			"keyword_reply", true,
		},
		{
			"web dashboard spelling (auto_del) enables auto delete",
			map[string]any{"paid_addons": map[string]any{"auto_del": true}},
			"auto_delete_message", true,
		},
		{
			"web dashboard spelling (affiliate) enables affiliate kit",
			map[string]any{"paid_addons": map[string]any{"affiliate": true}},
			"affiliate_kit", true,
		},
		{
			"alias false is still false",
			map[string]any{"paid_addons": map[string]any{"kw_reply": false}},
			"keyword_reply", false,
		},
		{
			"canonical false does not inherit a sibling alias of another addon",
			map[string]any{"paid_addons": map[string]any{"auto_del": true}},
			"keyword_reply", false,
		},
		{
			"unknown key has no alias",
			map[string]any{"paid_addons": map[string]any{"kw_reply": true}},
			"crypto_mode", false,
		},
		{
			"non-dict paid_addons means no entitlement",
			map[string]any{"paid_addons": "true"},
			"keyword_reply", false,
		},
		{
			"missing paid_addons means no entitlement",
			map[string]any{},
			"keyword_reply", false,
		},
	}
	for _, tc := range cases {
		if got := ruleAddonEnabled(tc.meta, tc.key); got != tc.want {
			t.Errorf("%s: ruleAddonEnabled(%q) = %v, want %v", tc.name, tc.key, got, tc.want)
		}
	}
}

// The keyword-reply projection — the exact path the delivery engine's
// classifier consumes — must see the entitlement through the alias too.
func TestKeywordSettingsSeesTheCatalogueAlias(t *testing.T) {
	r := &ForwardRule{
		Metadata: map[string]any{
			"keyword_confirmation": true,
			"keyword_reply":        "hello!",
			"whitelist":            []any{"hi"},
			"paid_addons":          map[string]any{"kw_reply": true},
		},
	}
	got := r.KeywordSettings()
	if !got.AddonEnabled {
		t.Fatal("kw_reply entitlement did not reach the keyword-reply gate — the production client spelling is still dead")
	}
	if !got.KeywordConfirmation || got.KeywordReply != "hello!" || !reflect.DeepEqual(got.Whitelist, []string{"hi"}) {
		t.Errorf("projection drifted: %+v", got)
	}
}
