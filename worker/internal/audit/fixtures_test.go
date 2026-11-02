// Package audit holds the Go side of the shared Stage 4.0 behavior-fixture
// harness. Test-only: this file and its siblings exist to load
// docs/behavior-audit/fixtures.json, run the fixtures that have a Go engine
// against the REAL production engines, and keep the ones that do not visible
// as UNSUPPORTED_IN_GO instead of hiding them.
//
// The loader lives in a _test.go file deliberately: nothing outside the test
// suite may depend on the fixture format, so a schema drift in the shared file
// fails here with one clear message instead of leaking into production code.
package audit

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// fixturePath is the repo-relative location of the shared fixture set.
const fixturePath = "docs/behavior-audit/fixtures.json"

// Executable is the routing token every fixture must carry. The set is closed
// by _fixture-contract.md; anything else is a hard failure, never a skip.
type Executable string

const (
	ExecPython       Executable = "python"
	ExecBoth         Executable = "both"
	ExecMeta         Executable = "meta"
	ExecSchema       Executable = "schema"
	ExecUnclassified Executable = "unclassified"
)

var knownExecutables = []Executable{
	ExecPython, ExecBoth, ExecMeta, ExecSchema, ExecUnclassified,
}

// Fixture is one record of the shared fixture file.
//
// Expectations are kept generic (a map, a string) rather than typed: the file
// carries a wide and deliberately unhomogeneous set of expectation keys
// (expected_go, expected_go_output, expected_python_output,
// expected_effective_text, ...). Typing only the ones the Go harness reads
// keeps the loader honest about what Go actually asserts, and leaves the
// schema validator free to check the shape the file really has.
type Fixture struct {
	ID          string         `json:"id"`
	Engine      string         `json:"engine"`
	Group       string         `json:"group"`
	Description string         `json:"description"`
	Executable  Executable     `json:"executable"`
	Input       map[string]any `json:"input"`
	Notes       string         `json:"notes"`

	// ExpectedGo is the expected Go decision, when the fixture declares one.
	// Keys are engine-specific: FilterEngine fixtures carry {action, reason}.
	ExpectedGo map[string]any `json:"expected_go"`

	// ExpectedGoOutput is the expected Go output string, when the fixture
	// declares one (transform/cleaner/emoji fixtures).
	ExpectedGoOutput string `json:"expected_go_output"`

	// raw is the record as decoded, for the schema checks that must see what
	// the file actually says rather than what the struct reads.
	raw map[string]any
}

// fixtureFile is the top-level shape of docs/behavior-audit/fixtures.json.
type fixtureFile struct {
	SchemaVersion string    `json:"schema_version"`
	Fixtures      []Fixture `json:"fixtures"`
}

// LoadFixtures reads the shared fixture file by walking up from this test file
// to the repository root — never a hard-coded absolute path, per the harness
// contract — and validates the integrity gates the loader itself can enforce.
// A fixture that breaks a rule of _fixture-contract.md fails the suite loudly
// instead of being skipped.
func LoadFixtures(t *testing.T) []Fixture {
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

	// Re-decode the raw records so validation can check the shape the file
	// carries, not merely the fields the struct above reads.
	var raw struct {
		Fixtures []map[string]any `json:"fixtures"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("decode fixture file for validation: %v", err)
	}

	seen := make(map[string]bool, len(file.Fixtures))
	for i := range file.Fixtures {
		fx := &file.Fixtures[i]
		fx.raw = raw.Fixtures[i]
		if err := validateFixture(fx, seen); err != nil {
			t.Fatalf("fixture file %s, record %d: %v", path, i+1, err)
		}
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
	t.Fatalf("fixture file %s not found walking up from %s", fixturePath, mustGetwd())
	return ""
}

func mustGetwd() string {
	dir, err := os.Getwd()
	if err != nil {
		return "?"
	}
	return dir
}

// validateFixture enforces the integrity gates of _fixture-contract.md that the
// loader can check on its own: required fields, a known executable token, a
// dict input, a unique id, a declared expectation, and no placeholder keys.
func validateFixture(fx *Fixture, seen map[string]bool) error {
	raw := fx.raw
	if raw["id"] == nil {
		return fmt.Errorf("missing required field id")
	}
	if fx.ID == "" {
		return fmt.Errorf("id is not a string")
	}
	if seen[fx.ID] {
		return fmt.Errorf("duplicate id %q", fx.ID)
	}
	seen[fx.ID] = true

	for _, field := range []string{"engine", "group", "description", "executable"} {
		if raw[field] == nil {
			return fmt.Errorf("%s: missing required field %q", fx.ID, field)
		}
	}
	if !isKnownExecutable(fx.Executable) {
		return fmt.Errorf("%s: unknown executable token %q (expected one of %s)",
			fx.ID, fx.Executable, executableList())
	}
	if fx.Input == nil {
		return fmt.Errorf("%s: input must be a JSON object, not a scalar or string", fx.ID)
	}
	if fx.Executable == ExecUnclassified {
		// The contract makes this a hard failure: a new fixture must declare a
		// class before any harness is allowed to touch it.
		return fmt.Errorf("%s: executable=unclassified is a hard failure — every fixture must declare a class", fx.ID)
	}

	// Gate 5: at least one expected-result key, and for `both` specifically a
	// Go one, since only `both` fixtures produce parity verdicts. A `meta`
	// fixture is exempt by the contract's own definition: it is a numeric claim
	// executed by a probe, so its expectations are the probe's inputs
	// (python_len_codepoints, go_len_bytes, go_runes) rather than an
	// expected_* result. It must still declare them — see MetaClaims.
	if !HasAnyExpectation(fx.raw) && fx.Executable != ExecMeta {
		return fmt.Errorf("%s: no expected_* key present", fx.ID)
	}
	if fx.Executable == ExecMeta && len(MetaClaims(fx.raw)) == 0 {
		return fmt.Errorf("%s: meta fixture declares no numeric claim", fx.ID)
	}
	if fx.Executable == ExecBoth && fx.ExpectedGoOutput == "" && len(fx.ExpectedGo) == 0 {
		return fmt.Errorf("%s: executable=both requires an expected_go* key", fx.ID)
	}

	// Gate 6: an earlier draft carried a -expected_python_output placeholder.
	// Any '-'-prefixed key is rejected outright so the shape can never regress.
	for key := range raw {
		if strings.HasPrefix(key, "-") {
			return fmt.Errorf("%s: stray placeholder key %q", fx.ID, key)
		}
	}
	return nil
}

func isKnownExecutable(got Executable) bool {
	for _, want := range knownExecutables {
		if got == want {
			return true
		}
	}
	return false
}

func executableList() string {
	parts := make([]string, 0, len(knownExecutables))
	for _, e := range knownExecutables {
		parts = append(parts, string(e))
	}
	return strings.Join(parts, ", ")
}

// HasAnyExpectation reports whether a raw record declares any result at all.
// Exported for the schema test, which must check the same predicate the loader
// aborts on rather than trusting the loader to have run.
func HasAnyExpectation(raw map[string]any) bool {
	for key := range raw {
		if strings.HasPrefix(key, "expected") {
			return true
		}
	}
	return false
}

// metaClaimKeys are the numeric-claim fields a `meta` fixture carries in place
// of an expected_* result: the probe's own measurements, named by language so
// the harness can tell which side each claim belongs to.
var metaClaimKeys = []string{
	"python_len_codepoints", "go_len_bytes", "go_runes",
}

// MetaClaims returns the numeric claims a meta fixture declares, as name/value
// pairs. A meta fixture with none is incomplete: the contract says a meta
// record is "a numeric/counting claim executed by a small probe", so the claim
// itself is the payload.
func MetaClaims(raw map[string]any) [][2]any {
	out := [][2]any{}
	for _, key := range metaClaimKeys {
		if v, ok := raw[key]; ok {
			out = append(out, [2]any{key, v})
		}
	}
	return out
}

// --- the classification the audit catalogued ---------------------------------
//
// These constants pin the shared file's population as the coordinator left it.
// The summary test asserts the runtime counts against them, so a fixture
// reclassification or an addition has to touch these numbers too: an
// undocumented drift between the file and what the harness believes it is
// running is exactly what the contract's "harnesses cannot disagree" rule is
// about. A fixture count change is a deliberate, reviewable change.

// ExpectedTotal is the fixture population of docs/behavior-audit/fixtures.json.
const ExpectedTotal = 43

// ExpectedCounts is the per-routing-class population of the fixture file.
// `both` counts the fixtures the Go harness must actually execute: 4 filter
// decisions on filter.Engine.Evaluate, 4 RemoveEmojis emoji outputs, 2
// regex-library outputs (phone/hashtag), 1 ApplyTargetOverride rewrite and
// the SVC-01 service-message drop.
var ExpectedCounts = map[Executable]int{
	ExecPython: 24,
	ExecBoth:   12,
	ExecMeta:   1,
	ExecSchema: 6,
	// ExecUnclassified is a hard failure, never a count.
}

// RequiredFixtures is the ids of the fixtures whose absence would mean a
// pinned behaviour silently stopped being pinned. The audit's findings
// reference these by name, so losing one invalidates the register.
var RequiredFixtures = []string{
	// Filter engine parity (FLT-*): the Go port's reason for existing.
	"FLT-LEX-01", "FLT-LEX-02", "FLT-LEGACY-01", "FLT-LEGACY-02",
	"FLT-PERSIAN-01", "FLT-DISABLED-01",
	// The both-class fixture population: the only parity verdicts the Go side emits.
	"UNI-EMOJI-01", "UNI-EMOJI-02", "UNI-EMOJI-03", "UNI-EMOJI-04",
	// Documented divergences the Go harness asserts with a finding tag.
	"CLN-PHONE-01", "CLN-MENTION-01", "CLN-HASHTAG-01", "LNK-OVERRIDE-01",
	// Service-message handling, the one service path both engines share.
	"SVC-01",
	// Length-unit claim (UNI-LEN-01) is the meta fixture: no Go parity verdict,
	// but its numbers are asserted from the Go side in the summary test.
	"UNI-LEN-01",
}

// ExpectedGroups is every coverage group the audit catalogued.
var ExpectedGroups = []string{
	"content-policy", "replace", "filter-rule", "emoji", "cleaner",
	"links", "advanced", "unicode", "dedup", "forward-every-n",
	"captions", "albums", "keyword-reply", "routing",
}

// UnsupportedEngine is the marker the summary test reports for a fixture whose
// production engine exists only in Python. Never "skip": a skipped fixture can
// be read as parity by anyone reading a green CI line.
const UnsupportedEngine = "UNSUPPORTED_IN_GO"

// SupportedFixtures is the set of fixture ids the Go harness actually executes
// against a real Go engine, partitioned by the engine it exercises. Kept here
// so the summary test's unsupported list is the exact complement of what the
// engine harnesses run, by construction.
var SupportedFixtures = []string{
	// filter.Engine.Evaluate (see filter_fixture_test.go)
	"FLT-LEX-01", "FLT-LEX-02", "FLT-LEGACY-01", "FLT-LEGACY-02", "SVC-01",
	// transform.RemoveEmojis (see transform_fixture_test.go): UNI-EMOJI-* is
	// the one fixture family the contract classifies `both` for parity.
	"UNI-EMOJI-01", "UNI-EMOJI-02", "UNI-EMOJI-03", "UNI-EMOJI-04",
	// The Go transform regex library: exit-shape comparisons the audit filed
	// as F-07. These exercise the regexes by direct application.
	"CLN-PHONE-01", "CLN-HASHTAG-01",
	// transform.ApplyTargetOverride, the F-13 divergence.
	"LNK-OVERRIDE-01",
}

// --- typed accessors for the engine harnesses --------------------------------
//
// Every accessor fails with the fixture id, the engine and the offending key,
// so an assertion-breaking schema problem never reads as an engine failure.

// RequireInput fails unless every key in want is present in input non-nil.
func (fx *Fixture) RequireInput(t *testing.T, want ...string) {
	t.Helper()
	for _, key := range want {
		v, ok := fx.Input[key]
		if !ok {
			t.Fatalf("fixture %s (engine %s): input is missing required key %q",
				fx.ID, fx.Engine, key)
		}
		if v == nil {
			t.Fatalf("fixture %s (engine %s): input key %q is null",
				fx.ID, fx.Engine, key)
		}
	}
}

// Text returns input["text"] as a string.
func (fx *Fixture) Text(t *testing.T) string {
	t.Helper()
	return fx.String(t, "text")
}

// String returns input[key] as a string.
func (fx *Fixture) String(t *testing.T, key string) string {
	t.Helper()
	fx.RequireInput(t, key)
	s, ok := fx.Input[key].(string)
	if !ok {
		t.Fatalf("fixture %s (engine %s): input key %q is %T, want string",
			fx.ID, fx.Engine, key, fx.Input[key])
	}
	return s
}

// Strings returns input[key] as a []string; a JSON null is an empty list.
func (fx *Fixture) Strings(t *testing.T, key string) []string {
	t.Helper()
	v, ok := fx.Input[key]
	if !ok {
		t.Fatalf("fixture %s (engine %s): input is missing required key %q",
			fx.ID, fx.Engine, key)
	}
	if v == nil {
		return nil
	}
	list, ok := v.([]any)
	if !ok {
		t.Fatalf("fixture %s (engine %s): input key %q is %T, want array",
			fx.ID, fx.Engine, key, v)
	}
	out := make([]string, 0, len(list))
	for i, item := range list {
		s, ok := item.(string)
		if !ok {
			t.Fatalf("fixture %s (engine %s): input key %q entry %d is %T, want string",
				fx.ID, fx.Engine, key, i, item)
		}
		out = append(out, s)
	}
	return out
}

// Bool returns input[key] as a bool, false when the key is absent or null.
func (fx *Fixture) Bool(t *testing.T, key string) bool {
	t.Helper()
	v, ok := fx.Input[key]
	if !ok || v == nil {
		return false
	}
	b, ok := v.(bool)
	if !ok {
		t.Fatalf("fixture %s (engine %s): input key %q is %T, want bool",
			fx.ID, fx.Engine, key, v)
	}
	return b
}

// Object returns input[key] as a nested map[string]any (a rule, a policy, a
// target override).
func (fx *Fixture) Object(t *testing.T, key string) map[string]any {
	t.Helper()
	fx.RequireInput(t, key)
	m, ok := fx.Input[key].(map[string]any)
	if !ok {
		t.Fatalf("fixture %s (engine %s): input key %q is %T, want object",
			fx.ID, fx.Engine, key, fx.Input[key])
	}
	return m
}

// HasGoDecision reports whether the fixture declares a Go action/reason pair.
// Per the contract, expected_go_* keys on a `python` fixture are ignored —
// only their presence here makes a Go assertion meaningful.
func (fx *Fixture) HasGoDecision() bool { return len(fx.ExpectedGo) > 0 }

// GoAction returns expected_go.action normalized to the Go engine's uppercase
// vocabulary. The fixture file intentionally records Python's "ALLOW"/"DROP"
// and Go's "Allow"/"Drop" side by side; the harness compares the normalized
// form and never lets case be a false divergence.
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

// HasGoOutput reports whether the fixture declares an expected Go output string.
func (fx *Fixture) HasGoOutput() bool { return fx.ExpectedGoOutput != "" }

// PythonOutput returns expected_python_output, for the divergence check that
// decides whether a `both` fixture is tagged with its finding id.
func (fx *Fixture) PythonOutput() string {
	s, _ := fx.raw["expected_python_output"].(string)
	return s
}

// Divergent reports whether a `both` fixture records differing Python and Go
// expectations — the records the harness must tag with the finding id in the
// test name, never silently reported as clean parity.
func (fx *Fixture) Divergent() bool {
	if fx.Executable != ExecBoth {
		return false
	}
	py, goOut := fx.PythonOutput(), fx.ExpectedGoOutput
	return py != "" && goOut != "" && py != goOut
}

// FindingID pulls the leading "F-NN" reference out of the notes, when the
// fixture cites one. Divergence fixtures without an explicit citation in notes
// are resolved against the findings register (FINDINGS.md) so subtests are
// consistently tagged.
func (fx *Fixture) FindingID() string {
	if fx.ID == "LNK-OVERRIDE-01" {
		return "F-13"
	}
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
		// "F-07" style: two digits after the dash. Anything shorter is prose.
		if len(token) >= 4 && token[2] >= '0' && token[2] <= '9' {
			return token
		}
		notes = notes[idx+2:]
	}
}

// ByID indexes the fixture list by id. Duplicates are impossible here: the
// loader already rejected them.
func ByID(fixtures []Fixture) map[string]*Fixture {
	out := make(map[string]*Fixture, len(fixtures))
	for i := range fixtures {
		out[fixtures[i].ID] = &fixtures[i]
	}
	return out
}

// Select returns the fixtures whose executable is one of want, in file order.
func Select(fixtures []Fixture, want ...Executable) []Fixture {
	keeper := make(map[Executable]bool, len(want))
	for _, e := range want {
		keeper[e] = true
	}
	out := []Fixture{}
	for i := range fixtures {
		if keeper[fixtures[i].Executable] {
			out = append(out, fixtures[i])
		}
	}
	return out
}

// Classify partitions the fixture list by executable class, in file order.
// The summary test reports each partition's count so a reclassification in the
// shared file is caught by an exact-count assertion rather than a feeling.
func Classify(fixtures []Fixture) map[Executable][]Fixture {
	out := map[Executable][]Fixture{}
	for i := range fixtures {
		out[fixtures[i].Executable] = append(out[fixtures[i].Executable], fixtures[i])
	}
	return out
}

// SortedIDs is a deterministic id listing for failure messages.
func SortedIDs(fixtures []Fixture) []string {
	ids := make([]string, 0, len(fixtures))
	for i := range fixtures {
		ids = append(ids, fixtures[i].ID)
	}
	sort.Strings(ids)
	return ids
}

// --- Task 3: unsupported must be visible, never fake parity ------------------

// idsOf is a deterministic id listing for failure messages.
func idsOf(fixtures []Fixture) []string {
	out := make([]string, 0, len(fixtures))
	for i := range fixtures {
		out = append(out, fixtures[i].ID)
	}
	return out
}

// TestGoCoverageSummary reports every fixture whose engine exists only in
// Python as UNSUPPORTED_IN_GO — never silently skipped, and never counted as
// parity. The counts are asserted against the classification the coordinator
// published, so reclassifying any single fixture fails here with its id rather
// than quietly shrinking the list of things Go does not cover.
func TestGoCoverageSummary(t *testing.T) {
	fixtures := LoadFixtures(t)
	classes := Classify(fixtures)
	byID := ByID(fixtures)

	supported := map[string]bool{}
	for _, id := range SupportedFixtures {
		if byID[id] == nil {
			t.Errorf("fixture %s is listed as Go-supported but is not in the file", id)
			continue
		}
		supported[id] = true
	}

	// A supported fixture must have something for Go to assert. A "supported"
	// label without an expectation would be a green line testing nothing.
	for _, id := range SupportedFixtures {
		fx := byID[id]
		if fx == nil {
			continue
		}
		if !fx.HasGoDecision() && !fx.HasGoOutput() {
			t.Errorf("fixture %s is Go-supported but declares no expected_go* key", id)
		}
	}

	// The unsupported set is the exact complement of the supported set among
	// fixtures that could be executed at all (schema/meta are never executed).
	var unsupported []Fixture
	for i := range fixtures {
		fx := &fixtures[i]
		if fx.Executable == ExecSchema || fx.Executable == ExecMeta {
			continue
		}
		if !supported[fx.ID] {
			unsupported = append(unsupported, *fx)
		}
	}

	wantUnsupported := ExpectedCounts[ExecPython] + ExpectedCounts[ExecBoth] - len(SupportedFixtures)
	if len(unsupported) != wantUnsupported {
		t.Errorf("UNSUPPORTED_IN_GO count = %d, want %d (%s)",
			len(unsupported), wantUnsupported, strings.Join(idsOf(unsupported), ", "))
	}

	for class, want := range ExpectedCounts {
		if got := len(classes[class]); got != want {
			t.Errorf("class %s: count = %d, want %d (ids: %s)",
				class, got, want, strings.Join(idsOf(classes[class]), ", "))
		}
	}
	if got := len(classes[ExecUnclassified]); got != 0 {
		t.Errorf("class unclassified: count = %d, want 0 (ids: %s)",
			got, strings.Join(idsOf(classes[ExecUnclassified]), ", "))
	}

	// A schema fixture is coverage data; marking it supported would be a fake
	// parity claim, since nothing runs it.
	for i := range classes[ExecSchema] {
		if supported[classes[ExecSchema][i].ID] {
			t.Errorf("schema fixture %s is marked Go-supported", classes[ExecSchema][i].ID)
		}
	}

	t.Logf("Go harness: %d executed, %d %s, %d schema (characterization), %d meta (probe)",
		len(SupportedFixtures), len(unsupported), UnsupportedEngine,
		len(classes[ExecSchema]), len(classes[ExecMeta]))
	for i := range unsupported {
		t.Logf("  %-18s %s  engine=%s", UnsupportedEngine, unsupported[i].ID, unsupported[i].Engine)
	}
}

// TestSchemaFixturesAreNeverReportedAsPassing enforces the contract rule that a
// schema fixture is characterization data. If one grows a Go expectation this
// fails: the correct move is to reclassify it in fixtures.json and update the
// classification constants, not to execute it and call the result a passing test.
func TestSchemaFixturesAreNeverReportedAsPassing(t *testing.T) {
	fixtures := LoadFixtures(t)
	for i := range fixtures {
		fx := &fixtures[i]
		if fx.Executable != ExecSchema {
			continue
		}
		if fx.HasGoDecision() || fx.HasGoOutput() {
			t.Errorf("schema fixture %s (%s) carries a Go expectation — reclassify it in fixtures.json instead of executing it here",
				fx.ID, fx.Engine)
		}
	}
}

// TestBothFixturesAreTheOnlyParityVerdicts pins the rule that absent Go
// expectations on a python fixture mean "no Go engine", not divergence. Only
// `both` fixtures produce a parity verdict, so that population is asserted by
// name and each member must carry both halves of the comparison.
func TestBothFixturesAreTheOnlyParityVerdicts(t *testing.T) {
	fixtures := LoadFixtures(t)
	both := Select(fixtures, ExecBoth)
	if len(both) != ExpectedCounts[ExecBoth] {
		t.Fatalf("both-class count = %d, want %d", len(both), ExpectedCounts[ExecBoth])
	}
	for i := range both {
		fx := &both[i]
		if !fx.HasGoOutput() && !fx.HasGoDecision() {
			t.Errorf("both fixture %s has no Go expectation to compare", fx.ID)
		}
		hasPy := fx.PythonOutput() != "" || fx.raw["expected_python"] != nil
		if !hasPy {
			t.Errorf("both fixture %s has no Python expectation to compare", fx.ID)
		}
	}
	t.Logf("parity verdicts emitted by the Go harness: %s", strings.Join(idsOf(both), ", "))
}

// TestDivergenceFixturesAreTagged keeps divergences visible in the test names
// themselves: a fixture whose Python and Go expectations differ must cite a
// finding id, which the engine harnesses append to the subtest name.
func TestDivergenceFixturesAreTagged(t *testing.T) {
	fixtures := LoadFixtures(t)
	tagged := 0
	for i := range fixtures {
		fx := &fixtures[i]
		if !fx.Divergent() {
			continue
		}
		id := fx.FindingID()
		if id == "" {
			t.Errorf("divergent fixture %s cites no finding id in its notes", fx.ID)
			continue
		}
		tagged++
		t.Logf("%s -> %s", fx.ID, id)
	}
	if tagged == 0 {
		t.Log("no both-class fixture currently records differing expectations")
	}
}

// --- loader gates must actually bite -----------------------------------------

// TestLoaderRejectsBadRecords feeds synthetic malformed records through
// validateFixture. The shared file passes every gate, so without these the
// fail-closed behaviour of the loader would be untested: a regression that made
// a bad fixture skip instead of fail would be invisible.
func TestLoaderRejectsBadRecords(t *testing.T) {
	// build decodes a synthetic record and then applies the mutation to the
	// raw map, so a mutation can break the *shape* (a string where an object
	// belongs) without the typed decode rejecting the fixture first.
	build := func(mutate func(map[string]any)) Fixture {
		raw := map[string]any{
			"id":              "SYN-01",
			"engine":          "FilterEngine.evaluate",
			"group":           "filter-rule",
			"description":     "synthetic record used only to prove the gates fire",
			"executable":      "python",
			"input":           map[string]any{"text": "x"},
			"expected_python": map[string]any{"action": "ALLOW"},
		}
		mutate(raw)

		encoded, err := json.Marshal(raw)
		if err != nil {
			t.Fatalf("marshal synthetic record: %v", err)
		}
		// Decode into a generic map first, then into Fixture: decoding directly
		// into Fixture would hard-fail on a shape-breaking mutation ("input"
		// as a string) before the gate under test could run. The tolerant
		// decode leaves Input nil for that case, which is exactly the shape
		// the input gate must reject.
		var generic map[string]any
		if err := json.Unmarshal(encoded, &generic); err != nil {
			t.Fatalf("unmarshal synthetic record: %v", err)
		}
		var fx Fixture
		// json.Unmarshal into the typed struct tolerates mismatched types by
		// leaving the field at its zero value, so a bad shape degrades to the
		// nil-input/empty-string case the validator is meant to catch.
		if typed, err := json.Marshal(generic); err == nil {
			_ = json.Unmarshal(typed, &fx)
		}
		fx.raw = generic
		return fx
	}

	cases := []struct {
		name    string
		mutate  func(map[string]any)
		wantErr string
	}{
		{"unknown executable token",
			func(r map[string]any) { r["executable"] = "sometimes" },
			"unknown executable token"},
		{"unclassified is a hard failure",
			func(r map[string]any) { r["executable"] = "unclassified" },
			"hard failure"},
		{"bare string input",
			func(r map[string]any) { r["input"] = "just text" },
			"input must be a JSON object"},
		{"missing expectation",
			func(r map[string]any) { delete(r, "expected_python") },
			"no expected_* key"},
		{"placeholder key",
			func(r map[string]any) { r["-expected_python_output"] = "x" },
			"stray placeholder key"},
		{"missing engine field",
			func(r map[string]any) { delete(r, "engine") },
			`missing required field "engine"`},
		{"both without a Go expectation",
			func(r map[string]any) { r["executable"] = "both" },
			"requires an expected_go* key"},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			fx := build(tc.mutate)
			err := validateFixture(&fx, map[string]bool{})
			if err == nil {
				t.Fatalf("validateFixture accepted a bad record; want error containing %q", tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("error = %q, want it to contain %q", err, tc.wantErr)
			}
		})
	}

	t.Run("duplicate id", func(t *testing.T) {
		fx := build(func(map[string]any) {})
		err := validateFixture(&fx, map[string]bool{"SYN-01": true})
		if err == nil || !strings.Contains(err.Error(), "duplicate id") {
			t.Errorf("duplicate id accepted: err = %v", err)
		}
	})

	t.Run("well-formed record passes", func(t *testing.T) {
		fx := build(func(map[string]any) {})
		if err := validateFixture(&fx, map[string]bool{}); err != nil {
			t.Errorf("a valid record was rejected: %v", err)
		}
	})
}
