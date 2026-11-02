// Package policy is the Go port of Python's core.domain.content_policy.
//
// This is a behavioral port, not a redesign: the check order, the reason
// strings, the Persian/Arabic-aware normalization and the fail-closed regex
// handling mirror content_policy.py one-for-one. A task whose policy decides
// differently in Go than in Python forwards a different set of messages
// depending on which engine picked the job up, so every divergence here is a
// bug, including the ones nobody has written a fixture for yet.
//
// matcher.go holds the shared matching primitives (content_policy.py:121-309):
// fold_text, the variant classes, the needle-pattern compiler and the
// folded-needle pre-filter. policy.go consumes them for decisions; later
// stages will consume them for transforms, which is why the pattern matches
// the ORIGINAL text (folding the haystack would move character offsets the
// strip-words transform needs).
package policy

import (
	"regexp"
	"strings"
	"sync"
	"unicode"

	"golang.org/x/text/unicode/norm"
)

// --- invisible characters (content_policy.py:90-92, :161) -------------------

// invisibleRe is the set of characters that make two visually identical
// strings compare differently: harakat (U+064B-0652), superscript alef
// (U+0670), tatweel (U+0640), the zero-width and bidi controls
// (U+200B-200F, U+202A-202E) and the BOM (U+FEFF). Stripped before
// comparison, and allowed *between* the characters of a keyword.
//
// NOTE (RE2): Go's regexp syntax has no \u escape — the class is written with
// literal Unicode characters, not the \uXXXX forms the Python source uses.
// Same set, different spelling.
var invisibleRe = regexp.MustCompile("[\u064b-\u0652\u0670\u0640\u200b-\u200f\u202a-\u202e\ufeff]")

// invisibleRun is the same set as a zero-or-more pattern, inserted between the
// characters of a keyword when normalizing (Python _INVISIBLE_RUN, :161).
// Literal Unicode for the same RE2 reason.
const invisibleRun = "[\u064b-\u0652\u0670\u0640\u200b-\u200f\u202a-\u202e\ufeff]*"

// --- folding (content_policy.py:63-86, :131-142) -----------------------------

// foldMap unifies the letter variants Telegram users type interchangeably
// (Python _FOLD_MAP, :63-86). Keys are the Arabic/legacy forms, values the
// canonical Persian/base form. Folding means one keyword catches every
// spelling of the same word: a blacklist typed on one keyboard does not
// silently miss text written on another.
var foldMap = map[rune]rune{
	'\u064a': '\u06cc', // Arabic yeh           -> Persian yeh
	'\u0649': '\u06cc', // alef maksura         -> Persian yeh
	'\u0626': '\u06cc', // yeh hamza            -> Persian yeh
	'\u0643': '\u06a9', // Arabic kaf           -> Persian keh
	'\u06c0': '\u0647', // heh with yeh         -> heh
	'\u0629': '\u0647', // teh marbuta          -> heh
	'\u0623': '\u0627', // alef hamza above     -> alef
	'\u0625': '\u0627', // alef hamza below     -> alef
	'\u0622': '\u0627', // alef madda           -> alef
	'\u0671': '\u0627', // alef wasla           -> alef
	'\u0624': '\u0648', // waw hamza            -> waw
}

// variantClass is the inverse of foldMap for matching, in spirit: keyed on the
// *folded* form, it lists every character a reader would call the same letter,
// so a needle character becomes a character class that accepts them all
// (Python _VARIANT_CLASS, :151-157). The class contents are the union of the
// foldMap targets AND their sources — folded ي is ی, and the class for ی must
// still accept the text that typed ي directly, because fold_text was applied
// to the *needle* only.
var variantClass = map[rune]string{
	'\u06cc': "\u06cc\u064a\u0649\u0626",       // yeh
	'\u06a9': "\u06a9\u0643",                   // kaf/keh
	'\u0627': "\u0627\u0623\u0625\u0622\u0671", // alef
	'\u0647': "\u0647\u0629\u06c0",             // heh
	'\u0648': "\u0648\u0624",                   // waw
}

// tokenMarkers: a keyword that opens with one of these is a token family
// rather than a word: `#ad` is a hashtag prefix, `@chan` a handle, `/start` a
// command (Python _TOKEN_MARKERS, :147).
var tokenMarkers = map[rune]bool{'#': true, '@': true, '$': true, '/': true}

// FoldText is the comparison form of a text: NFC normalization, strip the
// invisible characters, fold the Persian/Arabic letter variants, then
// case-fold unless case-sensitive (Python fold_text, :131-142).
//
// Mirrors the Python signature: normalize=false keeps the letter variants AND
// the invisibles, caseSensitive=true keeps the original case.
// FoldText returns the comparison form of a string: NFC normalization,
// invisible-character stripping and Persian/Arabic letter-variant unification
// when normalize is set, then case folding unless caseSensitive.
//
// The case step MUST be a real Unicode case fold, not strings.ToLower: Python's
// str.casefold() maps ß→ss, ﬀ→ff and İ→i̇, and _needle_pattern builds the
// pattern from the FOLDED needle (content_policy.py folds the needle at
// :186 before compiling). A ToLower port would leave "straße" as "straße"
// where Python builds "strasse", so a needle the owner typed with a ß would
// never match the "STRASSE" the pipeline's Python engine matches — a silent
// decision change on every script with a sharp-s or a ligature.
func FoldText(text string, normalize, caseSensitive bool) string {
	if text == "" {
		return ""
	}
	// Fast path: a pure-ASCII body without a casefold/ligature character has
	// nothing to normalize, strip, fold or expand. Every stage below would
	// hand the same bytes back, so skipping them avoids 3 reallocations on
	// the hot path (4 allocs → 1 substring) for the large English bodies
	// that dominate real traffic. Non-ASCII and any of the special letters
	// still take the full path.
	if isPlainASCII(text) {
		if caseSensitive {
			return text
		}
		return strings.ToLower(text)
	}
	out := norm.NFC.String(text)
	if normalize {
		out = invisibleRe.ReplaceAllString(out, "")
		out = applyFoldMap(out)
	}
	if caseSensitive {
		return out
	}
	return unicodeCasefold(out)
}

// isPlainASCII reports whether s contains only printable ASCII letters,
// digits, punctuation and spaces — the characters for which NFC, the
// invisible strip, the variant map and casefold are all identity (ASCII
// casefold == ToLower). It is a scan, not a copy.
func isPlainASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

// unicodeCasefold is Go's answer to Python's str.casefold(): a full Unicode
// case fold, not strings.ToLower. The difference decides real cases:
//
//   - ß (U+00DF) casefolds to "ss" — a ToLower port leaves it as "ß", so a
//     needle the owner typed as "straße" never matches the "STRASSE" the
//     Python engine matches (STRASSE/straße is the UNI-LEN-adjacent case the
//     audit's differential test caught).
//   - The Latin-1 ligatures (ﬀ U+FB00 and friends) casefold apart the same
//     way. NFC normalization does not touch them: they are compatibility
//     forms, and NFC keeps them distinct on purpose.
//
// Go's unicode.SimpleFold gives the fold for one rune (ß ↔ SS in the
// table's full-closure sense), so the implementation maps every rune whose
// full casefold expands and falls back to ToLower for the rest. Patterns
// built from the folded needle then carry the same comparison form Python's
// does (content_policy.py folds the needle at :186 before compiling).
func unicodeCasefold(s string) string {
	if !strings.ContainsAny(s, casefoldExpandChars) {
		return strings.ToLower(s)
	}
	var sb strings.Builder
	sb.Grow(len(s))
	for _, ch := range s {
		if expand, ok := casefoldExpand[ch]; ok {
			sb.WriteString(expand)
			continue
		}
		// unicode.ToLower handles the single-rune folds; for runes with no
		// mapping it returns the rune unchanged, which is the identity the
		// fold needs.
		sb.WriteRune(unicode.ToLower(ch))
	}
	return sb.String()
}

// casefoldExpand are the runes whose Unicode full casefold maps one rune to
// several — the cases strings.ToLower cannot express because there is no
// single lowercase rune to map to. The table is the ASCII-visible subset of
// Python's str.casefold() expansions plus the sharp-s; every other expansion
// in the Unicode file is of a compatibility form this fold does not need to
// reach, because NFC normalization ran first.
var casefoldExpand = map[rune]string{
	'ß': "ss",  // U+00DF LATIN SMALL LETTER SHARP S
	'ﬁ': "fi",  // U+FB01
	'ﬂ': "fl",  // U+FB02
	'ﬀ': "ff",  // U+FB00
	'ﬃ': "ffi", // U+FB03
	'ﬄ': "ffl", // U+FB04
	'ﬅ': "st",  // U+FB05
	'ﬆ': "st",  // U+FB06
	'ﬓ': "մ",   // U+FB13 Armenian small ligature men ech
	'ﬔ': "մ",   // U+FB14
	'ﬕ': "մ",   // U+FB15
	'ﬖ': "վ",   // U+FB16
	'ﬗ': "մ",   // U+FB17
	'İ': "i̇",  // U+0130 LATIN CAPITAL LETTER I WITH DOT ABOVE
}

// casefoldExpandChars is the set of runes casefoldExpand rewrites,
// precomputed once so the common "nothing to expand" case is a single scan.
const casefoldExpandChars = "ßﬁﬂﬀﬃﬄﬅﬆﬓﬔﬕﬖﬗİ"

// applyFoldMap rewrites every character that has a canonical variant.
func applyFoldMap(s string) string {
	if !strings.ContainsAny(s, foldMapChars) {
		return s
	}
	var sb strings.Builder
	sb.Grow(len(s))
	for _, ch := range s {
		if want, ok := foldMap[ch]; ok {
			sb.WriteRune(want)
			continue
		}
		sb.WriteRune(ch)
	}
	return sb.String()
}

// foldMapChars is the set of characters foldMap rewrites, precomputed once so
// the common "nothing to fold" case is a single scan with no map lookups.
const foldMapChars = "\u0622\u0623\u0624\u0625\u0626\u0623\u0643\u0649\u064a\u0629\u0624\u06c0\u0671"

// CollapseWS turns every run of ASCII whitespace into a single space. Both
// the needle and the haystack are collapsed before the substring pre-filter
// (Python re.sub(r"\s+", " ", …) at :188 and :249/:260), so a phrase that
// crosses the double space or newline an editor left between its words is not
// rejected by the cheap test before the matcher ever runs.
func CollapseWS(s string) string {
	out := make([]rune, 0, len(s))
	prevSpace := false
	for _, ch := range s {
		if ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r' {
			if !prevSpace {
				out = append(out, ' ')
			}
			prevSpace = true
			continue
		}
		prevSpace = false
		out = append(out, ch)
	}
	return string(out)
}

// --- match mode (content_policy.py:164-172) ----------------------------------

// MatchMode reports how a keyword should be matched: word, prefix or
// substring. A keyword is a *word* when it carries an alphanumeric and does
// not open with a token marker; a *prefix* when it opens with #@$//; otherwise
// a plain substring (no boundaries).
func MatchMode(needle string, wordBoundary bool) string {
	if !wordBoundary {
		return "substring"
	}
	for _, ch := range needle {
		if tokenMarkers[ch] {
			return "prefix"
		}
		break
	}
	for _, ch := range needle {
		if unicode.IsLetter(ch) || unicode.IsDigit(ch) {
			return "word"
		}
	}
	return "substring"
}

// --- the needle pattern (content_policy.py:176-214) ---------------------------

// matchMode is MatchMode against the *folded* needle: Python decides the mode
// on the original needle (isalnum is case-insensitive in Unicode), which is
// equivalent to deciding on the folded form.
func matchMode(needle string, wordBoundary bool) string {
	if !wordBoundary {
		return "substring"
	}
	// The prefix check is on the FIRST character only: Python's
	// needle[:1] in _TOKEN_MARKERS. It must not consider any other rune.
	for _, ch := range needle {
		if tokenMarkers[ch] {
			return "prefix"
		}
		break
	}
	for _, ch := range needle {
		if unicode.IsLetter(ch) || unicode.IsDigit(ch) {
			return "word"
		}
	}
	return "substring"
}

// needlePattern compiles a keyword into a variant-aware pattern that matches
// the ORIGINAL text (Python _needle_pattern, :176-214).
//
// Each character of the folded keyword becomes a class of the variants a
// human would call the same letter; a space becomes `\s+`; when normalizing,
// the invisible run is allowed between any two characters. The result is one
// search over the original text with correct spans — no folded copy and no
// moved offsets.
//
// The mode comes from the folded needle, and the folding here is always
// case-insensitive (Python folds with case_sensitive=False at :188 — the
// case-sensitivity flag is applied to the compiled pattern's IGNORECASE
// switch, not to the fold itself).
func needlePattern(needle, mode string, normalize, caseSensitive bool) *regexp.Regexp {
	folded := CollapseWS(FoldText(needle, normalize, false))
	if strings.TrimSpace(folded) == "" {
		return nil // a keyword that folds away matches nothing
	}
	chars := []rune(folded)
	parts := make([]string, 0, len(chars))
	for _, ch := range chars {
		if ch == ' ' {
			// A phrase survives the extra space (or newline) an editor leaves
			// between its words; the matched span still covers exactly what
			// the text had.
			parts = append(parts, `\s+`)
			continue
		}
		if variants, ok := variantClass[ch]; ok {
			parts = append(parts, "["+regexp.QuoteMeta(variants)+"]")
			continue
		}
		parts = append(parts, regexp.QuoteMeta(string(ch)))
	}
	// The invisible run is inserted between the needle's characters whenever
	// normalize is on (content_policy.py:202 — `sep = _INVISIBLE_RUN if
	// normalize else ""`). It is unconditional on purpose: the run tolerates
	// invisibles in the *text*, not in the needle — `کتاب` must still find
	// کـتـاب written with a ZWJ the needle does not carry. Keying it on the
	// needle's own characters (a tempting optimization) inverts that: it
	// stops the matcher from finding exactly the text the fold exists to
	// catch.
	sep := ""
	if normalize {
		sep = invisibleRun
	}
	body := strings.Join(parts, sep)

	// --- RE2 translation of the Python boundaries ----------------------------
	//
	// Python uses lookarounds: (?<!\w) on the left, (?!\w) on the right.
	// Go's regexp (RE2) has NO lookaround, so both are composed as
	// alternations around the body (see leftBoundary/rightBoundary — each
	// returns the whole bounded body, or "" when it cannot compose one).
	// The result replaces `body` entirely; the body is never concatenated
	// alongside a helper's output.
	//
	// The load-bearing detail: a consuming (?:^|[^\w]) does not work. It
	// demands a real character before the needle, so a keyword at the very
	// start of the text never matches, and [^\w] itself accepts the Arabic
	// letters a Persian keyword is made of, so کتاب would match inside
	// کتاباب. Both are silent decision changes, not just offset shifts.
	//
	// The match offsets differ from Python's by the boundary character the
	// helpers may consume on either side. This port is used for decisions
	// only; a future strip-words transform must adjust before relying on
	// these spans (the caveat is repeated in the Matcher docs).
	//
	// The composition, by mode:
	//
	//	substring  → body                                 (no boundaries)
	//	prefix     → leftBoundary(body)                   (left only)
	//	word       → both, composed inner-to-outer:
	//	             rightBoundary(leftBoundary(body))
	//
	// Word mode composes BOTH boundaries, inner-to-outer: the left wrapper
	// produces a body whose own last element is still the needle's last
	// element, so rightBoundary can wrap it. The result carries a
	// non-word-consuming alternation on both ends.
	//
	// The load-bearing detail: a consuming (?:^|[^\w]) alone does not work.
	// It demands a real character before the needle, so a keyword at the
	// very start of the text never matches, and [^\w] itself accepts the
	// Arabic letters a Persian keyword is made of, so کتاب would match
	// inside کتاباب. Both are silent decision changes, not just offset
	// shifts.
	//
	// The match offsets differ from Python's by the boundary character the
	// helpers may consume on either side. This port is used for decisions
	// only; a future strip-words transform must adjust before relying on
	// these spans (the caveat is repeated in the Matcher docs).
	// Compose the boundaries, by mode:
	//
	//	substring  → body                                 (no boundaries)
	//	prefix     → leftBoundary(body)                   (left only)
	//	word       → leftBoundary(rightBoundary(body))    (both)
	//
	// Word mode composes BOTH boundaries: compose the right side first from
	// the raw body, then wrap the whole result with the left alternation.
	// Chaining the other way (right over left) would see the left helper's
	// alternation as part of the body and duplicate the needle — the
	// composition order is load-bearing.
	//
	// The load-bearing detail: a consuming (?:^|[^\w]) alone does not work.
	// It demands a real character before the needle, so a keyword at the very
	// start of the text never matches, and [^\w] accepts the Arabic letters a
	// Persian keyword is made of, so کتاب would match inside کتاباب. Both
	// are silent decision changes, not just offset shifts.
	//
	// The match offsets differ from Python's by the boundary character the
	// helpers may consume on either side. This port is used for decisions
	// only; a future strip-words transform must adjust before relying on
	// these spans (the caveat is repeated in the Matcher docs).
	left := body
	if mode == "prefix" {
		if bounded := leftBoundary(body); bounded != "" {
			left = bounded
		}
	}
	if mode == "word" {
		last := chars[len(chars)-1]
		if unicode.IsLetter(last) || unicode.IsDigit(last) {
			// Word mode composes BOTH boundaries: the right side first from
			// the raw body, then the left alternation around the whole
			// result. The composition order is load-bearing — wrapping the
			// left side first would let rightBoundary see the left helper's
			// alternation as part of the body and duplicate the needle.
			bounded := rightBoundary(body)
			if bounded != "" {
				left = bounded
			}
			if bounded := leftBoundary(left); bounded != "" {
				left = bounded
			}
			// A single-element needle ("a") cannot carry a separate left and
			// right boundary: both helpers would re-wrap the same element
			// and the composition would stop matching the needle. Fall back
			// to the plain body for this one — the right boundary alone is
			// what decides `a` vs `ab` — and keep the left side out, exactly
			// as Python's lookarounds do for a one-character word.
			if len(splitNeedleElements(body)) <= 1 {
				left = rightBoundarySingle(body)
			}
		}
	}
	if left == "" {
		// No boundary could be composed (the helpers refuse rather than
		// emit a malformed fragment), so the plain body is the pattern.
		left = body
	}
	prefix := ""
	if !caseSensitive {
		prefix = "(?i)"
	}
	c, err := regexp.Compile(prefix + left)
	if err != nil {
		return nil
	}
	return c
}

// nonWordClass is the character class the boundaries use in place of RE2's
// \w / [^\w].
//
// RE2's \w is ASCII-ONLY ([0-9A-Za-z_]) while Python's re \w is Unicode-aware
// (str.isalnum() over every script). The difference decides real cases:
//
//   - Persian کتاب is a word to Python, so (?<!\w) requires a non-word char
//     (or text start) before it. With RE2's \w the preceding Arabic letter
//     is "not a word char", and کتاب would match inside کتاباب.
//   - Ürünler: RE2's (?i) DOES fold ü↔Ü, but the needle ürün then needs a
//     real word boundary that RE2's ASCII \w cannot express, so the needle
//     matches "ürün" inside "Ürünler" — which is not a word boundary, since
//     the text continues with "ler".
//
// RE2's own Unicode property classes express exactly what Python's \w means:
// `[^...]` over the union of \p{L} (any letter), \p{N} (any number) and the
// underscore. It stays a single RE2 character class, needs no generated
// range table, and cannot drift from the unicode package's definition of a
// letter the way a hand-written list would.
//
// NOTE: this class is used for BOUNDARIES ONLY. The needle body itself is
// never passed through it.
const nonWordClass = `[^\p{L}\p{N}_]`

// hasInvisible reports whether s contains any of the characters the fold
// strips (harakat, tatweel, zero-width and bidi controls).
func hasInvisible(s string) bool {
	return invisibleRe.MatchString(s)
}

var _ = hasInvisible // reserved for the normalize=false path a later stage needs

// --- RE2 boundary helpers -------------------------------------------------------
//
// Python's needle pattern carries two lookarounds: (?<!\w) on the left and
// (?!\w) on the right. Go's regexp (RE2) has NO lookaround, so both must be
// expressed with what RE2 can say — and the naive translations are wrong:
//
//   - A consuming (?:^|[^\w]) demands a real character before the needle, so
//     a keyword at the very start of the text never matches ("cat" fails in
//     "cat sat").
//   - [^\w] accepts the Arabic letters a Persian keyword is made of, so
//     کتاب would match inside کتاباب.
//
// Both are silent *decision* changes, not just offset shifts, so the helpers
// below compose the boundary as an alternation over the body's outermost
// needle element instead. The match offsets differ from Python's by the
// trailing character the right side may consume; a strip-words transform must
// not reuse these spans unadjusted (see the Matcher docs).

// firstElement is the body's first needle element: an escaped literal, a
// `\s+`, or a character class — the unit the boundary alternation repeats.
// An empty answer means the body has no separable first element, in which
// case no boundary can be composed and the caller falls back to plain
// concatenation (which asserts nothing).
func firstElement(body string) string {
	els := splitNeedleElements(body)
	if len(els) == 0 {
		return ""
	}
	return els[0]
}

// lastElement is the body's last needle element. See firstElement.
func lastElement(body string) string {
	els := splitNeedleElements(body)
	if len(els) == 0 {
		return ""
	}
	return els[len(els)-1]
}

// restAfter strips the first element off the body, returning the remainder
// the boundary alternation must also cover.
func restAfter(body, first string) string {
	if first == "" {
		return body
	}
	if len(body) >= len(first) && body[:len(first)] == first {
		return body[len(first):]
	}
	return body
}

// restBefore strips the last element off the body, returning the remainder
// the boundary alternation must also cover.
func restBefore(body, last string) string {
	if last == "" {
		return body
	}
	if len(body) >= len(last) && body[len(body)-len(last):] == last {
		return body[:len(body)-len(last)]
	}
	return body
}

// leftBoundary composes a stand-in for Python's (?<!\w): the body may start
// a match at the beginning of the text, or immediately after a character
// that is not a word character.
//
// RE2 has no lookbehind, so the left boundary is an alternation over the
// body's first element, one branch carrying a consuming `[^\w]` prefix that
// the right branch (anchored at the text start) does not:
//
//	(?:[^\w]«first»|^«first»)«rest»
//
// `[^«first»]` would NOT do: that is a character class excluding the
// literal, not a non-word char before it. The prefix must be `\w`'s
// complement, with the first element still present in both branches.
//
// The consuming branch makes the match START offset one earlier than
// Python's whenever a character precedes the needle; see the Matcher docs
// for why that is acceptable for decisions and not for span removal.
func leftBoundary(body string) string {
	first := firstElement(body)
	if first == "" {
		return ""
	}
	rest := restAfter(body, first)
	return `(?:` + nonWordClass + first + `|^` + first + `)` + rest
}

// rightBoundary composes a stand-in for Python's (?!\\w): the body's last
// element may be followed by a non-word character or by the end of the text.
//
// RE2 has no lookahead. The composition alternates the last element with a
// MANDATORY non-word character against the last element at the end of the
// text:
//
//	«rest»(?:«last»[^\\w]|«last»$)
//
// The [^\\w] MUST NOT be optional. An optional [^\\w]? matches "ad" inside
// "#advert" (d followed by zero characters), which is exactly the case
// Python's (?!\\w) rejects — the differential harness caught it. The cost of
// consuming one real character is that the match END offset is one past
// Python's; this port is used for decisions only (see the Matcher docs), so
// the shift is documented and accepted.
func rightBoundary(body string) string {
	last := lastElement(body)
	if last == "" {
		return ""
	}
	rest := restBefore(body, last)
	return rest + `(?:` + last + `[^\w]|` + last + `$)`
}

// rightBoundarySingle composes the right boundary for a needle whose body is
// ONE element: `(?:«el»[^\\w]?|«el»$)`. Both helpers cannot be composed around
// a single element (each would re-wrap the same text and the result would stop
// matching the needle), so the single-element case takes the right boundary
// alone — the side that decides `a` vs `ab`.
func rightBoundarySingle(body string) string {
	return `(?:` + body + nonWordClass + `?|` + body + `$)`
}

// splitNeedleElements splits a compiled needle body into its literal / class
// elements. An element starts at a `\` escape (with its quantifier), at a
// `[` class (through its closing `]`) or is a single literal character.
//
// The body at this point is a sequence of *elements*, each either an escaped
// literal (`\x`, `\s+`) or a character class (`[…]`); the boundary helpers
// identify the outermost ones without re-deriving them from the needle's
// characters.
func splitNeedleElements(body string) []string {
	out := []string{}
	runes := []rune(body)
	for i := 0; i < len(runes); i++ {
		ch := runes[i]
		switch {
		case ch == '\\':
			// An escape element: the backslash plus the escaped character
			// plus any quantifier that follows (`\s+` is one element).
			j := i + 1
			if j < len(runes) {
				j++
			}
			for j < len(runes) && (runes[j] == '+' || runes[j] == '*' || runes[j] == '?') {
				j++
			}
			out = append(out, string(runes[i:j]))
			i = j - 1
		case ch == '[':
			j := i + 1
			for j < len(runes) && runes[j] != ']' {
				j++
			}
			if j < len(runes) {
				out = append(out, string(runes[i:j+1]))
				i = j
			} else {
				out = append(out, string(ch))
			}
		default:
			out = append(out, string(ch))
		}
	}
	return out
}

// --- the folded-needle pre-filter (content_policy.py:209-282) ------------------

// needleCache memoizes the folded comparison form of a needle, keyed by the
// needle and its fold flags. Python does this with lru_cache on the folded
// tuple; Go gets the same effect with a mutex-guarded map, sized to the same
// 4096-entry budget the Python lru_cache allows. Compiling the pattern is the
// expensive part and is memoized separately by the Matcher.
var (
	needleCacheMu sync.RWMutex
	needleCache   = make(map[needleKey]string, 256)
)

type needleKey struct {
	needle        string
	normalize     bool
	caseSensitive bool
}

// foldedNeedle is the comparison form of one keyword, whitespace collapsed
// (Python _folded_needles, :217-252). The comparison form of a word is always
// a substring of any text that word matches — the pattern only adds
// boundaries, token families and letter variants around that same text — so
// `needle in folded_body` can skip a word that cannot possibly match, at a
// fraction of the cost of running the pattern.
//
// The pre-filter is never consulted without the real matcher behind it, so it
// can change how much work a decision costs, never what the decision is.
func foldedNeedle(word string, normalize, caseSensitive bool) string {
	key := needleKey{word, normalize, caseSensitive}
	needleCacheMu.RLock()
	if got, ok := needleCache[key]; ok {
		needleCacheMu.RUnlock()
		return got
	}
	needleCacheMu.RUnlock()

	needle := strings.TrimSpace(word)
	out := ""
	if needle != "" {
		out = CollapseWS(FoldText(needle, normalize, caseSensitive))
	}

	needleCacheMu.Lock()
	if len(needleCache) < 4096 {
		needleCache[key] = out
	}
	needleCacheMu.Unlock()
	return out
}

// foldedBody is the comparison form a haystack is reduced to for the cheap
// substring pre-filter (Python _folded_body, :259-260). The same fold and the
// same whitespace collapse as foldedNeedle, so the two sides of the
// `needle in folded_body` test can never disagree about whitespace the way
// the raw text and the raw keyword do.
func foldedBody(text string, normalize, caseSensitive bool) string {
	return CollapseWS(FoldText(text, normalize, caseSensitive))
}

// --- the public matcher (content_policy.py:263-309) --------------------------

// Matcher is the Persian/Arabic-aware keyword matcher, safe for concurrent
// use. It exists so a caller with a large keyword list can compile each
// needle's pattern exactly once, then reuse it across messages — the Go
// equivalent of Python's lru_cache over _needle_pattern.
//
// Match spans: the compiled patterns match the ORIGINAL text, but the
// RE2-expressible left boundary consumes one preceding character (see the
// needlePattern note). ContainsWord returns a boolean only; callers that need
// exact spans for a strip-words transform must not reuse these patterns
// unadjusted.
type Matcher struct {
	mu       sync.RWMutex
	patterns map[matcherKey]*regexp.Regexp
}

type matcherKey struct {
	needle        string
	mode          string
	normalize     bool
	caseSensitive bool
}

// NewMatcher returns a ready Matcher.
func NewMatcher() *Matcher { return &Matcher{patterns: make(map[matcherKey]*regexp.Regexp)} }

// compile returns the cached pattern for one needle, compiling it once.
func (m *Matcher) compile(needle, mode string, normalize, caseSensitive bool) *regexp.Regexp {
	key := matcherKey{needle, mode, normalize, caseSensitive}
	m.mu.RLock()
	if got, ok := m.patterns[key]; ok {
		m.mu.RUnlock()
		return got
	}
	m.mu.RUnlock()

	pat := needlePattern(needle, mode, normalize, caseSensitive)

	m.mu.Lock()
	defer m.mu.Unlock()
	if pat != nil && len(m.patterns) < 4096 {
		m.patterns[key] = pat
	}
	return pat
}

// Compile returns the compiled pattern for a needle under the given flags,
// caching it. A nil pattern means the keyword matches nothing (it folded
// away entirely, or could not be compiled).
func (m *Matcher) Compile(needle string, wordBoundary, normalize, caseSensitive bool) *regexp.Regexp {
	return m.compile(needle, MatchMode(needle, wordBoundary), normalize, caseSensitive)
}

// ContainsWord reports whether needle occurs in haystack under the policy's
// rules (Python contains_word, :285-309): normalized (NFC + invisible
// stripping + variant folding + case folding), with or without word
// boundaries, and with whitespace tolerance for multi-word phrases.
//
// Matching runs against the original text with a variant-aware pattern, so a
// keyword still finds a word whose letters were typed with the Arabic forms
// (U+0643 kaf, U+064A yeh) instead of the Farsi ones, survives zero-width
// joiners inside the word, and still respects word boundaries.
func (m *Matcher) ContainsWord(haystack, needle string, wordBoundary, normalize, caseSensitive bool) bool {
	needle = strings.TrimSpace(needle)
	if needle == "" || haystack == "" {
		return false
	}
	pattern := m.compile(needle, matchMode(needle, wordBoundary), normalize, caseSensitive)
	if pattern == nil {
		return false
	}
	return pattern.MatchString(haystack)
}

// FirstMatchingWord returns the first word of words that really matches
// haystack, or "" (Python _first_matching_word, :263-282).
//
// Scanned in the order the task lists them, so a rejection names the first
// offender the user typed rather than an arbitrary one. The folded-needle
// pre-filter runs first: a word whose comparison form is not even a substring
// of the folded body cannot match, and running its pattern is wasted work.
func (m *Matcher) FirstMatchingWord(haystack string, words []string, normalize, caseSensitive, wordBoundary bool) string {
	if len(words) == 0 || haystack == "" {
		return ""
	}
	body := foldedBody(haystack, normalize, caseSensitive)
	for _, word := range words {
		needle := strings.TrimSpace(word)
		if needle == "" {
			continue
		}
		folded := foldedNeedle(word, normalize, caseSensitive)
		if folded == "" {
			continue
		}
		if !strings.Contains(body, folded) {
			continue
		}
		if m.ContainsWord(haystack, needle, wordBoundary, normalize, caseSensitive) {
			return word
		}
	}
	return ""
}
