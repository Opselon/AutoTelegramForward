// Package filter is the Go port of the Python core.domain.services.FilterEngine.
//
// This is a *behavioral port*, not a redesign: the check order, the failure
// reasons, and the legacy regex_patterns compatibility rule are the existing
// semantics, preserved deliberately. The Python pipeline already ran the
// per-task content policies on the ingest side (their decision is baked into
// the job payload), so at delivery time only the persisted FilterRule that
// the task explicitly attached is re-evaluated — the same call the Python
// delivery worker makes before dispatching.
//
// Why the port must stay byte-faithful to the Python engine: a task whose
// filter decides differently in Go than in Python is a task that forwards a
// different set of messages depending on which engine picked the job up.
// Nothing in the strangler-fig transition protects against that except this
// code being the same decision.
package filter

import (
	"regexp"
	"strings"
	"unicode"

	"golang.org/x/text/unicode/norm"

	"atf/worker/internal/domain/rule"
)

// Action is the outcome of evaluating one message against a filter.
type Action string

const (
	Allow Action = "ALLOW"
	Drop  Action = "DROP"
)

// Result is the evaluation outcome. Reason names the exact gate that
// decided, mirroring the Python engine's reason strings one-for-one so an
// operator debugging a drop sees the same vocabulary from either engine.
type Result struct {
	Action Action
	Reason string
}

// allow is the shared ALLOW result, with the engine's own reason.
func allow(reason string) Result { return Result{Action: Allow, Reason: reason} }

func drop(reason string) Result { return Result{Action: Drop, Reason: reason} }

// urlRegex is the Python URL_REGEX, ported verbatim.
var urlRegex = regexp.MustCompile(`(https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+|@[A-Za-z0-9_]{3,32})`)

// reDoSGuard recognises the two catastrophic-backtracking shapes the Python
// compile_user_pattern refuses: a quantifier nested in a quantified group,
// and an alternation quantified. A *refused* pattern fails closed (DROP),
// exactly as the Python engine does — it never silently allows.
var reDoSNested = regexp.MustCompile(`\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})`)
var reDoSAlt = regexp.MustCompile(`\((?:[^()\\]|\\.)*\|(?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})`)

const (
	maxPatternLen = 1000
	// Ceiling on the text a user-supplied pattern is matched against. The
	// structural guard refuses the catalogued catastrophic shapes, but a
	// pattern nobody has catalogued yet could still be quadratic-or-worse on
	// a 4096-character message. 8 KiB is already far beyond a Telegram
	// message, and the cap keeps the worst case linear in the input.
	maxMatchLen = 8192
)

// Engine evaluates a message against the persisted FilterRule attached to a
// task. It is safe for concurrent use: it carries no mutable state.
type Engine struct{}

// New returns an Engine.
func New() *Engine { return &Engine{} }

// Evaluate mirrors FilterEngine.evaluate check-for-check.
//
// Order of checks (fail fast) — do not reorder, the order *is* the semantics:
//  1. service messages     2. media type allow/deny
//  3. length bounds        4. blacklist keywords
//  5. whitelist keywords   6. regex_block / regex_require
//
// Every DROP carries the Python engine's reason string.
func (e *Engine) Evaluate(text string, mediaType string, isService bool, f *rule.FilterRule) Result {
	if isService {
		// Service messages: the Python engine drops when NO filter is
		// attached, and when one is attached it honors the filter's own
		// drop_service_messages flag (which defaults true, but is an owner
		// switch — a filter configured to pass them through must not become
		// a drop because the engine hardcoded its default).
		if f == nil || f.DropService {
			return drop("service_message_dropped")
		}
	}
	if f == nil {
		return allow("no_filter")
	}
	// An owner can switch a configured filter off. A disabled filter is
	// skipped (allow) — never evaluated and never deleted — so toggling it
	// back on restores the exact lists the owner saved.
	if !f.IsEnabled {
		return allow("filter_disabled")
	}
	media := strings.ToLower(mediaType)

	if len(f.BlockedMediaTypes) > 0 {
		blocked := make([]string, 0, len(f.BlockedMediaTypes))
		for _, b := range f.BlockedMediaTypes {
			blocked = append(blocked, strings.ToLower(b))
		}
		for _, b := range blocked {
			if media == b {
				return drop("media_blocked:" + media)
			}
		}
		if contains(blocked, "link") {
			check := text
			if urlRegex.MatchString(check) || strings.Contains(strings.ToLower(check), "http://") ||
				strings.Contains(strings.ToLower(check), "https://") || strings.Contains(strings.ToLower(check), "t.me/") {
				return drop("link_blocked")
			}
		}
	}

	if len(f.AllowedMediaTypes) > 0 {
		ok := false
		for _, a := range f.AllowedMediaTypes {
			if strings.ToLower(a) == media {
				ok = true
				break
			}
		}
		if !ok {
			return drop("media_not_allowed:" + media)
		}
	}

	if f.MinMessageLength > 0 && len(text) < f.MinMessageLength {
		return drop("too_short")
	}
	if f.MaxMessageLength > 0 && len(text) > f.MaxMessageLength {
		return drop("too_long")
	}

	for _, kw := range f.BlacklistKeywords {
		if containsWord(text, kw, false) {
			return drop("blacklist_keyword")
		}
	}
	if len(f.WhitelistKeywords) > 0 {
		matched := false
		for _, kw := range f.WhitelistKeywords {
			if containsWord(text, kw, false) {
				matched = true
				break
			}
		}
		if !matched {
			return drop("whitelist_mismatch")
		}
	}

	// Explicit directional regex lists take precedence.
	rxBlock := append([]string{}, f.RegexBlock...)
	rxRequire := append([]string{}, f.RegexRequire...)

	// Legacy regex_patterns compatibility: when only the legacy list is set,
	// it acts as blocking for a blacklist-only filter and as
	// require-any otherwise. Changing this rule changes which messages
	// decade-old tasks forward.
	if len(f.RegexPatterns) > 0 && len(rxBlock) == 0 && len(rxRequire) == 0 {
		if len(f.BlacklistKeywords) > 0 && len(f.WhitelistKeywords) == 0 {
			rxBlock = f.RegexPatterns
		} else {
			rxRequire = f.RegexPatterns
		}
	}

	for _, pattern := range rxBlock {
		compiled, ok := compileUserRegex(pattern)
		if !ok {
			// Failing closed on an un-compilable pattern is the existing
			// semantics: a broken guard must never become an open gate.
			return drop("invalid_regex_block:" + truncate(pattern, 32))
		}
		if boundedMatch(compiled, text) {
			return drop("regex_block:" + truncate(pattern, 48))
		}
	}

	if len(rxRequire) > 0 {
		compiled := make([]*regexp.Regexp, 0, len(rxRequire))
		for _, pattern := range rxRequire {
			c, ok := compileUserRegex(pattern)
			if !ok {
				return drop("invalid_regex_require:" + truncate(pattern, 32))
			}
			compiled = append(compiled, c)
		}
		matched := false
		for _, c := range compiled {
			if boundedMatch(c, text) {
				matched = true
				break
			}
		}
		if !matched {
			return drop("regex_mismatch")
		}
	}

	// block_links gate (Python key: block_links on the FilterRule).
	if f.BlockLinks {
		lowered := strings.ToLower(text)
		if urlRegex.MatchString(text) || strings.Contains(lowered, "http://") ||
			strings.Contains(lowered, "https://") || strings.Contains(lowered, "t.me/") {
			return drop("link_blocked")
		}
	}

	return allow("filter_passed")
}

func contains(list []string, s string) bool {
	for _, item := range list {
		if item == s {
			return true
		}
	}
	return false
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// compileUserRegex compiles a user-supplied pattern or reports it unusable,
// refusing patterns that are long or carry a nested quantifier — the shapes
// that turn a crafted message into minutes of backtracking.
func compileUserRegex(pattern string) (*regexp.Regexp, bool) {
	if len(pattern) == 0 || len(pattern) > maxPatternLen {
		return nil, false
	}
	if reDoSNested.MatchString(pattern) || reDoSAlt.MatchString(pattern) {
		return nil, false
	}
	c, err := regexp.Compile("(?im)" + pattern)
	if err != nil {
		return nil, false
	}
	return c, true
}

// boundedMatch applies the length ceiling that keeps a user pattern's worst
// case linear in the input.
func boundedMatch(c *regexp.Regexp, text string) bool {
	if len(text) > maxMatchLen {
		text = text[:maxMatchLen]
	}
	return c.MatchString(text)
}

// --- keyword matching ----------------------------------------------------- //

// Persian/Arabic letter variants: every spelling a human reads as the same
// letter, so one keyword catches them all. Mirrors the Python _VARIANT_CLASS
// exactly — a keyword typed on one keyboard must still match text written on
// another.
var variantClass = map[rune]string{
	'\u06cc': "\u06cc\u064a\u0649\u0626", // yeh
	'\u06a9': "\u06a9\u0643",             // kaf/keh
	'\u0627': "\u0627\u0623\u0625\u0622\u0671", // alef
	'\u0647': "\u0647\u0629\u06c0",       // heh
	'\u0648': "\u0648\u0624",             // waw
}

// invisibleRun: harakat, tatweel, ZWJ/ZWNJ and bidi marks that may sit
// between the characters of a keyword when normalising.
const invisibleRun = "[\u064b-\u0652\u0670\u0640\u200b-\u200f\u202a-\u202e\ufeff]*"

// tokenMarkers: a keyword that opens with one of these is a token family
// rather than a word: `#ad` is a hashtag, `@scam` a handle, `$`, `/` a
// command.
var tokenMarkers = map[rune]bool{'#': true, '@': true, '$': true, '/': true}

// containsWord reports whether needle occurs in haystack under the same
// policy the Python contains_word() applies: normalized (NFC + invisible
// stripping + Persian/Arabic variant folding + case folding), with or without
// word boundaries, and with whitespace tolerance for multi-word phrases.
//
// The Python matcher compiles the *needle* into a variant-aware pattern and
// runs it against the original text. Doing exactly that in Go (rather than
// folding the haystack, which moves character offsets the strip-words
// transform needs) keeps matching and, later, span-removal in agreement.
func containsWord(haystack, needle string, wordBoundary bool) bool {
	needle = strings.TrimSpace(needle)
	if needle == "" || haystack == "" {
		return false
	}
	mode := matchMode(needle, wordBoundary)
	pattern := needlePattern(needle, mode)
	if pattern == nil {
		return false
	}
	return pattern.MatchString(haystack)
}

// matchMode mirrors the Python _match_mode.
func matchMode(needle string, wordBoundary bool) string {
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

// needlePattern compiles a keyword into a pattern that matches the ORIGINAL
// text (not the folded form), so spans surviving into a strip/replace
// transform stay actionable in the original string.
func needlePattern(needle, mode string) *regexp.Regexp {
	folded := foldText(needle)
	// Collapse internal whitespace: a phrase survives the extra space (or
	// newline) an editor leaves between its words.
	folded = collapseWS(folded)
	if strings.TrimSpace(folded) == "" {
		return nil // a keyword that folds away matches nothing
	}
	var sb strings.Builder
	for _, ch := range folded {
		if ch == ' ' {
			sb.WriteString(`\s+`)
			continue
		}
		if variants, ok := variantClass[ch]; ok {
			sb.WriteString("[" + regexp.QuoteMeta(variants) + "]")
			continue
		}
		sb.WriteString(regexp.QuoteMeta(string(ch)))
	}
	body := sb.String()
	// When normalising, an invisible character may sit between any two
	// characters of the keyword (harakat, ZWJ, tatweel): insert the run as
	// the separator instead of plain concatenation.
	body = joinInvisible(body)

	// RE2 (Go's regexp) has no lookbehind, so the left word boundary is
	// expressed as "start-of-text or a non-word character before". The
	// matched span therefore includes that preceding character; for a pure
	// *decision* (does the keyword occur?) that is irrelevant, and this port
	// is only used for decisions.
	left := ""
	if mode != "substring" {
		left = `(?:^|[^\w])`
	}
	right := ""
	switch mode {
	case "prefix":
		// A tag/handle keyword matches its whole family: `#ad` catches
		// `#advert`, `@scam` catches `@scam_bot`.
	case "word":
		last := []rune(folded)
		if len(last) > 0 && (unicode.IsLetter(last[len(last)-1]) || unicode.IsDigit(last[len(last)-1])) {
			right = `(?!\w)`
		}
	}
	c, err := regexp.Compile("(?i)" + left + body + right)
	if err != nil {
		return nil
	}
	return c
}

// joinInvisible rewrites a variant-aware needle body so each character
// boundary allows a run of invisible characters.
//
// The body at this point is a sequence of *elements*, each either an escaped
// literal (`\x`, `\s+`) or a character class (`[…]`). The invisible run is
// inserted BETWEEN elements — never inside one — so it cannot corrupt an
// existing class or escape.
func joinInvisible(body string) string {
	elements := splitNeedleElements(body)
	if len(elements) <= 1 {
		return body
	}
	out := make([]string, 0, 2*len(elements))
	for i, el := range elements {
		if i > 0 {
			out = append(out, invisibleRun)
		}
		out = append(out, el)
	}
	return strings.Join(out, "")
}

// splitNeedleElements splits a compiled needle body into its literal / class
// elements. An element starts at a `\` escape (with its quantifier), at a
// `[` class (through its closing `]`) or is a single literal character.
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

// foldText is the comparison form of a text: NFC unify, strip the invisible
// characters (harakat, tatweel, ZWJ/ZWNJ, bidi marks), then case-fold.
//
// This mirrors Python's fold_text(normalize=True, case_sensitive=False): the
// Persian/Arabic *variant* folding happens at needle-pattern build time (the
// variantClass map), not here, exactly as in Python — folding the haystack
// would move character offsets the strip/replace transform needs.
func foldText(s string) string {
	if s == "" {
		return ""
	}
	nfc := norm.NFC.String(s)
	nfc = invisibleRe.ReplaceAllString(nfc, "")
	return strings.ToLower(nfc)
}

// invisible characters stripped before comparison.
var invisibleRe = regexp.MustCompile("[\u064b-\u0652\u0670\u0640\u200b-\u200f\u202a-\u202e\ufeff]")

// collapseWS turns every run of whitespace into a single space.
func collapseWS(s string) string {
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
