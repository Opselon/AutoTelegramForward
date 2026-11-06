package cleaning

import "testing"

// mentions_test.go pins the Go port of the Python "@mention rewrites" stage
// (core/domain/text_replace.py: apply_mention_rewrites). The two engines
// execute the same queue, so every case here is the Python semantics:
// exact handles win over the catch-all, one pass only, standalone handles
// only, and 4..32 username characters.

func TestApplyMentionRewrites(t *testing.T) {
	tests := []struct {
		name string
		text string
		m    map[string]string
		want string
	}{
		{
			name: "empty map is a no-op",
			text: "hi @alfa",
			m:    map[string]string{},
			want: "hi @alfa",
		},
		{
			name: "exact handle rewrites case-insensitively",
			text: "buy from @OldShop now",
			m:    map[string]string{"@oldshop": "@newshop"},
			want: "buy from @newshop now",
		},
		{
			name: "catch-all rewrites every mention",
			text: "a @first b @second",
			m:    map[string]string{"*": "@main"},
			want: "a @main b @main",
		},
		{
			name: "exact beats catch-all",
			text: "@oldshop and @other",
			m:    map[string]string{"@oldshop": "@newshop", "*": "@main"},
			want: "@newshop and @main",
		},
		{
			name: "email address never matches",
			text: "mail me at bob@example.com or @valid_handle",
			m:    map[string]string{"*": "@main"},
			want: "mail me at bob@example.com or @main",
		},
		{
			name: "handle glued to a word never matches",
			text: "word@glued stays",
			m:    map[string]string{"*": "@main"},
			want: "word@glued stays",
		},
		{
			name: "one pass: output is never re-matched",
			text: "@alpha",
			m:    map[string]string{"@alpha": "@beta", "@beta": "@gamma"},
			want: "@beta",
		},
		{
			name: "value without @ still reads as a handle",
			text: "see @oldshop",
			m:    map[string]string{"@oldshop": "newshop"},
			want: "see @newshop",
		},
		{
			name: "short handles are not mentions",
			text: "a @a b @old",
			m:    map[string]string{"*": "@main"},
			want: "a @a b @old",
		},
		{
			name: "unicode word before the at sign blocks the match",
			text: "привет@glued stays",
			m:    map[string]string{"*": "@main"},
			want: "привет@glued stays",
		},
		{
			name: "empty text is a no-op",
			text: "",
			m:    map[string]string{"*": "@main"},
			want: "",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ApplyMentionRewrites(tt.text, tt.m); got != tt.want {
				t.Errorf("ApplyMentionRewrites(%q) = %q, want %q", tt.text, got, tt.want)
			}
		})
	}
}

func TestNormalizeMentionMap(t *testing.T) {
	tests := []struct {
		name string
		in   map[string]string
		want map[string]string
	}{
		{
			name: "keys fold to canonical at-lower spelling",
			in:   map[string]string{"@Shop": "@New"},
			want: map[string]string{"@shop": "@New"},
		},
		{
			name: "keys and values accepted without the at sign",
			in:   map[string]string{"shop": "newshop"},
			want: map[string]string{"@shop": "newshop"},
		},
		{
			name: "catch-all key is kept verbatim",
			in:   map[string]string{"*": "@main"},
			want: map[string]string{"*": "@main"},
		},
		{
			name: "unusable rows are dropped",
			in:   map[string]string{"": "@x", "@a": "", "@ok": "@fine"},
			want: map[string]string{"@ok": "@fine"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := NormalizeMentionMap(tt.in)
			if len(got) != len(tt.want) {
				t.Fatalf("NormalizeMentionMap(%v) = %v, want %v", tt.in, got, tt.want)
			}
			for k, v := range tt.want {
				if got[k] != v {
					t.Errorf("NormalizeMentionMap(%v)[%q] = %q, want %q", tt.in, k, got[k], v)
				}
			}
		})
	}
}
