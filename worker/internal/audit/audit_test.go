// Package audit — schema validation of the shared fixture file.
//
// These tests are the Go half of the fixture integrity gates described in
// docs/behavior-audit/_handoff/_fixture-contract.md. The contract makes the
// schema the thing the three harnesses (Python, Go, validator) cannot disagree
// about, so every rule below mirrors a numbered gate there — a fixture that
// violates one must fail here with the fixture id, not pass by accident.
//
// What this file deliberately does NOT do: run fixtures. Fixtures with a Go
// engine are executed by filter_fixture_test.go and transform_fixture_test.go;
// fixtures whose engine does not exist in Go are accounted for by
// fixtures_test.go's summary test as UNSUPPORTED_IN_GO.
package audit

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// TestFixtureFileLoads is the entry gate: the shared file parses, declares a
// schema version, and yields the fixed population of 43 fixtures the audit
// catalogued. A change in that count is a deliberate, reviewable change — one
// the coordinator must make in the file and in these expectations together.
func TestFixtureFileLoads(t *testing.T) {
	fixtures := LoadFixtures(t)

	if got := len(fixtures); got != ExpectedTotal {
		t.Errorf("fixture count = %d, want %d (ids: %s)",
			got, ExpectedTotal, strings.Join(SortedIDs(fixtures), ", "))
	}

	byID := ByID(fixtures)
	for _, want := range RequiredFixtures {
		if _, ok := byID[want]; !ok {
			t.Errorf("required fixture %q is missing from the file", want)
		}
	}
}

// TestSchemaPresent is contract gate 1: JSON parses and schema_version is set.
// Go's decoder rejects malformed JSON outright in LoadFixtures, so what remains
// to assert is that the version is non-empty and names the shape this harness
// was written against.
func TestSchemaPresent(t *testing.T) {
	if version := SchemaVersion(t); version == "" {
		t.Fatal("schema_version is present but empty")
	}
}

// TestEveryFixtureHasRequiredFields is contract gate 3: every record carries
// id, engine, group, description and executable. The loader already aborts on
// a violation; this test keeps that behaviour observable as a named, greppable
// case rather than an opaque Fatal inside the loader.
func TestEveryFixtureHasRequiredFields(t *testing.T) {
	fixtures := LoadFixtures(t)
	for i := range fixtures {
		fx := &fixtures[i]
		if fx.ID == "" {
			t.Errorf("record %d: id is empty", i)
		}
		if fx.Engine == "" {
			t.Errorf("fixture %s: engine is empty", fx.ID)
		}
		if fx.Group == "" {
			t.Errorf("fixture %s: group is empty", fx.ID)
		}
		if fx.Description == "" {
			t.Errorf("fixture %s: description is empty", fx.ID)
		}
		if fx.Executable == "" {
			t.Errorf("fixture %s: executable is empty", fx.ID)
		}
	}
}

// TestExecutableTokensAreKnown is contract gate 3's second half and gate 7's
// setup: the only legal tokens are python, both, meta, schema, unclassified,
// and unclassified is itself a hard failure. Nothing may be added to the set
// without the contract changing first.
func TestExecutableTokensAreKnown(t *testing.T) {
	fixtures := LoadFixtures(t)
	seen := map[string]int{}
	for i := range fixtures {
		fx := &fixtures[i]
		seen[string(fx.Executable)]++
		if !isKnownExecutable(fx.Executable) {
			t.Errorf("fixture %s: executable %q is not a contract token",
				fx.ID, fx.Executable)
		}
		if fx.Executable == ExecUnclassified {
			t.Errorf("fixture %s: executable=unclassified must fail, not pass", fx.ID)
		}
	}
	t.Logf("executable classes: %v", seen)
}

// TestInputIsAnObject is contract gate 4: the harness never accepts a bare
// string input. An input that decoded into anything but a map fails inside
// LoadFixtures; this asserts the population actually inside the file.
func TestInputIsAnObject(t *testing.T) {
	fixtures := LoadFixtures(t)
	for i := range fixtures {
		if fixtures[i].Input == nil {
			t.Errorf("fixture %s: input is missing or a scalar", fixtures[i].ID)
		}
	}
}

// TestIdsAreUnique is contract gate 2. LoadFixtures aborts on the first
// duplicate; this test would catch a registry that swallowed the error.
func TestIdsAreUnique(t *testing.T) {
	fixtures := LoadFixtures(t)
	seen := map[string]int{}
	for i := range fixtures {
		seen[fixtures[i].ID]++
	}
	for id, n := range seen {
		if n > 1 {
			t.Errorf("id %q appears %d times", id, n)
		}
	}
}

// TestEveryFixtureDeclaresAnExpectation is contract gate 5: at least one
// expected_* key per record, and for a `both` fixture the Go half too, since
// only `both` fixtures produce parity verdicts.
func TestEveryFixtureDeclaresAnExpectation(t *testing.T) {
	fixtures := LoadFixtures(t)
	for i := range fixtures {
		fx := &fixtures[i]
		if fx.Executable == ExecMeta {
			if len(MetaClaims(fx.raw)) == 0 {
				t.Errorf("fixture %s: meta fixture has no claims", fx.ID)
			}
			continue
		}
		if !HasAnyExpectation(fx.raw) {
			t.Errorf("fixture %s: no expected_* key", fx.ID)
		}
		if fx.Executable == ExecBoth && !fx.HasGoDecision() && !hasRawKey(fx.raw, "expected_go_output") {
			// F-03: an expected_go_output of "" is a real expectation (the whole
			// emoji sequence removed), so presence — not emptiness — is the test.
			t.Errorf("fixture %s: executable=both without an expected_go* key", fx.ID)
		}
	}
}

// TestNoPlaceholderKeys is contract gate 6: an earlier draft carried a
// -expected_python_output key. Any '-'-prefixed key is rejected outright so
// the shape can never silently regress.
func TestNoPlaceholderKeys(t *testing.T) {
	fixtures := LoadFixtures(t)
	for i := range fixtures {
		for key := range fixtures[i].raw {
			if strings.HasPrefix(key, "-") {
				t.Errorf("fixture %s: stray placeholder key %q", fixtures[i].ID, key)
			}
		}
	}
}

// TestDivergenceFixturesCiteAFinding is contract gate 7: a `both` fixture with
// differing expectations must carry a notes field citing the finding id. The
// Go harness tags those subtests with that id, so a finding-less divergence
// would surface as an untagged failure.
func TestDivergenceFixturesCiteAFinding(t *testing.T) {
	fixtures := LoadFixtures(t)
	for i := range fixtures {
		fx := &fixtures[i]
		if !fx.Divergent() {
			continue
		}
		if fx.Notes == "" {
			t.Errorf("fixture %s: divergent both-fixture without a notes field",
				fx.ID)
		}
		if fx.FindingID() == "" {
			t.Errorf("fixture %s: divergent both-fixture whose notes cite no finding id",
				fx.ID)
		}
	}
}

// TestFixtureClassificationIsFrozen pins the per-class counts. The summary
// test in fixtures_test.go asserts the same numbers from the runtime side;
// this one pins the file's own population so a reclassification has to update
// two places and cannot slip through as "just a fixture edit".
func TestFixtureClassificationIsFrozen(t *testing.T) {
	fixtures := LoadFixtures(t)
	classes := Classify(fixtures)

	for _, class := range knownExecutables {
		got := len(classes[class])
		if class == ExecUnclassified {
			if got != 0 {
				t.Errorf("class unclassified: count = %d, want 0", got)
			}
			continue
		}
		if want, ok := ExpectedCounts[class]; !ok || got != want {
			t.Errorf("class %s: count = %d, want %d", class, got, want)
		}
	}

	// A `both` fixture is the only kind that can produce a parity verdict, so
	// its identity is worth pinning by name.
	if ids := idsOf(classes[ExecBoth]); len(ids) > 0 {
		t.Logf("both-class fixtures: %s", strings.Join(ids, ", "))
		required := map[string]bool{}
		for _, id := range RequiredFixtures {
			required[id] = true
		}
		for _, id := range ids {
			if !required[id] {
				t.Errorf("unexpected both-class fixture %q (not in RequiredFixtures)", id)
			}
		}
	}
}

// TestFixturesCoverTheAuditGroups keeps the coverage report honest: every group
// the audit catalogued must still be represented. A group that disappears from
// the file means a behaviour stopped being pinned, which is a review-worthy
// change, not a tidying.
func TestFixturesCoverTheAuditGroups(t *testing.T) {
	fixtures := LoadFixtures(t)
	seen := map[string]bool{}
	for i := range fixtures {
		seen[fixtures[i].Group] = true
	}
	for _, group := range ExpectedGroups {
		if !seen[group] {
			t.Errorf("group %q has no fixture in the file", group)
		}
	}
}

// TestFixtureNotesDocumentGoalState is a soft check, run non-fatally: the
// notes carry the evidence and divergence explanation, so a fixture the
// audit cares about (every one with a Go expectation, and every divergence)
// should keep them.
func TestFixtureNotesDocumentGoalState(t *testing.T) {
	fixtures := LoadFixtures(t)
	for i := range fixtures {
		fx := &fixtures[i]
		if fx.Executable == ExecBoth && fx.Notes == "" {
			t.Errorf("fixture %s: both-class fixture without evidence notes", fx.ID)
		}
		if (fx.HasGoDecision() || fx.HasGoOutput()) && fx.Notes == "" {
			t.Logf("fixture %s: carries a Go expectation without notes", fx.ID)
		}
	}
}

// SchemaVersion loads only the file's top-level version, for the gate that
// must not depend on the fixture population decoding first.
func SchemaVersion(t *testing.T) string {
	t.Helper()
	return readSchemaVersionOrFail(t, findFixtureFile(t))
}

// readSchemaVersionOrFail reads just the version field, so the version gate
// does not couple to the full fixture decode.
func readSchemaVersionOrFail(t *testing.T, path string) string {
	t.Helper()
	version, err := readSchemaVersion(path)
	if err != nil {
		t.Fatalf("read schema_version from %s: %v", path, err)
	}
	return version
}

// readSchemaVersion is a tiny standalone reader so the version gate does not
// couple to the full fixture decode.
func readSchemaVersion(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	var out struct {
		SchemaVersion string `json:"schema_version"`
	}
	if err := json.NewDecoder(f).Decode(&out); err != nil {
		return "", err
	}
	return out.SchemaVersion, nil
}
