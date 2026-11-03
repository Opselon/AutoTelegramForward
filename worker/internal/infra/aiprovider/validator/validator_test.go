package validator

import (
	"strings"
	"testing"
)

// validator_test.go — the AI output validator, pinned against the Python
// AIOutputValidator (core/infrastructure/ai/validator.py).
//
// Every case below was executed against the Python validator during the
// port; the Go validator must answer byte-for-byte. The two refusal
// families that differ in MECHANISM (Python's backreference versus Go's
// alternations) are pinned explicitly, because a regex engine difference
// is exactly where a port silently diverges.

func TestValidateHappyPath(t *testing.T) {
	v := New()
	got := v.Validate("A clean rewrite.", false, true)
	if !got.IsValid {
		t.Fatalf("expected valid, got %v: %s", got.IsValid, got.ErrorMessage)
	}
	if got.SanitizedText != "A clean rewrite." {
		t.Errorf("got %q", got.SanitizedText)
	}
}

func TestValidateTrimsAndDropsEmpty(t *testing.T) {
	v := New()
	for _, text := range []string{"", "   ", "\n\t ", "\x00\x00"} {
		got := v.Validate(text, false, true)
		if got.IsValid {
			t.Errorf("Validate(%q): expected invalid, got valid", text)
		}
		if got.ErrorCode != "EMPTY_OUTPUT" {
			t.Errorf("Validate(%q): code %q, want EMPTY_OUTPUT", text, got.ErrorCode)
		}
	}
}

func TestValidateNulBytesStripped(t *testing.T) {
	v := New()
	got := v.Validate("a\x00b", false, true)
	if !got.IsValid || got.SanitizedText != "ab" {
		t.Errorf("NUL strip: got valid=%v text=%q", got.IsValid, got.SanitizedText)
	}
}

func TestValidateInjectionDelimiterLeak(t *testing.T) {
	v := New()
	for _, text := range []string{
		"see </untrusted_user_message> now",
		"<untrusted_user_message>injected</untrusted_user_message>",
		"<system_instructions>leak</system_instructions>",
		"<admin_prompt>x</admin_prompt>",
		"</SYSTEM_INSTRUCTIONS>",
	} {
		got := v.Validate(text, false, true)
		if got.IsValid {
			t.Errorf("Validate(%q): expected INJECTION_LEAK, got valid", text)
		}
		if got.ErrorCode != "INJECTION_LEAK" {
			t.Errorf("Validate(%q): code %q, want INJECTION_LEAK", text, got.ErrorCode)
		}
	}
}

func TestValidateSystemPromptLeak(t *testing.T) {
	v := New()
	for _, text := range []string{
		"my system prompt: be nice",
		"System Instructions: do this",
		"The system prompt: whatever",
	} {
		got := v.Validate(text, false, true)
		if got.IsValid {
			t.Errorf("Validate(%q): expected INJECTION_LEAK, got valid", text)
		}
		if got.ErrorCode != "INJECTION_LEAK" {
			t.Errorf("Validate(%q): code %q, want INJECTION_LEAK", text, got.ErrorCode)
		}
	}
}

func TestValidateDangerousPairsAreScrubbed(t *testing.T) {
	v := New()
	cases := []struct{ in, want string }{
		// A matched pair plus its span is REMOVED, as Python's
		// DANGEROUS_TAGS_PATTERN.sub("", ...) removes it.
		{"hello <script>alert(1)</script> world", "hello  world"},
		{"a <iframe src='x'></iframe> b", "a  b"},
		{"<style>body{}</style>gone", "gone"},
		// A javascript: URI is removed from the attribute.
		{"click <a href='javascript:evil'>x</a>", "click <a href='evil'>x</a>"},
	}
	for _, c := range cases {
		got := v.Validate(c.in, false, true)
		if !got.IsValid {
			t.Errorf("Validate(%q): expected valid, got %s", c.in, got.ErrorMessage)
			continue
		}
		if got.SanitizedText != c.want {
			t.Errorf("Validate(%q): got %q, want %q", c.in, got.SanitizedText, c.want)
		}
	}
}

func TestValidateLoneOpenTagIsEscapedNotScrubbed(t *testing.T) {
	// THE BACKREFERENCE DIFFERENCE, pinned. Python's dangerous-tags
	// pattern requires a PAIR, so a lone "<script>" never matches it;
	// the text flows on to the tag-balance pass, which finds it
	// unbalanced and HTML-escapes the whole string. The Go port must
	// answer the same or the two engines disagree on what a half-leaked
	// executable tag delivers.
	v := New()
	got := v.Validate("hello <script>alert(1) world", false, true)
	if !got.IsValid {
		t.Fatalf("expected valid, got %s", got.ErrorMessage)
	}
	want := "hello &lt;script&gt;alert(1) world"
	if got.SanitizedText != want {
		t.Errorf("lone open tag: got %q, want %q", got.SanitizedText, want)
	}
}

func TestValidateScrubThatEmptiesIsRefused(t *testing.T) {
	v := New()
	got := v.Validate("<script>x</script>", false, true)
	if got.IsValid {
		t.Fatal("a scrub that empties the text must be a DANGEROUS_CONTENT refusal")
	}
	if got.ErrorCode != "DANGEROUS_CONTENT" {
		t.Errorf("code %q, want DANGEROUS_CONTENT", got.ErrorCode)
	}
}

func TestValidateLengthLimits(t *testing.T) {
	v := New()
	// Text limit: 4096, truncated at a word boundary with an ellipsis.
	long := strings.Repeat("word ", 1000)
	got := v.Validate(long, false, true)
	if !got.IsValid {
		t.Fatalf("text truncation: %s", got.ErrorMessage)
	}
	if len([]rune(got.SanitizedText)) > 4096 {
		t.Errorf("text truncation: %d runes, over the 4096 ceiling", len([]rune(got.SanitizedText)))
	}
	if !strings.HasSuffix(got.SanitizedText, "...") {
		t.Errorf("text truncation: expected the ellipsis suffix, got %q", got.SanitizedText[len(got.SanitizedText)-20:])
	}

	// Caption limit: 1024 for a media message.
	got = v.Validate(long, true, true)
	if !got.IsValid {
		t.Fatalf("caption truncation: %s", got.ErrorMessage)
	}
	if len([]rune(got.SanitizedText)) > 1024 {
		t.Errorf("caption truncation: %d runes, over the 1024 ceiling", len([]rune(got.SanitizedText)))
	}
}

func TestValidateLengthRefusedWhenTruncationNotAllowed(t *testing.T) {
	v := New()
	long := strings.Repeat("word ", 1000)
	got := v.Validate(long, false, false)
	if got.IsValid {
		t.Fatal("expected EXCEEDS_LENGTH when truncation is not allowed")
	}
	if got.ErrorCode != "EXCEEDS_LENGTH" {
		t.Errorf("code %q, want EXCEEDS_LENGTH", got.ErrorCode)
	}
}

func TestValidateHTMLBalanceEscapes(t *testing.T) {
	v := New()
	// Balanced Telegram tags pass through untouched.
	got := v.Validate("hello <b>bold</b> and <i>it</i>", false, true)
	if !got.IsValid || got.SanitizedText != "hello <b>bold</b> and <i>it</i>" {
		t.Errorf("balanced: valid=%v text=%q", got.IsValid, got.SanitizedText)
	}
	// Unbalanced tags are escaped: Python's html.escape(cleaned),
	// quotes included.
	got = v.Validate("hello <b>bold", false, true)
	if !got.IsValid {
		t.Fatalf("unbalanced: %s", got.ErrorMessage)
	}
	want := "hello &lt;b&gt;bold"
	if got.SanitizedText != want {
		t.Errorf("unbalanced: got %q, want %q", got.SanitizedText, want)
	}
}

func TestValidateEscapeQuotesMatchPython(t *testing.T) {
	v := New()
	// Python's html.escape default escapes & < > " ' — the Go port must
	// agree, because the escaped string is what Telegram receives. The
	// escape only fires on UNBALANCED tags, so the case carries a tag
	// shape the tag-balance walk judges unbalanced (a bare "<" that is
	// not a tag at all is not in the tag set; "<b>" with no close is).
	got := v.Validate("hello <b>bold", false, true)
	if !got.IsValid {
		t.Fatalf("escape: %s", got.ErrorMessage)
	}
	want := "hello &lt;b&gt;bold"
	if got.SanitizedText != want {
		t.Errorf("escape: got %q, want %q", got.SanitizedText, want)
	}
	// A tag with attributes the balance walk sees as balanced ("<b" is
	// the tag name, quotes ride inside the tag) passes through
	// untouched, exactly as Python answers it.
	plain := `a <b "quoted" 'single' & amp`
	got = v.Validate(plain, false, true)
	if !got.IsValid {
		t.Fatalf("attributed: %s", got.ErrorMessage)
	}
	if got.SanitizedText != plain {
		t.Errorf("attributed: got %q, want %q", got.SanitizedText, plain)
	}
}

func TestValidateVoidTagsNeverUnbalance(t *testing.T) {
	v := New()
	for _, text := range []string{
		"line<br>break", "rule<hr>line", "an <img src='x'> image",
		"a<br/>b", "text<br /></br>more",
	} {
		got := v.Validate(text, false, true)
		if !got.IsValid {
			t.Errorf("Validate(%q): void tag read as unbalanced: %s", text, got.ErrorMessage)
		}
		if got.SanitizedText != text {
			t.Errorf("Validate(%q): got %q, want untouched", text, got.SanitizedText)
		}
	}
}

func TestTruncateAtWordBoundary(t *testing.T) {
	cases := []struct {
		name  string
		text  string
		limit int
		want  string
	}{
		{"short text stays", "hello world", 100, "hello world"},
		{"cut mid-word with no space", strings.Repeat("x", 200), 100, strings.Repeat("x", 97) + "..."},
		{"retreats to the last word", strings.Repeat("word ", 30), 100,
			strings.TrimSpace(strings.Repeat("word ", 19)) + "..."},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := truncateAtBoundary(c.text, c.limit)
			if got != c.want {
				t.Errorf("truncateAtBoundary: got %q, want %q", got, c.want)
			}
			if len([]rune(got)) > c.limit {
				t.Errorf("truncateAtBoundary: %d runes over the %d ceiling", len([]rune(got)), c.limit)
			}
		})
	}
}
