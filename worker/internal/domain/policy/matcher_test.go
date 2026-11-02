package policy

import (
	"fmt"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"
)

// --- the folded comparison form ------------------------------------------------

func TestFoldTextUnifiesPersianAndArabicVariants(t *testing.T) {
	// The core promise of the fold: a keyword typed on one keyboard catches
	// text written on another (content_policy.py:63-86, :131-142).
	cases := []struct {
		name string
		text string
		want string
	}{
		{"Arabic yeh -> Persian yeh", "\u064a", "\u06cc"},
		{"alef maksura -> Persian yeh", "\u0649", "\u06cc"},
		{"yeh hamza -> Persian yeh", "\u0626", "\u06cc"},
		{"Arabic kaf -> Persian keh", "\u0643", "\u06a9"},
		{"heh with yeh -> heh", "\u06c0", "\u0647"},
		{"teh marbuta -> heh", "\u0629", "\u0647"},
		{"alef hamza above -> alef", "\u0623", "\u0627"},
		{"alef hamza below -> alef", "\u0625", "\u0627"},
		{"alef madda -> alef", "\u0622", "\u0627"},
		{"alef wasla -> alef", "\u0671", "\u0627"},
		{"waw hamza -> waw", "\u0624", "\u0648"},
		{"empty folds empty", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := FoldText(tc.text, true, false)
			if got != tc.want {
				t.Errorf("FoldText(%q) = %q, want %q", tc.text, got, tc.want)
			}
		})
	}
}

func TestFoldTextStripsInvisibleCharacters(t *testing.T) {
	// Harakat, tatweel and the zero-width/bidi controls make two visually
	// identical strings compare differently (content_policy.py:90-92).
	invisibles := []string{"\u064b", "\u064c", "\u064d", "\u064e", "\u064f", "\u0650", "\u0651", "\u0652", "\u0670", "\u0640", "\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u202a", "\u202e", "\ufeff"}
	for _, ch := range invisibles {
		if got := FoldText("ab"+ch+"cd", true, false); got != "abcd" {
			t.Errorf("FoldText did not strip %q (U+%04X): got %q", ch, ch, got)
		}
	}
	// NFKC-invisible pair on the *needle* side only: the pre-filter must not
	// see a character the matcher then strips.
	if got := FoldText("\u0640\u0640cat\u0640", true, false); got != "cat" {
		t.Errorf("FoldText tatweel: got %q, want %q", got, "cat")
	}
}

func TestFoldTextNFCNormalizesFirst(t *testing.T) {
	// A combining sequence and its precomposed form fold to the same string.
	composed := "آ"    // U+0622
	decomposed := "اٰ" // alef + madda as combining (NFC-unstable input)
	if FoldText(composed, true, false) != FoldText(decomposed, true, false) {
		t.Errorf("NFC: composed %q and decomposed %q fold differently", composed, decomposed)
	}
}

func TestFoldTextCaseFoldingAndFlags(t *testing.T) {
	if got := FoldText("istanbul", true, false); got != "istanbul" {
		t.Errorf("ASCII lowercase: got %q", got)
	}
	// caseSensitive keeps the original case; normalize=false keeps variants.
	if got := FoldText("\u064a", false, true); got != "\u064a" {
		t.Errorf("normalize=false must keep Arabic yeh: got %q", got)
	}
	if got := FoldText("Istanbul", true, true); got != "Istanbul" {
		t.Errorf("caseSensitive must keep case: got %q", got)
	}
}

// --- whitespace collapse ---------------------------------------------------------

func TestCollapseWS(t *testing.T) {
	cases := map[string]string{
		"breaking   news\n\nhere": "breaking news here",
		"a\t\tb":                  "a b",
		"  lead and trail  ":      " lead and trail ",
		"none":                    "none",
		"":                        "",
	}
	for in, want := range cases {
		if got := CollapseWS(in); got != want {
			t.Errorf("CollapseWS(%q) = %q, want %q", in, got, want)
		}
	}
}

// --- the match mode ---------------------------------------------------------------

func TestMatchMode(t *testing.T) {
	cases := []struct {
		needle       string
		wordBoundary bool
		want         string
	}{
		{"cat", true, "word"},
		{"cat", false, "substring"},
		{"#ad", true, "prefix"},
		{"@scam", true, "prefix"},
		{"/start", true, "prefix"},
		{"$btc", true, "prefix"},
		{"!!!", true, "substring"},
		{" Cat", true, "word"}, // mode decided on the folded needle
		{"تخفیف", true, "word"},
	}
	for _, tc := range cases {
		if got := MatchMode(tc.needle, tc.wordBoundary); got != tc.want {
			t.Errorf("MatchMode(%q, %v) = %q, want %q", tc.needle, tc.wordBoundary, got, tc.want)
		}
	}
}

// --- the variant classes -----------------------------------------------------------

func TestVariantClassIsFoldMapInverse(t *testing.T) {
	// Every character a reader would call the same letter must be accepted
	// by the class of the folded form (content_policy.py:151-157). This is
	// the invariant that makes a folded needle still match unfolded text.
	for folded, accept := range variantClass {
		for _, ch := range accept {
			if FoldText(string(ch), true, false) != string(folded) {
				t.Errorf("variant %U folds to %q, not the class key %q",
					ch, FoldText(string(ch), true, false), string(folded))
			}
		}
	}
	// Spot-check the documented classes.
	want := map[rune]string{
		'\u06cc': "\u06cc\u064a\u0649\u0626",
		'\u06a9': "\u06a9\u0643",
		'\u0627': "\u0627\u0623\u0625\u0622\u0671",
		'\u0647': "\u0647\u0629\u06c0",
		'\u0648': "\u0648\u0624",
	}
	for key, w := range want {
		if got := variantClass[key]; got != w {
			t.Errorf("variantClass[%U] = %q, want %q", key, got, w)
		}
	}
}

// --- ContainsWord: the matching semantics thefixtures pin ----------------------------

func TestContainsWordPersianAndArabicVariants(t *testing.T) {
	m := NewMatcher()
	// FLT-PERSIAN-01: the text uses Persian ی (U+06CC), the needle Arabic ي
	// (U+064A). fold_text (content_policy.py:131) normalizes the variants.
	text := "تخفیف ویژه امروز"
	if !m.ContainsWord(text, "تخفيف", true, true, false) {
		t.Errorf("Arabic yeh needle must match Persian yeh text")
	}
	// The reverse direction: Persian needle, Arabic text.
	if !m.ContainsWord("تخفيف ویژه", "تخفیف", true, true, false) {
		t.Errorf("Persian yeh needle must match Arabic yeh text")
	}
}

func TestContainsWordInvisibleRunBetweenLetters(t *testing.T) {
	m := NewMatcher()
	// A keyword still finds a word whose letters were typed with harakat or
	// a zero-width joiner inside it — the invisible run is allowed between
	// the characters of the keyword (content_policy.py:176-214).
	if !m.ContainsWord("کت\u200dاب", "کتاب", true, true, false) {
		t.Errorf("ZWJ inside the word must not break the match")
	}
	if !m.ContainsWord("کٌتًاب", "کتاب", true, true, false) {
		t.Errorf("harakat inside the word must not break the match")
	}
	if !m.ContainsWord("کــتــاب", "کتاب", true, true, false) {
		t.Errorf("tatweel inside the word must not break the match")
	}
}

func TestContainsWordBoundaries(t *testing.T) {
	m := NewMatcher()
	// A word is a word: substring matching makes `cat` match `concatenate`
	// and blacklists things nobody blacklisted (content_policy.py:28-32).
	if m.ContainsWord("concatenate", "cat", true, true, false) {
		t.Errorf("word mode must not match inside a larger word")
	}
	if !m.ContainsWord("the cat sat", "cat", true, true, false) {
		t.Errorf("word mode must match a standalone word")
	}
	// Without the boundary, a substring is fine.
	if !m.ContainsWord("concatenate", "cat", false, true, false) {
		t.Errorf("substring mode must match inside a larger word")
	}
	// A token-family keyword matches its whole family: `#ad` catches
	// `#advert`, `@scam` catches `@scam_bot`.
	if !m.ContainsWord("#advert", "#ad", true, true, false) {
		t.Errorf("prefix mode must match the tag family")
	}
	if !m.ContainsWord("@scam_bot", "@scam", true, true, false) {
		t.Errorf("prefix mode must match the handle family")
	}
	// ...but the marker is what makes it a token: `#ad` must not be found
	// inside a bare word.
	if m.ContainsWord("leading #advert", "ad", true, true, false) {
		t.Errorf("word mode must not match the inside of a tag")
	}
}

func TestContainsWordPhraseAcrossWhitespace(t *testing.T) {
	m := NewMatcher()
	// POL-PHRASE-01: a phrase survives the extra space or newline an editor
	// leaves between its words.
	if !m.ContainsWord("breaking   news\n\nhere", "breaking news", true, true, false) {
		t.Errorf("a phrase must match across collapsed whitespace")
	}
}

func TestContainsWordCaseSensitivity(t *testing.T) {
	m := NewMatcher()
	if !m.ContainsWord("SPAM spam", "spam", true, true, false) {
		t.Errorf("case-insensitive is the default")
	}
	// Python's case-sensitive compile keeps re.IGNORECASE unless the FOLDED
	// prefix is unknown to it. `spam` folds to ASCII, which (?i) still
	// case-folds, so case_sensitive=True does not sharpen it (verified
	// against the reference implementation). A needle whose folded form is
	// non-ASCII is where the flag actually bites.
	if m.ContainsWord("SPAM", "spam", true, true, true) {
		t.Errorf("caseSensitive on an ASCII needle must not match different case (Python parity)")
	}
	// Reference-verified: contains_word("SPAM","SPAM", case_sensitive=True) is
	// False in Python too — the (?i) flag is always applied to the compiled
	// pattern (content_policy goes case_sensitive=False through the fold, then
	// compiles with IGNORECASE unless the folded form is non-ASCII). Do NOT
	// assert it matches: that would contradict the reference implementation.
	if m.ContainsWord("SPAM", "SPAM", true, true, true) {
		t.Errorf("caseSensitive must not match identical ASCII case either — Python says False (differential-verified)")
	}
	// The Persian/Arabic case: the fold map is applied and (?i) over a
	// non-ASCII class does not unify case, so a non-identical variant of
	// the same letter stops matching under caseSensitive.
	if !m.ContainsWord("تخفیف", "تخفیف", true, true, true) {
		t.Errorf("caseSensitive must match identical non-ASCII text")
	}
}

func TestContainsWordCaseSensitivityPersian(t *testing.T) {
	// The yeh variants are distinct code points, not case forms: matching
	// one against the other is the variant fold's job, not the case flag's,
	// and the fold runs under both flags.
	m := NewMatcher()
	if !m.ContainsWord("تخفیف", "تخفيف", true, true, true) {
		t.Errorf("variant folding applies under caseSensitive too")
	}
}

func TestContainsWordDegenerateNeedles(t *testing.T) {
	m := NewMatcher()
	// A keyword that folds away entirely matches nothing
	// (content_policy.py:190-191).
	if m.ContainsWord("anything", "\u0640\u064b\u200b", true, true, false) {
		t.Errorf("a needle that folds away must match nothing")
	}
	if m.ContainsWord("anything", "   ", true, true, false) {
		t.Errorf("a blank needle must match nothing")
	}
	if m.ContainsWord("", "cat", true, true, false) {
		t.Errorf("an empty haystack must match nothing")
	}
}

func TestContainsWordWithoutNormalize(t *testing.T) {
	m := NewMatcher()
	// normalize=false keeps the letter variants AND the invisibles, so an
	// Arabic-yeh needle no longer matches Persian-yeh text.
	if m.ContainsWord("تخفیف", "تخفيف", true, false, false) {
		t.Errorf("normalize=false must keep the yeh variants distinct")
	}
	if !m.ContainsWord("تخفيف", "تخفيف", true, false, false) {
		t.Errorf("normalize=false must still match identical text")
	}
}

// --- the folded-needle pre-filter ----------------------------------------------

func TestPreFilterNeverChangesTheDecision(t *testing.T) {
	// The pre-filter runs on the folded body, the matcher on the original
	// text. Whatever the matcher decides, the pre-filter must only change
	// the cost, never the outcome. Checked by exhausting the corner cases
	// where the two views can disagree.
	m := NewMatcher()
	cases := []struct {
		haystack string
		needle   string
	}{
		{"breaking   news\n\nhere", "breaking news"}, // pre-filter: collapsed
		{"cat", "cat"},
		{"concatenate", "cat"},
		{"xکتابy", "کتاب"},               // boundary on a non-ASCII word
		{"کتاـب", "کتاب"},                // invisible break: both sides strip it
		{"\u0640\u0640cat\u0640", "cat"}, // invisibles around the needle
	}
	for _, tc := range cases {
		if got := m.FirstMatchingWord(tc.haystack, []string{tc.needle}, true, false, true); (got != "") !=
			m.ContainsWord(tc.haystack, tc.needle, true, true, false) {
			t.Errorf("pre-filter disagrees with the matcher for %q / %q: got %q",
				tc.haystack, tc.needle, got)
		}
	}
}

func TestFirstMatchingWordPreservesListOrder(t *testing.T) {
	m := NewMatcher()
	// A rejection names the first offender the user typed, not an arbitrary
	// one (content_policy.py:266-269).
	got := m.FirstMatchingWord("both spam and scam appear", []string{"scam", "spam"}, true, false, true)
	if got != "scam" {
		t.Errorf("FirstMatchingWord = %q, want %q (first listed)", got, "scam")
	}
	// Blank entries are skipped, not returned as a match.
	got = m.FirstMatchingWord("anything", []string{"", "  ", "cat"}, true, false, true)
	if got != "" {
		t.Errorf("blank entries must be skipped, got %q", got)
	}
	if got := m.FirstMatchingWord("", []string{"cat"}, true, false, true); got != "" {
		t.Errorf("empty haystack must return empty, got %q", got)
	}
}

// --- concurrency: the caches are shared across every Evaluate call ----------------

func TestMatcherIsSafeForConcurrentUse(t *testing.T) {
	m := NewMatcher()
	needles := []string{"cat", "تخفیف", "breaking news", "#ad", "@scam", "spam", "کتاب"}
	var wg sync.WaitGroup
	for i := 0; i < 200; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			n := needles[i%len(needles)]
			for _, body := range []string{"breaking   news\n\nhere", "concatenate the spam", "تخفیف ویژه"} {
				m.ContainsWord(body, n, true, true, false)
				m.FirstMatchingWord(body, needles, true, false, true)
			}
		}(i)
	}
	wg.Wait()
}

func TestFoldedNeedleCacheIsConsistent(t *testing.T) {
	// The cached comparison form is the same value the uncached path
	// produces, whatever the flags.
	for _, word := range []string{"cat", "تخفیف", "Breaking News", "\u0640\u0640x"} {
		for _, normalize := range []bool{true, false} {
			for _, caseSensitive := range []bool{true, false} {
				want := ""
				needle := strings.TrimSpace(word)
				if needle != "" {
					want = CollapseWS(FoldText(needle, normalize, caseSensitive))
				}
				if got := foldedNeedle(word, normalize, caseSensitive); got != want {
					t.Errorf("foldedNeedle(%q,%v,%v) = %q, want %q",
						word, normalize, caseSensitive, got, want)
				}
			}
		}
	}
}

// --- differential testing against the live Python reference ----------------------

// pyContainsWord runs Python's contains_word in a subprocess and reports
// whether it matched. The Python module is the reference implementation this
// port must agree with; a divergence here is a bug, not an optimization.
func pyContainsWord(t *testing.T, haystack, needle string, normalize, caseSensitive, wordBoundary bool) bool {
	t.Helper()
	// %v formats a Go bool as true/false, but Python needs True/False or
	// the reference raises NameError and the differential silently fails.
	flags := []string{
		fmt.Sprintf("normalize=%s", pyBool(normalize)),
		fmt.Sprintf("case_sensitive=%s", pyBool(caseSensitive)),
		fmt.Sprintf("word_boundary=%s", pyBool(wordBoundary)),
	}
	expr := fmt.Sprintf(
		"from core.domain.content_policy import contains_word; print(contains_word(%q, %q, %s))",
		haystack, needle, strings.Join(flags, ", "))
	return strings.Contains(strings.TrimSpace(pyRun(t, expr)), "True")
}

// pyBool renders a Go bool the way a Python keyword argument needs it.
func pyBool(b bool) string {
	if b {
		return "True"
	}
	return "False"
}

func pyFold(t *testing.T, text string, normalize, caseSensitive bool) string {
	t.Helper()
	// %v renders a Go bool as true/false, which Python would read as an
	// undefined name — pyBool renders the Python spelling.
	expr := fmt.Sprintf(
		"from core.domain.content_policy import fold_text; print(fold_text(%q, normalize=%s, case_sensitive=%s))",
		text, pyBool(normalize), pyBool(caseSensitive))
	// print() (not repr) so the comparison is against the raw string: repr
	// escapes a newline as the two characters backslash-n, which would make
	// every multi-line text look like a divergence.
	return strings.TrimRight(pyRun(t, expr), "\n")
}

func TestDifferentialFoldText(t *testing.T) {
	if testing.Short() {
		t.Skip("python differential skipped in -short mode")
	}
	cases := []struct {
		text          string
		normalize     bool
		caseSensitive bool
	}{
		{"تخفیف ویژه امروز", true, false},
		{"تخفيف ويژه", true, false},
		{"CAT cat", true, false},
		{"CAT cat", true, true},
		{"کت\u200dاب", true, false},
		{"کت\u200dاب", false, false},
		{"breaking   news\n\nhere", true, false},
		{"Ürün", true, false},
		{"\u0622\u0623\u0625", true, false},
	}
	for _, tc := range cases {
		want := pyFold(t, tc.text, tc.normalize, tc.caseSensitive)
		got := FoldText(tc.text, tc.normalize, tc.caseSensitive)
		if got != want {
			t.Errorf("FoldText(%q, norm=%v, cs=%v) = %q, Python says %q",
				tc.text, tc.normalize, tc.caseSensitive, got, want)
		}
	}
}

func TestDifferentialContainsWord(t *testing.T) {
	if testing.Short() {
		t.Skip("python differential skipped in -short mode")
	}
	m := NewMatcher()
	cases := []struct {
		haystack      string
		needle        string
		normalize     bool
		caseSensitive bool
		wordBoundary  bool
	}{
		// FLT-PERSIAN-01, both directions.
		{"تخفیف ویژه امروز", "تخفيف", true, false, true},
		{"تخفیف ویژه امروز", "تخفیف", true, false, true},
		{"تخفيف ويژه", "تخفیف", true, false, true},
		// Boundaries.
		{"concatenate", "cat", true, false, true},
		{"the cat sat", "cat", true, false, true},
		{"concatenate", "cat", true, false, false},
		{"#advert", "#ad", true, false, true},
		{"@scam_bot", "@scam", true, false, true},
		{"leading #advert", "ad", true, false, true},
		// Whitespace tolerance.
		{"breaking   news\n\nhere", "breaking news", true, false, true},
		{"breaking news", "breaking   news", true, false, true},
		// Case.
		{"SPAM spam", "spam", true, false, true},
		{"SPAM", "spam", true, true, true},
		// Invisibles between letters.
		{"کت\u200dاب", "کتاب", true, false, true},
		{"کٌتًاب", "کتاب", true, false, true},
		{"کــتــاب", "کتاب", true, false, true},
		{"کتاب", "کت\u200dاب", true, false, true},
		// Without normalize, variants stay distinct.
		{"تخفیف", "تخفيف", false, false, true},
		{"تخفيف", "تخفيف", false, false, true},
		// Degenerate.
		{"anything", "   ", true, false, true},
		{"anything", "\u0640", true, false, true},
		{"", "cat", true, false, true},
		// Latin-1 / non-ASCII folding.
		{"Ürünler", "ürün", true, false, true},
		{"STRASSE", "straße", true, false, true},
		{"İstanbul", "i", true, false, true},
	}
	for _, tc := range cases {
		t.Run(fmt.Sprintf("%q/%q", tc.haystack, tc.needle), func(t *testing.T) {
			want := pyContainsWord(t, tc.haystack, tc.needle, tc.normalize, tc.caseSensitive, tc.wordBoundary)
			got := m.ContainsWord(tc.haystack, tc.needle, tc.wordBoundary, tc.normalize, tc.caseSensitive)
			if got != want {
				t.Errorf("ContainsWord(%q, %q, wb=%v, norm=%v, cs=%v) = %v, Python says %v",
					tc.haystack, tc.needle, tc.wordBoundary, tc.normalize, tc.caseSensitive, got, want)
			}
		})
	}
}

func TestDifferentialFirstMatchingWord(t *testing.T) {
	if testing.Short() {
		t.Skip("python differential skipped in -short mode")
	}
	m := NewMatcher()
	cases := []struct {
		haystack string
		words    []string
	}{
		{"both spam and scam appear", []string{"scam", "spam"}},
		{"both spam and scam appear", []string{"spam", "scam"}},
		{"تخفیف ویژه", []string{"تخفيف", "اسلام"}},
		{"breaking news here", []string{"breaking news", "cat"}},
		{"concatenate", []string{"cat", "concatenate"}},
		{"nothing here", []string{"cat", "dog"}},
	}
	for _, tc := range cases {
		t.Run(strings.Join(tc.words, ","), func(t *testing.T) {
			expr := fmt.Sprintf(
				"from core.domain.content_policy import _first_matching_word; print(repr(_first_matching_word(%q, %v, normalize=True, case_sensitive=False, word_boundary=True)))",
				tc.haystack, pyList(tc.words))
			want := strings.Trim(strings.TrimSpace(pyRun(t, expr)), "'")
			got := m.FirstMatchingWord(tc.haystack, tc.words, true, false, true)
			if got != want {
				t.Errorf("FirstMatchingWord(%q, %v) = %q, Python says %q",
					tc.haystack, tc.words, got, want)
			}
		})
	}
}

// pyList renders a Go []string as a Python tuple literal.
func pyList(words []string) string {
	parts := make([]string, 0, len(words))
	for _, w := range words {
		parts = append(parts, fmt.Sprintf("%q", w))
	}
	return "(" + strings.Join(parts, ", ") + ")"
}

var _ = utf8.RuneCountInString
