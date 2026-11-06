package cleaning

import (
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// mentions.go is the Worker's port of the Python "@mention rewrites" stage
// (core/domain/text_replace.py: apply_mention_rewrites): a task-configured
// map of @handle -> @handle (or a "*" catch-all) applied to the message text
// in the final replacement pass, after the AI stage, so a rewrite cannot
// reintroduce a handle the task asked to replace.
//
// The two engines must agree because they execute the same queue: a task
// whose map rewrites @oldshop must produce the same text whichever engine
// claimed the job. The semantics ported verbatim:
//
//   - exact (case-insensitive) handles win over the catch-all;
//   - "*" rewrites every mention the text carries;
//   - the replacement is used as-is when it starts with "@", otherwise an
//     "@" is prefixed, so a value typed without one still reads as a handle;
//   - one pass only: the output of one pair is never re-matched by another;
//   - a mention is "@" + 4..32 username characters that stands alone — not
//     glued to a word (an e-mail address never matches) and not a second
//     "@" of a pair.
//
// Go's RE2 has no look-around, so the boundaries the Python pattern asserts
// with (?<![\w@]) / (?![\w@]) are checked as index conditions around each
// candidate match instead — same decisions, same outcomes.

// mentionMatchAll is the catch-all key ("replace any @ID").
const mentionMatchAll = "*"

// maxMentionPairs is Python's MAX_MENTION_PAIRS bound on the usable map.
const maxMentionPairs = 100

// mentionCore finds the candidate handle bodies. The boundary conditions
// live in mentionBoundaryOK, not here.
var mentionCore = regexp.MustCompile(`@([A-Za-z0-9_]{4,32})`)

// NormalizeMentionMap is Python normalize_mention_map: keys case-folded to
// their canonical "@lower" spelling (a stored "@Shop" matches a message that
// says "@shop"), keys and values accepted with or without the leading "@",
// unusable rows dropped. A bad pair must never block the message it was
// meant to edit.
func NormalizeMentionMap(raw map[string]string) map[string]string {
	if len(raw) == 0 {
		return nil
	}
	out := make(map[string]string, len(raw))
	for key, value := range raw {
		k := strings.TrimSpace(key)
		v := strings.TrimSpace(value)
		if k == "" || v == "" {
			continue
		}
		if k != mentionMatchAll {
			k = "@" + strings.TrimPrefix(strings.ToLower(k), "@")
		}
		if len(out) >= maxMentionPairs {
			break
		}
		out[k] = v
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// ApplyMentionRewrites rewrites @handles per the map. Exact (case-folded)
// handles win over the "*" catch-all, so a task that maps one shop
// specifically still maps that shop while every other mention goes to the
// catch-all target. Candidates are resolved in one left-to-right pass and
// the text is assembled once, so the output of one pair can never be
// re-matched by another — the same single-pass guarantee Python's re.sub
// implementation gives.
func ApplyMentionRewrites(text string, mentionMap map[string]string) string {
	if text == "" || len(mentionMap) == 0 {
		return text
	}
	mapping := NormalizeMentionMap(mentionMap)
	if len(mapping) == 0 {
		return text
	}
	exact := make(map[string]string, len(mapping))
	var catchAll string
	hasCatchAll := false
	for key, value := range mapping {
		if key == mentionMatchAll {
			catchAll, hasCatchAll = value, true
			continue
		}
		exact[key] = value
	}

	var b strings.Builder
	b.Grow(len(text))
	last := 0
	for _, loc := range mentionCore.FindAllStringSubmatchIndex(text, -1) {
		start, end := loc[0], loc[1]
		if !mentionBoundaryOK(text, start, end) {
			continue
		}
		replacement, ok := exact["@"+strings.ToLower(text[loc[2]:loc[3]])]
		if !ok {
			replacement, ok = catchAll, hasCatchAll
		}
		if !ok {
			continue
		}
		if !strings.HasPrefix(replacement, "@") {
			replacement = "@" + strings.TrimPrefix(replacement, "@")
		}
		b.WriteString(text[last:start])
		b.WriteString(replacement)
		last = end
	}
	if last == 0 {
		return text
	}
	b.WriteString(text[last:])
	return b.String()
}

// mentionBoundaryOK asserts the two conditions the Python pattern expresses
// with look-around: the "@" must not continue a word or another "@" (a
// Unicode letter/digit counts — Python's \w is Unicode-aware), and the
// handle must not run into one.
func mentionBoundaryOK(text string, start, end int) bool {
	if start > 0 {
		prev, _ := utf8.DecodeLastRuneInString(text[:start])
		if isMentionWordRune(prev) || prev == '@' {
			return false
		}
	}
	if end < len(text) {
		next, _ := utf8.DecodeRuneInString(text[end:])
		if isMentionWordRune(next) {
			return false
		}
	}
	return true
}

func isMentionWordRune(r rune) bool {
	return r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r)
}
