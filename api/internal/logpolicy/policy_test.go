package logpolicy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// The fixture is the contract. Both this test and the Python producer's test
// (core/tests/test_log_policy_fixtures.py) evaluate it, so the two
// implementations cannot drift apart without CI noticing.
const fixturePath = "../../../contracts/log_policy_decisions.json"

type fixtureEvent struct {
	Level      string `json:"level"`
	EventType  string `json:"event_type"`
	ReasonCode string `json:"reason_code"`
	Expected   bool   `json:"expected"`
	Why        string `json:"why"`
}

type fixtureScenario struct {
	Name                 string         `json:"name"`
	Levels               []string       `json:"levels"`
	DiagnosticSeconds    int            `json:"diagnostic_seconds"`
	ExpireDiagnostic     bool           `json:"expire_diagnostic"`
	VerboseRatePerSecond float64        `json:"verbose_rate_per_second"`
	AdvanceSecondsBefore float64        `json:"advance_seconds_before"`
	Events               []fixtureEvent `json:"events"`
}

type fixtureFile struct {
	Version            int      `json:"version"`
	CollectedByDefault []string `json:"collected_by_default"`
	LevelNormalization []struct {
		Input    string `json:"input"`
		Expected string `json:"expected"`
		Why      string `json:"why"`
	} `json:"level_normalization"`
	LevelListParsing []struct {
		Input    string   `json:"input"`
		Expected []string `json:"expected"`
		Why      string   `json:"why"`
	} `json:"level_list_parsing"`
	Scenarios []fixtureScenario `json:"scenarios"`
}

type frozenClock struct{ now time.Time }

func (c *frozenClock) Now() time.Time          { return c.now }
func (c *frozenClock) Advance(d time.Duration) { c.now = c.now.Add(d) }

func loadFixture(t *testing.T) fixtureFile {
	t.Helper()
	raw, err := os.ReadFile(filepath.Clean(fixturePath))
	if err != nil {
		t.Fatalf("the shared policy fixture must exist (%s): %v", fixturePath, err)
	}
	var f fixtureFile
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatalf("the shared policy fixture must be valid JSON: %v", err)
	}
	if len(f.Scenarios) == 0 {
		t.Fatal("the shared policy fixture has no scenarios; a vacuous pass is worse than a failure")
	}
	return f
}

func TestLevelNormalization_MatchesTheSharedFixture(t *testing.T) {
	for _, c := range loadFixture(t).LevelNormalization {
		if got := NormalizeLevel(c.Input); got != c.Expected {
			t.Errorf("NormalizeLevel(%q) = %q, fixture requires %q (%s)", c.Input, got, c.Expected, c.Why)
		}
	}
}

func TestLevelListParsing_MatchesTheSharedFixture(t *testing.T) {
	// A level *list* has one extra rule the single-level normalisation does not:
	// an unrecognised name is dropped rather than becoming INFO. Getting this
	// wrong would let a typo in ATF_LOG_LEVELS silently re-enable a verbose level.
	for _, c := range loadFixture(t).LevelListParsing {
		got := ParseLevels(c.Input)
		if len(got) != len(c.Expected) {
			t.Errorf("ParseLevels(%q) = %v, fixture requires %v (%s)", c.Input, got, c.Expected, c.Why)
			continue
		}
		for i := range c.Expected {
			if got[i] != c.Expected[i] {
				t.Errorf("ParseLevels(%q) = %v, fixture requires %v (%s)", c.Input, got, c.Expected, c.Why)
				break
			}
		}
	}
}

func TestCollectionDecisions_MatchTheSharedFixture(t *testing.T) {
	for _, scenario := range loadFixture(t).Scenarios {
		scenario := scenario
		t.Run(scenario.Name, func(t *testing.T) {
			clock := &frozenClock{now: time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)}
			policy := New(scenario.Levels, scenario.VerboseRatePerSecond)
			policy.SetClock(clock.Now)

			if scenario.DiagnosticSeconds > 0 {
				policy.EnableDiagnostics(scenario.DiagnosticSeconds)
			}
			if scenario.ExpireDiagnostic {
				// Walk past the window so the expiry path itself is exercised.
				clock.Advance(time.Duration(scenario.DiagnosticSeconds+1) * time.Second)
				if policy.DiagnosticActive() {
					t.Fatal("an expired diagnostic window must close itself")
				}
			}
			if scenario.AdvanceSecondsBefore > 0 {
				clock.Advance(time.Duration(scenario.AdvanceSecondsBefore * float64(time.Second)))
			}

			for i, event := range scenario.Events {
				got := policy.Allow(event.Level, event.EventType, event.ReasonCode)
				if got != event.Expected {
					t.Errorf("event %d (%s %s/%s): Allow = %v, fixture requires %v (%s)",
						i, event.Level, event.EventType, event.ReasonCode, got, event.Expected, event.Why)
				}
			}
		})
	}
}

func TestDefaultsAreTheDocumentedDefaults(t *testing.T) {
	policy := New(nil, 0)
	snapshot := policy.Snapshot()
	want := []string{LevelWarn, LevelError, LevelCritical}
	if len(snapshot.ConfiguredLevels) != len(want) {
		t.Fatalf("configured levels = %v, want %v", snapshot.ConfiguredLevels, want)
	}
	for i := range want {
		if snapshot.ConfiguredLevels[i] != want[i] {
			t.Fatalf("configured levels = %v, want %v", snapshot.ConfiguredLevels, want)
		}
	}
	if snapshot.DiagnosticActive {
		t.Fatal("a fresh policy must not be in diagnostic mode")
	}
}

func TestARefusalIsCountedAsSuppressionNotAsLoss(t *testing.T) {
	policy := New(nil, 0)
	for i := 0; i < 3; i++ {
		if policy.Allow(LevelInfo, "http.request", "") {
			t.Fatal("INFO must not be collected by default")
		}
	}
	snapshot := policy.Snapshot()
	if snapshot.SuppressedLevel != 3 {
		t.Fatalf("suppressed level count = %d, want 3", snapshot.SuppressedLevel)
	}
	if snapshot.Emitted != 0 {
		t.Fatalf("emitted = %d, want 0", snapshot.Emitted)
	}
	// Counters here are policy decisions only: there is no delivery counter to
	// confuse them with, which is the point.
	if snapshot.EmittedSecurity != 0 {
		t.Fatal("no security events were evaluated")
	}
}

func TestDiagnosticWindowIsBoundedAndSelfClosing(t *testing.T) {
	clock := &frozenClock{now: time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)}
	policy := New(nil, 0)
	policy.SetClock(clock.Now)

	expiry := policy.EnableDiagnostics(30)
	if expiry == 0 {
		t.Fatal("enabling diagnostics must report a wall-clock expiry for the operator")
	}
	if !policy.Allow(LevelDebug, "http.request", "") {
		t.Fatal("an open diagnostic window must collect DEBUG")
	}

	clock.Advance(31 * time.Second)
	if policy.DiagnosticActive() {
		t.Fatal("an expired window must close itself rather than stay open")
	}
	if policy.Allow(LevelDebug, "http.request", "") {
		t.Fatal("an expired window must stop collecting verbose events")
	}
	if policy.DiagnosticActive() {
		t.Fatal("observing an expired window must close it")
	}
	if !policy.Allow(LevelError, "http.request", "") {
		t.Fatal("closing a diagnostic window must not disable ERROR collection")
	}
}

func TestEnableDiagnosticsClampsANonsenseDuration(t *testing.T) {
	policy := New(nil, 0)
	if expiry := policy.EnableDiagnostics(0); expiry == 0 {
		t.Fatal("a zero duration must still produce a usable bounded window")
	}
	if expiry := policy.EnableDiagnostics(-5); expiry == 0 {
		t.Fatal("a negative duration must be clamped, not ignored")
	}
	if !policy.DiagnosticActive() {
		t.Fatal("the clamped window must be active")
	}
}

func TestConfigFromEnvFailsSafeToTheDocumentedDefaults(t *testing.T) {
	env := map[string]string{
		"ATF_LOG_LEVELS":             "warn, ERROR ,bogus;CRITICAL",
		"ATF_LOG_VERBOSE_RATE":       "",
		"ATF_LOG_DIAGNOSTIC_SECONDS": "not-a-number",
	}
	cfg := ConfigFromEnv(func(key string) string { return env[key] })
	want := []string{LevelWarn, LevelError, LevelCritical}
	if len(cfg.Levels) != len(want) {
		t.Fatalf("levels = %v, want %v (aliases normalised, unknown dropped)", cfg.Levels, want)
	}
	for i := range want {
		if cfg.Levels[i] != want[i] {
			t.Fatalf("levels = %v, want %v", cfg.Levels, want)
		}
	}
	if cfg.VerboseRatePerSecond != 0 {
		t.Fatalf("an empty rate must mean uncapped, got %v", cfg.VerboseRatePerSecond)
	}
	if cfg.DiagnosticSeconds != 0 {
		t.Fatalf("a garbage duration must disable diagnostics, got %d", cfg.DiagnosticSeconds)
	}

	garbage := ConfigFromEnv(func(key string) string {
		if key == "ATF_LOG_LEVELS" {
			return "!!!"
		}
		return ""
	})
	if len(garbage.Levels) != len(DefaultLevels) {
		t.Fatalf("an unparseable level list must fall back to the defaults, got %v", garbage.Levels)
	}
}

func TestPolicyIsConcurrencySafe(t *testing.T) {
	// The API logs from many request goroutines at once; the policy must neither
	// race nor lose a count.
	policy := New([]string{LevelInfo}, 0)
	const goroutines, perGoroutine = 8, 250
	done := make(chan struct{}, goroutines)
	for g := 0; g < goroutines; g++ {
		go func() {
			for i := 0; i < perGoroutine; i++ {
				policy.Allow(LevelInfo, "http.request", "")
			}
			done <- struct{}{}
		}()
	}
	for g := 0; g < goroutines; g++ {
		<-done
	}
	if got := policy.Snapshot().Emitted; got != goroutines*perGoroutine {
		t.Fatalf("emitted = %d, want %d (a lost count hides a lost event)", got, goroutines*perGoroutine)
	}
}
