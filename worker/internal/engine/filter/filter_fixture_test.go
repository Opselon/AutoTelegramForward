// Package filter — the Go side of the shared Stage 4.0 fixture harness.
//
// This file runs the both-class FilterEngine.evaluate fixtures through the REAL
// production Go filter engine. There is no shadow implementation here: the
// harness calls filter.Engine.Evaluate, builds rule.FilterRule from the
// fixture's input.rule map, and compares the engine's decision to expected_go.
//
// What this is for: the Python engine and its Go port must decide identically,
// because a task whose filter disagrees across engines forwards a different set
// of messages depending on which engine picked the job up. The shared fixture
// file is the human-reviewable statement of what "identically" means; this
// harness is what makes that statement executable from the Go side.
//
// What this is NOT for: fixtures whose engine does not exist in Go
// (ContentPolicy, CleanerRule, apply_replacements, the pipeline-level gates).
// Those are reported UNSUPPORTED_IN_GO by the audit package's summary test —
// never skipped here, never reported as parity.
package filter

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"atf/worker/internal/domain/rule"
)

// fixtureRelPath is the repo-relative location of the shared fixture set.
const fixtureRelPath = "docs/behavior-audit/fixtures.json"

// filterEngineName is the fixture "engine" value this harness owns.
const filterEngineName = "FilterEngine.evaluate"

// FilterFixtureIDs is the both-class population this harness executes. The
// audit package's coverage summary asserts the same set from its side; this
// list asserts it again, locally, so a new filter fixture that does not reach
// this harness fails the filter suite itself rather than only a summary test
// in another package.
var FilterFixtureIDs = []string{
	"FLT-LEX-01", "FLT-LEX-02", "FLT-LEGACY-01", "FLT-LEGACY-02", "SVC-01",
}

// TestFilterFixtureSetIsExactlyWhatTheHarnessRuns asserts the fixture file's
// both-class FilterEngine.evaluate population is the set this harness runs. If
// the shared file grows a filter fixture, this fails until the harness grows
// with it — the point of a shared fixture set is that both sides run the same
// one, and a fixture only one harness executes is not a parity check.
func TestFilterFixtureSetIsExactlyWhatTheHarnessRuns(t *testing.T) {
	fixtures := loadFixtures(t)

	var got []string
	for i := range fixtures {
		fx := &fixtures[i]
		if fx.Engine != filterEngineName || fx.Executable != ExecutableBoth {
			continue
		}
		got = append(got, fx.ID)
	}
	if !sameSet(got, FilterFixtureIDs) {
		t.Errorf("the fixture file's both-class FilterEngine.evaluate set is %v, this harness runs %v",
			got, FilterFixtureIDs)
		return
	}
	for i := range fixtures {
		fx := &fixtures[i]
		if !listContains(FilterFixtureIDs, fx.ID) {
			continue
		}
		if fx.Engine != filterEngineName {
			t.Errorf("fixture %s is in the filter harness list but its engine is %q",
				fx.ID, fx.Engine)
		}
		if fx.Executable != ExecutableBoth {
			t.Errorf("fixture %s is in the filter harness list but its executable class is %q",
				fx.ID, fx.Executable)
		}
		if !fx.HasGoDecision() {
			t.Errorf("fixture %s is in the filter harness list but declares no expected_go decision",
				fx.ID)
		}
	}
}

// TestFilterEngineFixtures is the parity guard proper: every both-class filter
// fixture runs through the real engine, and any deviation from the expected Go
// decision fails with the fixture id, the engine, and expected vs actual.
//
// When a fixture's Python and Go expectations differ, the subtest is tagged
// with the finding id its notes cite (e.g. FLT-LEX-02//F-NN_divergence): the
// harness asserts the Go half and names the finding it evidences, so a failure
// is traceable straight into FINDINGS.md.
func TestFilterEngineFixtures(t *testing.T) {
	fixtures := loadFixtures(t)

	ran := 0
	for i := range fixtures {
		fx := &fixtures[i]
		if !listContains(FilterFixtureIDs, fx.ID) {
			continue
		}
		if !fx.HasGoDecision() {
			t.Fatalf("fixture %s is a filter fixture without an expected_go decision", fx.ID)
		}
		ran++
		t.Run(subtestName(fx), func(t *testing.T) {
			text := fx.Text(t)
			f := buildFilterRule(t, fx)
			got := New().Evaluate(text, mediaType(fx), isService(fx), f)

			if want := strings.ToUpper(strings.TrimSpace(fx.GoAction())); string(got.Action) != want {
				t.Errorf("fixture %s (engine %s): action = %s, want %s (reason %q)",
					fx.ID, fx.Engine, got.Action, want, got.Reason)
			}
			if want := fx.GoReason(); got.Reason != want {
				t.Errorf("fixture %s (engine %s): reason = %q, want %q",
					fx.ID, fx.Engine, got.Reason, want)
			}
		})
	}
	if ran != len(FilterFixtureIDs) {
		t.Errorf("ran %d filter subtests, expected %d (missing ids: %s)",
			ran, len(FilterFixtureIDs), strings.Join(missingIDs(fixtures), ", "))
	}
}

// missingIDs lists the harness ids the fixture file no longer carries, so a
// deletion in the shared file names what it lost instead of passing silently.
func missingIDs(fixtures []Fixture) []string {
	have := map[string]bool{}
	for i := range fixtures {
		have[fixtures[i].ID] = true
	}
	out := []string{}
	for _, id := range FilterFixtureIDs {
		if !have[id] {
			out = append(out, id)
		}
	}
	return out
}

// --- building the production FilterRule from a fixture ----------------------

// buildFilterRule constructs the production rule.FilterRule from the fixture's
// input.rule map. Only the fields the engine reads are populated, mirroring the
// projection the rulestore builds at delivery time; a rule key the struct has
// no field for is reported rather than silently dropped, because a fixture key
// the harness ignored is a divergence nobody would notice.
func buildFilterRule(t *testing.T, fx *Fixture) *rule.FilterRule {
	t.Helper()

	raw, hasRule := fx.Input["rule"]
	_, hasFilterRule := fx.Input["filter_rule"]
	if !hasRule && !hasFilterRule {
		t.Fatalf("fixture %s (engine %s): input carries neither rule nor filter_rule",
			fx.ID, fx.Engine)
	}
	if raw == nil {
		// SVC-01's shape: filter_rule is explicitly null. A nil rule is the
		// behaviour under test, not a missing input.
		return nil
	}
	m, ok := raw.(map[string]any)
	if !ok {
		t.Fatalf("fixture %s (engine %s): input.rule is %T, want object",
			fx.ID, fx.Engine, raw)
	}

	f := &rule.FilterRule{IsEnabled: true}
	var unknown []string
	for key, value := range m {
		switch key {
		case "blacklist_keywords":
			f.BlacklistKeywords = fixtureStrings(t, fx, value)
		case "whitelist_keywords":
			f.WhitelistKeywords = fixtureStrings(t, fx, value)
		case "regex_block":
			f.RegexBlock = fixtureStrings(t, fx, value)
		case "regex_require":
			f.RegexRequire = fixtureStrings(t, fx, value)
		case "regex_patterns":
			f.RegexPatterns = fixtureStrings(t, fx, value)
		case "blocked_media_types":
			f.BlockedMediaTypes = fixtureStrings(t, fx, value)
		case "allowed_media_types":
			f.AllowedMediaTypes = fixtureStrings(t, fx, value)
		case "drop_service_messages":
			f.DropService, _ = value.(bool)
		case "is_enabled":
			// A fixture may pin the flag; the default above is true, matching
			// a filter an owner saved and left switched on.
			f.IsEnabled, _ = value.(bool)
		case "min_message_length":
			f.MinMessageLength = fixtureInt(t, fx, value)
		case "max_message_length":
			f.MaxMessageLength = fixtureInt(t, fx, value)
		case "block_links":
			f.BlockLinks, _ = value.(bool)
		default:
			unknown = append(unknown, key)
		}
	}
	if len(unknown) > 0 {
		t.Fatalf("fixture %s (engine %s): input.rule carries keys the Go FilterRule has no field for: %s",
			fx.ID, fx.Engine, strings.Join(unknown, ", "))
	}
	return f
}

// fixtureStrings coerces a rule value into the []string a rule field needs.
// JSON null is an empty list; anything that is not a list of strings is a
// schema problem and fails with the fixture id.
func fixtureStrings(t *testing.T, fx *Fixture, value any) []string {
	t.Helper()
	if value == nil {
		return nil
	}
	list, ok := value.([]any)
	if !ok {
		t.Fatalf("fixture %s (engine %s): rule value is %T, want array of strings",
			fx.ID, fx.Engine, value)
	}
	out := make([]string, 0, len(list))
	for i, item := range list {
		s, ok := item.(string)
		if !ok {
			t.Fatalf("fixture %s (engine %s): rule array entry %d is %T, want string",
				fx.ID, fx.Engine, i, item)
		}
		out = append(out, s)
	}
	return out
}

// fixtureInt coerces a rule value into an int; JSON numbers arrive as float64.
func fixtureInt(t *testing.T, fx *Fixture, value any) int {
	t.Helper()
	switch v := value.(type) {
	case nil:
		return 0
	case float64:
		return int(v)
	case int:
		return v
	default:
		t.Fatalf("fixture %s (engine %s): numeric rule field is %T, want number",
			fx.ID, fx.Engine, value)
		return 0
	}
}

// mediaType returns the fixture's media type, defaulting to plain text: the
// engine lowercases it before comparison, and "text" is what a text-only
// fixture means.
func mediaType(fx *Fixture) string {
	if v, ok := fx.Input["media_type"].(string); ok {
		return v
	}
	return "text"
}

// isService returns the fixture's service-message flag.
func isService(fx *Fixture) bool {
	b, _ := fx.Input["is_service"].(bool)
	return b
}

// subtestName names a subtest with the fixture's finding tag when the fixture
// records a divergence: TestFilterEngineFixtures/FLT-LEX-02//F-03_divergence.
func subtestName(fx *Fixture) string {
	if fx.Divergent() {
		if finding := fx.FindingID(); finding != "" {
			return fx.ID + "//" + finding + "_divergence"
		}
	}
	return fx.ID
}

// listContains is the trivial membership check used by the harness bookkeeping.
func listContains(list []string, want string) bool {
	for _, item := range list {
		if item == want {
			return true
		}
	}
	return false
}

// sameSet compares two id lists disregarding order.
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

// --- shared fixture loading --------------------------------------------------
//
// The audit package owns the authoritative loader for the fixture contract.
// Its types live in that package's _test.go files, and Go does not allow a
// package to import another package's test-only build, so each engine harness
// carries the same small reader over the same file. The duplication is
// deliberate and bounded — one well-known file, one shape — and the audit
// package's classification tests are the gate that keeps the views from
// drifting: they assert the file's population, which this reader must decode
// identically to produce any subtests at all.

// ExecutableBoth is the routing class that produces parity verdicts.
const ExecutableBoth = "both"

// Fixture is the subset of a shared-fixture record an engine harness needs:
// identity, the routing class, and the Go expectation(s).
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
	expectedPythonOutput string
}

// loadFixtures reads and validates the shared fixture file, walking up from
// this test file to the repository root.
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
			ID                   string         `json:"id"`
			Engine               string         `json:"engine"`
			Group                string         `json:"group"`
			Description          string         `json:"description"`
			Notes                string         `json:"notes"`
			Executable           string         `json:"executable"`
			Input                map[string]any `json:"input"`
			ExpectedGo           map[string]any `json:"expected_go"`
			ExpectedGoOutput     string         `json:"expected_go_output"`
			ExpectedPythonOutput string         `json:"expected_python_output"`
		}
	}
	if err := json.Unmarshal(data, &file); err != nil {
		t.Fatalf("decode fixture file %s: %v", path, err)
	}
	if file.SchemaVersion == "" {
		t.Fatalf("fixture file %s: schema_version is missing", path)
	}

	out := make([]Fixture, 0, len(file.Fixtures))
	seen := map[string]bool{}
	for i := range file.Fixtures {
		raw := &file.Fixtures[i]
		if raw.ID == "" {
			t.Fatalf("fixture file %s: record %d has no id", path, i+1)
		}
		if seen[raw.ID] {
			t.Fatalf("fixture file %s: duplicate id %q", path, raw.ID)
		}
		seen[raw.ID] = true
		if raw.Executable == "" {
			t.Fatalf("fixture file %s: fixture %s has no executable class", path, raw.ID)
		}
		if raw.Input == nil {
			t.Fatalf("fixture file %s: fixture %s input is not an object", path, raw.ID)
		}
		out = append(out, Fixture{
			ID:                   raw.ID,
			Engine:               raw.Engine,
			Group:                raw.Group,
			Description:          raw.Description,
			Notes:                raw.Notes,
			Executable:           raw.Executable,
			Input:                raw.Input,
			ExpectedGo:           raw.ExpectedGo,
			ExpectedGoOutput:     raw.ExpectedGoOutput,
			expectedPythonOutput: raw.ExpectedPythonOutput,
		})
	}
	if len(out) == 0 {
		t.Fatalf("fixture file %s: no fixtures", path)
	}
	return out
}

// walkUpTo locates repoRoot/rel by walking up from the test file's directory,
// the same discovery rule the contract requires of every harness.
func walkUpTo(t *testing.T, rel string) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
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
	t.Fatalf("could not find %s walking up from %s", rel, dir)
	return ""
}

// Text returns the fixture's input text, the one input every engine fixture
// shares.
func (fx *Fixture) Text(t *testing.T) string {
	t.Helper()
	value, ok := fx.Input["text"]
	if !ok || value == nil {
		// SVC-01 carries no text — the service-message path does not read it.
		if _, has := fx.Input["is_service"]; has {
			return ""
		}
		t.Fatalf("fixture %s (engine %s): input has no text", fx.ID, fx.Engine)
	}
	s, ok := value.(string)
	if !ok {
		t.Fatalf("fixture %s (engine %s): input.text is %T, want string",
			fx.ID, fx.Engine, value)
	}
	return s
}

// HasGoDecision reports whether the fixture declares a Go action/reason pair.
func (fx *Fixture) HasGoDecision() bool { return len(fx.ExpectedGo) > 0 }

// HasGoOutput reports whether the fixture declares a Go output string.
func (fx *Fixture) HasGoOutput() bool { return fx.ExpectedGoOutput != "" }

// GoAction returns expected_go.action normalized to the engine's own
// uppercase vocabulary. The fixture file deliberately records Python's
// "ALLOW"/"DROP" and Go's "Allow"/"Drop" side by side; comparing on the
// normalized form keeps letter case from being mistaken for a divergence.
func (fx *Fixture) GoAction() string {
	if len(fx.ExpectedGo) == 0 {
		return ""
	}
	s, _ := fx.ExpectedGo["action"].(string)
	return strings.ToUpper(strings.TrimSpace(s))
}

// GoReason returns expected_go.reason.
func (fx *Fixture) GoReason() string {
	if len(fx.ExpectedGo) == 0 {
		return ""
	}
	s, _ := fx.ExpectedGo["reason"].(string)
	return s
}

// FindingID pulls the leading "F-NN" reference out of the notes, when the
// fixture cites one. Divergence subtests are tagged with it so a failure names
// the finding it evidences.
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

// Divergent reports whether a both-class fixture records differing Python and
// Go expectations. Those fixtures run with the finding id in the subtest name,
// and are never reported as clean parity.
func (fx *Fixture) Divergent() bool {
	if fx.Executable != ExecutableBoth {
		return false
	}
	py := fx.expectedPythonOutput
	return py != "" && fx.ExpectedGoOutput != "" && py != fx.ExpectedGoOutput
}

// Describe renders the fixture for a failure message: id, engine, and what was
// expected — every piece an operator needs to find the record in the shared
// file without re-reading the harness.
func (fx *Fixture) Describe() string {
	return fmt.Sprintf("%s (engine %s)", fx.ID, fx.Engine)
}
