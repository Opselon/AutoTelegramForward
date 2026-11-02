package cleaning

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fixture_load_test.go — shared loading for the cleaning fixture harness.
//
// The fixture file (docs/behavior-audit/fixtures.json) is the one
// characterization contract both language harnesses read. These helpers
// load it once per test call; they live in a _test.go file on purpose —
// nothing outside the test suite may depend on the fixture format. The
// walk-up locator is the same one worker/internal/audit uses, so both
// harnesses find the same file from any package depth.

// fixtureRelPath is the repo-relative location of the shared fixture set.
const fixtureRelPath = "docs/behavior-audit/fixtures.json"

// findFixtureFile locates repoRoot/docs/behavior-audit/fixtures.json by
// walking up from the test binary's working directory.
func findFixtureFile(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for i := 0; i < 16; i++ {
		if info, err := os.Stat(filepath.Join(dir, fixtureRelPath)); err == nil && !info.IsDir() {
			return filepath.Join(dir, fixtureRelPath)
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	t.Fatalf("fixture file %s not found walking up from %s", fixtureRelPath, os.Getenv("PWD"))
	return ""
}

// loadFixtureFile returns the fixture array from the shared file.
func loadFixtureFile(t *testing.T) []any {
	t.Helper()
	body, err := os.ReadFile(findFixtureFile(t))
	if err != nil {
		t.Fatalf("read fixture file: %v", err)
	}
	var doc struct {
		Fixtures []any `json:"fixtures"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatalf("parse fixture file: %v", err)
	}
	if len(doc.Fixtures) == 0 {
		t.Fatal("fixture file carries no fixtures")
	}
	return doc.Fixtures
}

// fixtureGoOutput returns the expected_go_output of one fixture by id, and
// whether the fixture actually declares one.
func fixtureGoOutput(t *testing.T, id string) (string, bool) {
	t.Helper()
	for _, item := range loadFixtureFile(t) {
		record, ok := item.(map[string]any)
		if !ok {
			continue
		}
		if got, _ := record["id"].(string); got != id {
			continue
		}
		expected, ok := record["expected_go_output"].(string)
		if !ok {
			return "", false
		}
		return expected, true
	}
	t.Fatalf("fixture %s is not in the fixture file", id)
	return "", false
}

// fixtureDivergent reports whether a fixture documents a Python/Go
// divergence (a finding id in its notes).
func fixtureDivergent(t *testing.T, id string) bool {
	t.Helper()
	for _, item := range loadFixtureFile(t) {
		record, ok := item.(map[string]any)
		if !ok {
			continue
		}
		if got, _ := record["id"].(string); got == id {
			notes, _ := record["notes"].(string)
			return strings.Contains(notes, "F-")
		}
	}
	return false
}
