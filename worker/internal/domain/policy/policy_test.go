package policy

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// --- the Stage 4.0 fixture harness ---------------------------------------------
//
// The tests in this file load docs/behavior-audit/fixtures.json the same way
// worker/internal/audit/fixtures_test.go does: walk up from the test binary
// to the repo root, never a hard-coded absolute path (per the harness
// contract). The loader here is a small copy rather than an import of the
// audit package — importing a _test.go-only harness from another package's
// test would make the policy suite build-dependent on the audit suite, and
// the contract wants the fixture format to be test-only.

// fixturePath is the repo-relative location of the shared fixture set.
const fixturePath = "docs/behavior-audit/fixtures.json"

// fixtureRecord is one record of the shared fixture file, limited to the
// keys the policy tests read. Every ContentPolicy fixture carries its policy
// in input.policy and its expectation in expected_python.
type fixtureRecord struct {
	ID          string         `json:"id"`
	Engine      string         `json:"engine"`
	Group       string         `json:"group"`
	Description string         `json:"description"`
	Executable  string         `json:"executable"`
	Input       map[string]any `json:"input"`
	Notes       string         `json:"notes"`
	// ExpectedPython is the ContentPolicy.evaluate expectation: an
	// {allowed, reason} pair. ContentPolicy fixtures are executable=python:
	// no Go parity verdict is claimed by the file, so this suite produces
	// one — the port's reason for existing.
	ExpectedPython map[string]any `json:"expected_python"`
	// ExpectedFilterEngine / ExpectedLinkBlockReason are the LNK-GATE-01/02
	// records: they assert the FilterEngine's own gate and the policy's gate
	// disagree, which is the F-14 divergence the link classifier pins.
	ExpectedFilterEngine    string `json:"expected_filter_engine"`
	ExpectedLinkBlockReason string `json:"expected_link_block_reason"`
}

// fixtureFile is the top-level shape of docs/behavior-audit/fixtures.json.
type fixtureFile struct {
	SchemaVersion string          `json:"schema_version"`
	Fixtures      []fixtureRecord `json:"fixtures"`
}

// loadFixtures reads the shared fixture file by walking up from this test
// file to the repository root.
func loadFixtures(t *testing.T) []fixtureRecord {
	t.Helper()
	path := findFixtureFile(t)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture file %s: %v", path, err)
	}
	var file fixtureFile
	if err := json.Unmarshal(data, &file); err != nil {
		t.Fatalf("decode fixture file %s: %v", path, err)
	}
	if file.SchemaVersion == "" {
		t.Fatalf("fixture file %s: schema_version is missing", path)
	}
	if len(file.Fixtures) == 0 {
		t.Fatalf("fixture file %s: fixture list is empty", path)
	}
	return file.Fixtures
}

// findFixtureFile locates repoRoot/docs/behavior-audit/fixtures.json by
// walking up from the test file's working directory.
func findFixtureFile(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for i := 0; i < 16; i++ {
		if info, err := os.Stat(filepath.Join(dir, fixturePath)); err == nil && !info.IsDir() {
			return filepath.Join(dir, fixturePath)
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	t.Fatalf("fixture file %s not found walking up from %s", fixturePath, os.Getenv("PWD"))
	return ""
}

// byID indexes the fixture list by id. The shared file's loader already
// rejects duplicates.
func byID(records []fixtureRecord) map[string]*fixtureRecord {
	out := make(map[string]*fixtureRecord, len(records))
	for i := range records {
		out[records[i].ID] = &records[i]
	}
	return out
}

// --- ContentPolicy construction from a fixture ----------------------------------

// policyFromMap builds a ContentPolicy from a fixture's policy object the
// way ContentPolicy.from_dict does: every list key is read as a string list,
// every flag as a bool, whitelist_mode normalized to any|all|phrase.
//
// The mapping is deliberately permissive — a missing key is the zero value,
// never an error — so a fixture that exercises one gate does not have to
// spell the other nineteen.
func policyFromMap(t *testing.T, raw map[string]any) *ContentPolicy {
	t.Helper()
	p := &ContentPolicy{
		WordBoundary:  true,         // from_dict: absent means on
		Normalize:     true,         // from_dict: absent means on
		WhitelistMode: WhitelistAny, // from_dict: absent means "any"
	}
	if raw == nil {
		return p
	}
	if v, ok := raw["enabled"]; ok && v != nil {
		b := asBool(v)
		p.Enabled = &b
	}
	p.BlacklistOff = boolField(raw, "blacklist_off")
	p.WhitelistOff = boolField(raw, "whitelist_off")
	p.BlockLinks = boolField(raw, "block_links")
	p.RemoveLinks = boolField(raw, "remove_links")
	p.CaseSensitive = boolField(raw, "case_sensitive")
	if _, ok := raw["word_boundary"]; ok {
		p.WordBoundary = boolField(raw, "word_boundary")
	}
	if _, ok := raw["normalize"]; ok {
		p.Normalize = boolField(raw, "normalize")
	}
	p.BlacklistWords = strListField(raw, "blacklist_words")
	p.WhitelistWords = strListField(raw, "whitelist_words")
	p.StripWords = strListField(raw, "strip_words")
	p.RegexBlock = strListField(raw, "regex_block")
	p.RegexRequire = strListField(raw, "regex_require")
	p.AllowedMedia = strListField(raw, "allowed_media")
	p.BlockedMedia = strListField(raw, "blocked_media")
	if mode, ok := raw["whitelist_mode"]; ok {
		if m, ok := mode.(string); ok {
			switch strings.ToLower(strings.TrimSpace(m)) {
			case "all":
				p.WhitelistMode = WhitelistAll
			case "phrase":
				p.WhitelistMode = WhitelistPhrase
			default:
				p.WhitelistMode = WhitelistAny
			}
		}
	}
	p.MinLength = intField(raw, "min_length")
	p.MaxLength = intField(raw, "max_length")
	return p
}

// boolField reads one key as a bool, false when absent or null.
func boolField(raw map[string]any, key string) bool {
	if v, ok := raw[key]; ok && v != nil {
		return asBool(v)
	}
	return false
}

// asBool coerces a JSON scalar to a bool the way Python's _as_bool does:
// bools and the truthy strings "1/true/yes/on".
func asBool(v any) bool {
	switch b := v.(type) {
	case bool:
		return b
	case float64:
		return b != 0
	case string:
		switch strings.ToLower(strings.TrimSpace(b)) {
		case "1", "true", "yes", "on":
			return true
		}
	}
	return false
}

// strListField reads one key as a []string; a JSON null is an empty list.
func strListField(raw map[string]any, key string) []string {
	v, ok := raw[key]
	if !ok || v == nil {
		return nil
	}
	list, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(list))
	for _, item := range list {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// intField reads one key as an int; a junk value is 0 (Python _as_int's
// default).
func intField(raw map[string]any, key string) int {
	v, ok := raw[key]
	if !ok || v == nil {
		return 0
	}
	switch n := v.(type) {
	case float64:
		return int(n)
	case string:
		var i int
		_, err := fmt.Sscanf(strings.TrimSpace(n), "%d", &i)
		if err != nil {
			return 0
		}
		return i
	}
	return 0
}

// --- the POL-* fixtures: ContentPolicy.evaluate parity --------------------------

// TestContentPolicyFixtures runs every ContentPolicy.evaluate fixture in the
// shared file against the Go engine. The fixtures are executable=python —
// the file claims no Go parity verdict — so this suite is the port's claim:
// each one is pinned with its fixture id, and a failure reports both
// engines' answers side by side.
func TestContentPolicyFixtures(t *testing.T) {
	records := loadFixtures(t)
	n := 0
	for i := range records {
		fx := &records[i]
		if fx.Engine != "ContentPolicy.evaluate" {
			continue
		}
		n++
		t.Run(fx.ID, func(t *testing.T) {
			text, _ := fx.Input["text"].(string)
			policy := policyFromMap(t, objectField(t, fx.Input, "policy"))
			got := policy.Evaluate(text, "text")

			wantAllowed, _ := fx.ExpectedPython["allowed"].(bool)
			wantReason, _ := fx.ExpectedPython["reason"].(string)
			if got.Allowed != wantAllowed {
				t.Errorf("%s: allowed = %v, want %v (reason got %q, want %q)",
					fx.ID, got.Allowed, wantAllowed, got.Reason, wantReason)
			}
			if got.Reason != wantReason {
				t.Errorf("%s: reason = %q, want %q", fx.ID, got.Reason, wantReason)
			}
		})
	}
	if n == 0 {
		t.Fatal("no ContentPolicy.evaluate fixtures found — the shared file drifted")
	}
	t.Logf("ran %d ContentPolicy.evaluate fixtures", n)
}

// TestContentPolicyEmptyPolicyIsInactive pins POL-EMPTY-01: a policy with no
// rules is inactive and allows everything. An allow-list is not a deny-all.
func TestContentPolicyEmptyPolicyIsInactive(t *testing.T) {
	p := &ContentPolicy{WordBoundary: true, Normalize: true}
	if p.HasRules() {
		t.Errorf("an empty policy must report HasRules() == false")
	}
	if p.IsActive() {
		t.Errorf("an empty policy must report IsActive() == false (no rules to run)")
	}
	d := p.Evaluate("anything", "text")
	if !d.Allowed || d.Reason != "policy_inactive" {
		t.Errorf("empty policy: got {%v %q}, want {true \"policy_inactive\"}", d.Allowed, d.Reason)
	}
}

// TestContentPolicyParkedListIsSkipped pins FLT-DISABLED-01: blacklist_off
// skips the list without deleting it. FilterRule has no such switch — the
// fixture pins that boundary, and the Go port must keep it.
func TestContentPolicyParkedListIsSkipped(t *testing.T) {
	p := &ContentPolicy{
		BlacklistWords: []string{"spam"},
		BlacklistOff:   true,
		WordBoundary:   true,
		Normalize:      true,
	}
	d := p.Evaluate("this mentions spam freely", "text")
	if !d.Allowed || d.Reason != "policy_passed" {
		t.Errorf("blacklist_off: got {%v %q}, want {true \"policy_passed\"}", d.Allowed, d.Reason)
	}
	// The list itself is untouched: re-enabling restores the constraint.
	p.BlacklistOff = false
	d = p.Evaluate("this mentions spam freely", "text")
	if d.Allowed || d.Reason != "blacklist:spam" {
		t.Errorf("blacklist re-enabled: got {%v %q}, want {false \"blacklist:spam\"}", d.Allowed, d.Reason)
	}
}

// --- the enabled tri-state -------------------------------------------------------

func TestContentPolicyEnabledTriState(t *testing.T) {
	// nil (absent) = auto: active iff it has rules.
	auto := &ContentPolicy{BlacklistWords: []string{"spam"}, WordBoundary: true, Normalize: true}
	if !auto.IsActive() {
		t.Errorf("enabled=nil with rules must be active")
	}
	autoEmpty := &ContentPolicy{WordBoundary: true, Normalize: true}
	if autoEmpty.IsActive() {
		t.Errorf("enabled=nil without rules must be inactive")
	}

	tr := true
	on := &ContentPolicy{Enabled: &tr, WordBoundary: true, Normalize: true}
	if on.IsActive() {
		t.Errorf("enabled=true without rules is still inert (has_rules is false)")
	}
	onWith := &ContentPolicy{Enabled: &tr, BlacklistWords: []string{"spam"}, WordBoundary: true, Normalize: true}
	if !onWith.IsActive() {
		t.Errorf("enabled=true with rules must be active")
	}

	fl := false
	off := &ContentPolicy{Enabled: &fl, BlacklistWords: []string{"spam"}, WordBoundary: true, Normalize: true}
	if off.IsActive() {
		t.Errorf("enabled=false must be off whatever the rules say")
	}
	d := off.Evaluate("spam", "text")
	if !d.Allowed || d.Reason != "policy_inactive" {
		t.Errorf("enabled=false: got {%v %q}, want {true \"policy_inactive\"}", d.Allowed, d.Reason)
	}
}

// --- the check order ---------------------------------------------------------------

// TestContentPolicyCheckOrder pins the evaluation order (content_policy.py:823-903):
// media gates before lengths, lengths before links, links before words,
// words before regexes. Each subtest gives two rules that would both deny
// and asserts the one earlier in the order names itself in the reason.
func TestContentPolicyCheckOrder(t *testing.T) {
	newPolicy := func(modify func(*ContentPolicy)) *ContentPolicy {
		p := &ContentPolicy{WordBoundary: true, Normalize: true}
		if modify != nil {
			modify(p)
		}
		return p
	}

	cases := []struct {
		name   string
		modify func(*ContentPolicy)
		text   string
		media  string
		want   string
	}{
		{
			name:   "blocked_media beats min_length",
			modify: func(p *ContentPolicy) { p.BlockedMedia = []string{"photo"}; p.MinLength = 5 },
			text:   "short", media: "photo", want: "media_blocked:photo",
		},
		{
			name:   "allowed_media beats min_length",
			modify: func(p *ContentPolicy) { p.AllowedMedia = []string{"text"}; p.MinLength = 5 },
			text:   "short", media: "video", want: "media_not_allowed:video",
		},
		{
			// Length counts code points, not bytes: a Persian message is not
			// "longer" in Go than in Python (the UNI-LEN-01 claim).
			// 4 code points / 12 UTF-8 bytes: a byte count would read 12
			// and pass min_length=5; the codepoint count reads 4 and drops.
			name:   "min_length counts code points not bytes",
			modify: func(p *ContentPolicy) { p.MinLength = 5 },
			text:   "کتاب", media: "text", want: "too_short:4",
		},
		{
			name:   "min_length passes at exactly the codepoint count",
			modify: func(p *ContentPolicy) { p.MinLength = 4 },
			text:   "کتاب", media: "text", want: "policy_passed",
		},
		{
			name:   "max_length uses code points",
			modify: func(p *ContentPolicy) { p.MaxLength = 3 },
			text:   "کتاب", media: "text", want: "too_long:4",
		},
		{
			name:   "min_length beats block_links",
			modify: func(p *ContentPolicy) { p.MinLength = 100; p.BlockLinks = true },
			text:   "https://a.io", media: "text", want: "too_short:12",
		},
		{
			name:   "block_links beats blacklist",
			modify: func(p *ContentPolicy) { p.BlockLinks = true; p.BlacklistWords = []string{"a"} },
			text:   "https://a.io", media: "text", want: "links_blocked",
		},
		{
			name:   "blacklist beats regex_block",
			modify: func(p *ContentPolicy) { p.BlacklistWords = []string{"spam"}; p.RegexBlock = []string{"\\.io$"} },
			text:   "spam at a.io", media: "text", want: "blacklist:spam",
		},
		{
			name:   "blacklist beats whitelist",
			modify: func(p *ContentPolicy) { p.BlacklistWords = []string{"spam"}; p.WhitelistWords = []string{"oil"} },
			text:   "spam and oil", media: "text", want: "blacklist:spam",
		},
		{
			// The whitelist contains "oil" and so does the text, so the
			// whitelist passes and regex_block decides — verified against
			// the Python evaluator.
			name:   "a passing whitelist still lets regex_block decide",
			modify: func(p *ContentPolicy) { p.WhitelistWords = []string{"oil"}; p.RegexBlock = []string{"oil"} },
			text:   "oil", media: "text", want: "regex_block:oil",
		},
		{
			name:   "regex_block beats regex_require",
			modify: func(p *ContentPolicy) { p.RegexBlock = []string{"oil"}; p.RegexRequire = []string{"oil"} },
			text:   "oil", media: "text", want: "regex_block:oil",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := newPolicy(tc.modify).Evaluate(tc.text, tc.media)
			if got.Reason != tc.want {
				t.Errorf("reason = %q, want %q", got.Reason, tc.want)
			}
		})
	}
}

// --- length bounds count code points ---------------------------------------------

// TestContentPolicyLengthIsCodePoints is the UNI-LEN-01 claim applied to the
// policy gates: len(body) in Python is codepoints, so the Go port must use
// RuneCountInString — a byte count would drop a Persian message at a
// different length than its Python twin.
func TestContentPolicyLengthIsCodePoints(t *testing.T) {
	persian := "تخفیف ویژه" // 10 code points, 19 UTF-8 bytes
	p := &ContentPolicy{MaxLength: 20, WordBoundary: true, Normalize: true}
	d := p.Evaluate(persian, "text")
	if !d.Allowed {
		t.Errorf("a 10-codepoint text must pass MaxLength=20 (a 19-byte count would too, but for the wrong reason): got %v %q", d.Allowed, d.Reason)
	}
	p2 := &ContentPolicy{MinLength: 20, WordBoundary: true, Normalize: true}
	d = p2.Evaluate(persian, "text")
	if d.Reason != "too_short:10" {
		t.Errorf("too_short must report code points: got %q, want %q", d.Reason, "too_short:10")
	}
}

// --- whitelist modes ----------------------------------------------------------------

func TestContentPolicyWhitelistModes(t *testing.T) {
	cases := []struct {
		name  string
		mode  WhitelistMode
		words []string
		text  string
		want  string
		allow bool
	}{
		// Python: any-mode needs at least one listed word; "gold update"
		// carries "gold", so it passes. Mismatch is when NONE matches.
		{"any matches one", WhitelistAny, []string{"gold", "oil"}, "gold update", "policy_passed", true},
		{"any with none present", WhitelistAny, []string{"gold", "oil"}, "silver", "whitelist_mismatch", false},
		{"any needs one", WhitelistAny, []string{"gold", "oil"}, "oil", "policy_passed", true},
		// POL-ALL-01
		{"all: every word must be present", WhitelistAll, []string{"gold", "oil"}, "gold and silver",
			"whitelist_missing:oil", false},
		{"all: all present", WhitelistAll, []string{"gold", "oil"}, "gold and oil", "policy_passed", true},
		{"all: none present names the whole list", WhitelistAll, []string{"gold", "oil"}, "silver",
			"whitelist_missing:gold,oil", false},
		// Python reports the missing entries' first three; "a" matched, so
		// the missing list is b,c,d — not the head of the whole list.
		{"all: missing list capped at 3", WhitelistAll, []string{"a", "b", "c", "d"}, "a",
			"whitelist_missing:b,c,d", false},
		// POL-PHRASE-01
		{"phrase matches across whitespace", WhitelistPhrase, []string{"breaking news"},
			"breaking   news\n\nhere", "policy_passed", true},
		{"phrase missing", WhitelistPhrase, []string{"breaking news"}, "breaking", "whitelist_phrase_missing", false},
		{"phrase words join with a single space", WhitelistPhrase, []string{"breaking", "news"},
			"breaking news", "policy_passed", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := &ContentPolicy{
				WhitelistWords: tc.words,
				WhitelistMode:  tc.mode,
				WordBoundary:   true,
				Normalize:      true,
			}
			d := p.Evaluate(tc.text, "text")
			if d.Allowed != tc.allow || d.Reason != tc.want {
				t.Errorf("got {%v %q}, want {%v %q}", d.Allowed, d.Reason, tc.allow, tc.want)
			}
		})
	}
}

// --- invalid regex fails closed -----------------------------------------------------

// TestContentPolicyInvalidRegexFailsClosed pins content_policy.py:1042-1070:
// an active regex that cannot be compiled or that carries a catastrophic-
// backtracking shape drops the message. A broken guard must never become an
// open gate — on both the block side and the require side.
func TestContentPolicyInvalidRegexFailsClosed(t *testing.T) {
	cases := []struct {
		name    string
		block   []string
		require []string
		want    string
	}{
		{"invalid regex_block", []string{"(["}, nil, "invalid_regex_block:(["},
		{"invalid regex_require", nil, []string{"(["}, "invalid_regex_require:(["},
		// The ReDoS shapes the guard refuses (filter.go:56-57 shares them):
		// a quantifier nested in a quantified group, and an alternation
		// under a quantifier.
		{"nested quantifier is refused", []string{"(a+)+"}, nil, "invalid_regex_block:(a+)+"},
		{"ambiguous alternation is refused", []string{"(a|a)*"}, nil, "invalid_regex_block:(a|a)*"},
		{"nested quantifier on require side", nil, []string{"(a+)+"}, "invalid_regex_require:(a+)+"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := &ContentPolicy{
				RegexBlock:   tc.block,
				RegexRequire: tc.require,
				WordBoundary: true,
				Normalize:    true,
			}
			d := p.Evaluate("some text", "text")
			if d.Allowed {
				t.Errorf("invalid regex must fail closed: got allowed with %q", d.Reason)
			}
			if d.Reason != tc.want {
				t.Errorf("reason = %q, want %q", d.Reason, tc.want)
			}
		})
	}
}

// TestContentPolicyRegexTruncatesReasonStrings pins the Python slicing:
// invalid_regex_block carries pattern[:32], regex_block carries pattern[:48].
func TestContentPolicyRegexTruncatesReasonStrings(t *testing.T) {
	long := strings.Repeat("a", 64) + "(" // invalid: open group, and over 32 chars
	p := &ContentPolicy{RegexBlock: []string{long}, WordBoundary: true, Normalize: true}
	d := p.Evaluate("text", "text")
	if !strings.HasPrefix(d.Reason, "invalid_regex_block:") {
		t.Errorf("reason = %q, want an invalid_regex_block:* reason", d.Reason)
	}
	if len(d.Reason) > len("invalid_regex_block:")+32 {
		t.Errorf("invalid_regex_block must carry at most 32 pattern chars: %q is %d long",
			d.Reason, len(d.Reason))
	}
}

// TestContentPolicyHostileRegexIsRefusedNotCompiled asserts the *guard*
// refuses the shape even when the text would not match — the refusal is on
// the pattern, not on the message.
func TestContentPolicyHostileRegexIsRefusedNotCompiled(t *testing.T) {
	if _, ok := CompileUserRegex("(a+)+"); ok {
		t.Errorf("(a+)+ must be refused by the guard")
	}
	if _, ok := CompileUserRegex("(a|a)*b"); ok {
		t.Errorf("(a|a)*b must be refused by the guard")
	}
	if _, ok := CompileUserRegex(strings.Repeat("a", 1001)); ok {
		t.Errorf("a 1001-char pattern must be refused by the guard")
	}
	// The benign cases compile.
	for _, pat := range []string{`\bcat\b`, `https?://`, `^foo$`, `(?i)bar`} {
		if _, ok := CompileUserRegex(pat); !ok {
			t.Errorf("%q must compile", pat)
		}
	}
}

// TestContentPolicyRegexIsBounded pins the maxMatchLen ceiling: a user
// pattern is only matched against the first 8 KiB of the body, so an
// uncatalogued quadratic cannot blow up on a long message.
func TestContentPolicyRegexIsBounded(t *testing.T) {
	// A pattern that matches only in the tail beyond the cap is not found.
	p := &ContentPolicy{RegexRequire: []string{"tail-marker"}, WordBoundary: true, Normalize: true}
	body := strings.Repeat("x", maxMatchLen) + "tail-marker"
	d := p.Evaluate(body, "text")
	if d.Allowed {
		t.Errorf("a match beyond the 8 KiB cap must not be found: got %v %q", d.Allowed, d.Reason)
	}
	if d.Reason != "regex_mismatch" {
		t.Errorf("reason = %q, want %q", d.Reason, "regex_mismatch")
	}
	// The same marker inside the cap is found.
	d = p.Evaluate(strings.Repeat("x", 100)+"tail-marker", "text")
	if !d.Allowed {
		t.Errorf("a match inside the cap must be found: got %v %q", d.Allowed, d.Reason)
	}
}

// --- block_links uses the canonical has_link ---------------------------------------

// TestContentPolicyBlockLinks pins the fixtures' link gates: LNK-GATE-01 and
// LNK-GATE-02, the F-14 divergence between the FilterEngine's URL_REGEX and
// the policy's has_link. The Go policy gate must agree with the PYTHON
// policy gate, not with the Go filter's ad-hoc regex.
func TestContentPolicyBlockLinks(t *testing.T) {
	cases := []struct {
		text string
		want string
	}{
		// LNK-GATE-01: tg:// is a link to has_link, so the policy drops.
		{"see tg://resolve?domain=x", "links_blocked"},
		{"ftp://e.com/f.zip", "links_blocked"},
		// LNK-GATE-02: a bare mention is not a link, so the policy passes.
		{"email @user hi", "policy_passed"},
		{"@scam_bot says hi", "policy_passed"},
	}
	for _, tc := range cases {
		p := &ContentPolicy{BlockLinks: true, WordBoundary: true, Normalize: true}
		d := p.Evaluate(tc.text, "text")
		if d.Reason != tc.want {
			t.Errorf("block_links on %q: reason = %q, want %q", tc.text, d.Reason, tc.want)
		}
	}
}

// TestContentPolicyBlockLinksAlsoDropsMentionFree cases the gate against the
// differential Python predicate, so an edge the fixtures do not carry is
// still pinned.
func TestContentPolicyBlockLinksDifferential(t *testing.T) {
	if testing.Short() {
		t.Skip("python differential skipped in -short mode")
	}
	texts := []string{
		"plain", "https://a.io", "tg://x", "t.me/x", "www.a.io", "@mention",
		"mailto:x@y.z", "file:///etc/passwd", "//a.io",
	}
	for _, text := range texts {
		p := &ContentPolicy{BlockLinks: true, WordBoundary: true, Normalize: true}
		got := p.Evaluate(text, "text")
		expr := "from core.domain.content_policy import has_link; print(has_link(" + quoteGo(text) + "))"
		want := strings.Contains(strings.TrimSpace(pyRun(t, expr)), "True")
		if got.Allowed == want {
			t.Errorf("block_links on %q: Go allowed=%v but Python has_link=%v — the gate disagrees with the reference",
				text, got.Allowed, want)
		}
	}
}

// --- the chain ------------------------------------------------------------------------

// TestChainIsAdditiveOnly pins POL-SOURCE-01: a source override can only add
// constraints, never relax the task policy. Both must allow; first deny wins.
func TestChainIsAdditiveOnly(t *testing.T) {
	task := &ContentPolicy{BlacklistWords: []string{"silver"}, WordBoundary: true, Normalize: true}
	source := &ContentPolicy{
		WhitelistWords: []string{"oil"},
		WordBoundary:   true,
		Normalize:      true,
		Source:         "source:-100333",
	}
	d := NewChain(task, source, nil).Evaluate("gold", "text")
	if d.Allowed || d.Reason != "whitelist_mismatch" {
		t.Errorf("chain: got {%v %q}, want {false \"whitelist_mismatch\"}", d.Allowed, d.Reason)
	}
}

// TestChainTaskPolicyStillDecidesOnItsOwn shows the override does not replace
// the task policy: when the override allows, the task policy still applies.
func TestChainTaskPolicyStillDecidesOnItsOwn(t *testing.T) {
	task := &ContentPolicy{BlacklistWords: []string{"gold"}, WordBoundary: true, Normalize: true}
	source := &ContentPolicy{WhitelistWords: []string{"gold"}, WordBoundary: true, Normalize: true}
	d := NewChain(task, source, nil).Evaluate("gold", "text")
	if d.Allowed {
		t.Errorf("the task policy must still deny when the override allows: got %v %q", d.Allowed, d.Reason)
	}
	// The deny names the task's reason, not the override's.
	if d.Reason != "blacklist:gold" {
		t.Errorf("reason = %q, want %q", d.Reason, "blacklist:gold")
	}
}

// TestChainTargetOverride is POL-TARGET-01's decision half: a target
// override rejects only its own copy of the message, on the same text.
func TestChainTargetOverride(t *testing.T) {
	task := &ContentPolicy{BlacklistWords: []string{"silver"}, WordBoundary: true, Normalize: true}
	target := &ContentPolicy{
		BlacklistWords: []string{"gold"},
		WordBoundary:   true,
		Normalize:      true,
		Source:         "target:-100222",
	}
	text := "gold update"
	if d := NewChain(task, nil, nil).Evaluate(text, "text"); !d.Allowed {
		t.Errorf("task-only chain must allow %q: got %v %q", text, d.Allowed, d.Reason)
	}
	if d := NewChain(task, nil, target).Evaluate(text, "text"); d.Allowed {
		t.Errorf("target override must deny %q: got %v %q", text, d.Allowed, d.Reason)
	}
}

// TestChainNilPoliciesAreSkipped: a nil entry in the chain constrains
// nothing (entities.py policy_chain skips an absent override).
func TestChainNilPoliciesAreSkipped(t *testing.T) {
	empty := NewChain(nil, nil, nil)
	d := empty.Evaluate("anything", "text")
	if !d.Allowed {
		t.Errorf("an all-nil chain must allow: got %v %q", d.Allowed, d.Reason)
	}
	task := &ContentPolicy{BlacklistWords: []string{"spam"}, WordBoundary: true, Normalize: true}
	d = NewChain(task, nil, nil).Evaluate("spam", "text")
	if d.Allowed || d.Reason != "blacklist:spam" {
		t.Errorf("nil overrides must not disable the task policy: got %v %q", d.Allowed, d.Reason)
	}
}

// TestChainFirstDenyWins: with two policies that would both deny, the first
// one in the chain names the reason.
func TestChainFirstDenyWins(t *testing.T) {
	task := &ContentPolicy{
		BlacklistWords: []string{"gold"},
		WordBoundary:   true,
		Normalize:      true,
		Source:         "task",
	}
	source := &ContentPolicy{
		WhitelistWords: []string{"oil"},
		WordBoundary:   true,
		Normalize:      true,
		Source:         "source:-100333",
	}
	d := NewChain(task, source, nil).Evaluate("gold", "text")
	if d.Allowed || d.Reason != "blacklist:gold" {
		t.Errorf("first deny must win: got %v %q, want {false \"blacklist:gold\"}", d.Allowed, d.Reason)
	}
}

// TestChainDenyCarriesProvenance: the deny records which policy objected, so
// an operator reads the source of a drop in one line — without the reason
// string itself being rewritten.
func TestChainDenyCarriesProvenance(t *testing.T) {
	task := &ContentPolicy{BlacklistWords: []string{"silver"}, WordBoundary: true, Normalize: true}
	target := &ContentPolicy{
		BlacklistWords: []string{"gold"},
		WordBoundary:   true,
		Normalize:      true,
		Source:         "target:-100222",
	}
	d := NewChain(task, nil, target).Evaluate("gold", "text")
	if d.Allowed {
		t.Fatal("expected a deny")
	}
	if d.Reason != "blacklist:gold" {
		t.Errorf("the Python reason must be preserved: got %q", d.Reason)
	}
	if len(d.Matched) == 0 || !strings.Contains(d.Matched[0], "target:-100222") {
		t.Errorf("the deny must record the objecting policy: got %v", d.Matched)
	}
}

// TestChainPolicies exposes the chain's order for a caller that wants to
// report each step.
func TestChainPolicies(t *testing.T) {
	task := &ContentPolicy{Source: "task"}
	source := &ContentPolicy{Source: "source:-100333"}
	got := NewChain(task, source, nil).Policies()
	if len(got) != 2 || got[0] != task || got[1] != source {
		t.Errorf("Policies() = %v, want [task source]", got)
	}
	// A caller cannot mutate the chain through the returned slice.
	got[0] = nil
	if NewChain(task, source, nil).Policies()[0] == nil {
		t.Errorf("Policies() must return a copy, not a view onto the chain")
	}
}

// --- the fixture-driven chain test ---------------------------------------------------

// TestChainSourceScopeFixture runs POL-SOURCE-01 end to end: one task policy,
// one source override, and the additive-only verdict the Python pipeline
// produces (pipeline.py:974-1016).
func TestChainSourceScopeFixture(t *testing.T) {
	records := loadFixtures(t)
	fx := byID(records)["POL-SOURCE-01"]
	if fx == nil {
		t.Fatal("POL-SOURCE-01 not found in the shared fixture file")
	}
	text, _ := fx.Input["text"].(string)
	taskRaw, _ := fx.Input["task_policy"].(map[string]any)
	sourceMap, _ := fx.Input["source_policy"].(map[string]any)
	sourceRaw := objectAt(t, sourceMap, "-100333")

	d := NewChain(policyFromMap(t, taskRaw), policyFromMap(t, sourceRaw).WithSource("source:-100333"), nil).
		Evaluate(text, "text")

	wantAllowed, _ := fx.ExpectedPython["allowed"].(bool)
	wantReason, _ := fx.ExpectedPython["reason"].(string)
	if d.Allowed != wantAllowed || d.Reason != wantReason {
		t.Errorf("POL-SOURCE-01: got {%v %q}, want {%v %q}", d.Allowed, d.Reason, wantAllowed, wantReason)
	}
}

// --- media gates ----------------------------------------------------------------------

func TestContentPolicyMediaGates(t *testing.T) {
	cases := []struct {
		name    string
		blocked []string
		allowed []string
		media   string
		want    string
	}{
		{"blocked media drops", []string{"photo"}, nil, "photo", "media_blocked:photo"},
		{"blocked media compares case-insensitively", []string{"PHOTO"}, nil, "photo", "media_blocked:photo"},
		{"allowed media passes", nil, []string{"text"}, "text", "policy_passed"},
		{"allowed media drops the rest", nil, []string{"text"}, "video", "media_not_allowed:video"},
		{"empty media defaults to text", nil, []string{"text"}, "", "policy_passed"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := &ContentPolicy{
				BlockedMedia: tc.blocked,
				AllowedMedia: tc.allowed,
				WordBoundary: true,
				Normalize:    true,
			}
			d := p.Evaluate("text", tc.media)
			if d.Reason != tc.want {
				t.Errorf("reason = %q, want %q", d.Reason, tc.want)
			}
		})
	}
}

// --- helpers ---------------------------------------------------------------------------

func objectField(t *testing.T, input map[string]any, key string) map[string]any {
	t.Helper()
	if v, ok := input[key]; ok && v != nil {
		if m, ok := v.(map[string]any); ok {
			return m
		}
	}
	return nil
}

func objectAt(t *testing.T, m map[string]any, key string) map[string]any {
	t.Helper()
	if m == nil {
		return nil
	}
	if v, ok := m[key]; ok && v != nil {
		if mm, ok := v.(map[string]any); ok {
			return mm
		}
	}
	return nil
}

// fixtureIDs lists the ids of every fixture with the given engine, for the
// tests that assert on which records exist.
func fixtureIDs(records []fixtureRecord, engine string) []string {
	out := []string{}
	for i := range records {
		if records[i].Engine == engine {
			out = append(out, records[i].ID)
		}
	}
	return out
}

// TestPolicyFixturesCoverTheExpectedSet asserts the decision fixtures this
// suite runs are exactly the ones the shared file declares, so a fixture
// added to the file cannot silently run uncovered (or be silently dropped).
func TestPolicyFixturesCoverTheExpectedSet(t *testing.T) {
	records := loadFixtures(t)
	got := fixtureIDs(records, "ContentPolicy.evaluate")
	want := []string{"POL-PHRASE-01", "POL-ALL-01", "POL-EMPTY-01"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("ContentPolicy.evaluate fixtures = %v, want %v", got, want)
	}
}

// TestPolicySourceTargetFixturesExist pins the chain fixtures the spec asks
// for; losing either from the file invalidates the register.
func TestPolicySourceTargetFixturesExist(t *testing.T) {
	records := loadFixtures(t)
	by := byID(records)
	for _, id := range []string{"POL-SOURCE-01", "POL-TARGET-01", "LNK-GATE-01", "LNK-GATE-02"} {
		if by[id] == nil {
			t.Errorf("fixture %s is missing from the shared file", id)
		}
	}
}

// TestPolicyFromFixtureMapIsPermissive: the builder never fails on a partial
// policy object — a fixture exercising one gate does not spell the rest.
func TestPolicyFromFixtureMapIsPermissive(t *testing.T) {
	p := policyFromMap(t, map[string]any{"blacklist_words": []any{"spam"}})
	if len(p.BlacklistWords) != 1 || p.BlacklistWords[0] != "spam" {
		t.Errorf("blacklist_words: %v", p.BlacklistWords)
	}
	if !p.WordBoundary || !p.Normalize {
		t.Errorf("absent word_boundary/normalize default to true, got wb=%v norm=%v", p.WordBoundary, p.Normalize)
	}
	if p.WhitelistMode != WhitelistAny {
		t.Errorf("absent whitelist_mode must default to any, got %q", p.WhitelistMode)
	}
	if p.HasRules() == false {
		t.Errorf("a policy with a blacklist has rules")
	}
	// An unknown whitelist_mode falls back to any, not to an error.
	p = policyFromMap(t, map[string]any{"whitelist_mode": "bogus"})
	if p.WhitelistMode != WhitelistAny {
		t.Errorf("unknown whitelist_mode must fall back to any, got %q", p.WhitelistMode)
	}
}

// TestContentPolicyEvaluateMediaNilAndEmpty covers the media normalization:
// the Python default is "text", and an empty Go media string is normalized
// the same way.
func TestContentPolicyEvaluateMediaNilAndEmpty(t *testing.T) {
	p := &ContentPolicy{AllowedMedia: []string{"text"}, WordBoundary: true, Normalize: true}
	for _, media := range []string{"", "text", "TEXT"} {
		if d := p.Evaluate("hi", media); !d.Allowed {
			t.Errorf("media %q must normalize to text and pass: got %v %q", media, d.Allowed, d.Reason)
		}
	}
	if d := p.Evaluate("hi", "photo"); d.Allowed {
		t.Errorf("photo must be rejected: got %v %q", d.Allowed, d.Reason)
	}
}

// TestContentPolicyMatcherRespectsPolicyFlags wires the policy's own
// case_sensitive / word_boundary / normalize flags into the matcher, so a
// fixture that turns one off is evaluated with it off.
func TestContentPolicyMatcherRespectsPolicyFlags(t *testing.T) {
	// normalize=false keeps the yeh variants distinct, so an Arabic-yeh
	// blacklist word does not catch Persian-yeh text.
	p := &ContentPolicy{
		BlacklistWords: []string{"تخفيف"},
		Normalize:      false,
		WordBoundary:   true,
	}
	d := p.Evaluate("تخفیف ویژه", "text")
	if !d.Allowed {
		t.Errorf("normalize=false must keep the yeh variants distinct: got %v %q", d.Allowed, d.Reason)
	}
	p.Normalize = true
	d = p.Evaluate("تخفیف ویژه", "text")
	if d.Allowed || d.Reason != "blacklist:تخفيف" {
		t.Errorf("normalize=true must fold the yeh variants: got %v %q", d.Allowed, d.Reason)
	}
	// word_boundary=false turns a word match into a substring match.
	p = &ContentPolicy{BlacklistWords: []string{"cat"}, WordBoundary: false, Normalize: true}
	d = p.Evaluate("concatenate", "text")
	if d.Allowed || d.Reason != "blacklist:cat" {
		t.Errorf("word_boundary=false must match substrings: got %v %q", d.Allowed, d.Reason)
	}
}

// TestContentPolicyBlacklistReportsFirstListedWord pins the order guarantee
// (content_policy.py:266-269): a rejection names the first offender the user
// typed, not an arbitrary one.
func TestContentPolicyBlacklistReportsFirstListedWord(t *testing.T) {
	p := &ContentPolicy{
		BlacklistWords: []string{"scam", "spam"},
		WordBoundary:   true,
		Normalize:      true,
	}
	d := p.Evaluate("both spam and scam appear", "text")
	if d.Reason != "blacklist:scam" {
		t.Errorf("reason = %q, want %q (the first listed offender)", d.Reason, "blacklist:scam")
	}
	if len(d.Matched) != 1 || d.Matched[0] != "scam" {
		t.Errorf("Matched = %v, want [scam]", d.Matched)
	}
}

// TestContentPolicyBlacklistSkipsBlankEntries: a blank entry in a list is
// skipped, not treated as a match-everything wildcard.
func TestContentPolicyBlacklistSkipsBlankEntries(t *testing.T) {
	p := &ContentPolicy{
		BlacklistWords: []string{"", "  ", "spam"},
		WordBoundary:   true,
		Normalize:      true,
	}
	d := p.Evaluate("clean text", "text")
	if !d.Allowed {
		t.Errorf("blank entries must not match: got %v %q", d.Allowed, d.Reason)
	}
	d = p.Evaluate("spam", "text")
	if d.Allowed || d.Reason != "blacklist:spam" {
		t.Errorf("the real entry must still match: got %v %q", d.Allowed, d.Reason)
	}
}

// TestContentPolicyAllowedMediaWhenBothSet: with both lists set, the blocked
// list wins (it is checked first) — the same precedence Python's evaluator
// applies.
func TestContentPolicyAllowedMediaWhenBothSet(t *testing.T) {
	p := &ContentPolicy{
		BlockedMedia: []string{"photo"},
		AllowedMedia: []string{"photo", "video"},
		WordBoundary: true,
		Normalize:    true,
	}
	if d := p.Evaluate("hi", "photo"); d.Reason != "media_blocked:photo" {
		t.Errorf("blocked wins over allowed: got %q", d.Reason)
	}
	if d := p.Evaluate("hi", "video"); !d.Allowed {
		t.Errorf("video is allowed and not blocked: got %v %q", d.Allowed, d.Reason)
	}
}
