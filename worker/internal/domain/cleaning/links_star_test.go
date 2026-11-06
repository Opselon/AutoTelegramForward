package cleaning

import "testing"

// links_star_test.go pins the "replace any link found" shapes the rewrite
// map grew: the "*" catch-all (every link to one target) and the URL-prefix
// key (one deep link), matching the Python engine's _rewrite_url exactly.

func TestRewriteURLCatchAllAndPrefixKeys(t *testing.T) {
	tests := []struct {
		name string
		url  string
		m    map[string]string
		want string
	}{
		{
			name: "star rewrites every unmatched link",
			url:  "https://example.com/x",
			m:    map[string]string{"*": "https://t.me/me"},
			want: "https://t.me/me",
		},
		{
			name: "exact domain key wins over star",
			// A domain key substitutes the substring (Python's
			// u.replace(old_d, new_d)), so domain keys pair with domain
			// values; a full-URL target is what the prefix key is for.
			url:  "https://special.com/a",
			m:    map[string]string{"*": "https://t.me/me", "special.com": "vip.com"},
			want: "https://vip.com/a",
		},
		{
			name: "url-prefix key rewrites the prefix it matches",
			url:  "https://old.t.me/x/deep",
			m:    map[string]string{"https://old.t.me/x": "https://new.t.me/y"},
			want: "https://new.t.me/y/deep",
		},
		{
			name: "url-prefix key is case-insensitive on the match",
			url:  "https://OLD.t.me/x",
			m:    map[string]string{"https://old.t.me/x": "https://new.t.me/y"},
			want: "https://new.t.me/y",
		},
		{
			name: "no match and no star returns the url unchanged",
			url:  "https://example.com/x",
			m:    map[string]string{"other.com": "https://t.me/me"},
			want: "https://example.com/x",
		},
		{
			name: "empty star value is a deliberate strip",
			url:  "https://example.com/x",
			m:    map[string]string{"*": ""},
			want: "",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := rewriteURL(tt.url, tt.m); got != tt.want {
				t.Errorf("rewriteURL(%q, %v) = %q, want %q", tt.url, tt.m, got, tt.want)
			}
		})
	}
}

func TestPipelineAppliesMentionRewritesAfterReplacements(t *testing.T) {
	// Stage order contract: the mention pass runs AFTER the replacements, in
	// the same final pass Python applies them in — a replacement's output is
	// still visible to the mention map, and neither resurrects the other's
	// input.
	p := NewPipeline()
	cfg := Config{
		Replacements:      []ReplacementEntry{{Find: "@oldshop", Replace: "@oldest"}},
		MentionRewriteMap: map[string]string{"@oldest": "@main", "@oldshop": "@newshop"},
	}
	res := p.ApplyText("go to @oldshop", cfg, nil, CaptionHint{})
	if res.Text != "go to @main" {
		t.Fatalf("final text = %q, want %q", res.Text, "go to @main")
	}
}

func TestPipelineLinkReplaceEnabledUpgradesThePolicy(t *testing.T) {
	// The toggle upgrades a task with no explicit policy to REWRITE_LINKS so
	// the map applies — the same upgrade Python's pipeline performs.
	p := NewPipeline()
	cfg := Config{
		LinkReplaceEnabled: true,
		LinkRewriteMap:     map[string]string{"*": "https://t.me/me"},
	}
	res := p.ApplyText("see https://example.com/x", cfg, nil, CaptionHint{})
	if res.Text != "see https://t.me/me" {
		t.Fatalf("final text = %q, want %q", res.Text, "see https://t.me/me")
	}
}
