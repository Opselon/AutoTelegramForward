// Package cleaning — behavioral parity tests.
//
// These tests are the Go half of the cross-language differential suite.
// Every case here was first executed against the real Python production
// engines (core.domain.entities.CleanerRule, core.domain.services.
// LinkManagementEngine, FilterEngine.replace_*, AdvancedSettingsEngine) and
// the expected value copied from the actual output — not from a
// specification. When Python and Go disagree, the disagreement is pinned
// by the shared fixtures.json and named with a finding id; this file
// asserts the AGREEMENTS that make the engines interchangeable.
//
// The cases are grouped by the pipeline stage that owns them, in pipeline
// order, so a reader can verify the ordering contract by reading the file
// top to bottom.
package cleaning

import (
	"strings"
	"testing"
)

// --- stage 1: the cleaner (CleanerRule.clean) --------------------------------

func TestCleanerFullOrder(t *testing.T) {
	// CLN-FULL-01: full cleaner order on one message. Expected value is the
	// fixture's expected_python_output, produced by the real Python
	// CleanerRule.clean.
	rule := &CleanerRule{
		RemoveURLs:         true,
		RemoveMentions:     true,
		RemoveHashtags:     true,
		RemovePhoneNumbers: true,
		RemoveEmptyLines:   true,
		IsActive:           true,
	}
	in := "@bob see https://a.io/x #news 5% OFF\n\n\n@alice call +1 415 555 2671 now"
	want := "@bob see 5% OFF\n call now"
	if got := rule.Clean(in); got != want {
		t.Errorf("CLN-FULL-01: got %q, want %q", got, want)
	}
}

func TestCleanerPreservesShortMention(t *testing.T) {
	// CLN-MENTION-01 (F-07): the cleaner's mention bound is {4,64}, so a
	// 3-character mention survives. The legacy FilterEngine path ({3,32})
	// strips it — the divergence is pinned by the fixture, this test pins
	// the cleaner's own semantics.
	rule := &CleanerRule{RemoveMentions: true, IsActive: true}
	if got := rule.Clean("ping @abc now"); got != "ping @abc now" {
		t.Errorf("CLN-MENTION-01: short mention must survive, got %q", got)
	}
}

func TestCleanerStripsLongMention(t *testing.T) {
	rule := &CleanerRule{RemoveMentions: true, IsActive: true}
	in := "ping @alice now"
	want := "ping now"
	if got := rule.Clean(in); got != want {
		t.Errorf("cleaner mention removal: got %q, want %q", got, want)
	}
}

func TestCleanerHashtagStopsAtHyphen(t *testing.T) {
	// CLN-HASHTAG-01 (F-07): the cleaner's #\w+ stops at the hyphen. The
	// old Go #[^\s#@]+ consumed it; this test is the parity fix.
	rule := &CleanerRule{RemoveHashtags: true, IsActive: true}
	in := "trend #hello-world now"
	want := "trend -world now"
	if got := rule.Clean(in); got != want {
		t.Errorf("CLN-HASHTAG-01: got %q, want %q", got, want)
	}
}

func TestCleanerPhonePreservesBareProductId(t *testing.T) {
	// CLN-PHONE-01 (F-07): a bare 10-digit id is NOT a contact number.
	// Python's guarded candidate leaves it; the Go port must too — the
	// previous Go pattern ate it, which is the divergence the fixture pins.
	rule := &CleanerRule{RemovePhoneNumbers: true, IsActive: true}
	in := "order 1234567890 done"
	if got := rule.Clean(in); got != in {
		t.Errorf("CLN-PHONE-01: bare product id must survive, got %q", got)
	}
}

func TestCleanerPhoneRemovesContactNumber(t *testing.T) {
	rule := &CleanerRule{RemovePhoneNumbers: true, IsActive: true}
	cases := map[string]string{
		"call +1 415 555 2671 now": "call now",
		"call +989123456789 today": "call today",
	}
	for in, want := range cases {
		if got := rule.Clean(in); got != want {
			t.Errorf("phone removal %q: got %q, want %q", in, got, want)
		}
	}
}

func TestCleanerInvalidRegexDoesNotBreak(t *testing.T) {
	// RPL-INVALID-01's sibling for the cleaner: a malformed pattern is
	// skipped, never raised. The valid "ok" pattern in the same list still
	// runs; the whitespace the removal punches out is collapsed by the
	// cleaner's own unconditional tidy.
	rule := &CleanerRule{
		UseRegex:  true,
		Patterns:  []string{"(unclosed", "ok"},
		IsActive:  true,
	}
	if got := rule.Clean("hello ok world"); got != "hello world" {
		t.Errorf("invalid pattern must be skipped: got %q", got)
	}
	// Only the invalid pattern: nothing changes at all.
	rule = &CleanerRule{UseRegex: true, Patterns: []string{"(unclosed"}, IsActive: true}
	if got := rule.Clean("hello world"); got != "hello world" {
		t.Errorf("sole invalid pattern must leave text unchanged: got %q", got)
	}
}

func TestCleanerCustomWordCaseInsensitive(t *testing.T) {
	rule := &CleanerRule{Patterns: []string{"Buy now"}, RemoveURLs: true, IsActive: true}
	in := "BUY NOW https://a.io deal"
	want := "deal"
	if got := rule.Clean(in); got != want {
		t.Errorf("case-insensitive custom word: got %q, want %q", got, want)
	}
}

func TestCleanerEmptyLinesCollapse(t *testing.T) {
	rule := &CleanerRule{RemoveEmptyLines: true, IsActive: true}
	if got := rule.Clean("first\n\n\n\nsecond"); !strings.Contains(got, "first") || !strings.Contains(got, "second") {
		t.Errorf("empty-line collapse lost content: %q", got)
	}
	if strings.Contains(rule.Clean("first\n\n\n\nsecond"), "\n\n") {
		t.Errorf("empty-line collapse left a blank run")
	}
}

func TestCleanerDisabledIsInert(t *testing.T) {
	in := "keep https://a.io @bob #tag 🚀 +1 415 555 2671"
	rule := &CleanerRule{
		RemoveURLs: true, RemoveMentions: true, RemoveHashtags: true,
		RemoveEmojis: true, RemovePhoneNumbers: true, RemoveEmptyLines: true,
	}
	if got := rule.Clean(in); got != in {
		t.Errorf("inactive cleaner must be inert: got %q", got)
	}
}

func TestCleanerApplyToCaptionIsCallersDecision(t *testing.T) {
	// The ApplyToCaption flag is the caller's gating decision (F-09: the
	// flag never disabled caption cleaning in Python because the pipeline
	// feeds effective_text). The rule itself must honor whatever body it is
	// handed — the pipeline decides which body that is.
	rule := &CleanerRule{RemoveURLs: true, ApplyToCaption: false, IsActive: true}
	// Applied to a caption-shaped body anyway: Python's behavior (the flag
	// does not gate Clean itself) is what the Worker must reproduce.
	if got := rule.Clean("see https://a.io"); got != "see" {
		t.Errorf("ApplyToCaption must not gate Clean: got %q", got)
	}
}

// --- stage 1b: emoji (the pinned, byte-identical pattern) --------------------

func TestEmojiPatternIsByteIdenticalToPython(t *testing.T) {
	// UNI-EMOJI-01..05: the emoji pattern is deliberately byte-identical to
	// Python's EMOJI_REGEX, defects included (F-03: it matches ALL non-BMP
	// codepoints, so cuneiform is destroyed, and ZWJ sequences are
	// shredded). Changing the pattern is a cross-language decision that
	// must flip the fixtures first.
	cases := map[string]string{
		"\U0001F468\u200d\U0001F469\u200d\U0001F467": "\u200d\u200d", // ZWJ family: joiners survive
		"5\ufe0f\u20e3":            "5\u20e3",                // keycap: selector removed, keycap kept
		"\U00012000 cuneiform":     " cuneiform",             // non-emoji SMP destroyed (F-03)
		"سلام 🌟 دنیا":              "سلام  دنیا",             // Persian survives, double space left
		"a ← b, á":                 "a ← b, á",               // arrows and combining marks kept
	}
	for in, want := range cases {
		if got := cleanEmojiRegex.ReplaceAllString(in, ""); got != want {
			t.Errorf("UNI-EMOJI %q: got %q, want %q", in, got, want)
		}
	}
}

// --- stage 2: the link management engine -------------------------------------

func TestLinkPolicyRemovals(t *testing.T) {
	in := "see https://a.io/x and https://t.me/y and https://evil.com/z"
	cases := map[string]string{
		// Python: _should_remove under each policy, verified by execution.
		"REMOVE_ALL_URLS":       "see and and",
		"REMOVE_EXTERNAL_URLS":  "see and https://t.me/y and",
		"ALLOWLIST_ONLY":        "see and https://t.me/y and",
		"REWRITE_LINKS":         "see https://b.io/x and https://t.me/y and https://evil.com/z",
		"REMOVE_SELECTED_DOMAINS": "see https://a.io/x and https://t.me/y and",
		"CUSTOM_LINK_POLICY":    "see and https://t.me/y and",
		"REMOVE_BUTTON_URLS":    in, // text is untouched; buttons are markup
		"PRESERVE_ALL":          in,
	}
	engine := LinkManagementEngine{}
	for policy, want := range cases {
		out := engine.ApplyToBody(in, nil, LinkConfig{
			Policy: policy, Allowlist: []string{"t.me"}, Blocklist: []string{"evil.com"},
			RewriteMap: map[string]string{"a.io": "b.io"},
		})
		if out.Text != want {
			t.Errorf("policy %s: got %q, want %q", policy, out.Text, want)
		}
	}
}

func TestLinkRemovalKeepsMentions(t *testing.T) {
	// LNK-OVERRIDE-01 (F-13): the Python engine keeps @mentions when
	// removing links. The old Go target-override regex ate them because
	// URL_REGEX carries the mention alternation; the skip of an "@"-prefixed
	// match is the parity fix.
	engine := LinkManagementEngine{}
	out := engine.ApplyToBody("see @bob and https://a.io", nil, LinkConfig{Policy: "REMOVE_ALL_URLS"})
	if want := "see @bob and"; out.Text != want {
		t.Errorf("LNK-OVERRIDE-01: got %q, want %q", out.Text, want)
	}
}

func TestUnknownLinkPolicyFailsClosedToPreserve(t *testing.T) {
	// Python resolve_policy: an unsupported value is PRESERVE_ALL, never
	// "remove everything". A malformed policy must not strip content.
	if got := ResolvePolicy("REMOVE_ALL", false); got != LinkPolicyPreserveAll {
		t.Errorf("unknown policy must fail closed, got %q", got)
	}
	if got := ResolvePolicy("", false); got != LinkPolicyPreserveAll {
		t.Errorf("empty policy must be PRESERVE_ALL, got %q", got)
	}
	// The legacy "remove links" toggle means REMOVE_ALL_URLS.
	if got := ResolvePolicy("", true); got != LinkPolicyRemoveAllURLs {
		t.Errorf("remove_links toggle must resolve to REMOVE_ALL_URLS, got %q", got)
	}
	if got := ResolvePolicy("PRESERVE_ALL", true); got != LinkPolicyRemoveAllURLs {
		t.Errorf("remove_links + PRESERVE_ALL must still remove, got %q", got)
	}
}

func TestRemoveLinksIgnoresLinkReplacementOnDurablePath(t *testing.T) {
	// LNK-REMOVE-LINKS-01 (F-04): on the durable path remove_links wins
	// and link_replacement is silently ignored. The pipeline implements the
	// if/elif; this test asserts the outcome of that precedence.
	p := NewPipeline()
	got := p.Apply("visit https://a.io now", Config{RemoveLinks: true, LinkReplacement: "[LINK]"}, nil)
	if want := "visit now"; got.Text != want {
		t.Errorf("LNK-REMOVE-LINKS-01: got %q, want %q", got.Text, want)
	}
	if !contains(got.StageNotes, "link_policy:REMOVE_ALL_URLS") {
		t.Errorf("expected the REMOVE_ALL_URLS stage note, got %v", got.StageNotes)
	}
}

func TestLinkReplacementAppliesWhenNoRemoval(t *testing.T) {
	p := NewPipeline()
	got := p.Apply("visit https://a.io now", Config{LinkReplacement: "[LINK]"}, nil)
	if want := "visit [LINK] now"; got.Text != want {
		t.Errorf("link_replacement: got %q, want %q", got.Text, want)
	}
}

func TestExtractDomainStripsWWWAndPort(t *testing.T) {
	cases := map[string]string{
		"https://www.Example.com/path": "example.com",
		"www.example.com":              "example.com",
		"https://a.io:8080/x":         "a.io",
		"t.me/x":                       "t.me",
		"":                             "",
	}
	for in, want := range cases {
		if got := ExtractDomain(in); got != want {
			t.Errorf("ExtractDomain(%q): got %q, want %q", in, got, want)
		}
	}
}

func TestMatchesDomainParent(t *testing.T) {
	list := []string{"evil.com", "t.me"}
	if !matchesDomain("sub.evil.com", list) {
		t.Error("a subdomain must match its parent")
	}
	if matchesDomain("notevil.com", list) {
		t.Error("a suffix match must not be a parent match")
	}
	if !matchesDomain("t.me", list) {
		t.Error("exact match must match")
	}
}

// --- stages 3-9: the ordered pipeline -----------------------------------------

func TestPipelineEmptyAfterCleanDropsOnlyWhenContentExisted(t *testing.T) {
	p := NewPipeline()
	// A message whose only content was a link the task strips.
	got := p.Apply("https://a.io", Config{Cleaner: &CleanerRule{RemoveURLs: true, IsActive: true}}, nil)
	if !got.EmptyAfterClean {
		t.Error("a fully-stripped message must report EmptyAfterClean")
	}
	// A message that had no content to begin with is not a drop.
	got = p.Apply("", Config{Cleaner: &CleanerRule{RemoveURLs: true, IsActive: true}}, nil)
	if got.EmptyAfterClean {
		t.Error("an empty input must not report EmptyAfterClean")
	}
	// A message that survives is not a drop.
	got = p.Apply("keep this https://a.io", Config{Cleaner: &CleanerRule{RemoveURLs: true, IsActive: true}}, nil)
	if got.EmptyAfterClean {
		t.Error("a message with surviving content must not report EmptyAfterClean")
	}
}

func TestPipelineReplacementsRunAfterCleaner(t *testing.T) {
	// The order matters: the cleaner strips the URL, then the replacement
	// runs on the cleaned text. A replacement that reintroduces a URL the
	// cleaner removed would be a ordering bug.
	p := NewPipeline()
	cfg := Config{
		Cleaner:      &CleanerRule{RemoveURLs: true, IsActive: true},
		Replacements: []ReplacementEntry{{Find: "see", Replace: "visit https://a.io now"}},
	}
	got := p.Apply("see https://a.io/x", cfg, nil)
	// Python applies replacements AFTER the cleaner, so the reintroduced
	// URL survives — this test pins that ordering (pipeline.py:1543 sits
	// after pipeline.py:1408).
	if want := "visit https://a.io now"; got.Text != want {
		t.Errorf("ordering: got %q, want %q", got.Text, want)
	}
}

func TestPipelineHeaderFooterDedup(t *testing.T) {
	// HEADER-FOOTER-DEDUP-01: a header already present is not prepended twice.
	p := NewPipeline()
	got := p.Apply("HDR\n\nbody", Config{Header: "HDR"}, nil)
	if want := "HDR\n\nbody"; got.Text != want {
		t.Errorf("header dedup: got %q, want %q", got.Text, want)
	}
}

func TestPipelineHeaderPlaceholdersRenderWhenVarsPresent(t *testing.T) {
	// Stage 4.4 closes F-15: the durable path now renders the placeholders
	// the preview always promised — "From [[chat_title]] on {date}" no
	// longer reaches the target literally. (The ADV-HEADER-01 fixture pins
	// the OLD Python behavior; the Go side now renders and the fixture is
	// due for the same flip this test documents.)
	p := NewPipeline()
	cfg := Config{
		Header: "From [[chat_title]] on {date}",
		HeaderVars: &HeaderVars{SourceChatID: "-1001234567890", SourceChatTitle: "News Channel", SourceDate: 1730000000},
	}
	got := p.Apply("body", cfg, nil)
	want := "From News Channel on 2024-10-27 03:33 UTC\n\nbody"
	if got.Text != want {
		t.Errorf("F-15 fix: got %q, want %q", got.Text, want)
	}
	if !contains(got.StageNotes, "header_footer") {
		t.Errorf("expected the header_footer stage note, got %v", got.StageNotes)
	}
}

func TestPipelineHeaderStaysRawWithoutVars(t *testing.T) {
	// A caller with no message snapshot (the parity harness's header_footer
	// cases, and any test that builds a Config by hand) keeps the
	// pre-4.4 raw behavior: half-rendered output is worse than none.
	p := NewPipeline()
	got := p.Apply("body", Config{Header: "From [[chat_title]] on {date}"}, nil)
	want := "From [[chat_title]] on {date}\n\nbody"
	if got.Text != want {
		t.Errorf("raw fallback: got %q, want %q", got.Text, want)
	}
}

func TestHeaderFormatterRendersEveryPlaceholder(t *testing.T) {
	// Every variable Python's HeaderFormatter exposes, rendered against
	// one message snapshot, compared to the Python output captured from
	// the real engine (core.domain.services.HeaderFormatter.format_text).
	vars := HeaderVars{
		SourceChatID:       "-1001234567890",
		SourceChatUsername: "newschannel",
		SourceChatTitle:    "News Channel",
		SourceMessageID:    55,
		SourceDate:         1730000000,
		SenderName:         "Alice",
		SenderID:           "4242",
	}
	task := HeaderTask{Name: "My Task", Category: "ALL"}
	cases := []struct{ template, want string }{
		{"From [[chat_title]] on {date} — {source_link} by {sender_name} | {rule_name} {category}",
			"From News Channel on 2024-10-27 03:33 UTC — https://t.me/newschannel/55 by Alice | My Task ALL"},
		{"{ORIGIN_NAME_URL} {ORIGIN_POST_ID} {ORIGIN_USERNAME} {SOURCE_NAME} {SENDER_CHAT}",
			"[News Channel](https://t.me/newschannel/55) 55 @newschannel News Channel -1001234567890"},
		{"{rule_name} {category} {source_chat} {ORIGIN_USERID} {ORIGIN_CHAT_ID} {ORIGIN_POST_DATE} {FROM_USER}",
			"My Task ALL -1001234567890 4242 -1001234567890 2024-10-27 03:33 UTC Alice"},
		// Python's format_text also .strip()s the result, so the trailing
		// space a join would leave is trimmed on both engines.
		{"{FORWARD_FROM_CHAT} {ORIGIN_TEXT}",
			"-1001234567890"},
		{"{unknown_placeholder}",
			"{unknown_placeholder}"}, // unknown stays, Python leaves it too
		{"", ""}, // empty template renders empty
	}
	for _, c := range cases {
		if got := FormatHeader(c.template, task, vars); got != c.want {
			t.Errorf("FormatHeader(%q): got %q, want %q", c.template, got, c.want)
		}
	}
}

func TestHeaderFormatterPrivateChannelLink(t *testing.T) {
	// A private channel has no @username: the link falls back to Telegram's
	// /c/<internal id> form, and the date falls back to "now" when the
	// message carries no timestamp.
	vars := HeaderVars{SourceChatID: "-1001234567890", SourceMessageID: 7}
	out := FormatHeader("{source_link} {chat_title}", HeaderTask{}, vars)
	if !strings.Contains(out, "https://t.me/c/1234567890/7") {
		t.Errorf("private channel link: got %q", out)
	}
	if !strings.Contains(out, "-1001234567890") {
		t.Errorf("chat id fallback title: got %q", out)
	}
}

func TestHeaderFormatterRuleNameFallback(t *testing.T) {
	// Python: rule_name = rule.name or f"Rule-{rule.id[:8]}". A task with
	// no name renders the id-derived label, so an unconfigured rule still
	// produces a readable header.
	out := FormatHeader("{rule_name}", HeaderTask{}, HeaderVars{})
	if out != "" {
		t.Errorf("an empty task name renders empty at the formatter level: got %q", out)
	}
	// The delivery engine supplies the fallback (ruleName), not the
	// formatter: the formatter is a pure renderer and the fallback is a
	// rule projection decision. The ruleName tests live with the engine.
}

func TestHeaderFormatterDateUsesMessageTime(t *testing.T) {
	// {date} is the SOURCE message's time, not the render time; only a
	// message with no timestamp falls back to now.
	vars := HeaderVars{SourceDate: 1730000000}
	if got := FormatHeader("{date}", HeaderTask{}, vars); got != "2024-10-27 03:33 UTC" {
		t.Errorf("source date: got %q", got)
	}
}

func TestPipelineLengthClampCountsCodePoints(t *testing.T) {
	// UNI-LEN-01: Python len() counts codepoints. A 4097-codepoint body
	// must clamp at 4096; counting bytes would cut a Persian/emoji body
	// early and diverge from the Python pipeline.
	long := strings.Repeat("ا", 5000) // 5000 codepoints, 5000*2 bytes
	p := NewPipeline()
	got := p.Apply(long, Config{}, nil)
	if len([]rune(got.Text)) != 4096 {
		t.Errorf("text clamp: got %d codepoints, want 4096", len([]rune(got.Text)))
	}
	// A media message clamps at 1024 (caption ceiling).
	got = p.Apply(long, Config{HasMedia: true}, nil)
	if len([]rune(got.Text)) != 1024 {
		t.Errorf("caption clamp: got %d codepoints, want 1024", len([]rune(got.Text)))
	}
}

func TestPipelineWatermarkIsIdempotent(t *testing.T) {
	p := NewPipeline()
	cfg := Config{Watermark: "© me"}
	once := p.Apply("body", cfg, nil)
	twice := p.Apply(once.Text, cfg, nil)
	if twice.Text != once.Text {
		t.Errorf("watermark must not duplicate: once=%q twice=%q", once.Text, twice.Text)
	}
	if !strings.Contains(once.Text, "© me") {
		t.Errorf("watermark must be applied: %q", once.Text)
	}
}

func TestPipelineAffiliateRewritesOnlyHTTP(t *testing.T) {
	// Python: only a URL starting with "http" gets the prefix; the mention
	// alternation and t.me forms do not.
	p := NewPipeline()
	cfg := Config{AffiliateKit: &AffiliateKit{Prefix: "https://aff.io/"}}
	got := p.Apply("see https://a.io/x and t.me/y now", cfg, nil)
	if want := "see https://aff.io/https://a.io/x and t.me/y now"; got.Text != want {
		t.Errorf("affiliate: got %q, want %q", got.Text, want)
	}
}

func TestPipelineLinkButtonsRenderAsTextBlock(t *testing.T) {
	p := NewPipeline()
	cfg := Config{LinkButtons: []LinkButton{{Text: "Join", URL: "https://t.me/x"}}}
	got := p.Apply("body", cfg, nil)
	if want := "body\n\n🔗 Join: https://t.me/x"; got.Text != want {
		t.Errorf("link buttons: got %q, want %q", got.Text, want)
	}
}

// --- replacements: the two distinct Python semantics ---------------------------

func TestReplacePriorityOrderComposes(t *testing.T) {
	// RPL-PRIORITY-01: both default-priority, so insertion order decides
	// ("# -> ##" then "## -> x" yields "x sale").
	entries := []ReplacementEntry{
		{Find: "#", Replace: "##"},
		{Find: "##", Replace: "x"},
	}
	if got := applyReplacements("# sale", entries); got != "x sale" {
		t.Errorf("RPL-PRIORITY-01: got %q, want %q", got, "x sale")
	}
}

func TestReplaceSmartCaseExactFirst(t *testing.T) {
	// RPL-SMART-CASE-01: the exact form is present, so str.replace handles
	// both occurrences exactly — the lowercase "discount" is untouched.
	entries := []ReplacementEntry{{Find: "DISCOUNT", Replace: "SALE"}}
	if got := applyReplacements("discount 10% DISCOUNT 20%", entries); got != "discount 10% SALE 20%" {
		t.Errorf("RPL-SMART-CASE-01: got %q", got)
	}
}

func TestReplaceSmartCaseInsensitiveFallback(t *testing.T) {
	// RPL-SMART-CASE-02: the exact form is absent, so one case-insensitive
	// pass replaces both.
	entries := []ReplacementEntry{{Find: "Discount", Replace: "Sale"}}
	if got := applyReplacements("discount 10% DISCOUNT 20%", entries); got != "Sale 10% Sale 20%" {
		t.Errorf("RPL-SMART-CASE-02: got %q", got)
	}
}

func TestReplaceDeletionKeepsTrailingSpace(t *testing.T) {
	// RPL-DELETION-01: the trailing space is NOT stripped by the replace
	// engine (downstream stages may).
	entries := []ReplacementEntry{{Find: "world", Replace: ""}}
	if got := applyReplacements("hello world", entries); got != "hello " {
		t.Errorf("RPL-DELETION-01: got %q", got)
	}
}

func TestReplaceLiteralBackslashRendersLiterally(t *testing.T) {
	// RPL-BACKREF-01 (literal half): a literal entry's replacement escapes
	// backslashes so \1 renders as \1, not a group reference.
	entries := []ReplacementEntry{{Find: "a\\1b", Replace: "KEPT"}}
	if got := applyReplacements("a\\1b", entries); got != "KEPT" {
		t.Errorf("literal backslash: got %q", got)
	}
}

func TestReplaceRegexBackreferencesExpand(t *testing.T) {
	// RPL-BACKREF-01 (regex half): a regex entry passes the template
	// through, so \1 and \2 expand.
	entries := []ReplacementEntry{{Find: "(\\w+): (\\w+)", Replace: "\\1 is \\2", IsRegex: true, IgnoreCase: true}}
	if got := applyReplacements("item: apple", entries); got != "item is apple" {
		t.Errorf("regex backref: got %q", got)
	}
}

func TestReplaceInvalidRegexEntryIsSkipped(t *testing.T) {
	// RPL-INVALID-01: an unusable pattern leaves the text unchanged and
	// never raises.
	entries := []ReplacementEntry{{Find: "(unclosed", Replace: "x", IsRegex: true}}
	if got := applyReplacements("hello", entries); got != "hello" {
		t.Errorf("RPL-INVALID-01: got %q", got)
	}
}

func TestReplaceWholeWordAnchored(t *testing.T) {
	// text_replace._pattern_body: an anchored pattern already states its
	// own boundary; wrapping it would make it unmatchable.
	entries := []ReplacementEntry{{Find: "^promo\\d+$", Replace: "X", IsRegex: true, WholeWord: true}}
	if got := applyReplacements("promo123", entries); got != "X" {
		t.Errorf("anchored whole-word: got %q", got)
	}
}

// --- content policy rewrite: the single-pass alternation -----------------------

func TestContentPolicyReplacementsSinglePass(t *testing.T) {
	// content_policy._apply_replacements: one pass over the original text,
	// so a=>b then b=>c yields b, NOT c (the opposite of the per-task
	// replace engine's ordered composition).
	policy := &ContentPolicy{Replacements: [][2]string{{"a", "b"}, {"b", "c"}}}
	if got := applyPairs("a b", policy.Replacements, policy.CaseSensitive); got != "b c" {
		t.Errorf("single-pass replacements: got %q, want %q", got, "b c")
	}
}

func TestContentPolicyStripWordsBoundaryAware(t *testing.T) {
	// Python strip_words is boundary-aware: "spam" inside "spammer" is not
	// removed. (See the F-07 family: word boundaries are the policy's
	// default.)
	policy := &ContentPolicy{StripWords: []string{"spam"}}
	out, applied := applyContentPolicyRewrite("a spammer buys spam now", policy)
	if !applied {
		t.Error("strip_words must report applied")
	}
	if strings.Contains(out, "buys") == false || !strings.Contains(out, "spammer") {
		t.Errorf("strip_words must keep the boundary case: %q", out)
	}
	if strings.Contains(out, "spam ") {
		t.Errorf("strip_words must remove the standalone word: %q", out)
	}
}

func TestContentPolicyRemoveLinksKeepsMentions(t *testing.T) {
	// ContentPolicy.transform routes remove_links through
	// FilterEngine.remove_urls, which keeps @mentions (services.py:159-169).
	policy := &ContentPolicy{RemoveLinks: true}
	out, applied := applyContentPolicyRewrite("see @bob and https://a.io", policy)
	if !applied {
		t.Error("remove_links must report applied")
	}
	if want := "see @bob and"; out != want {
		t.Errorf("policy remove_links: got %q, want %q", out, want)
	}
}

func TestContentPolicyIsInertWhenEmpty(t *testing.T) {
	policy := &ContentPolicy{}
	if _, applied := applyContentPolicyRewrite("anything", policy); applied {
		t.Error("an empty policy must not report applied")
	}
}

// --- the payload boundary contract --------------------------------------------

func TestPipelineRunsAgainstRawTextNeverTransformed(t *testing.T) {
	// The execution-boundary invariant: the pipeline runs against the RAW
	// source text. A payload that carries BOTH a raw snapshot and a
	// pre-transformed text is the migration window's normal shape, and the
	// pipeline must never see the transformed copy.
	//
	// This test is structural: the delivery engine's ownsTransforms/jobs
	// contract is exercised in the delivery package; here we assert the
	// weaker property the domain guarantees — Apply is a pure function of
	// (raw input, config), with no hidden state.
	p := NewPipeline()
	cfg := Config{Cleaner: &CleanerRule{RemoveURLs: true, IsActive: true}}
	first := p.Apply("see https://a.io/x", cfg, nil)
	second := p.Apply("see https://a.io/x", cfg, nil)
	if first.Text != second.Text {
		t.Errorf("Apply must be deterministic: %q vs %q", first.Text, second.Text)
	}
	if first.Text != "see" {
		t.Errorf("raw-input pipeline: got %q", first.Text)
	}
}

// --- helpers ------------------------------------------------------------------

func contains(list []string, want string) bool {
	for _, item := range list {
		if item == want {
			return true
		}
	}
	return false
}

// --- the target-scoped override ----------------------------------------------

func TestTargetOverrideAppliesToItsOwnTarget(t *testing.T) {
	// The Python fan-out applies the matching target entry's rewrite to
	// one target's copy alone (pipeline.py:1276-1285). The Worker must do
	// the same: an override for -1002 rewrites the text bound for -1002
	// and leaves an identical job for -1003 alone.
	p := NewPipeline()
	cfg := Config{
		TargetOverrides: map[string]*ContentPolicy{
			"-1002": {RemoveLinks: true},
		},
	}
	// The target with the override: the URL is stripped.
	cfg.TargetChatID = "-1002"
	res := p.Apply("see https://a.io now", cfg, nil)
	if res.Text != "see now" {
		t.Errorf("target override must apply to its own target: got %q", res.Text)
	}
	if !contains(res.StageNotes, "target_override") {
		t.Errorf("expected the target_override stage note, got %v", res.StageNotes)
	}
	// A different target: the same text is untouched.
	cfg.TargetChatID = "-1003"
	if got := p.Apply("see https://a.io now", cfg, nil); got.Text != "see https://a.io now" {
		t.Errorf("a foreign target must not be rewritten: got %q", got.Text)
	}
}

func TestTargetOverrideRunsAfterTaskPolicy(t *testing.T) {
	// chain[1:] is the Python order: the task policy first, then the
	// override on the policy's result. An override that strips a word the
	// task policy already removed is a no-op, not a second removal.
	p := NewPipeline()
	cfg := Config{
		ContentPolicy: &ContentPolicy{StripWords: []string{"spam"}},
		TargetOverrides: map[string]*ContentPolicy{
			"-1002": {StripWords: []string{"offer"}},
		},
		TargetChatID: "-1002",
	}
	got := p.Apply("buy spam offer now", cfg, nil)
	if want := "buy now"; got.Text != want {
		t.Errorf("policy+override order: got %q, want %q", got.Text, want)
	}
}

func TestChatKeyNormalizesIdForms(t *testing.T) {
	// Python _chat_key: -100123 as int, float and string are one chat; an
	// @username passes through. The override map the rulestore builds and
	// the lookup the pipeline performs must agree on every spelling.
	cases := map[string]string{
		"-100123":  "-100123",
		"-100123 ": "-100123",
		"-100123.0": "-100123",
		"@channel": "@channel",
		"":         "",
	}
	for in, want := range cases {
		if got := chatKey(in); got != want {
			t.Errorf("chatKey(%q): got %q, want %q", in, got, want)
		}
	}
}
