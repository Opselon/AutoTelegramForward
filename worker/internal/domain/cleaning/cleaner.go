// Package cleaning owns the message-cleaning pipeline the Python
// DurableMessagePipeline applies in _apply_transforms
// (core/application/pipeline.py:1401-1580).
//
// It is a removal-only, never-dropping pass: a failure in any stage delivers
// the text unchanged (pipeline.py:1437 "A cleaner failure must never drop the
// message"), and the only allowed drop is empty_after_clean, decided by the
// caller from the pipeline result.
//
// Stage order is the contract (processing-pipeline.md step 1-5) and must not
// be reordered:
//
//  1. cleaner (CleanerRule.clean)      — custom words, urls, mentions,
//                                        hashtags, emoji, phones, blank lines
//  2. link management                  — the task link policy, entity-aware
//  3. remove_emojis flag               — the task-level flag (distinct from
//                                        the cleaner's own switch)
//  4. empty_after_clean decision
//  5. content policy rewrite           — remove_links / strip_words /
//                                        replacements on the cleaned text
//
// Why a separate package from transform: the Python audit (F-08) proved that
// a Go transform package that compiles but is never called by production is
// worse than no package at all — it looks like parity while silently changing
// nothing. Everything here is wired through decision.Pipeline, which the
// delivery engine calls on the raw payload text, so there is exactly one
// caller and it is on the production path.
package cleaning

import (
	"regexp"
	"strings"
)

// maxPatternLen bounds how much of the body a user-supplied pattern may be
// asked to match. The Python content policy applies the same 8 KiB ceiling
// (content_policy._MAX_MATCH_LEN) for the same reason: the structural ReDoS
// guard refuses the catalogued catastrophic shapes, but a pattern nobody
// catalogued could still degrade quadratically on a long body.
const maxMatchLen = 8192

// --- the task-level configuration (metadata-resolved) -----------------------

// Config is the task-level cleaning + transform configuration the Python
// ForwardRule resolves out of its metadata blob at transform time. Every field
// here is read by the Python pipeline in _apply_transforms; carrying them in
// one Go struct means the worker decides from the same view the Python
// engine decided from.
type Config struct {
	// RuleID / OwnerUserID are attribution for the decision log, never
	// message content.
	RuleID     string
	OwnerUserID int64

	// TargetChatID is the chat this delivery is bound for. The transform
	// pipeline is per-target: the target-scoped override applies to this
	// target's copy of the text alone, so the pipeline must know which
	// target that is.
	TargetChatID string

	// Cleaner is the materialized CleanerRule (ForwardRule.build_cleaner),
	// or nil when the task has the cleaner disabled/empty.
	Cleaner *CleanerRule

	// LinkPolicy is the resolved link policy. "" means PRESERVE_ALL.
	LinkPolicy string
	// RemoveLinks is the legacy task-level "Links: remove" toggle. Python
	// resolves it into REMOVE_ALL_URLS when no explicit policy is set.
	RemoveLinks bool
	LinkReplacement string
	DomainAllowlist []string
	DomainBlocklist []string
	LinkRewriteMap  map[string]string

	// RemoveEmojis is the task-level emoji flag (pipeline.py:1461). It is
	// deliberately separate from Cleaner.RemoveEmojis: the Python pipeline
	// can run both, and both are idempotent on the same input (the audit's
	// emoji double-processing resolution).
	RemoveEmojis bool

	// HasMedia tells the empty-after-clean gate which ceiling applies and
	// which text the pipeline was actually transforming (the caption).
	HasMedia bool

	// ContentPolicy is the task-level content policy rewrite applied after
	// the cleaner (pipeline.py:1496). Nil means no rewrite.
	ContentPolicy *ContentPolicy

	// Replacements are the task's replace entries (pipeline.py:1543),
	// applied AFTER the content policy so an expanding entry cannot resurrect
	// text an earlier stage removed.
	Replacements []ReplacementEntry

	// Header / Footer are the task's header and footer text
	// (pipeline.py:1547). Applied by the pipeline WITH placeholder
	// interpolation (Stage 4.4 closes F-15): when HeaderVars is nil the
	// templates are applied raw — the pre-4.4 durable-path behavior,
	// preserved so a caller that has no message snapshot cannot
	// accidentally render half-expanded placeholders.
	Header string
	Footer string
	// HeaderVars / HeaderTask are the message- and task-level variables
	// the header templates render against. Nil means "no snapshot
	// available", and the pipeline responds by applying the templates
	// without expansion.
	HeaderVars *HeaderVars
	HeaderTask HeaderTask

	// Watermark / LinkButtons / AffiliateKit are the advanced content
	// transforms (services.py apply_content_transforms, applied at
	// pipeline.py:1558). Nil/empty means the add-on is off.
	Watermark    string
	LinkButtons  []LinkButton
	AffiliateKit *AffiliateKit

	// TargetOverrides are the target-scoped content policies keyed by
	// normalized chat id. The Python fan-out applies the matching entry's
	// rewrite to one target's copy of the text (pipeline.py:1276-1285);
	// the Worker applies it to the job's own target when it owns the
	// transform stage. An entry that is absent never relaxes anything —
	// it simply does not apply to that target.
	TargetOverrides map[string]*ContentPolicy

	// SplitLongCaption mirrors the task's split_long_caption flag. The
	// dispatch stage consumes the pipeline result; the caption split itself
	// runs there because it needs the entity list.
	SplitLongCaption bool
}

// LinkButton is one rendered link button of the link_buttons add-on.
type LinkButton struct {
	Text string
	URL  string
}

// AffiliateKit is the affiliate rewrite add-on: naked URLs whose domain
// matches a configured entry are prefixed, preserving every other character.
type AffiliateKit struct {
	Prefix string
}

// ContentPolicy is the narrow rewrite slice of the task's content policy —
// only the parts _apply_transforms' ContentPolicy.transform applies. The
// decision (allow/deny) half of the policy is evaluated at ingest; this is
// what rewrites the text that survives.
type ContentPolicy struct {
	RemoveLinks  bool
	StripWords   []string
	Replacements [][2]string
	CaseSensitive bool
}

// ReplacementEntry is one find/replace entry (text_replace apply_replacements).
// FindPattern is the source text or regex; order and priority are the caller's.
type ReplacementEntry struct {
	Find      string
	Replace   string
	IsRegex   bool
	IgnoreCase bool
	WholeWord  bool
}

// --- the cleaner rule --------------------------------------------------------

// CleanerRule mirrors Python CleanerRule (entities.py:2335): a removal-only
// text transform. Everything here is a port of the Python regexes, kept
// byte-identical so a task's cleaner produces the same output in either
// engine — the contracts that pin this are the CLN-* and UNI-* fixtures.
type CleanerRule struct {
	Patterns           []string
	UseRegex           bool
	RemoveURLs         bool
	RemoveMentions     bool
	RemoveHashtags     bool
	RemoveEmojis       bool
	RemovePhoneNumbers bool
	RemoveEmptyLines   bool
	StripButtons       bool
	ApplyToCaption     bool
	CaseSensitive      bool
	IsActive           bool
}

// cleanURLRegex is _CLEANER_URL_RE (entities.py:97-100).
var cleanURLRegex = regexp.MustCompile(`(?i)https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+`)

// cleanMentionRegex is _CLEANER_MENTION_RE (entities.py:101): {4,64}, NOT the
// legacy {3,32} — the audit's F-07 divergence pins the cleaner's bound.
var cleanMentionRegex = regexp.MustCompile(`@[A-Za-z0-9_]{4,64}`)

// cleanHashtagRegex is _CLEANER_HASHTAG_RE (entities.py:102): #\w+, which
// stops at a hyphen (F-07: Go's old #[^\s#@]+ consumed it).
var cleanHashtagRegex = regexp.MustCompile(`#\w+`)

// cleanEmojiRegex is _CLEANER_EMOJI_RE (entities.py:103-106). Byte-identical
// to the Python pattern, including its known defects (F-03: it matches every
// non-BMP codepoint, so a historical script like cuneiform is destroyed).
// Changing this pattern changes what a pinned fixture expects; the fix is a
// deliberate cross-language decision, not a Go-side cleanup.
var cleanEmojiRegex = regexp.MustCompile(`[\x{10000}-\x{10ffff}]|[\x{2600}-\x{27bf}]|[\x{2300}-\x{23ff}]|[\x{2b50}-\x{2b55}]|[\x{fe00}-\x{fe0f}]`)

// phoneCandidate is _CLEANER_PHONE_CANDIDATE_RE (entities.py:121): a bounded
// candidate, then a callback that only removes things that really look like a
// contact number. A bare 10-digit product id is deliberately preserved
// (fixture CLN-PHONE-01).
//
// Python writes the boundary as lookarounds — `(?<![\d]) ... (?![\d])` — but
// RE2 has no lookbehind, so the same boundary is expressed by *consuming* the
// surrounding character as a capture group and restoring it when the match is
// removed. `([^0-9]|^)` matches exactly "a non-digit, or the start of the
// text", which is the same test the Python lookbehind makes. The previous Go
// port dropped the boundary entirely and ate a bare product id; keeping it is
// the CLN-PHONE-01 parity fix, not a cosmetic change.
var phoneCandidate = regexp.MustCompile(`([^0-9]|^)\+?\d[\d\s\-().]{5,18}\d([^0-9]|$)`)

var phoneSeparator = regexp.MustCompile(`[\s\-().]`)
var phoneNonDigits = regexp.MustCompile(`\D`)

// blankLines is _CLEANER_BLANK_LINES_RE.
var blankLines = regexp.MustCompile(`\n[ \t]*\n(?:[ \t]*\n)*`)
var manySpaces = regexp.MustCompile(`[ \t]{2,}`)
var manyNewlines = regexp.MustCompile(`\n{3,}`)

// Clean is Python CleanerRule.clean, ported stage for stage: patterns first,
// then urls, mentions, hashtags, emoji, phones; then the UNCONDITIONAL
// whitespace collapse that follows every removal, the optional blank-line
// squeeze, the 3+-newline squeeze, and the final trim.
func (c *CleanerRule) Clean(text string) string {
	if c == nil || !c.IsActive || text == "" {
		return text
	}
	out := text
	for _, pat := range c.Patterns {
		if pat == "" {
			continue
		}
		if c.UseRegex {
			prefix := ""
			if !c.CaseSensitive {
				prefix = "(?i)"
			}
			// A malformed user pattern must not break forwarding: the Python
			// rule wraps re.sub in except re.error and continues.
			re, err := regexp.Compile(prefix + truncatePattern(pat))
			if err == nil {
				out = re.ReplaceAllString(out, "")
			}
		} else if c.CaseSensitive {
			out = strings.ReplaceAll(out, pat, "")
		} else {
			re, err := regexp.Compile("(?i)" + regexp.QuoteMeta(pat))
			if err == nil {
				out = re.ReplaceAllString(out, "")
			}
		}
	}
	if c.RemoveURLs {
		out = cleanURLRegex.ReplaceAllString(out, "")
	}
	if c.RemoveMentions {
		out = cleanMentionRegex.ReplaceAllString(out, "")
	}
	if c.RemoveHashtags {
		out = cleanHashtagRegex.ReplaceAllString(out, "")
	}
	if c.RemoveEmojis {
		out = cleanEmojiRegex.ReplaceAllString(out, "")
	}
	if c.RemovePhoneNumbers {
		out = stripPhoneNumbers(out)
	}
	// Collapse the whitespace holes the removals punched out so the rendered
	// message does not show "word  word" artifacts (entities.py:2402).
	out = manySpaces.ReplaceAllString(out, " ")
	if c.RemoveEmptyLines {
		out = blankLines.ReplaceAllString(out, "\n")
	}
	out = manyNewlines.ReplaceAllString(out, "\n\n")
	return strings.TrimSpace(out)
}

// stripPhoneNumbers is Python _strip_phone_numbers: remove only candidates
// that really look like contact numbers — a leading "+" or at least 10 digits
// carrying a separator. A bare id stays.
//
// The pattern consumes one boundary character on each side (RE2 has no
// lookbehind), so a removed candidate must put the leading boundary back and
// drop the trailing one: "call 1234567890 now" matches as "l 1234567890 n",
// and removing the whole span would eat the spaces around the number.
func stripPhoneNumbers(text string) string {
	return phoneCandidate.ReplaceAllStringFunc(text, func(candidate string) string {
		// groups[0] is the leading non-digit (or "" at the text start),
		// groups[1] the trailing one. Both are restored/preserved by hand.
		groups := phoneCandidate.FindStringSubmatch(candidate)
		body := candidate
		lead := ""
		trail := ""
		if groups != nil {
			lead = groups[1]
			trail = groups[2]
			body = strings.TrimPrefix(candidate, lead)
			body = strings.TrimSuffix(body, trail)
		}
		digits := phoneNonDigits.ReplaceAllString(body, "")
		if len(digits) < 8 || len(digits) > 15 {
			return candidate
		}
		if strings.HasPrefix(strings.TrimSpace(body), "+") {
			return lead
		}
		if len(digits) >= 10 && phoneSeparator.MatchString(body) {
			return lead
		}
		return candidate
	})
}

// truncatePattern bounds a user pattern before it ever reaches a regex
// engine, mirroring the Python length cap on user-supplied patterns.
func truncatePattern(pattern string) string {
	if len(pattern) > maxMatchLen {
		return pattern[:maxMatchLen]
	}
	return pattern
}
