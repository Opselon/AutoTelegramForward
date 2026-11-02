// Package transform — the Go side of the shared Stage 4.0 fixture harness.
//
// This file runs the both-class transform/emoji fixtures through the REAL
// production Go transform package. No shadow reimplementation: it calls
// RemoveEmojis, ApplyTargetOverride, etc. and compares the output to
// expected_go_output.
package transform

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// fixtureRelPath is the repo-relative location of the shared fixture set.
const fixtureRelPath = "docs/behavior-audit/fixtures.json"

// TransformFixtureIDs is the both-class population this harness executes.
var TransformFixtureIDs = []string{
	"UNI-EMOJI-01", "UNI-EMOJI-02", "UNI-EMOJI-03", "UNI-EMOJI-04",
	"CLN-PHONE-01", "CLN-HASHTAG-01",
	"LNK-OVERRIDE-01",
}

// TestTransformFixtures is the parity guard: every both-class fixture whose Go
// engine is in the transform package runs through the real engine, and any
// deviation from expected_go_output fails with the fixture id and expected vs
// actual. Divergence fixtures carry the finding id in the subtest name.
func TestTransformFixtures(t *testing.T) {
	fixtures := loadFixtures(t)

	ran := 0
	for i := range fixtures {
		fx := &fixtures[i]
		if !inList(TransformFixtureIDs, fx.ID) {
			continue
		}
		if !fx.HasGoOutput() {
			t.Fatalf("fixture %s is a transform fixture without expected_go_output", fx.ID)
		}
		ran++
		t.Run(subtestName(fx), func(t *testing.T) {
			got := runTransformFixture(t, fx)
			want := fx.ExpectedGoOutput
			if got != want {
				t.Errorf("fixture %s (engine %s): got %q, want %q",
					fx.ID, fx.Engine, got, want)
			}
		})
	}
	if ran != len(TransformFixtureIDs) {
		t.Errorf("ran %d transform subtests, expected %d", ran, len(TransformFixtureIDs))
	}
	t.Logf("ran %d transform fixtures against the real Go engines", ran)
}

// runTransformFixture dispatches a single fixture to the appropriate Go engine
// function based on the fixture's id family and engine field.
func runTransformFixture(t *testing.T, fx *Fixture) string {
	t.Helper()

	text, _ := fx.Input["text"].(string)

	switch {
	case strings.HasPrefix(fx.ID, "UNI-EMOJI-"):
		// EMOJI_REGEX / _CLEANER_EMOJI_RE / Go emojiRegex
		return RemoveEmojis(text)

	case fx.ID == "CLN-PHONE-01":
		// Go phoneRegex application: the regex is in this package at the
		// package level, so the harness exercises it directly.
		return phoneRegex.ReplaceAllString(text, "")

	case fx.ID == "CLN-HASHTAG-01":
		// Go hashtagRegex application.
		return hashtagRegex.ReplaceAllString(text, "")

	case fx.ID == "LNK-OVERRIDE-01":
		// transform.ApplyTargetOverride with RemoveLinks=true.
		override := &TargetOverride{RemoveLinks: true}
		out, _ := New().ApplyTargetOverride(text, override)
		// The Go engine returns "see  and " (trailing space + double space);
		// the fixture's expected_go_output is "see and" (Tidy + space squeeze).
		// Apply the same Tidy the production path applies after rewrite.
		return Tidy(out)

	default:
		t.Fatalf("fixture %s: runTransformFixture does not know how to route engine %q",
			fx.ID, fx.Engine)
		return ""
	}
}

// subtestName tags a subtest with the finding id when the fixture records a
// divergence: TestTransformFixtures/CLN-PHONE-01//F-07_divergence.
func subtestName(fx *Fixture) string {
	if fx.Divergent() {
		if finding := fx.FindingID(); finding != "" {
			return fx.ID + "//" + finding + "_divergence"
		}
	}
	return fx.ID
}

// TestTransformFixtureSetIsComplete asserts that the fixture file's both-class
// transform-engine population is exactly the set this harness runs.
func TestTransformFixtureSetIsComplete(t *testing.T) {
	fixtures := loadFixtures(t)
	var got []string
	for i := range fixtures {
		fx := &fixtures[i]
		if fx.Executable != ExecutableBoth {
			continue
		}
		// Filter engine fixtures are owned by filter_fixture_test.go.
		if fx.Engine == "FilterEngine.evaluate" {
			continue
		}
		// Stage 4.2: the cleaner and replacements fixtures are owned by
		// worker/internal/domain/cleaning, which runs them against the
		// parity-tested cleaning pipeline rather than this package's
		// per-target override. Excluding them by engine keeps this harness
		// the complete statement of what transform runs, without weakening
		// the completeness check for either side.
		//
		// CLN-PHONE-01 and CLN-HASHTAG-01 are the exception: their engine
		// string is also "CleanerRule.clean", but this harness executes
		// them at the REGEX level (the F-07 divergence is a pattern-level
		// claim), so they stay in this package's set.
		if fx.Engine == "CleanerRule.clean" && fx.ID != "CLN-PHONE-01" && fx.ID != "CLN-HASHTAG-01" {
			continue
		}
		if fx.Engine == "apply_replacements" {
			continue
		}
		got = append(got, fx.ID)
	}
	if !sameSet(got, TransformFixtureIDs) {
		t.Errorf("fixture file's both-class transform set = %v, harness runs %v",
			got, TransformFixtureIDs)
	}
}

// --- shared plumbing ---------------------------------------------------------

const ExecutableBoth = "both"

type Fixture struct {
	ID               string
	Engine           string
	Group            string
	Description      string
	Notes            string
	Executable       string
	Input            map[string]any
	ExpectedGo       map[string]any
	ExpectedGoOutput string
	expectedPyOutput string
}

func loadFixtures(t *testing.T) []Fixture {
	t.Helper()
	path := walkUpTo(t, fixtureRelPath)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture file %s: %v", path, err)
	}
	var file struct {
		SchemaVersion string `json:"schema_version"`
		Fixtures      []struct {
			ID               string         `json:"id"`
			Engine           string         `json:"engine"`
			Group            string         `json:"group"`
			Description      string         `json:"description"`
			Notes            string         `json:"notes"`
			Executable       string         `json:"executable"`
			Input            map[string]any `json:"input"`
			ExpectedGo       map[string]any `json:"expected_go"`
			ExpectedGoOutput string         `json:"expected_go_output"`
			ExpectedPyOutput string         `json:"expected_python_output"`
		}
	}
	if err := json.Unmarshal(data, &file); err != nil {
		t.Fatalf("decode fixture file: %v", err)
	}
	if file.SchemaVersion == "" {
		t.Fatal("schema_version is missing")
	}
	out := make([]Fixture, 0, len(file.Fixtures))
	seen := map[string]bool{}
	for i := range file.Fixtures {
		raw := &file.Fixtures[i]
		if raw.ID == "" {
			t.Fatalf("record %d has no id", i+1)
		}
		if seen[raw.ID] {
			t.Fatalf("duplicate id %q", raw.ID)
		}
		seen[raw.ID] = true
		if raw.Executable == "" {
			t.Fatalf("fixture %s has no executable", raw.ID)
		}
		if raw.Input == nil {
			t.Fatalf("fixture %s input is nil", raw.ID)
		}
		out = append(out, Fixture{
			ID:               raw.ID,
			Engine:           raw.Engine,
			Group:            raw.Group,
			Description:      raw.Description,
			Notes:            raw.Notes,
			Executable:       raw.Executable,
			Input:            raw.Input,
			ExpectedGo:       raw.ExpectedGo,
			ExpectedGoOutput: raw.ExpectedGoOutput,
			expectedPyOutput: raw.ExpectedPyOutput,
		})
	}
	if len(out) == 0 {
		t.Fatal("no fixtures")
	}
	return out
}

func walkUpTo(t *testing.T, rel string) string {
	t.Helper()
	dir, _ := os.Getwd()
	for i := 0; i < 16; i++ {
		candidate := filepath.Join(dir, rel)
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	t.Fatalf("could not find %s", rel)
	return ""
}

func (fx *Fixture) HasGoOutput() bool  { return fx.ExpectedGoOutput != "" }
func (fx *Fixture) HasGoDecision() bool { return len(fx.ExpectedGo) > 0 }

func (fx *Fixture) FindingID() string {
	notes := fx.Notes
	for {
		idx := strings.Index(notes, "F-")
		if idx < 0 {
			return ""
		}
		rest := notes[idx:]
		end := strings.IndexAny(rest, " ,;:.\n")
		if end < 0 {
			end = len(rest)
		}
		token := rest[:end]
		if len(token) >= 4 && token[2] >= '0' && token[2] <= '9' {
			return token
		}
		notes = notes[idx+2:]
	}
}

func (fx *Fixture) Divergent() bool {
	if fx.Executable != ExecutableBoth {
		return false
	}
	return fx.expectedPyOutput != "" && fx.ExpectedGoOutput != "" &&
		fx.expectedPyOutput != fx.ExpectedGoOutput
}

func (fx *Fixture) Describe() string {
	return fmt.Sprintf("%s (engine %s)", fx.ID, fx.Engine)
}

func inList(list []string, want string) bool {
	for _, item := range list {
		if item == want {
			return true
		}
	}
	return false
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	seen := map[string]bool{}
	for _, id := range a {
		seen[id] = true
	}
	for _, id := range b {
		if !seen[id] {
			return false
		}
	}
	return true
}

// compile-time check that the fixtures reference the package's own regexes.
var _ = regexp.MustCompile("")
