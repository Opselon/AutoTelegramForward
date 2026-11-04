// runner.go executes the 369-scenario E2E suite and asserts the manifest.
package e2e

import (
	"testing"
)

// RunSuite executes every registered scenario and fails the suite if any
// required scenario is missing or failed. BLOCKED results are reported but
// do not fail (an external dependency — live Telegram, a provider — was
// unreachable and is named in the evidence).
func RunSuite(t *testing.T, rec *Recorder, want map[string]int) {
	t.Helper()
	cats, total, fails := rec.Summary()

	for cat, n := range want {
		got := cats[cat]
		if got[0] != n {
			t.Errorf("category %s: executed %d scenarios, want %d", cat, got[0], n)
		}
	}

	if total["TOTAL"][0] != 369 {
		t.Errorf("total executed = %d, want exactly 369", total["TOTAL"][0])
	}

	for _, f := range fails {
		if f.Result == ResultBlocked {
			t.Logf("BLOCKED %s: %s", f.ScenarioID, f.FailureReason)
			continue
		}
		t.Errorf("FAIL %s (%s): %s", f.ScenarioID, f.Category, f.FailureReason)
	}
}
