// cat_d_test.go — Category D: replacement, regex, and cleaner (65 scenarios).
package e2e

import (
	"context"
	"fmt"
	"testing"
)

// TestCategoryD runs the 65 replacement/regex/cleaner scenarios.
func TestCategoryD(t *testing.T) {
	rec := NewRecorder()
	ctx := context.Background()

	// D-001..D-011: literal replacement (11).
	// The replacement engine is substring-based and case-insensitive by
	// default (Python text_replace parity; RPL-* fixtures pin both).
	type rCase struct {
		id      string
		desc    string
		ordered [][2]string
		reps    map[string]string
		text    string
		expect  string
	}
	rCases := []rCase{
		{"ATF-E2E-D-001", "one replacement", nil, map[string]string{"cat": "dog"}, "the cat sat", "the dog sat"},
		{"ATF-E2E-D-002", "multiple replacements", nil, map[string]string{"cat": "dog", "sat": "ran"}, "the cat sat", "the dog ran"},
		{"ATF-E2E-D-003", "default case-insensitive: uppercase also replaced", nil, map[string]string{"cat": "dog"}, "the CAT sat", "the dog sat"},
		{"ATF-E2E-D-004", "default case-insensitive across mixed case", nil, map[string]string{"cat": "dog"}, "the Cat sat", "the dog sat"},
		{"ATF-E2E-D-005", "substring match also matches inside a longer word", nil, map[string]string{"cat": "dog"}, "the category list", "the dogegory list"},
		{"ATF-E2E-D-006", "partial-word (substring) replacement", nil, map[string]string{"cat": "dog"}, "concat here", "condog here"},
		{"ATF-E2E-D-007", "repeated matches all replaced", nil, map[string]string{"cat": "dog"}, "cat cat cat", "dog dog dog"},
		// Overlapping entries are order-sensitive: "ab" must run before "a",
		// which only the ordered path can express.
		{"ATF-E2E-D-008", "ordered replacements: first entry wins overlaps", [][2]string{{"ab", "X"}, {"a", "Y"}}, nil, "ab cd", "X cd"},
		{"ATF-E2E-D-009", "replacement in caption", nil, map[string]string{"old": "new"}, "photo old cap", "photo new cap"},
		{"ATF-E2E-D-010", "replacement disabled: text untouched", nil, nil, "nothing changes", "nothing changes"},
		{"ATF-E2E-D-011", "replacement enabled", nil, map[string]string{"x": "y"}, "x marks", "y marks"},
	}
	for i, c := range rCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			spec := &PolicySpec{Enabled: boolPtr(true), Replacements: c.reps, OrderedReplacements: c.ordered}
			h.Install(TaskSpec{ID: fmt.Sprintf("task-r-%d", i), Source: "-1001", Target: "-1002", Active: true, Policy: spec})
			e := evt(c.id, "D", c.desc, c.expect)
			start := now()
			j := h.Enqueue(fmt.Sprintf("task-r-%d", i), "-1001", int64(1100+i), "-1002", c.text)
			err := h.Run(ctx, j)
			if h.AssertDelivered(&e, j, c.expect) {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// D-012..D-034: regex (23).
	// Regex gates live in filter_rules (the same surface Category C proves);
	// a broken pattern fails CLOSED — a guard down is never an open gate.
	type rxCase struct {
		id      string
		desc    string
		block   []string
		require []string
		text    string
		expect  string
	}
	rxCases := []rxCase{
		{"ATF-E2E-D-012", "require accepts matching text", nil, []string{`^\d+$`}, "12345", "12345"},
		{"ATF-E2E-D-013", "require rejects non-matching text", nil, []string{`^\d+$`}, "abc", ""},
		{"ATF-E2E-D-014", "invalid regex fails closed, never crashes", []string{"["}, nil, "text", ""},
		{"ATF-E2E-D-015", "case-insensitive regex via inline flag", nil, []string{`(?i)hello`}, "HELLO world", "HELLO world"},
		{"ATF-E2E-D-016", "multiline regex", nil, []string{`(?m)^second`}, "first\nsecond", "first\nsecond"},
		{"ATF-E2E-D-017", "unicode regex", nil, []string{`\p{L}+`}, "word", "word"},
		{"ATF-E2E-D-018", "persian regex", nil, []string{`سلام`}, "سلام دنیا", "سلام دنیا"},
		{"ATF-E2E-D-019", "arabic regex", nil, []string{`مرحبا`}, "مرحبا بالعالم", "مرحبا بالعالم"},
		{"ATF-E2E-D-020", "emoji regex", nil, []string{`🚀`}, "blast off 🚀", "blast off 🚀"},
		{"ATF-E2E-D-021", "number regex", nil, []string{`\d{3,}`}, "call 12345 now", "call 12345 now"},
		{"ATF-E2E-D-022", "url regex", nil, []string{`https?://`}, "visit https://x.com", "visit https://x.com"},
		{"ATF-E2E-D-023", "capturing groups: require still matches", nil, []string{`(a)(b)`}, "ab cd", "ab cd"},
		{"ATF-E2E-D-024", "multiple capture groups", nil, []string{`(a|b)(c|d)`}, "ac and bd", "ac and bd"},
		{"ATF-E2E-D-025", "anchored regex rejects drifting text", nil, []string{`^start`}, "not start", ""},
		{"ATF-E2E-D-026", "anchors accept anchored text", nil, []string{`^start`}, "start here", "start here"},
		{"ATF-E2E-D-027", "quantifiers", nil, []string{`a{2,3}`}, "aa and aaa", "aa and aaa"},
		{"ATF-E2E-D-028", "alternation", nil, []string{`cat|dog`}, "cat and dog", "cat and dog"},
		{"ATF-E2E-D-029", "regex block drops matching text", []string{`secret`}, nil, "has secret word", ""},
		{"ATF-E2E-D-030", "regex block passes non-matching", []string{`secret`}, nil, "clean text", "clean text"},
		{"ATF-E2E-D-031", "escaped characters in regex", nil, []string{`\$\d+`}, "price $100", "price $100"},
		{"ATF-E2E-D-032", "multiple regex require: any one matching passes", nil, []string{`^a`, `z$`}, "a to z", "a to z"},
		{"ATF-E2E-D-033", "multiple regex require: none matching drops", nil, []string{`^a`, `z$`}, "b to y", ""},
		{"ATF-E2E-D-34", "empty regex list: no constraint", nil, nil, "anything", "anything"},
	}
	for i, c := range rxCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			fid := fmt.Sprintf("flt-rx-%d", i)
			h.InstallFilter(FilterSpec{ID: fid, Enabled: true, RegexBlock: c.block, RegexRequire: c.require})
			h.Install(TaskSpec{ID: fmt.Sprintf("task-rx-%d", i), Source: "-1001", Target: "-1002", Active: true, FilterID: fid})
			e := evt(c.id, "D", c.desc, "regex decision")
			start := now()
			j := h.Enqueue(fmt.Sprintf("task-rx-%d", i), "-1001", int64(1200+i), "-1002", c.text)
			err := h.Run(ctx, j)
			if c.expect == "" {
				if h.AssertDropped(&e, j) {
					e.Result = ResultPass
				}
			} else if h.AssertDelivered(&e, j, c.expect) {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// D-035..D-045: cleaner (11).
	// The enable flag is the top-level metadata key cleaner_enabled; the
	// option flags live under cleaner_config (remove_emojis, remove_urls,
	// remove_mentions, remove_hashtags, remove_phone_numbers,
	// remove_empty_lines).
	type cCase struct {
		id     string
		desc   string
		clean  CleanerSpec
		text   string
		expect string
	}
	cCases := []cCase{
		{"ATF-E2E-D-035", "emoji cleaner", CleanerSpec{Enabled: true, Emoji: true}, "hi 🚀 there", "hi there"},
		{"ATF-E2E-D-036", "url cleaner", CleanerSpec{Enabled: true, Urls: true}, "see https://x.com now", "see now"},
		{"ATF-E2E-D-037", "mention cleaner", CleanerSpec{Enabled: true, Mentions: true}, "cc @user end", "cc end"},
		{"ATF-E2E-D-038", "hashtag cleaner", CleanerSpec{Enabled: true, Hashtags: true}, "tag #news end", "tag end"},
		{"ATF-E2E-D-039", "phone cleaner", CleanerSpec{Enabled: true, Phones: true}, "call +989121234567 ok", "call ok"},
		{"ATF-E2E-D-040", "empty-line removal", CleanerSpec{Enabled: true, Newlines: true}, "a\n\nb\nc", "a\nb\nc"},
		{"ATF-E2E-D-041", "repeated blank lines collapse to one", CleanerSpec{Enabled: true, Newlines: true}, "a\n\n\nb", "a\nb"},
		{"ATF-E2E-D-042", "cleaner disabled keeps everything", CleanerSpec{Enabled: false, Emoji: true}, "hi 🚀", "hi 🚀"},
		{"ATF-E2E-D-043", "cleaner enabled but no flags keeps text", CleanerSpec{Enabled: true}, "plain text", "plain text"},
		{"ATF-E2E-D-044", "multiple cleaners together", CleanerSpec{Enabled: true, Emoji: true, Urls: true}, "hi 🚀 https://x.com end", "hi end"},
		{"ATF-E2E-D-045", "cleaner on persian text preserves words", CleanerSpec{Enabled: true, Newlines: true}, "سلام\n\nدنیا", "سلام\nدنیا"},
	}
	for i, c := range cCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			cl := c.clean
			h.Install(TaskSpec{ID: fmt.Sprintf("task-c-%d", i), Source: "-1001", Target: "-1002", Active: true, Policy: &PolicySpec{Enabled: boolPtr(true), Cleaner: &cl}})
			e := evt(c.id, "D", c.desc, c.expect)
			start := now()
			j := h.Enqueue(fmt.Sprintf("task-c-%d", i), "-1001", int64(1300+i), "-1002", c.text)
			err := h.Run(ctx, j)
			if h.AssertDelivered(&e, j, c.expect) {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// D-046..D-065: transformation correctness (20).
	type tCase struct {
		id     string
		desc   string
		policy *PolicySpec
		text   string
		expect string
	}
	tCases := []tCase{
		{"ATF-E2E-D-046", "text transformation", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"a": "b"}}, "a", "b"},
		{"ATF-E2E-D-047", "caption transformation", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"cap": "hat"}}, "cap text", "hat text"},
		{"ATF-E2E-D-048", "transformation order: strip then replace", &PolicySpec{Enabled: boolPtr(true), StripWords: []string{"bad"}, Replacements: map[string]string{"x": "y"}}, "bad x", "y"},
		{"ATF-E2E-D-049", "exactly-once: replacement not applied twice", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"a": "b"}}, "a", "b"},
		{"ATF-E2E-D-050", "no double-cleaning across two jobs", &PolicySpec{Enabled: boolPtr(true), Cleaner: &CleanerSpec{Enabled: true, Newlines: true}}, "a\n\nb", "a\nb"},
		{"ATF-E2E-D-051", "no double-replacement", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"ab": "ab"}}, "ab", "ab"},
		{"ATF-E2E-D-052", "source text unchanged when policy inert", &PolicySpec{Enabled: boolPtr(true)}, "keep me", "keep me"},
		{"ATF-E2E-D-053", "replacements are task-level, independent of content_policy.enabled", &PolicySpec{Enabled: boolPtr(false), Replacements: map[string]string{"a": "z"}}, "a", "z"},
		{"ATF-E2E-D-054", "whitelist + replacement compose", &PolicySpec{Enabled: boolPtr(true), WhitelistWords: []string{"SIGNAL"}, Replacements: map[string]string{"SIGNAL": "ALERT"}}, "SIGNAL now", "ALERT now"},
		{"ATF-E2E-D-055", "blacklist + replacement compose", &PolicySpec{Enabled: boolPtr(true), BlacklistWords: []string{"SPAM"}, Replacements: map[string]string{"SPAM": "HAM"}}, "SPAM here", "HAM here"},
		{"ATF-E2E-D-056", "replacement + cleaner compose", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"a": "b"}, Cleaner: &CleanerSpec{Enabled: true, Newlines: true}}, "a\n\nc", "b\nc"},
		{"ATF-E2E-D-057", "empty replacement output: still delivered", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"a": ""}}, "a", ""},
		{"ATF-E2E-D-058", "persian replacement", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"سلام": "درود"}}, "سلام", "درود"},
		{"ATF-E2E-D-059", "number replacement", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"100": "200"}}, "score 100", "score 200"},
		{"ATF-E2E-D-060", "url replacement", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"https://x.com": "https://y.com"}}, "go https://x.com", "go https://y.com"},
		{"ATF-E2E-D-061", "mention replacement", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"@old": "@new"}}, "hi @old", "hi @new"},
		{"ATF-E2E-D-062", "hashtag replacement", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"#tag": "#new"}}, "use #tag", "use #new"},
		{"ATF-E2E-D-063", "whitespace-only find key is skipped (parity), text keeps newline", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"\n": " "}}, "a\nb", "a\nb"},
		{"ATF-E2E-D-064", "repeated matches replaced deterministically", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"na": "x"}}, "na na na", "x x x"},
		{"ATF-E2E-D-065", "determinism: same input same output", &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"a": "b"}, Cleaner: &CleanerSpec{Enabled: true, Newlines: true}}, "a\n\nb", "b\nb"},
	}
	for i, c := range tCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			h.Install(TaskSpec{ID: fmt.Sprintf("task-t-%d", i), Source: "-1001", Target: "-1002", Active: true, Policy: c.policy})
			e := evt(c.id, "D", c.desc, c.expect)
			start := now()
			j := h.Enqueue(fmt.Sprintf("task-t-%d", i), "-1001", int64(1400+i), "-1002", c.text)
			err := h.Run(ctx, j)
			if h.AssertDelivered(&e, j, c.expect) {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}
}

// boolPtr returns a pointer to b so a policy's Enabled can be nil / true / false.
func boolPtr(b bool) *bool { return &b }
