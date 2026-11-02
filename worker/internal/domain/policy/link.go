package policy

import (
	"regexp"
)

// LinkClass is the category of one link-shaped span in a message.
//
// The classifier distinguishes them because Stage 4.1's block-links gate only
// needs the boolean, but the surfaces that come later (link rewriting, the
// rule tester, per-class allow lists) need to know *which* kind of link was
// found. Recording the class at classification time keeps that a read of the
// text, not a second set of divergent regexes.
type LinkClass string

const (
	// LinkHTTPS is an https:// URL.
	LinkHTTPS LinkClass = "https"
	// LinkHTTP is an http:// URL.
	LinkHTTP LinkClass = "http"
	// LinkFTP is an ftp:// URL.
	LinkFTP LinkClass = "ftp"
	// LinkTelegram is a t.me/ short link (the form Telegram itself renders
	// for invites and posts).
	LinkTelegram LinkClass = "t.me"
	// LinkTelegramMe is a telegram.me/ link.
	LinkTelegramMe LinkClass = "telegram.me"
	// LinkTelegramDog is a telegram.dog/ link (the invite-only mirror of
	// t.me — a reader sees a link, so a link gate must see one too).
	LinkTelegramDog LinkClass = "telegram.dog"
	// LinkTG is a tg:// deep link (tg://resolve?domain=…). Not an http URL
	// at all, and exactly the shape the FilterEngine's gate misses.
	LinkTG LinkClass = "tg"
	// LinkWWW is a bare www. host without a scheme.
	LinkWWW LinkClass = "www"
	// LinkOther carries a scheme the policy does not name explicitly (file://,
	// mailto:, ssh://…). Recorded so Classify stays total; HasLink treats it
	// as a link because the text visibly claims to be one.
	LinkOther LinkClass = "other"
)

// linkRe is the canonical link detector: Python's _RE_LINK
// (content_policy.py:96-99), ported verbatim and case-insensitive.
//
// This is deliberately broader than an http substring check: a message that
// only carries `t.me/x` or `tg://` is still a link to a reader, and a
// "block links" toggle that misses it is a toggle users stop trusting.
// Alternations are ordered longest-first for a deterministic class.
var linkRe = regexp.MustCompile(`(?i)(https?://|ftp://|www\.|t\.me/|telegram\.me/|telegram\.dog/|tg://)`)

// schemeRe is reserved for a later stage that widens HasLink beyond the
// schemes the Python gate names (file://, mailto:, ssh://). It is unused now
// and deliberately not compiled: adding an unused regexp would cost an init
// for nothing, and the day a caller needs it, an unused-variable failure at
// compile time is the correct reminder to wire it in.

// --- the canonical predicate -------------------------------------------------

// HasLink is THE canonical "does this text carry a link" predicate
// (Python has_link, content_policy.py:121-128).
//
// It replaces the three predicates that disagreed with each other in the
// audit: Python services' URL_REGEX gate (services.py:79), Python's
// has_link/_RE_LINK (content_policy.py:96/121) and the Go filter's ad-hoc
// URL_REGEX (worker/internal/engine/filter/filter.go:50). Of those, this one
// follows has_link, because has_link is what the ContentPolicy block-links
// gate actually applies — it pins the F-14 divergences:
//
//   - LNK-GATE-01: `tg://resolve?domain=x` and `ftp://` are links here. The
//     FilterEngine's URL_REGEX misses tg:// entirely, so its block_links gate
//     passes a message the policy drops.
//   - LNK-GATE-02: a bare `@username` is NOT a link here. The FilterEngine's
//     URL_REGEX includes @[A-Za-z0-9_]{3,32} as an alternation, so it drops
//     messages that merely mention a user.
//
// Note for callers: HasLink is the predicate the *policy* applies. The
// FilterEngine's own gate is a different, documented divergence (F-14) and is
// not retuned here — this package owns the canonical answer, filter.go keeps
// its existing one, and the audit fixtures pin both.
func HasLink(text string) bool {
	if text == "" {
		return false
	}
	return linkRe.MatchString(text)
}

// Classify returns the ordered list of link classes found in text, in the
// order they appear. A text without a link returns an empty slice.
//
// Every link HasLink reports appears here with its class, so a caller can
// assert on categories (a rule that blocks only t.me links, a test that a
// message carries exactly one https link) without re-deriving them. Spans
// whose scheme the gate does not name are reported as LinkOther; every span
// HasLink reports has a class, and no other span does —Classify is total
// over exactly the same text.
func Classify(text string) []LinkClass {
	if text == "" {
		return nil
	}
	out := []LinkClass{}
	start := 0
	for start <= len(text) {
		loc := linkRe.FindStringSubmatchIndex(text[start:])
		if loc == nil {
			break
		}
		// loc[2] is the start of group 1 (the alternation), relative to start.
		abs := start + loc[2]
		out = append(out, classAt(text, abs))
		// Advance past the classed span so each link is reported once, in
		// order. The minimum advance is one character (a bare "w" of www.
		// matched only partially), and a scheme match advances past the
		// colon so the host is not re-scanned for another scheme.
		end := start + loc[3]
		adv := end - start
		if adv < 1 {
			adv = 1
		}
		start = end + advancePastColon(text[end:], adv)
	}
	return out
}

// advancePastColon returns the extra advance, past the scheme, that skips the
// host of the link just classed, so the host is not re-scanned as another
// scheme. For non-scheme matches (www.) the extra is zero; the host scan then
// resumes right after the matched prefix, which is what keeps Classify's
// report to one class per link.
func advancePastColon(rest string, adv int) int {
	if adv >= len(rest) {
		return 0
	}
	if rest[adv] == ':' {
		return 1
	}
	return 0
}

// classAt resolves the link class of the span beginning at index i in text.
// The caller guarantees i is a match boundary of linkRe; the class is decided
// by reading the literal alternatives in the order the regex tries them —
// longest first, so telegram.me/ wins over a bare t-prefixed scheme.
func classAt(text string, i int) LinkClass {
	// Ordered exactly as linkRe's alternation is written: https?:// before
	// the bare-scheme branches, and the telegram hosts before the t-prefixed
	// schemes, so `telegram.me/` is not mistaken for a `tel:`-like scheme.
	if hasPrefixFold(text[i:], "https://") {
		return LinkHTTPS
	}
	if hasPrefixFold(text[i:], "http://") {
		return LinkHTTP
	}
	if hasPrefixFold(text[i:], "ftp://") {
		return LinkFTP
	}
	if hasPrefixFold(text[i:], "telegram.me/") {
		return LinkTelegramMe
	}
	if hasPrefixFold(text[i:], "telegram.dog/") {
		return LinkTelegramDog
	}
	if hasPrefixFold(text[i:], "t.me/") {
		return LinkTelegram
	}
	if hasPrefixFold(text[i:], "tg://") {
		return LinkTG
	}
	if hasPrefixFold(text[i:], "www.") {
		return LinkWWW
	}
	// A linkRe match that none of the literals above recognized should be
	// impossible — linkRe's alternation is exactly the list above. Report it
	// rather than silently misclassifying; a test that reaches this branch is
	// a bug in the classifier, not an unknown link.
	return LinkOther
}

// hasPrefixFold reports whether s starts with prefix, ignoring ASCII case —
// the link regex is IGNORECASE, so HTTP:// and Tg:// are links too.
func hasPrefixFold(s, prefix string) bool {
	if len(s) < len(prefix) {
		return false
	}
	for i := 0; i < len(prefix); i++ {
		c, p := s[i], prefix[i]
		if 'A' <= c && c <= 'Z' {
			c += 'a' - 'A'
		}
		if 'A' <= p && p <= 'Z' {
			p += 'a' - 'A'
		}
		if c != p {
			return false
		}
	}
	return true
}
