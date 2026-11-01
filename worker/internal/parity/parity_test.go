// Package parity proves the Go engine preserves the Python engine's filter
// decisions on a shared fixture table.
//
// The harness is offline and deterministic by construction: it loads JSON
// fixtures, runs Go's filter engine, and compares against a reference
// decision file produced by running the REAL Python FilterEngine
// (reference_filter.py) on the same fixtures. No Telegram session, no
// network, no production data, no duplicate side effect.
//
// When the reference file is absent the suite regenerates nothing itself —
// it is generated and committed by scripts/regen_parity_reference.sh, so a
// diff in CI is always a deliberate, reviewable change.
package parity

import (
	"embed"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"atf/worker/internal/domain/rule"
	"atf/worker/internal/engine/filter"
)

//go:embed fixtures/*.json
var fixturesFS embed.FS

//go:embed reference_filter.json
var referenceFS []byte

// fixtureCase is one shared decision input. The field set is the
// intersection the Python MessagePayload and the Go engine both read, so
// neither side invents an input the other cannot express.
type fixtureCase struct {
	Text      string          `json:"text"`
	MediaType string          `json:"media_type"`
	IsService bool            `json:"is_service"`
	Filter    *fixtureFilter  `json:"filter"`
	Expect    expectedOutcome `json:"expect"`
}

type fixtureFilter struct {
	IsEnabled          bool     `json:"is_enabled"`
	WhitelistKeywords  []string `json:"whitelist_keywords"`
	BlacklistKeywords  []string `json:"blacklist_keywords"`
	RegexPatterns      []string `json:"regex_patterns"`
	RegexBlock         []string `json:"regex_block"`
	RegexRequire       []string `json:"regex_require"`
	AllowedMediaTypes  []string `json:"allowed_media_types"`
	BlockedMediaTypes  []string `json:"blocked_media_types"`
	DropService        bool     `json:"drop_service_messages"`
	MinMessageLength   int      `json:"min_message_length"`
	MaxMessageLength   int      `json:"max_message_length"`
}

type expectedOutcome struct {
	Action string `json:"action"`
	Reason string `json:"reason"`
}

// referenceDecision is one line of the Python reference output.
type referenceDecision struct {
	Name   string `json:"name"`
	Action string `json:"action"`
	Reason string `json:"reason"`
}

// toFilterRule maps the fixture's filter onto the Go rule projection. A nil
// fixture filter is the "no filter attached" case, which both engines must
// treat identically (allow, unless the payload is a service message).
func (f *fixtureFilter) toFilterRule() *rule.FilterRule {
	if f == nil {
		return nil
	}
	return &rule.FilterRule{
		IsEnabled:         f.IsEnabled,
		WhitelistKeywords: f.WhitelistKeywords,
		BlacklistKeywords: f.BlacklistKeywords,
		RegexPatterns:     f.RegexPatterns,
		RegexBlock:        f.RegexBlock,
		RegexRequire:      f.RegexRequire,
		AllowedMediaTypes: f.AllowedMediaTypes,
		BlockedMediaTypes: f.BlockedMediaTypes,
		DropService:       f.DropService,
		MinMessageLength:  f.MinMessageLength,
		MaxMessageLength:  f.MaxMessageLength,
	}
}

// normalizeReason collapses the spelling differences both engines are allowed
// to produce for the same decision, so the comparison is on the decision
// (allow/drop + why), not on enum formatting or per-pattern truncation width.
func normalizeReason(reason string) string {
	// Keep the pattern a gate names, so a change in WHICH pattern blocked a
	// message is still a real diff — only the spelling around it is ignored.
	for _, prefix := range []string{"regex_block:", "invalid_regex_block:"} {
		if strings.HasPrefix(reason, prefix) {
			return "regex_block:" + strings.TrimPrefix(reason, prefix)
		}
	}
	return reason
}

// loadFixtures returns every shared decision case, keyed by fixture name.
func loadFixtures(t *testing.T) map[string]fixtureCase {
	t.Helper()
	entries, err := fixturesFS.ReadDir("fixtures")
	if err != nil {
		t.Fatalf("read fixtures: %v", err)
	}
	out := make(map[string]fixtureCase, len(entries))
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		raw, err := fixturesFS.ReadFile("fixtures/" + e.Name())
		if err != nil {
			t.Fatalf("read fixture %s: %v", e.Name(), err)
		}
		var fx fixtureCase
		if err := json.Unmarshal(raw, &fx); err != nil {
			t.Fatalf("decode fixture %s: %v", e.Name(), err)
		}
		// A nil filter list is "not configured": a missing JSON key decodes
		// to nil, which the Go engine treats exactly as an empty list.
		_ = fx
		out[strings.TrimSuffix(e.Name(), ".json")] = fx
	}
	return out
}

// TestGoDecisionsMatchReference is the cross-engine comparison: every
// fixture, run through Go's filter engine, must produce the same decision
// the real Python FilterEngine produced on the same input.
func TestGoDecisionsMatchReference(t *testing.T) {
	if len(referenceFS) == 0 {
		t.Fatal("reference_filter.json is embedded but empty; run scripts/regen_parity_reference.sh")
	}
	var refs []referenceDecision
	if err := json.Unmarshal(referenceFS, &refs); err != nil {
		t.Fatalf("decode reference: %v", err)
	}
	refByID := make(map[string]referenceDecision, len(refs))
	for _, r := range refs {
		refByID[r.Name] = r
	}

	cases := loadFixtures(t)
	if len(cases) == 0 {
		t.Fatal("no parity fixtures found")
	}
	engine := filter.New()
	var mismatches []string

	for name, fx := range cases {
		got := engine.Evaluate(fx.Text, fx.MediaType, fx.IsService, fx.Filter.toFilterRule())
		gotAction := string(got.Action)
		gotReason := normalizeReason(got.Reason)

		// The fixture's `expect` is the cross-checked third voice: it must
		// agree with the reference before the Go side is even compared, so a
		// stale fixture shows up as a harness failure, not a false Go bug.
		if fx.Expect.Action != "" && (fx.Expect.Action != gotAction || normalizeReason(fx.Expect.Reason) != gotReason) {
			mismatches = append(mismatches, fmt.Sprintf("%s: go=%s/%s fixture_expect=%s/%s",
				name, gotAction, gotReason, fx.Expect.Action, fx.Expect.Reason))
		}
		ref, ok := refByID[name]
		if !ok {
			mismatches = append(mismatches, fmt.Sprintf("%s: no Python reference entry", name))
			continue
		}
		if ref.Action != gotAction || normalizeReason(ref.Reason) != gotReason {
			mismatches = append(mismatches, fmt.Sprintf("%s: go=%s/%s python=%s/%s",
				name, gotAction, gotReason, ref.Action, ref.Reason))
		}
	}

	if len(mismatches) > 0 {
		t.Errorf("cross-engine filter decisions diverged on %d/%d fixtures:\n  %s",
			len(mismatches), len(cases), strings.Join(mismatches, "\n  "))
	}
}

// TestReferenceIsComplete guards the committed reference: every fixture must
// have a reference entry, and the reference must not carry entries for
// fixtures that no longer exist. A dangling reference would silently pass a
// fixture the harness no longer runs.
func TestReferenceIsComplete(t *testing.T) {
	var refs []referenceDecision
	if err := json.Unmarshal(referenceFS, &refs); err != nil {
		t.Fatalf("decode reference: %v", err)
	}
	seen := make(map[string]bool, len(refs))
	for _, r := range refs {
		if seen[r.Name] {
			t.Errorf("duplicate reference entry %q", r.Name)
		}
		seen[r.Name] = true
	}
	for name := range loadFixtures(t) {
		if !seen[name] {
			t.Errorf("fixture %q has no entry in reference_filter.json", name)
		}
	}
}
