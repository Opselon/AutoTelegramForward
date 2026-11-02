package policy

import (
	"strings"
	"testing"
)

// --- the canonical HasLink predicate ------------------------------------------

func TestHasLinkPinsTheF14Divergences(t *testing.T) {
	cases := []struct {
		text string
		want bool
		why  string
	}{
		// LNK-GATE-01: tg:// and ftp:// pass the FilterEngine's gate but are
		// links to has_link — the F-14 divergence this package pins.
		{"see tg://resolve?domain=x", true, "tg:// is a link to a reader"},
		{"ftp://example.com/file.zip", true, "ftp:// is a link"},
		{"https://a.io", true, "https"},
		{"http://a.io", true, "http"},
		{"see www.a.io now", true, "bare www. host"},
		{"join t.me/+abc123", true, "t.me short link"},
		{"telegram.me/foo", true, "telegram.me"},
		{"telegram.dog/foo", true, "telegram.dog"},
		{"HTTP://A.IO", true, "case-insensitive"},
		{"Tg://resolve", true, "case-insensitive"},
		// LNK-GATE-02: a bare @username is NOT a link — the other half of
		// F-14. The FilterEngine's URL_REGEX includes
		// @[A-Za-z0-9_]{3,32} as an alternation and drops these messages.
		{"email @user hi", false, "a mention is not a link"},
		{"@scam_bot", false, "a handle is not a link"},
		{"plain text only", false, "no link"},
		{"", false, "empty is not a link"},
		{"a.b.c without scheme", false, "a dotted name without a scheme or www."},
	}
	for _, tc := range cases {
		if got := HasLink(tc.text); got != tc.want {
			t.Errorf("HasLink(%q) = %v, want %v (%s)", tc.text, got, tc.want, tc.why)
		}
	}
}

// TestHasLinkMatchesPythonHasLink is the differential claim: HasLink agrees
// with the reference implementation on every input the fixtures exercise and
// a spread past them. content_policy.py:121-128 defines the canonical
// predicate; this is the one the ContentPolicy block-links gate applies.
func TestHasLinkMatchesPythonHasLink(t *testing.T) {
	if testing.Short() {
		t.Skip("python differential skipped in -short mode")
	}
	texts := []string{
		"",
		"plain text",
		"https://a.io",
		"http://a.io",
		"HTTP://A.IO",
		"ftp://files.example.com/x.zip",
		"see tg://resolve?domain=x",
		"join t.me/+abc123",
		"T.ME/abc",
		"telegram.me/foo",
		"telegram.dog/foo",
		"see www.a.io now",
		"WWW.A.IO",
		"email @user hi",
		"@scam_bot",
		"mailto:someone@example.com",
		"file:///etc/passwd",
		"//a.io protocol-relative",
		"x.y.z",
		"https://a.io and t.me/b and @mention",
	}
	for _, text := range texts {
		t.Run(quoteGo(text), func(t *testing.T) {
			expr := "from core.domain.content_policy import has_link; print(has_link(" +
				quoteGo(text) + "))"
			want := strings.Contains(strings.TrimSpace(pyRun(t, expr)), "True")
			if got := HasLink(text); got != want {
				t.Errorf("HasLink(%q) = %v, Python has_link says %v", text, got, want)
			}
		})
	}
}

// --- Classify -----------------------------------------------------------------

func TestClassifyReportsEveryCategory(t *testing.T) {
	cases := []struct {
		text string
		want []LinkClass
	}{
		{"https://a.io", []LinkClass{LinkHTTPS}},
		{"http://a.io", []LinkClass{LinkHTTP}},
		{"ftp://a.io/f", []LinkClass{LinkFTP}},
		{"t.me/+abc", []LinkClass{LinkTelegram}},
		{"telegram.me/foo", []LinkClass{LinkTelegramMe}},
		{"telegram.dog/foo", []LinkClass{LinkTelegramDog}},
		{"tg://resolve?domain=x", []LinkClass{LinkTG}},
		{"see www.a.io now", []LinkClass{LinkWWW}},
		{"", nil},
		{"no links here", nil},
	}
	for _, tc := range cases {
		if got := Classify(tc.text); !classesEqual(got, tc.want) {
			t.Errorf("Classify(%q) = %v, want %v", tc.text, got, tc.want)
		}
	}
}

func TestClassifyIsOrderedAndComplete(t *testing.T) {
	// Every link HasLink reports appears in Classify's output, in order, and
	// a text with several links reports all of them exactly once.
	text := "see https://a.io then t.me/b and tg://x plus www.c.io"
	want := []LinkClass{LinkHTTPS, LinkTelegram, LinkTG, LinkWWW}
	if got := Classify(text); !classesEqual(got, want) {
		t.Errorf("Classify(%q) = %v, want %v", text, got, want)
	}
	if !HasLink(text) {
		t.Errorf("HasLink must report the multi-link text as a link")
	}
}

func TestClassifyCaseInsensitive(t *testing.T) {
	// The Python gate is re.IGNORECASE; the classifier must not lose a link
	// to case, and every scheme's class must survive it.
	for _, tc := range []struct {
		text string
		want LinkClass
	}{
		{"HTTP://A.IO", LinkHTTP},
		{"HTTPS://A.IO", LinkHTTPS},
		{"Tg://x", LinkTG},
		{"T.ME/x", LinkTelegram},
		{"Www.a.io", LinkWWW},
		{"FTP://a", LinkFTP},
	} {
		if got := Classify(tc.text); len(got) != 1 || got[0] != tc.want {
			t.Errorf("Classify(%q) = %v, want [%v]", tc.text, got, tc.want)
		}
	}
}

func TestClassifyHasLinkAgreement(t *testing.T) {
	// The invariant the spec asks for: no span HasLink reports is missing
	// from Classify, and no span Classify reports is missing from HasLink.
	texts := []string{
		"https://a.io", "tg://x", "@mention", "t.me/x", "www.a.io",
		"nothing", "https://a.io and t.me/b", "email @user hi",
	}
	for _, text := range texts {
		classes := Classify(text)
		if has := HasLink(text); has != (len(classes) > 0) {
			t.Errorf("HasLink(%q) = %v but Classify returned %v — the two disagree",
				text, has, classes)
		}
	}
}

// --- helpers -------------------------------------------------------------------

func classesEqual(a, b []LinkClass) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func quoteGo(s string) string {
	var sb strings.Builder
	sb.WriteRune('"')
	for _, r := range s {
		switch r {
		case '"':
			sb.WriteString(`\"`)
		case '\\':
			sb.WriteString(`\\`)
		case '\n':
			sb.WriteString(`\n`)
		case '\t':
			sb.WriteString(`\t`)
		case '\r':
			sb.WriteString(`\r`)
		default:
			sb.WriteRune(r)
		}
	}
	sb.WriteRune('"')
	return sb.String()
}
