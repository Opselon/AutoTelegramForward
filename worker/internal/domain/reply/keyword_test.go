package reply

import (
	"fmt"
	"strings"
	"testing"
)

// TestClassifyPortsPythonGuardOrder pins the exact order
// classify_keyword_trigger evaluates its guards in. Each case supplies
// exactly one reason to return none and one earlier reason to fire, so a
// reordering is a failed case rather than a silent agreement.
func TestClassifyPortsPythonGuardOrder(t *testing.T) {
	tests := []struct {
		name string
		text string
		s    Settings
		want string
	}{
		{
			name: "confirmation off is terminal even with a match",
			text: "big sale today",
			s: Settings{
				KeywordConfirmation: false,
				KeywordReply:        "thanks",
				Whitelist:           []string{"sale"},
				AddonEnabled:        true,
			},
			want: "",
		},
		{
			name: "empty reply is terminal even with a match",
			text: "big sale today",
			s: Settings{
				KeywordConfirmation: true,
				KeywordReply:        "   ",
				Whitelist:           []string{"sale"},
				AddonEnabled:        true,
			},
			want: "",
		},
		{
			name: "absent entitlement is terminal even with a match",
			text: "big sale today",
			s: Settings{
				KeywordConfirmation: true,
				KeywordReply:        "thanks",
				Whitelist:           []string{"sale"},
				AddonEnabled:        false,
			},
			want: "",
		},
		{
			name: "no keywords is terminal",
			text: "big sale today",
			s: Settings{
				KeywordConfirmation: true,
				KeywordReply:        "thanks",
				Whitelist:           nil,
				AddonEnabled:        true,
			},
			want: "",
		},
		{
			name: "only blank keywords is terminal",
			text: "big sale today",
			s: Settings{
				KeywordConfirmation: true,
				KeywordReply:        "thanks",
				Whitelist:           []string{"   ", ""},
				AddonEnabled:        true,
			},
			want: "",
		},
		{
			name: "no match returns nothing",
			text: "nothing to see here",
			s: Settings{
				KeywordConfirmation: true,
				KeywordReply:        "thanks",
				Whitelist:           []string{"sale"},
				AddonEnabled:        true,
			},
			want: "",
		},
		{
			name: "full match returns the reply",
			text: "big sale today",
			s: Settings{
				KeywordConfirmation: true,
				KeywordReply:        "thanks for reading",
				Whitelist:           []string{"sale"},
				AddonEnabled:        true,
			},
			want: "thanks for reading",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := Classify(tc.text, tc.s); got != tc.want {
				t.Fatalf("Classify(%q, %+v) = %q, want %q", tc.text, tc.s, got, tc.want)
			}
		})
	}
}

// TestClassifyMatchSemantics pins the `kw in haystack` matching rules:
// case-insensitive, substring (not whole-word), and case-sensitive inputs
// matching on both sides.
func TestClassifyMatchSemantics(t *testing.T) {
	base := Settings{
		KeywordConfirmation: true,
		KeywordReply:        "ok",
		AddonEnabled:        true,
	}
	tests := []struct {
		text     string
		words    []string
		wantFire bool
	}{
		// Lowercased match: Python lowercases BOTH the text and the
		// keyword, so uppercase source text still fires.
		{"BIG SALE TODAY", []string{"sale"}, true},
		// A mixed-case keyword matches lowercased text.
		{"big sale today", []string{"SaLe"}, true},
		// Substring, not whole word: "sale" inside "wholesale" fires —
		// Python's `in` operator has no word-boundary notion.
		{"wholesale prices", []string{"sale"}, true},
		// Unicode case folding: the Persian/Arabic scripts the product is
		// built around never change shape under Python's str.lower, and
		// Go's strings.ToLower agrees.
		{"فروش ویژه", []string{"فروش"}, true},
		// A keyword longer than the text cannot match.
		{"sale", []string{"wholesale"}, false},
		// Any one of several keywords firing is enough.
		{"regular price", []string{"sale", "regular"}, true},
		// A keyword surrounded by punctuation still matches.
		{"#sale! now", []string{"sale"}, true},
	}
	for _, tc := range tests {
		t.Run(fmt.Sprintf("%q/%v", tc.text, tc.words), func(t *testing.T) {
			s := base
			s.Whitelist = tc.words
			got := Classify(tc.text, s)
			if tc.wantFire && got != "ok" {
				t.Fatalf("Classify(%q, %v) = %q, want a trigger", tc.text, tc.words, got)
			}
			if !tc.wantFire && got != "" {
				t.Fatalf("Classify(%q, %v) = %q, want no trigger", tc.text, tc.words, got)
			}
		})
	}
}

// TestClampReply pins the reply normalization: trimmed, clamped, and never
// split mid-rune.
func TestClampReply(t *testing.T) {
	t.Run("trim happens before the clamp", func(t *testing.T) {
		if got := Classify("match", Settings{
			KeywordConfirmation: true,
			KeywordReply:        "  padded reply  ",
			Whitelist:           []string{"match"},
			AddonEnabled:        true,
		}); got != "padded reply" {
			t.Fatalf("trim before clamp: got %q", got)
		}
	})
	t.Run("a reply over the limit is clamped to it (code points)", func(t *testing.T) {
		long := strings.Repeat("x", KeywordReplyMaxLen+50)
		s := Settings{
			KeywordConfirmation: true,
			KeywordReply:        long,
			Whitelist:           []string{"match"},
			AddonEnabled:        true,
		}
		got := Classify("match", s)
		if len([]rune(got)) != KeywordReplyMaxLen {
			t.Fatalf("clamp length: got %d runes, want %d", len([]rune(got)), KeywordReplyMaxLen)
		}
	})
	t.Run("exactly at the limit is untouched", func(t *testing.T) {
		exact := strings.Repeat("y", KeywordReplyMaxLen)
		got := Classify("match", Settings{
			KeywordConfirmation: true,
			KeywordReply:        exact,
			Whitelist:           []string{"match"},
			AddonEnabled:        true,
		})
		if got != exact {
			t.Fatalf("at-limit reply changed: got len %d, want %d", len(got), len(exact))
		}
	})
	t.Run("a multibyte reply clamps on a code-point boundary", func(t *testing.T) {
		// Python's [:N] counts CODE POINTS: 5000 emoji runes clamp to
		// 4096 RUNES (16384 bytes), not 4096 bytes. Anything else would
		// send a truncated reply on every Persian/Arabic/CJK task.
		reply := strings.Repeat("😀", 5000)
		got := Classify("match", Settings{
			KeywordConfirmation: true,
			KeywordReply:        reply,
			Whitelist:           []string{"match"},
			AddonEnabled:        true,
		})
		if got != strings.Repeat("😀", KeywordReplyMaxLen) {
			t.Fatalf("multibyte clamp: got %d runes, want %d", len([]rune(got)), KeywordReplyMaxLen)
		}
	})
}

// TestClassifyEmptyInputs pins the nil-safety of the classifier: an empty
// or whitespace-only text simply cannot match a non-empty keyword, exactly
// as Python's empty haystack cannot contain one.
func TestClassifyEmptyInputs(t *testing.T) {
	s := Settings{
		KeywordConfirmation: true,
		KeywordReply:        "ok",
		Whitelist:           []string{"sale"},
		AddonEnabled:        true,
	}
	for _, text := range []string{"", "   ", "\n\t"} {
		if got := Classify(text, s); got != "" {
			t.Fatalf("Classify(%q) = %q, want none", text, got)
		}
	}
}
