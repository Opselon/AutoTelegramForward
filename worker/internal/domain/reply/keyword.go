// Package reply ports the keyword-confirmation / keyword-reply add-on
// decision (core/domain/services.py::AdvancedSettingsEngine.classify_keyword_trigger)
// so the Go Worker can make the same delivery-time choice the Python
// pipeline makes at ingest.
//
// The port is deliberately narrow: it is the DECISION only — "given this
// task and this text, is the configured reply also posted, and what does
// it say?" — never the delivery mechanics. A trigger never drops or delays
// the forward it matched on; it only schedules a secondary, best-effort
// text deliverable to the same target (pipeline.py:1381-1411).
//
// The word list the classifier reads is the task's content-policy
// whitelist, resolved exactly as the Python ForwardRule.whitelist property
// resolves it (entities.py:1467): the policy blob's whitelist_words first,
// with the legacy pre-policy metadata["whitelist"] list as the fallback a
// task configured before the content policy existed keeps its line on.
package reply

import (
	"strings"
)

// KeywordReplyMaxLen is KEYWORD_REPLY_MAX_LEN (services.py): the reply text
// is clamped before it is compared or returned, so a task configured with
// a novel-length reply never sends more than the add-on allows. Python's
// slice counts code points, so on multi-byte scripts the clamped reply can
// be up to 4x this many BYTES — the engine below clamps by rune count to
// match, and the byte cap never applies in practice.
const KeywordReplyMaxLen = 4096

// AddonKey is the paid-addon key the gate consults
// (AdvancedSettingsEngine.KEYWORD_REPLY_ADDON_KEY). A client-supplied
// paid_addons flag is never trusted on its own: the entitlement must be
// recorded in the persisted rule metadata (entities.py:1843).
const AddonKey = "keyword_reply"

// Settings is the narrow view of one task the classifier reads. It is a
// struct, not the full rule.ForwardRule, so the decision can be exercised
// in tests and from the parity harness without pretending the whole task
// projection is an input.
type Settings struct {
	// KeywordConfirmation mirrors ForwardRule.keyword_confirmation
	// (entities.py:1787): the add-on's master switch. False (or absent)
	// means no reply is ever considered, whatever else is configured.
	KeywordConfirmation bool
	// KeywordReply mirrors ForwardRule.keyword_reply (entities.py:1799):
	// the text posted on a match. Empty (after the trim below) means the
	// add-on is configured but has nothing to say, which Python treats as
	// "off" — a trigger with no reply text is not a trigger.
	KeywordReply string
	// Whitelist mirrors ForwardRule.whitelist (entities.py:1467): the
	// words whose presence anywhere in the text fires the reply. Order is
	// irrelevant; emptiness is terminal.
	Whitelist []string
	// AddonEnabled mirrors ForwardRule.is_addon_enabled("keyword_reply")
	// (entities.py:1843): the persisted paid-addon entitlement. The gate
	// is consulted AFTER the reply text, exactly as Python orders it, so
	// the two engines cannot diverge on which guard fired.
	AddonEnabled bool
}

// Classify port classify_keyword_trigger (services.py:1503).
//
// It returns the reply text to send when a whitelisted keyword is present,
// or "" when the add-on is disabled or no keyword matches. The order of
// the guards is the contract, and it mirrors Python line for line:
//
//  1. keyword_confirmation off → none
//  2. reply text empty after trim+clamp → none
//  3. paid-addon entitlement absent → none
//  4. no keywords → none
//  5. any keyword (lowercased substring) in the lowercased text → the reply
//
// A substring match, not a whole-word match: Python's
// `kw in haystack` is the pinned semantics, so "sale" fires on "wholesale"
// on both engines or neither.
func Classify(text string, s Settings) string {
	if !s.KeywordConfirmation {
		return ""
	}
	reply := clampReply(s.KeywordReply)
	if reply == "" {
		return ""
	}
	if !s.AddonEnabled {
		return ""
	}
	keywords := normalizedKeywords(s.Whitelist)
	if len(keywords) == 0 {
		return ""
	}
	// A keyword confirmation never drops the forwarded message: it only
	// decides whether the configured reply is also posted to the target.
	haystack := strings.ToLower(text)
	for _, kw := range keywords {
		if strings.Contains(haystack, kw) {
			return reply
		}
	}
	return ""
}

// clampReply strips surrounding whitespace and clamps to the add-on's
// maximum length, mirroring Python's
// `(rule.keyword_reply or "").strip()[: cls.KEYWORD_REPLY_MAX_LEN]`.
//
// The clamp is rune-aware on the Go side. Python's slice cuts at a code
// point boundary by construction; a naive Go byte slice can split a
// multi-byte UTF-8 rune in half and produce invalid UTF-8, which no
// Telegram send accepts. Backing off to the last complete rune keeps both
// engines returning valid strings that agree on every input that is valid
// UTF-8 to begin with — the only input either engine accepts from
// Telegram. (The two lengths differ only when the cut lands mid-rune;
// Python never produces those bytes in the first place.)
func clampReply(reply string) string {
	trimmed := strings.TrimSpace(reply)
	if len([]rune(trimmed)) <= KeywordReplyMaxLen {
		return trimmed
	}
	runes := []rune(trimmed)
	return string(runes[:KeywordReplyMaxLen])
}

// normalizedKeywords lowercases and de-blanks the word list, mirroring
// Python's
// `[str(k).strip().lower() for k in (rule.whitelist or []) if str(k).strip()]`.
// A keyword that is empty after trimming is dropped: an empty string is a
// substring of every text, so honouring it would fire the reply on
// everything, which Python's own filter prevents.
func normalizedKeywords(words []string) []string {
	out := make([]string, 0, len(words))
	for _, word := range words {
		kw := strings.ToLower(strings.TrimSpace(word))
		if kw != "" {
			out = append(out, kw)
		}
	}
	return out
}
