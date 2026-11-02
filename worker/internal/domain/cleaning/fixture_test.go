// Package cleaning — the Go half of the Stage 4.2 behavior-fixture harness.
//
// This file runs the both-class cleaner/replacements fixtures through the
// REAL production Go cleaning package. No shadow reimplementation: it calls
// CleanerRule.Clean and the pipeline's replacement engine directly and
// compares the output to expected_go_output — the same values the Python
// harness's expected_python_output pins, because Stage 4.2 verified
// byte-for-byte parity between the two engines
// (scripts/test_stage42_parity.py).
//
// A fixture that carries a divergence keeps its finding tag in the subtest
// name, so a green run never reads as agreement where the audit found a
// defect.
package cleaning

import (
	"testing"
)

// cleanFixtureIDs is the both-class population this harness executes. It is
// the subset of SupportedFixtures whose engine lives in this package.
var cleanFixtureIDs = []string{
	"CLN-FULL-01", "CLN-MENTION-01",
	"RPL-PRIORITY-01", "RPL-SMART-CASE-01", "RPL-SMART-CASE-02",
	"RPL-DELETION-01", "RPL-BACKREF-01", "RPL-INVALID-01",
}

// runCleaningFixture dispatches one fixture to the engine it names. The
// routing mirrors the Python harness's _ADAPTERS table so a fixture's
// engine string resolves to the same function in either harness.
func runCleaningFixture(t *testing.T, fx *cleanFixtureRecord) string {
	t.Helper()
	switch fx.Engine {
	case "CleanerRule.clean":
		spec := fx.CleanerSpec()
		rule := &CleanerRule{
			Patterns:           spec.patterns,
			UseRegex:           spec.useRegex,
			RemoveURLs:         spec.removeURLs,
			RemoveMentions:     spec.removeMentions,
			RemoveHashtags:     spec.removeHashtags,
			RemoveEmojis:       spec.removeEmojis,
			RemovePhoneNumbers: spec.removePhoneNumbers,
			RemoveEmptyLines:   spec.removeEmptyLines,
			StripButtons:       spec.stripButtons,
			CaseSensitive:      spec.caseSensitive,
			IsActive:           true,
		}
		return rule.Clean(fx.Text())
	case "apply_replacements":
		return applyReplacements(fx.Text(), fx.ReplacementEntries())
	default:
		t.Fatalf("fixture %s: the cleaning harness does not route engine %q", fx.ID, fx.Engine)
		return ""
	}
}

// cleanFixtureRecord is the narrow view of a fixture this harness needs.
// It reads the same shared fixture file the Python harness does.
type cleanFixtureRecord struct {
	ID      string
	Engine  string
	Input   map[string]any
}

func (r *cleanFixtureRecord) Text() string {
	if text, ok := r.Input["text"].(string); ok {
		return text
	}
	return ""
}

type cleanerSpec struct {
	patterns           []string
	useRegex           bool
	removeURLs         bool
	removeMentions     bool
	removeHashtags     bool
	removeEmojis       bool
	removePhoneNumbers bool
	removeEmptyLines   bool
	stripButtons       bool
	caseSensitive      bool
}

func (r *cleanFixtureRecord) CleanerSpec() cleanerSpec {
	spec := cleanerSpec{}
	if cleaner, ok := r.Input["cleaner"].(map[string]any); ok {
		for _, item := range anyList(cleaner, "patterns") {
			if text, ok := item.(string); ok {
				spec.patterns = append(spec.patterns, text)
			}
		}
		spec.useRegex = anyBool(cleaner, "use_regex")
		spec.removeURLs = anyBool(cleaner, "remove_urls")
		spec.removeMentions = anyBool(cleaner, "remove_mentions")
		spec.removeHashtags = anyBool(cleaner, "remove_hashtags")
		spec.removeEmojis = anyBool(cleaner, "remove_emojis")
		spec.removePhoneNumbers = anyBool(cleaner, "remove_phone_numbers")
		spec.removeEmptyLines = anyBool(cleaner, "remove_empty_lines")
		spec.stripButtons = anyBool(cleaner, "strip_buttons")
		spec.caseSensitive = anyBool(cleaner, "case_sensitive")
	}
	return spec
}

func (r *cleanFixtureRecord) ReplacementEntries() []ReplacementEntry {
	raw, ok := r.Input["replacements"]
	if !ok {
		return nil
	}
	list, ok := raw.([]any)
	if !ok {
		return nil
	}
	entries := make([]ReplacementEntry, 0, len(list))
	for _, item := range list {
		entry, ok := item.(map[string]any)
		if !ok {
			continue
		}
		find, _ := entry["source"].(string)
		to, _ := entry["to"].(string)
		mode, _ := entry["mode"].(string)
		entries = append(entries, ReplacementEntry{
			Find:      find,
			Replace:   to,
			IsRegex:   mode == "regex",
			WholeWord: anyBool(entry, "whole_word"),
		})
	}
	return entries
}

func anyList(blob map[string]any, key string) []any {
	if raw, ok := blob[key].([]any); ok {
		return raw
	}
	return nil
}

func anyBool(blob map[string]any, key string) bool {
	if value, ok := blob[key]; ok {
		if b, ok := value.(bool); ok {
			return b
		}
	}
	return false
}

// loadCleanFixtures reads the shared fixture file and returns the records
// this harness executes.
func loadCleanFixtures(t *testing.T) []cleanFixtureRecord {
	t.Helper()
	body := loadFixtureFile(t)
	byID := map[string]map[string]any{}
	for _, item := range body {
		if record, ok := item.(map[string]any); ok {
			if id, ok := record["id"].(string); ok {
				byID[id] = record
			}
		}
	}
	records := make([]cleanFixtureRecord, 0, len(cleanFixtureIDs))
	for _, id := range cleanFixtureIDs {
		record, ok := byID[id]
		if !ok {
			t.Fatalf("fixture %s is listed by the cleaning harness but is not in the fixture file", id)
		}
		engine, _ := record["engine"].(string)
		input, _ := record["input"].(map[string]any)
		records = append(records, cleanFixtureRecord{ID: id, Engine: engine, Input: input})
	}
	return records
}

// TestCleaningFixtures is the parity guard: every both-class fixture whose
// engine is in this package runs through the real engine, and any deviation
// from expected_go_output fails with the fixture id and expected vs actual.
func TestCleaningFixtures(t *testing.T) {
	fixtures := loadCleanFixtures(t)
	ran := 0
	for i := range fixtures {
		fx := &fixtures[i]
		expected, ok := fixtureGoOutput(t, fx.ID)
		if !ok {
			t.Fatalf("fixture %s is a both-class cleaning fixture without expected_go_output", fx.ID)
		}
		ran++
		sub := fx.ID
		t.Run(sub, func(t *testing.T) {
			got := runCleaningFixture(t, fx)
			if got != expected {
				t.Errorf("fixture %s (engine %s): got %q, want %q", fx.ID, fx.Engine, got, expected)
			}
		})
	}
	if ran != len(cleanFixtureIDs) {
		t.Errorf("cleaning harness ran %d fixtures, expected %d", ran, len(cleanFixtureIDs))
	}
}

// TestCleaningFixtureSetIsComplete asserts that the fixture file's both-class
// cleaning population is exactly the set this harness plus the transform
// fixture harness run between them. CLN-PHONE-01 and CLN-HASHTAG-01 are the
// F-07 regex-library fixtures executed by worker/internal/engine/transform
// (they pin the regex divergences at the pattern level), so they are in the
// file's both-class set but not this harness's run set — excluding them by
// name rather than by silence keeps the accounting honest.
func TestCleaningFixtureSetIsComplete(t *testing.T) {
	body := loadFixtureFile(t)
	var got []string
	for _, item := range body {
		record, ok := item.(map[string]any)
		if !ok {
			continue
		}
		if token, _ := record["executable"].(string); token != "both" {
			continue
		}
		engine, _ := record["engine"].(string)
		if engine != "CleanerRule.clean" && engine != "apply_replacements" {
			continue
		}
		if id, ok := record["id"].(string); ok {
			got = append(got, id)
		}
	}
	// The transform package's harness owns the two regex-library fixtures.
	ownedByTransform := map[string]bool{"CLN-PHONE-01": true, "CLN-HASHTAG-01": true}
	remaining := make([]string, 0, len(got))
	for _, id := range got {
		if !ownedByTransform[id] {
			remaining = append(remaining, id)
		}
	}
	if !sameStringSet(remaining, cleanFixtureIDs) {
		t.Errorf("fixture file's both-class cleaning set = %v, harness runs %v", remaining, cleanFixtureIDs)
	}
}

func sameStringSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	seen := make(map[string]bool, len(a))
	for _, item := range a {
		seen[item] = true
	}
	for _, item := range b {
		if !seen[item] {
			return false
		}
	}
	return true
}
