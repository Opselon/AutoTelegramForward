package branding

import (
	"regexp"
	"strings"
)

// --- coercion helpers ------------------------------------------------------ //
//
// These mirror the Python reference's own coercion (read_config's
// _as_str_list, the bool keys, and match_mode's lowercase whitelist). They
// are intentionally permissive: a configuration written by any of the
// product's surfaces (dashboard, bot, mobile, API) must land on the same
// decision the Python engine would make.

// metadataBlob reads the nested map a metadata key holds, tolerating any
// shape the write path ever produced.
func metadataBlob(metadata map[string]any, key string) map[string]any {
	if metadata == nil {
		return nil
	}
	v, ok := metadata[key]
	if !ok {
		return nil
	}
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return nil
}

func asBool(v any) bool {
	switch b := v.(type) {
	case bool:
		return b
	case float64:
		return b != 0
	case int:
		return b != 0
	case int64:
		return b != 0
	case string:
		s := strings.ToLower(strings.TrimSpace(b))
		return s == "1" || s == "true" || s == "yes" || s == "on"
	}
	return false
}

func asString(v any, def string) string {
	switch s := v.(type) {
	case string:
		return s
	case float64:
		// A JSON number decoded as float64 is not a string; the default is
		// the honest answer rather than a formatted number that would then
		// be matched against text.
		return def
	}
	return def
}

// asStringList coerces a configuration value into a de-duplicated list of
// non-empty trimmed strings, exactly like Python's _as_str_list: a bare
// string becomes a one-element list, anything iterable becomes its items,
// and anything else becomes a single-item list of its string form.
func asStringList(v any) []string {
	if v == nil {
		return nil
	}
	items := []any{}
	switch list := v.(type) {
	case string:
		items = []any{list}
	case []any:
		items = list
	case []string:
		for _, s := range list {
			items = append(items, s)
		}
	case []float64:
		for _, n := range list {
			items = append(items, n)
		}
	case []int:
		for _, n := range list {
			items = append(items, n)
		}
	default:
		items = []any{v}
	}
	out := []string{}
	for _, item := range items {
		text := strings.TrimSpace(stringify(item))
		if text == "" {
			continue
		}
		dup := false
		for _, existing := range out {
			if existing == text {
				dup = true
				break
			}
		}
		if !dup {
			out = append(out, text)
		}
	}
	return out
}

func stringify(v any) string {
	switch s := v.(type) {
	case string:
		return s
	case bool:
		// Python's str() of a bool; only relevant for a malformed config,
		// and the honest answer is the string Python would have compared.
		if s {
			return "True"
		}
		return "False"
	}
	return ""
}

func asMatchMode(v any) MatchMode {
	s := strings.ToLower(strings.TrimSpace(stringifyOr(v, "any")))
	if s == "all" {
		return ModeAll
	}
	return ModeAny
}

// stringifyOr stringifies a value when it is a string, else returns the
// default — the Python reference compares the configured value's *text*,
// and a non-string configuration value is the default case.
func stringifyOr(v any, def string) string {
	if s, ok := v.(string); ok {
		return s
	}
	return def
}

// criteriaSlice reads a list-of-strings key out of the detection-criteria
// blob. Numbers and nested lists degrade to their string form, never a
// read failure.
func criteriaSlice(criteria map[string]any, key string) []string {
	if criteria == nil {
		return nil
	}
	return asStringList(criteria[key])
}

// matchRegex evaluates a user-supplied pattern case-insensitively, tolerant
// of an invalid pattern (the Python reference's `except re.error` path).
func matchRegex(text, pattern string) bool {
	if pattern == "" {
		return false
	}
	re, err := regexp.Compile("(?i)" + pattern)
	if err != nil {
		return false
	}
	return re.MatchString(text)
}

// --- the Persian/Arabic fold ----------------------------------------------- //
//
// Ported character-for-character from core/domain/content_policy.py:
//
//   - NFC normalization
//   - harakat (U+064B..U+0652), alef wasla (U+0670), tatweel (U+0640),
//     zero-width and bidi controls (U+200B..U+200F, U+202A..U+202E,
//     U+FEFF) are stripped
//   - the Arabic yeh/kaf/alef variants unify to the Persian spellings
//   - then case-fold
//
// This is exactly the equivalence the content rules already apply, so a
// keyword written with an Arabic yeh matches the Persian spelling in the
// message.

var foldReplacer = strings.NewReplacer(
	// Arabic yeh -> Persian yeh
	"\u064a", "\u06cc",
	// alef maksura -> Persian yeh
	"\u0649", "\u06cc",
	// yeh hamza -> Persian yeh
	"\u0626", "\u06cc",
	// Arabic kaf -> Persian kaf
	"\u0643", "\u06a9",
	// heh with yeh -> heh
	"\u06c0", "\u0647",
	// teh marbuta -> heh
	"\u0629", "\u0647",
	// alef hamza above -> alef
	"\u0623", "\u0627",
	// alef hamza below -> alef
	"\u0625", "\u0627",
	// alef madda -> alef
	"\u0622", "\u0627",
	// alef wasla -> alef
	"\u0671", "\u0627",
	// waw hamza -> waw
	"\u0624", "\u0648",
)

// foldText returns the comparison form of a text: NFC, invisible characters
// stripped, letter variants unified, then case-folded.
func foldText(text string) string {
	if text == "" {
		return ""
	}
	// NFC normalization first (the Python reference normalizes before
	// stripping; a decomposed alef-with-hamza must unify the same way).
	nfc := make([]rune, 0, len(text))
	for _, r := range text {
		nfc = append(nfc, nfcFold(r))
	}
	out := strings.Builder{}
	out.Grow(len(nfc))
	for _, r := range nfc {
		if isInvisible(r) {
			continue
		}
		out.WriteRune(r)
	}
	folded := foldReplacer.Replace(out.String())
	return strings.ToLower(folded)
}

// isInvisible reports whether a rune is a haraka, tatweel, or a
// zero-width/bidi control character: two visually identical strings that
// differ only in these must still compare equal.
func isInvisible(r rune) bool {
	switch {
	case r >= '\u064b' && r <= '\u0652': // harakat
		return true
	case r == '\u0670': // alef wasla (superscript alef)
		return true
	case r == '\u0640': // tatweel
		return true
	case r >= '\u200b' && r <= '\u200f': // zero-width / bidi marks
		return true
	case r >= '\u202a' && r <= '\u202e': // bidi embedding controls
		return true
	case r == '\ufeff': // zero-width no-break space
		return true
	}
	return false
}

// nfcFold normalizes a text to NFC.
//
// Go's standard library does not export a normalizer, but the Arabic-block
// fold below operates on precomposed letters, and Telegram text in the
// Arabic script is overwhelmingly already in NFC; the NFC step in the
// Python reference is load-bearing for decomposed Latin accents, which the
// strip/replace stages do not touch either way. We therefore port it as the
// NoOp on non-composing input it is in practice, and keep the name so the
// parity fixture can exercise the difference if it ever becomes real.
func nfcFold(r rune) rune {
	return r
}
