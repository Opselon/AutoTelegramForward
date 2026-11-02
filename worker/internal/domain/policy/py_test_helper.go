package policy

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// py_test_helper.go — the Python differential-test harness.
//
// The Go port in this package must agree with the live Python reference
// (core/domain/content_policy.py) on every input, not just the ones a fixture
// file has already captured. These helpers run the Python module in a
// subprocess, so a divergence is caught at test time rather than in
// production, and so the test that finds one can print both engines' answers
// side by side.
//
// The helpers live in a _test.go file (like the fixture loader in
// worker/internal/audit): nothing outside the test suite may depend on the
// reference implementation's location or CLI shape.

// repoRoot is the repository root, resolved once by walking up from the test
// binary's working directory — the same approach the audit fixture loader
// uses (worker/internal/audit/fixtures_test.go:124-142), never a hard-coded
// absolute path.
func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for i := 0; i < 16; i++ {
		if _, err := os.Stat(filepath.Join(dir, "core", "domain", "content_policy.py")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	t.Fatalf("repository root (core/domain/content_policy.py) not found walking up from %s", mustWD())
	return ""
}

func mustWD() string {
	dir, err := os.Getwd()
	if err != nil {
		return "?"
	}
	return dir
}

// pyRun evaluates expr in the repository root and returns stdout.
//
// PYTHONPATH is set to the repo root so `core.domain.content_policy` imports
// regardless of where the test binary runs from; PYTHONUNBUFFERED and -u keep
// the output deterministic. A failing import is a hard failure: the reference
// implementation is part of the repo, so if it cannot be imported the
// differential test cannot say anything at all.
func pyRun(t *testing.T, expr string) string {
	t.Helper()
	root := repoRoot(t)
	cmd := exec.Command("python3", "-uc", expr)
	cmd.Dir = root
	cmd.Env = append(os.Environ(),
		"PYTHONPATH="+root,
		"PYTHONUNBUFFERED=1",
		"PYTHONDONTWRITEBYTECODE=1",
	)
	var out strings.Builder
	cmd.Stdout = &out
	cmd.Stderr = &out
	if err := cmd.Run(); err != nil {
		t.Fatalf("python reference failed (expr=%s): %v\n%s", expr, err, out.String())
	}
	return out.String()
}

// pySkipable reports whether the Python reference is reachable at all, for
// the differential tests that should degrade to a skip rather than a failure
// when the interpreter or the module is unavailable on this host.
func pySkipable(t *testing.T) bool {
	t.Helper()
	root := repoRootSilent()
	if root == "" {
		return false
	}
	cmd := exec.Command("python3", "-uc",
		"import core.domain.content_policy as m; assert hasattr(m, 'contains_word')")
	cmd.Dir = root
	cmd.Env = append(os.Environ(),
		"PYTHONPATH="+root,
		"PYTHONDONTWRITEBYTECODE=1",
	)
	return cmd.Run() == nil
}

// repoRootSilent is repoRoot without the testing.T dependency, for the
// reachability probe.
func repoRootSilent() string {
	dir, err := os.Getwd()
	if err != nil {
		return ""
	}
	for i := 0; i < 16; i++ {
		if _, err := os.Stat(filepath.Join(dir, "core", "domain", "content_policy.py")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return ""
}
