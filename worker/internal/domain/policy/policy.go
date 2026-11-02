package policy

import (
	"regexp"
	"strconv"
	"strings"
	"sync"
	"unicode/utf8"
)

// reDoSNested / reDoSAlt recognise the two catastrophic-backtracking shapes
// Python's compile_user_pattern refuses: a quantifier nested in a quantified
// group — (a+)+ — and an alternation under a quantifier — (a|a)*. The shapes
// and the constants below are shared with worker/internal/engine/filter
// (filter.go:56-67) so a pattern is accepted or refused for the same reason
// on both sides of the ContentPolicy/FilterRule boundary.
var reDoSNested = regexp.MustCompile(`\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})`)
var reDoSAlt = regexp.MustCompile(`\((?:[^()\\]|\\.)*\|(?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})`)

const (
	// maxPatternLen is the length ceiling on a user-supplied pattern.
	maxPatternLen = 1000
	// maxMatchLen is the ceiling on the text a user-supplied pattern is
	// matched against. The structural guard refuses the catalogued
	// catastrophic shapes, but it cannot prove a pattern *safe*: an
	// uncatalogued one could still degrade quadratically or worse on a long
	// body. Capping the scanned text keeps the worst case linear in the
	// input, while a message longer than the cap only loses matching in its
	// tail — and 8 KiB is far beyond a Telegram message (4096) already.
	maxMatchLen = 8192
)

// --- the regex cache (content_policy.py:333-349, lru_cache) -------------------

// patternCache memoizes compiled user patterns, keyed by the pattern text.
// Python gets the same effect with lru_cache(maxsize=2048); Go uses a
// mutex-guarded map with the same 2048-entry budget. A cache miss compiles,
// which is the cost the cache exists to avoid on a hot delivery path.
var (
	patternCacheMu sync.RWMutex
	patternCache   = make(map[string]*regexp.Regexp, 128)
	// patternRefused records the patterns the guard refused, so a refused
	// pattern is not re-tested against the guard on every message: the
	// answer is cached exactly as a compiled pattern would be.
	patternRefusedMu sync.RWMutex
	patternRefused   = make(map[string]bool, 32)
)

// CompileUserRegex compiles a user-supplied pattern, or reports it unusable
// (Python compile_user_regex / compile_user_pattern, :333-362).
//
// Refused patterns are the ones that are long or carry a nested quantifier —
// the shapes that turn a crafted message into minutes of backtracking. A
// refused pattern never *allows* anything on its own: callers fail closed on
// it (the evaluator returns invalid_regex_*), mirroring content_policy.py:1042-1070.
//
// Compiled patterns carry (?im): Python compiles with re.IGNORECASE |
// re.MULTILINE, and every RE2 translation of those flags that changes the
// language is a divergence, not an optimization.
func CompileUserRegex(pattern string) (*regexp.Regexp, bool) {
	if len(pattern) == 0 || len(pattern) > maxPatternLen {
		return nil, false
	}

	patternRefusedMu.RLock()
	if patternRefused[pattern] {
		patternRefusedMu.RUnlock()
		return nil, false
	}
	patternRefusedMu.RUnlock()

	patternCacheMu.RLock()
	if got, ok := patternCache[pattern]; ok {
		patternCacheMu.RUnlock()
		return got, true
	}
	patternCacheMu.RUnlock()

	if reDoSNested.MatchString(pattern) || reDoSAlt.MatchString(pattern) {
		patternRefusedMu.Lock()
		if len(patternRefused) < 2048 {
			patternRefused[pattern] = true
		}
		patternRefusedMu.Unlock()
		return nil, false
	}

	c, err := regexp.Compile("(?im)" + pattern)
	if err != nil {
		patternRefusedMu.Lock()
		if len(patternRefused) < 2048 {
			patternRefused[pattern] = true
		}
		patternRefusedMu.Unlock()
		return nil, false
	}

	patternCacheMu.Lock()
	if len(patternCache) < 2048 {
		patternCache[pattern] = c
	}
	patternCacheMu.Unlock()
	return c, true
}

// boundedMatch applies the length ceiling that keeps a user pattern's worst
// case linear in the input (Python bounded_search, :317-330).
func boundedMatch(c *regexp.Regexp, text string) bool {
	if len(text) > maxMatchLen {
		text = text[:maxMatchLen]
	}
	return c.MatchString(text)
}

// truncate caps a pattern for a reason string (Python pattern[:32] /
// pattern[:48], byte-slice slicing — same as filter.go:233-238).
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// --- the policy (content_policy.py:690-788) ------------------------------------

// WhitelistMode is how a whitelist's words must combine: any (at least one),
// all (every one), or phrase (the words as one phrase).
type WhitelistMode string

const (
	// WhitelistAny is the default: the text must contain at least one listed
	// word.
	WhitelistAny WhitelistMode = "any"
	// WhitelistAll requires every listed word.
	WhitelistAll WhitelistMode = "all"
	// WhitelistPhrase requires the words, joined by single spaces, as one
	// phrase.
	WhitelistPhrase WhitelistMode = "phrase"
)

// ContentPolicy is the content rules of one task, or one source/target
// override (Python ContentPolicy, content_policy.py:690-788).
//
// The fields map one-for-one onto the Python dataclass. The zero value is an
// empty, inactive policy — no rules, nothing to enforce — so a caller that
// forgets to populate a field gets an allow, not a surprise drop.
type ContentPolicy struct {
	// Enabled is the tri-state activator: nil (absent) means auto — active
	// iff the policy has rules; true means active iff it has rules; false
	// means off, whatever the rules say. Modelled as *bool so the three
	// states stay distinguishable, exactly as Python's Optional[bool] does.
	Enabled *bool

	// BlacklistOff / WhitelistOff are the v30 per-list switches: a switched-
	// off list is skipped by Evaluate but keeps its words, so re-enabling
	// restores exactly what was saved. The switch also parks the regex list
	// on the same side (regex_block is blacklist-side, regex_require
	// whitelist-side).
	BlacklistOff bool
	WhitelistOff bool

	BlacklistWords []string
	WhitelistWords []string
	// WhitelistMode is any | all | phrase. An unrecognized value behaves as
	// "any" rather than as an error the caller must translate.
	WhitelistMode WhitelistMode

	BlockLinks  bool
	RemoveLinks bool

	StripWords    []string
	Replacements  [][2]string
	RegexBlock    []string
	RegexRequire  []string
	MinLength     int
	MaxLength     int
	AllowedMedia  []string
	BlockedMedia  []string
	CaseSensitive bool
	WordBoundary  bool
	Normalize     bool
	// Source is the provenance for logs and UI: "" | "task" | "source:<id>" |
	// "target:<id>".
	Source string
	// Notes carries the free-form metadata the UI wants next to a decision;
	// it takes no part in evaluation.
	Notes map[string]any
}

// WithSource returns a copy of the policy with Source set, so a chain can
// label a policy's provenance without mutating the caller's copy.
func (p *ContentPolicy) WithSource(source string) *ContentPolicy {
	if p == nil {
		return nil
	}
	clone := *p
	clone.Source = source
	return &clone
}

// HasRules reports whether the policy actually constrains or rewrites
// anything (Python has_rules, :776-788). An empty policy is inert, whatever
// Enabled says.
func (p *ContentPolicy) HasRules() bool {
	if p == nil {
		return false
	}
	return len(p.BlacklistWords) > 0 || len(p.WhitelistWords) > 0 || p.BlockLinks ||
		p.RemoveLinks || len(p.StripWords) > 0 || len(p.Replacements) > 0 ||
		len(p.RegexBlock) > 0 || len(p.RegexRequire) > 0 || p.MinLength != 0 ||
		p.MaxLength != 0 || len(p.AllowedMedia) > 0 || len(p.BlockedMedia) > 0
}

// IsActive reports whether configured rules actually run (Python is_active,
// :790-797): rules run unless the task explicitly switched them off, and an
// explicitly-enabled policy with no rules is still inert.
func (p *ContentPolicy) IsActive() bool {
	if p == nil {
		return false
	}
	if p.Enabled != nil && !*p.Enabled {
		return false
	}
	return p.HasRules()
}

// mode returns the whitelist mode, defaulting to "any" for anything the
// constant set does not recognize — including the zero value, so a policy
// built as a struct literal (no from_dict) behaves like ContentPolicy's own
// default (ContentPolicy.from_dict, :725-727).
func (p *ContentPolicy) mode() WhitelistMode {
	switch p.WhitelistMode {
	case WhitelistAll, WhitelistPhrase:
		return p.WhitelistMode
	default:
		return WhitelistAny
	}
}

// --- the decision (content_policy.py:376-386) -----------------------------------

// Decision is the outcome of evaluating one policy against one message
// (Python PolicyDecision). Allowed=false always carries the reason the
// Python evaluator produced, so an operator debugging a drop sees the same
// vocabulary from either engine.
type Decision struct {
	Allowed bool
	Reason  string
	// Matched is the entries that decided the outcome, as the user typed
	// them — what a rule owner reads in a test result is what they wrote in
	// the editor.
	Matched []string
}

// DecisionAllowed is the shared allow result for a policy that passed.
func DecisionAllowed(reason string) Decision { return Decision{Allowed: true, Reason: reason} }

// DecisionDenied is the shared deny result with the matched entries attached.
func DecisionDenied(reason string, matched ...string) Decision {
	return Decision{Allowed: false, Reason: reason, Matched: matched}
}

// Evaluate decides whether text may be forwarded (Python
// ContentPolicy.evaluate, content_policy.py:823-903). Cheapest check first.
//
// Check order, which *is* the semantics — do not reorder:
//  1. is_active     2. blocked_media   3. allowed_media
//  4. min_length    5. max_length      6. block_links
//  7. blacklist     8. whitelist (by mode)
//  9. regex_block   10. regex_require
//
// Length counts Unicode code points, not UTF-8 bytes: Python len(body) is
// codepoints, so the gate must be RuneCountInString or a Persian message
// would hit max_length before its Python twin does.
func (p *ContentPolicy) Evaluate(text, media string) Decision {
	if !p.IsActive() {
		return DecisionAllowed("policy_inactive")
	}
	body := text
	mediaL := strings.ToLower(media)
	if mediaL == "" {
		mediaL = "text"
	}

	// Media gates. Python default is "text"; a missing media type on the Go
	// side is normalized the same way so the two engines agree on the bucket.
	if len(p.BlockedMedia) > 0 {
		for _, m := range p.BlockedMedia {
			if strings.ToLower(m) == mediaL {
				return DecisionDenied("media_blocked:" + mediaL)
			}
		}
	}
	if len(p.AllowedMedia) > 0 {
		ok := false
		for _, m := range p.AllowedMedia {
			if strings.ToLower(m) == mediaL {
				ok = true
				break
			}
		}
		if !ok {
			return DecisionDenied("media_not_allowed:" + mediaL)
		}
	}

	// Length gates: code points, never bytes.
	n := utf8.RuneCountInString(body)
	if p.MinLength != 0 && n < p.MinLength {
		return DecisionDenied("too_short:" + strconv.Itoa(n))
	}
	if p.MaxLength != 0 && n > p.MaxLength {
		return DecisionDenied("too_long:" + strconv.Itoa(n))
	}

	if p.BlockLinks && HasLink(body) {
		return DecisionDenied("links_blocked")
	}

	// v30: a switched-off list is skipped, not deleted. Only the list the
	// flag names is skipped — switching the blacklist off must not silently
	// stop the whitelist from doing its job, and vice versa.
	if len(p.BlacklistWords) > 0 && !p.BlacklistOff {
		if word := globalMatcher.FirstMatchingWord(body, p.BlacklistWords, p.Normalize, p.CaseSensitive, p.WordBoundary); word != "" {
			return DecisionDenied("blacklist:"+word, word)
		}
	}

	if len(p.WhitelistWords) > 0 && !p.WhitelistOff {
		switch p.mode() {
		case WhitelistAll:
			// Not one word of the list is in the text: there is no need to
			// check each one to know the message fails.
			if globalMatcher.FirstMatchingWord(body, p.WhitelistWords, p.Normalize, p.CaseSensitive, p.WordBoundary) == "" {
				return DecisionDenied("whitelist_missing:" + strings.Join(p.WhitelistWords[:min(3, len(p.WhitelistWords))], ","))
			}
			missing := make([]string, 0, len(p.WhitelistWords))
			for _, w := range p.WhitelistWords {
				if !globalMatcher.ContainsWord(body, w, p.WordBoundary, p.Normalize, p.CaseSensitive) {
					missing = append(missing, w)
				}
			}
			if len(missing) > 0 {
				return DecisionDenied("whitelist_missing:" + strings.Join(missing[:min(3, len(missing))], ","))
			}
		case WhitelistPhrase:
			phrase := strings.Join(p.WhitelistWords, " ")
			if !globalMatcher.ContainsWord(body, phrase, p.WordBoundary, p.Normalize, p.CaseSensitive) {
				return DecisionDenied("whitelist_phrase_missing")
			}
		default: // WhitelistAny
			if globalMatcher.FirstMatchingWord(body, p.WhitelistWords, p.Normalize, p.CaseSensitive, p.WordBoundary) == "" {
				return DecisionDenied("whitelist_mismatch")
			}
		}
	}

	// v30: the per-list switches govern the regex lists that belong to the
	// same list, so parking a list parks its patterns too.
	if !p.BlacklistOff {
		for _, pattern := range p.RegexBlock {
			compiled, ok := CompileUserRegex(pattern)
			if !ok {
				// Active blocking regex is invalid or refused: fail closed,
				// exactly as the Python evaluator does — a broken guard must
				// never become an open gate.
				return DecisionDenied("invalid_regex_block:" + truncate(pattern, 32))
			}
			if boundedMatch(compiled, body) {
				return DecisionDenied("regex_block:"+truncate(pattern, 48), pattern)
			}
		}
	}

	if len(p.RegexRequire) > 0 && !p.WhitelistOff {
		compiled := make([]*regexp.Regexp, 0, len(p.RegexRequire))
		for _, pattern := range p.RegexRequire {
			c, ok := CompileUserRegex(pattern)
			if !ok {
				// Active requirement regex is invalid or refused: fail closed.
				return DecisionDenied("invalid_regex_require:" + truncate(pattern, 32))
			}
			compiled = append(compiled, c)
		}
		matched := false
		for _, c := range compiled {
			if boundedMatch(c, body) {
				matched = true
				break
			}
		}
		if !matched {
			return DecisionDenied("regex_mismatch")
		}
	}

	return DecisionAllowed("policy_passed")
}

// globalMatcher is the package-level Matcher every Evaluate call shares, so
// a keyword's pattern is compiled once per process rather than once per
// message. It is concurrency-safe; the only cost of sharing it is the cache
// it carries.
var globalMatcher = NewMatcher()

// --- the chain (entities.py policy_chain, :1380-1406) ---------------------------

// Chain is the additive-only list of policies that apply to one (source,
// target) pair: the task policy first, then an optional source override,
// then an optional target override (Python ForwardRule.policy_chain).
//
// An override is an additional constraint, never a relaxation: every policy
// in the chain must allow the message, and the first deny wins. A nil policy
// in the chain is skipped — an absent override constrains nothing.
type Chain struct {
	policies []*ContentPolicy
}

// NewChain builds a policy chain from the task policy and its overrides.
// Nil entries are allowed and skipped at evaluation time.
func NewChain(task *ContentPolicy, source, target *ContentPolicy) *Chain {
	return &Chain{policies: []*ContentPolicy{task, source, target}}
}

// Policies returns the chain's policies in evaluation order, skipping nils.
// The slice is a copy: a caller cannot mutate the chain through it.
func (c *Chain) Policies() []*ContentPolicy {
	if c == nil {
		return nil
	}
	out := make([]*ContentPolicy, 0, len(c.policies))
	for _, p := range c.policies {
		if p != nil {
			out = append(out, p)
		}
	}
	return out
}

// Evaluate runs every non-nil policy in the chain against the message.
//
// Additive-only: all must allow; the first deny wins, and its reason is the
// one an operator sees — the first policy to object is the one named in the
// log. A chain whose policies are all inactive allows everything, which is
// the semantics of "no rules", not a silent relaxation.
//
// The allow reason mirrors the Python chain check: the message passed every
// policy it has, so the reason is the chain-level verdict rather than the
// last sub-policy's.
func (c *Chain) Evaluate(text, media string) Decision {
	if c == nil {
		return DecisionAllowed("policy_inactive")
	}
	allowed := false
	for _, p := range c.policies {
		if p == nil {
			continue
		}
		d := p.Evaluate(text, media)
		if !d.Allowed {
			// Label the deny with the policy that produced it, so the source
			// of a drop is visible in one read instead of a policy-by-policy
			// replay. The Python reason string is preserved verbatim.
			return c.label(d, p)
		}
		allowed = true
	}
	if !allowed {
		return DecisionAllowed("policy_inactive")
	}
	return DecisionAllowed("policy_passed")
}

// label attaches the deciding policy's provenance to a deny. The reason
// string is untouched — a blacklist hit is still "blacklist:<word>", exactly
// the Python vocabulary — and the policy that objected is recorded in
// Matched, which a decision consumer may ignore.
//
// The policy passed in is the one that denied, not the chain's first member:
// a target override's drop must be attributed to the target override, not to
// the task policy that happened to be listed first.
func (c *Chain) label(d Decision, p *ContentPolicy) Decision {
	d.Matched = append([]string{}, d.Matched...)
	if len(d.Matched) == 0 {
		d.Matched = []string{""}
	}
	d.Matched[0] = d.Matched[0] + "	" + sourceLabel(p)
	return d
}

// sourceLabel normalizes a policy's provenance for the deny note: an empty
// Source is the task policy itself, so it reads as "task".
func sourceLabel(p *ContentPolicy) string {
	if p == nil {
		return "task"
	}
	if p.Source != "" {
		return p.Source
	}
	return "task"
}
