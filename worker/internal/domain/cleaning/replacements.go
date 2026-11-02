package cleaning

import (
	"regexp"
	"strings"
)

// replacements.go — the find/replace engines the cleaning pipeline uses.
//
// Two implementations live here deliberately, because the Python code has
// two and they are NOT the same:
//
//   - applyReplacements is text_replace.apply_replacements: the per-task
//     replace entries, applied one entry at a time in priority order so
//     "# -> ##" then "## -> x" composes the way the task's priority numbers
//     say it does.
//   - applyPairs is content_policy._apply_replacements: the content policy's
//     single-pass combined alternation, so a=>b followed by b=>c yields b.
//
// Merging them would silently change what a task's configured replacements
// produce.

// replacementBudget bounds how much work a pathological replacement set may
// cost on one message. A task may carry 100 entries (text_replace.MAX_ENTRIES)
// and a message may carry 4096 code points; the product is the worst case.
const replacementBudget = 100 * 4096

// applyReplacements is text_replace.apply_replacements (text_replace.py:415):
// every enabled entry, highest priority first, equal priorities in insertion
// order, applied one entry at a time to the running result. An unusable
// pattern is skipped — it never corrupts or drops the message (RPL-INVALID-01).
func applyReplacements(text string, entries []ReplacementEntry) string {
	if text == "" || len(entries) == 0 {
		return text
	}
	active := make([]ReplacementEntry, 0, len(entries))
	for _, entry := range entries {
		if strings.TrimSpace(entry.Find) == "" {
			continue
		}
		active = append(active, entry)
	}
	// sort.SliceStable with priority desc would need the index too; the
	// caller has already ordered the entries (priority handled by the
	// store-side projection), and Python's stable sort on -priority keeps
	// insertion order for ties — the entries arrive in that order.
	worked := 0
	out := text
	for _, entry := range active {
		if worked > replacementBudget {
			break
		}
		next := applyOneEntry(out, entry)
		if next != out {
			worked++
		}
		out = next
	}
	return out
}

// applyOneEntry is text_replace.apply_entry: smart case for a plain literal
// (exact-case pass first, case-insensitive only when the exact form is
// absent), and a compiled regex for a regex entry. A literal replacement has
// its backslashes escaped so a stray \1 renders literally, not as a group
// reference (text_replace._literal_replacement, RPL-BACKREF-01).
func applyOneEntry(text string, entry ReplacementEntry) string {
	if text == "" || strings.TrimSpace(entry.Find) == "" {
		return text
	}
	if !entry.IsRegex && !entry.WholeWord {
		// Smart case: the exact form first, case-insensitive only as a
		// fallback (RPL-SMART-CASE-01/02).
		if !entry.IgnoreCase {
			if strings.Contains(text, entry.Find) {
				return strings.ReplaceAll(text, entry.Find, escapeLiteralReplacement(entry.Replace))
			}
			compiled, err := regexp.Compile("(?i)" + regexp.QuoteMeta(entry.Find))
			if err != nil {
				return text
			}
			return compiled.ReplaceAllString(text, escapeLiteralReplacement(entry.Replace))
		}
		compiled, err := regexp.Compile("(?i)" + regexp.QuoteMeta(entry.Find))
		if err != nil {
			return text
		}
		return compiled.ReplaceAllString(text, escapeLiteralReplacement(entry.Replace))
	}

	body := entry.Find
	if !entry.IsRegex {
		body = regexp.QuoteMeta(entry.Find)
	}
	if entry.WholeWord {
		// An anchored pattern already states its own boundary; wrapping it
		// in a word lookaround would make ^promo\d+$ unmatchable
		// (text_replace._pattern_body).
		if !strings.HasPrefix(body, "^") {
			body = `(?:\b)(?:` + body + `)`
		}
	}
	prefix := ""
	if entry.IgnoreCase {
		prefix = "(?i)"
	}
	compiled, err := compileSafe(prefix + body)
	if err != nil {
		// An unusable pattern leaves the text unchanged, never raises.
		return text
	}
	if entry.IsRegex {
		return compiled.ReplaceAllString(text, expandReplacement(entry.Replace))
	}
	return compiled.ReplaceAllString(text, escapeLiteralReplacement(entry.Replace))
}

// escapeLiteralReplacement is _literal_replacement: a literal entry's
// replacement is text, so a stray backslash renders as a backslash.
func escapeLiteralReplacement(replacement string) string {
	return strings.ReplaceAll(replacement, `\`, `\\`)
}

// expandReplacement converts a Python re.sub replacement template into the
// form Go's regexp expansion understands.
//
// Python's re.sub expands \1, \2, … and treats a literal backslash as
// escape material. Go's Expand reads $1, $2, … — and a `\1` in a Go
// replacement template is an unrecognized escape, which regexp turns into
// a literal "1". Without this conversion a regex entry written as
// "\\1 is \\2" (the Python form the task row stores) would expand to
// "1 is 2", silently destroying the user's backreference (RPL-BACKREF-01).
//
// Only the digits-after-backslash forms are rewritten: \$ stays a literal
// dollar in Go by writing $$, and every other backslash sequence in a
// Python template is an error or a literal, neither of which Go needs
// translated.
func expandReplacement(replacement string) string {
	var out strings.Builder
	out.Grow(len(replacement))
	for i := 0; i < len(replacement); i++ {
		ch := replacement[i]
		if ch == '\\' && i+1 < len(replacement) && isASCIIDigit(replacement[i+1]) {
			// Python group reference -> Go group reference.
			out.WriteByte('$')
			continue
		}
		if ch == '$' {
			// A literal $ in the Python template must be quoted so Go does
			// not try to expand what follows it.
			out.WriteString("$$")
			continue
		}
		out.WriteByte(ch)
	}
	return out.String()
}

func isASCIIDigit(ch byte) bool { return ch >= '0' && ch <= '9' }

// compileSafe compiles a pattern, returning the error instead of panicking.
// A pattern that does not compile is a configuration problem the caller
// skips, never a crash in the delivery path.
func compileSafe(pattern string) (*regexp.Regexp, error) {
	return regexp.Compile(pattern)
}

// regexpQuoteMeta is regexp.QuoteMeta, named here so the pipeline file reads
// the same name the Python module uses (re.escape).
func regexpQuoteMeta(literal string) string {
	return regexp.QuoteMeta(literal)
}

// wordBoundaryPattern builds the boundary-aware pattern content_policy's
// strip_words uses: the needle, with the shared matcher's notion of a word
// boundary. The match runs against the ORIGINAL text, so the spans are
// correct for span-removal (content_policy._needle_pattern).
func wordBoundaryPattern(needle string, caseSensitive bool) *regexp.Regexp {
	body := regexp.QuoteMeta(needle)
	prefix := ""
	if !caseSensitive {
		prefix = "(?i)"
	}
	compiled, err := regexp.Compile(prefix + `\b(?:` + body + `)\b`)
	if err != nil {
		return nil
	}
	return compiled
}
