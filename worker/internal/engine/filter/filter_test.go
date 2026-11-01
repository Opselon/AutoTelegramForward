package filter

import (
	"strings"
	"testing"

	"atf/worker/internal/domain/rule"
)

// TestFilterPortMatchesPythonSemantics is the parity guard: every case here
// mirrors the Python core.domain.services.FilterEngine's behaviour for the
// same input, so a task's filter decides identically in either engine. A
// task whose filter disagrees across engines forwards a different set of
// messages depending on which engine picked the job up.
func TestFilterPortMatchesPythonSemantics(t *testing.T) {
	e := New()

	tests := []struct {
		name      string
		text      string
		media     string
		service   bool
		filter    *rule.FilterRule
		wantAction Action
		wantReason string
	}{
		{
			name: "no filter allows",
			text: "anything at all", wantAction: Allow, wantReason: "no_filter",
		},
		{
			name: "disabled filter is skipped not evaluated",
			text: "blocked-word",
			filter: &rule.FilterRule{IsEnabled: false, BlacklistKeywords: []string{"blocked"}},
			wantAction: Allow, wantReason: "filter_disabled",
		},
		{
			name: "blacklist keyword drops",
			text: "this has a blocked-word inside",
			filter: &rule.FilterRule{IsEnabled: true, BlacklistKeywords: []string{"blocked-word"}},
			wantAction: Drop, wantReason: "blacklist_keyword",
		},
		{
			name: "blacklist matches across whitespace reflow",
			text: "this has a blocked\n\nword here",
			filter: &rule.FilterRule{IsEnabled: true, BlacklistKeywords: []string{"blocked word"}},
			wantAction: Drop, wantReason: "blacklist_keyword",
		},
		{
			name: "whitelist requires a match",
			text: "nothing relevant",
			filter: &rule.FilterRule{IsEnabled: true, WhitelistKeywords: []string{"wanted"}},
			wantAction: Drop, wantReason: "whitelist_mismatch",
		},
		{
			name: "whitelist passes on a match",
			text: "this is wanted text",
			filter: &rule.FilterRule{IsEnabled: true, WhitelistKeywords: []string{"wanted"}},
			wantAction: Allow, wantReason: "filter_passed",
		},
		{
			name: "regex_block drops on match",
			text: "invoice 2026 paid",
			filter: &rule.FilterRule{IsEnabled: true, RegexBlock: []string{`invoice\s+\d+`}},
			wantAction: Drop, wantReason: "regex_block:invoice\\s+\\d+",
		},
		{
			name: "regex_require needs a match",
			text: "nothing here",
			filter: &rule.FilterRule{IsEnabled: true, RegexRequire: []string{`^promo`}},
			wantAction: Drop, wantReason: "regex_mismatch",
		},
		{
			name: "regex_require passes on match",
			text: "promo today only",
			filter: &rule.FilterRule{IsEnabled: true, RegexRequire: []string{`^promo`}},
			wantAction: Allow, wantReason: "filter_passed",
		},
		{
			name: "blocked media type drops",
			text: "hi", media: "video",
			filter: &rule.FilterRule{IsEnabled: true, BlockedMediaTypes: []string{"video"}},
			wantAction: Drop, wantReason: "media_blocked:video",
		},
		{
			name: "allowed media type passes",
			text: "hi", media: "photo",
			filter: &rule.FilterRule{IsEnabled: true, AllowedMediaTypes: []string{"photo", "video"}},
			wantAction: Allow, wantReason: "filter_passed",
		},
		{
			name: "min length drops short text",
			text: "hi",
			filter: &rule.FilterRule{IsEnabled: true, MinMessageLength: 10},
			wantAction: Drop, wantReason: "too_short",
		},
		{
			name: "max length drops long text",
			text: "this text is definitely more than ten characters",
			filter: &rule.FilterRule{IsEnabled: true, MaxMessageLength: 10},
			wantAction: Drop, wantReason: "too_long",
		},
		{
			// The legacy regex_patterns list is blocking when the filter has
			// blacklist keywords and no whitelist — the deterministic
			// compatibility rule. Changing it changes which messages
			// decade-old tasks forward.
			name: "legacy regex_patterns is blocking for blacklist-only filters",
			text: "special offer now",
			filter: &rule.FilterRule{
				IsEnabled:         true,
				BlacklistKeywords: []string{"other"},
				RegexPatterns:     []string{`offer`},
			},
			wantAction: Drop, wantReason: "regex_block:offer",
		},
		{
			// Persian/Arabic variant folding: a keyword typed with the Arabic
			// yeh (U+064A) must still match text written with the Farsi yeh
			// (U+06CC) — the whole point of the shared matcher. The same
			// guarantee the Python suite asserts.
			name: "persian variant folding",
			text: "فیلم新品种",
			filter: &rule.FilterRule{IsEnabled: true, BlacklistKeywords: []string{"فیل​م"}},
			wantAction: Drop, wantReason: "blacklist_keyword",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := e.Evaluate(tt.text, tt.media, tt.service, tt.filter)
			if got.Action != tt.wantAction {
				t.Errorf("action = %s, want %s (reason %q)", got.Action, tt.wantAction, got.Reason)
			}
			if tt.wantReason != "" && got.Reason != tt.wantReason {
				t.Errorf("reason = %q, want %q", got.Reason, tt.wantReason)
			}
		})
	}
}

// TestPersianVariantsAndInvisibles: the shared matcher's reason for existing.
// A blacklist typed on one keyboard must catch text written on another.
func TestPersianVariantsAndInvisibles(t *testing.T) {
	cases := []struct {
		name, text, keyword string
		want                bool
	}{
		{"arabic yeh matches farsi yeh keyword", "فروش", "فروزش", false},
		{"same word matches", "فروش ویژه", "فروش ویژه", true},
		{"fatha inside the word", "فَروش", "فروش", true},
		{"tatweel stretch inside the word", "فـروش", "فروش", true},
		{"extra whitespace between phrase words", "فروش  ویژه", "فروش ویژه", true},
		{"newline between phrase words", "فروش\nویژه", "فروش ویژه", true},
		{"case folded", "BLOCKED", "blocked", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := containsWord(c.text, c.keyword, false); got != c.want {
				t.Errorf("containsWord(%q, %q) = %v, want %v", c.text, c.keyword, got, c.want)
			}
		})
	}
}

// TestServiceMessagesDrop: the pipeline never enqueues them, but if one
// arrives the engine drops it — a service message is not content.
func TestServiceMessagesDrop(t *testing.T) {
	e := New()
	got := e.Evaluate("a group was created", "text", true, nil)
	if got.Action != Drop {
		t.Errorf("service message action = %s, want DROP", got.Action)
	}
}

// TestUserRegexIsBounded: a hostile user pattern must fail closed (DROP),
// never be trusted to run, and never be silently allowed.
func TestUserRegexIsBounded(t *testing.T) {
	e := New()
	hostile := []string{
		`(a+)+b`,
		`(a|a)*b`,
		strings.Repeat("a", 1001), // over the length ceiling
	}
	for _, pattern := range hostile {
		got := e.Evaluate("aaaaaaaaaaaaaaaaaaaaaaaaaaab", "text", false,
			&rule.FilterRule{IsEnabled: true, RegexBlock: []string{pattern}})
		if got.Action != Drop {
			t.Errorf("hostile pattern %q: action = %s, want DROP (fail closed)", truncate(pattern, 24), got.Action)
		}
		if got.Reason != "invalid_regex_block:"+truncate(pattern, 32) && got.Action != Drop {
			t.Errorf("hostile pattern %q: reason = %q", pattern, got.Reason)
		}
	}
}
