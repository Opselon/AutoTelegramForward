// Package transform is the Go port of the delivery-side text transforms the
// Python pipeline applies before enqueue, plus the per-target rewrite the
// delivery worker applies at dispatch time.
//
// The pipeline-side transforms (FilterEngine.transform_text: link policy,
// cleaner rules, replacements, header/footer) have ALREADY run on the ingest
// side — their result is the `text` in the job payload, so the Go worker
// never re-applies them. What the worker applies here is the per-target
// policy chain override: only the target-scoped policies the ingest side
// never saw, evaluated on this target's copy of the text, exactly as the
// Python dispatch path does.
//
// Re-running the task's own policies here would be a real defect: the
// Python pipeline deliberately re-decides only the target-scoped override
// because judging the task's own transforms against the task's own blacklist
// used to silently reject every message the task ever forwarded.
package transform

import (
	"regexp"
	"strings"
)

// URL and social patterns are the Python URL_REGEX, ported verbatim so the
// "contains a link" question gets the same answer in either engine.
var urlRegex = regexp.MustCompile(`(https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+|@[A-Za-z0-9_]{3,32})`)

var usernameRegex = regexp.MustCompile(`@[A-Za-z0-9_]{3,32}`)
var hashtagRegex = regexp.MustCompile(`#[^\s#@]+`)
var phoneRegex = regexp.MustCompile(`\+?\d[\d\s\-()]{7,18}\d`)

// emojiRegex is the Python EMOJI_REGEX.
var emojiRegex = regexp.MustCompile(`[\x{10000}-\x{10ffff}]|[\x{2600}-\x{27bf}]|[\x{2300}-\x{23ff}]|[\x{2b50}-\x{2b55}]|[\x{fe00}-\x{fe0f}]`)

// Engine applies a per-target policy's rewrite to this target's copy of the
// text. It is stateless and safe for concurrent use.
type Engine struct{}

// New returns an Engine.
func New() *Engine { return &Engine{} }

// TargetOverride is the target-scoped policy chain entry the dispatch path
// re-evaluates. Only target-scoped entries reach here — the ingest side
// already decided the task's own policy on the *source* message.
type TargetOverride struct {
	Source      string // "target:<chat id>", for the log line only
	RemoveLinks bool
	StripWords  []string
}

// ApplyTargetOverride rewrites this target's copy of the text under the
// target-scoped override. The Python dispatch path applies the chain's
// rewrite notes in chain order; the Go port keeps that behaviour for the
// link-stripping and word-stripping overrides, returning the rewritten text
// and a short note for the log line ("" when nothing changed).
func (e *Engine) ApplyTargetOverride(text string, o *TargetOverride) (string, string) {
	if o == nil {
		return text, ""
	}
	notes := []string{}
	if o.RemoveLinks {
		stripped := urlRegex.ReplaceAllString(text, "")
		if stripped != text {
			text = stripped
			notes = append(notes, "links_removed")
		}
	}
	if len(o.StripWords) > 0 {
		stripped := text
		for _, w := range o.StripWords {
			if w == "" {
				continue
			}
			stripped = strings.ReplaceAll(stripped, w, "")
		}
		if stripped != text {
			text = Tidy(stripped)
			notes = append(notes, "words_stripped")
		}
	}
	return text, strings.Join(notes, ",")
}

// Tidy collapses the blank lines a removal can leave behind — the Python
// _tidy() used by the strip path, ported to keep the output identical.
func Tidy(text string) string {
	if text == "" {
		return text
	}
	// Collapse runs of 3+ newlines to two (a blank line survives), trim the
	// edges. Nothing else: this is a cosmetic pass, not a formatter.
	text = strings.ReplaceAll(text, "\r\n", "\n")
	for strings.Contains(text, "\n\n\n") {
		text = strings.ReplaceAll(text, "\n\n\n", "\n\n")
	}
	return strings.TrimSpace(text)
}

// Clamp enforces the Telegram caption/message length ceilings the Python
// pipeline applies after a transform: 1024 for a media caption, 4096 for a
// plain text message.
func Clamp(text string, hasMedia bool) string {
	if text == "" {
		return text
	}
	max := 4096
	if hasMedia {
		max = 1024
	}
	if len([]rune(text)) > max {
		return string([]rune(text)[:max])
	}
	return text
}

// HasLink is the shared "does this text carry a link" predicate, kept in the
// same package so the transform and the filter cannot disagree about it.
func HasLink(text string) bool {
	lowered := strings.ToLower(text)
	return urlRegex.MatchString(text) ||
		strings.Contains(lowered, "http://") ||
		strings.Contains(lowered, "https://") ||
		strings.Contains(lowered, "t.me/")
}

// Hashtags returns every #hashtag in the text (used by the strip pass).
func Hashtags(text string) []string {
	return hashtagRegex.FindAllString(text, -1)
}

// Usernames returns every @username in the text (used by the strip pass).
func Usernames(text string) []string {
	return usernameRegex.FindAllString(text, -1)
}

// RemoveEmojis strips emoji, the Python remove_emojis transform.
func RemoveEmojis(text string) string {
	return emojiRegex.ReplaceAllString(text, "")
}

// FormatText is the template formatter for header/footer placeholders. The
// pipeline applies it on the ingest side; it is exposed here so a worker-side
// preview renders identically. It never panics on a bad placeholder.
func FormatText(template string, placeholders map[string]string) string {
	if template == "" || placeholders == nil {
		return template
	}
	out := template
	for key, value := range placeholders {
		out = strings.ReplaceAll(out, "{"+key+"}", value)
	}
	return out
}
