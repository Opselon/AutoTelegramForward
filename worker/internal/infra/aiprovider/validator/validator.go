// Package validator is the Go port of the AI output validator
// (core/infrastructure/ai/validator.py): the gate a provider answer
// passes before it is allowed to become delivered text.
package validator

import (
	"regexp"
	"strings"
)

// Telegram limits (validator.py:18-19): a text message caps at 4096
// code points, a media caption at 1024.
const (
	MaxTextLength    = 4096
	MaxCaptionLength = 1024
)

// Result is the validation outcome (ValidationResult, validator.py:37).
//
// SanitizedText is the text to deliver when valid; the error pair names
// the refusal when it is not.
type Result struct {
	IsValid       bool
	SanitizedText string
	ErrorMessage  string
	ErrorCode     string
}

// The hazardous-pattern family (validator.py:22-34). Each one answers a
// concrete leak the prompt-injection envelope defends against: the
// untrusted delimiter echoed back (a model that repeats its input's
// tags), the system prompt narrated, an executable tag smuggled in, or a
// javascript: URI.
//
// Python's DANGEROUS_TAGS_PATTERN is r"<\s*(script|…)[^>]*>.*?<\s*/\s*\1\s*>"
// — a backreference from the open tag's family to its close, so a pair
// only matches when the SAME family opened and closed it. Go's regexp
// engine has no backreferences, so the pair test is rebuilt as a
// per-family loop: for each of the six families, a
// family-specific<pair> pattern is applied repeatedly to the surviving
// text, which removes exactly the spans Python's backreference would
// remove (an open tag, any span, that same family's close) and leaves
// every other tag where Python leaves it — a cross-family pair
// ("<script>x</style>") matches NEITHER engine, and the stray "</style>"
// then flows to the tag-balance pass, which HTML-escapes it, exactly as
// Python does. The alternations share the (?is) posture Python compiled
// in (case-insensitive, dot-matches-newline).
//
// A lone open tag (no close) deliberately does NOT match: Python's
// pattern needs the pair, and such a tag then falls through to the
// tag-balance pass, which HTML-escapes it (validator.py:127). That
// observable difference — "<script>evil" becomes "&lt;script&gt;evil",
// not "evil" — is pinned by the parity harness.
var (
	dangerousTagFamilies      = []string{"script", "style", "iframe", "embed", "object", "applet"}
	untrustedDelimiterPattern = regexp.MustCompile(`(?i)</?(?:untrusted_user_message|system_instructions|admin_prompt)[^>]*>`)
	systemPromptLeakPattern   = regexp.MustCompile(`(?i)\b(?:my\s+system\s+prompt|system\s+instructions|system\s+prompt)\s*:`)
	javascriptURIPattern      = regexp.MustCompile(`(?i)javascript\s*:`)
)

// Validator validates and sanitizes AI output (validator.py:45).
type Validator struct {
	MaxTextLen    int
	MaxCaptionLen int
}

// New builds a validator with the Telegram limits.
func New() *Validator {
	return &Validator{MaxTextLen: MaxTextLength, MaxCaptionLen: MaxCaptionLength}
}

// Validate is the port of AIOutputValidator.validate (validator.py:56).
//
// The order is the contract:
//
//  1. nil or empty-after-trim output is EMPTY_OUTPUT (a model that
//     answered nothing has not transformed the message);
//  2. an injection-delimiter or system-prompt echo is INJECTION_LEAK
//     (fail closed, no truncation);
//  3. executable tags are scrubbed, and a scrub that leaves nothing is
//     DANGEROUS_CONTENT;
//  4. length: truncate at a word boundary when allowed, refuse when not;
//  5. unbalanced HTML is HTML-escaped so Telegram cannot misparse it.
//
// Never logged: the raw output itself. A caller that logs the failure
// logs the classification and the reason, never the answer.
func (v *Validator) Validate(text string, isCaption bool, allowTruncate bool) Result {
	cleaned := strings.ReplaceAll(text, "\x00", "")
	cleaned = strings.TrimSpace(cleaned)
	if cleaned == "" {
		return Result{ErrorCode: "EMPTY_OUTPUT", ErrorMessage: "AI returned empty or whitespace-only text"}
	}

	// 1. Injection delimiter leaks.
	if untrustedDelimiterPattern.MatchString(cleaned) || systemPromptLeakPattern.MatchString(cleaned) {
		return Result{ErrorCode: "INJECTION_LEAK", ErrorMessage: "AI output leaked internal delimiters or instructions"}
	}

	// 2. Dangerous script/iframe content: scrub rather than refuse, but
	// a scrub that empties the text is a refusal. The scrub removes a
	// tag PAIR and the span between them, mirroring Python's
	// backreference-based sub("", ...); a lone opening tag is NOT
	// scrubbed here, because Python's pattern requires the pair and the
	// text then flows to the tag-balance pass, which HTML-escapes it
	// (validator.py:127). Keeping that ordering is what makes the two
	// engines agree on "<script> without a close".
	if dangerousPairPresent(cleaned) || javascriptURIPattern.MatchString(cleaned) {
		cleaned = scrubDangerousSpans(cleaned)
		cleaned = javascriptURIPattern.ReplaceAllString(cleaned, "")
		cleaned = strings.TrimSpace(cleaned)
		if cleaned == "" {
			return Result{ErrorCode: "DANGEROUS_CONTENT", ErrorMessage: "AI output contained disallowed executable tags"}
		}
	}

	// 3. Length. The caption limit applies to media messages.
	limit := v.MaxTextLen
	if isCaption {
		limit = v.MaxCaptionLen
	}
	if limit <= 0 {
		limit = MaxTextLength
	}
	if len(cleaned) > limit {
		if !allowTruncate {
			return Result{
				ErrorCode:    "EXCEEDS_LENGTH",
				ErrorMessage: errorMessage("Output length (", len(cleaned), ") exceeds Telegram limit (", limit, ")"),
			}
		}
		cleaned = truncateAtBoundary(cleaned, limit)
	}

	// 4. HTML tag balance: escape rather than refuse, the Python
	// posture (validator.py:127) — a broken tag must not make Telegram
	// misrender the whole message.
	if !htmlTagsBalanced(cleaned) {
		cleaned = escapeHTML(cleaned)
	}

	return Result{IsValid: true, SanitizedText: cleaned}
}

// scrubDangerousSpans removes every hazardous tag pair and the span
// between them, the exact spans Python's backreference-based
// DANGEROUS_TAGS_PATTERN.sub("", ...) removes: one pass per family,
// repeated until no pair of THAT family survives, matching Python's
// left-to-right non-overlapping semantics for one family before the
// next family runs. A lone closer left behind by a removed span is NOT
// swept here: Python's sub leaves it (it was not part of any match),
// it then flows to the tag-balance pass, and the balance pass
// HTML-escapes it into the delivered text — the observable behaviour
// both engines share.
func scrubDangerousSpans(text string) string {
	out := text
	for _, family := range dangerousTagFamilies {
		pattern := dangerousPairPattern(family)
		for {
			pair := pattern.FindStringIndex(out)
			if pair == nil {
				break
			}
			out = out[:pair[0]] + out[pair[1]:]
		}
	}
	return out
}

// dangerousPairPattern compiles one family's pair pattern, the Go form
// of Python's r"<\s*(script)[^>]*>.*?<\s*/\s*\1\s*>" with the
// backreference replaced by the literal family name. Compiled per call
// per family; the six families and the short inputs make the cost
// negligible, and keeping the pattern constructed (not a pre-built
// alternation over every family) is what makes a cross-family pair
// ("<script>x</style>") match the same way it matches Python: not at
// all.
func dangerousPairPattern(family string) *regexp.Regexp {
	return regexp.MustCompile(`(?is)<\s*(?:` + family + `)\s*[^>]*>.*?<\s*/\s*(?:` + family + `)\s*>`)
}

// dangerousPairPresent answers whether ANY family's pair is present,
// the gate Python's `if DANGEROUS_TAGS_PATTERN.search(cleaned)` applies
// before the scrub runs.
func dangerousPairPresent(text string) bool {
	for _, family := range dangerousTagFamilies {
		if dangerousPairPattern(family).MatchString(text) {
			return true
		}
	}
	return false
}

// truncateAtBoundary is the Python truncation (validator.py:109-114):
// cut at limit-3, retreat to the last space when it is within 100 of
// the cut, and append the ellipsis. Code-point aware (a byte cut would
// split a multi-byte rune).
func truncateAtBoundary(text string, limit int) string {
	runes := []rune(text)
	if len(runes) <= limit-3 {
		return string(runes)
	}
	truncated := runes[:limit-3]
	lastSpace := -1
	for i := len(truncated) - 1; i >= 0; i-- {
		if truncated[i] == ' ' {
			lastSpace = i
			break
		}
	}
	if lastSpace > limit-100 && lastSpace > 0 {
		truncated = truncated[:lastSpace]
	}
	return strings.TrimSpace(string(truncated)) + "..."
}

// htmlTagsBalanced is _check_html_tag_balance (validator.py:134): a
// stack walk over the standard Telegram tags. Void tags (br/hr/img)
// never push and their closing spellings never pop.
func htmlTagsBalanced(text string) bool {
	tags := htmlTagPattern.FindAllStringSubmatch(text, -1)
	if len(tags) == 0 {
		return true
	}
	stack := []string{}
	voidTags := map[string]bool{"br": true, "hr": true, "img": true}
	for _, match := range tags {
		name := strings.ToLower(match[1])
		if strings.HasPrefix(name, "/") {
			closing := strings.TrimPrefix(name, "/")
			if voidTags[closing] {
				continue
			}
			if len(stack) == 0 || stack[len(stack)-1] != closing {
				return false
			}
			stack = stack[:len(stack)-1]
		} else if !voidTags[name] {
			stack = append(stack, name)
		}
	}
	return len(stack) == 0
}

// htmlTagPattern is Python's r"<(/?[a-zA-Z0-9]+)[^>]*>" (validator.py:137).
var htmlTagPattern = regexp.MustCompile(`<(/?[a-zA-Z0-9]+)[^>]*>`)

// escapeHTML is html.escape with quote=True, the Python default
// (validator.py:127 escapes &, <, > and the quote pair).
func escapeHTML(text string) string {
	replacements := []struct{ from, to string }{
		{"&", "&amp;"},
		{"<", "&lt;"},
		{">", "&gt;"},
		{`"`, "&#34;"},
		{"'", "&#39;"},
	}
	out := text
	for _, r := range replacements {
		out = strings.ReplaceAll(out, r.from, r.to)
	}
	return out
}

// errorMessage joins a variadic description without pulling fmt into
// the validator's hot path.
func errorMessage(parts ...any) string {
	var b strings.Builder
	for _, part := range parts {
		switch value := part.(type) {
		case string:
			b.WriteString(value)
		case int:
			b.WriteString(itoa(value))
		}
	}
	return b.String()
}

// itoa is a minimal decimal formatter for the two numeric fields the
// error messages carry.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	negative := n < 0
	if negative {
		n = -n
	}
	digits := []byte{}
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	if negative {
		digits = append([]byte{'-'}, digits...)
	}
	return string(digits)
}
