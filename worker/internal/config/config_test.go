package config

import (
	"os"
	"strings"
	"testing"
)

// TestValidateRefusesEmptyToken is the security gate: an empty internal RPC
// token with the worker enabled must be a refuse-to-start condition, never
// silently allowed — an unauthenticated internal RPC would let any caller on
// the network drive Telegram sessions.
func TestValidateRefusesEmptyToken(t *testing.T) {
	cases := []struct{ name, token string }{
		{"empty", ""},
		{"whitespace", "   "},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cfg := &Config{WorkerEnabled: true, CoreRPCToken: c.token, DBPath: "/tmp/x.db"}
			err := cfg.Validate()
			if err == nil || !strings.Contains(err.Error(), "ATF_CORE_RPC_TOKEN") {
				t.Errorf("Validate() = %v, want a refusal naming ATF_CORE_RPC_TOKEN", err)
			}
		})
	}
}

// TestValidatePassesWithToken: the happy path must not grow a constellation
// of extra requirements.
func TestValidatePassesWithToken(t *testing.T) {
	cfg := &Config{WorkerEnabled: true, CoreRPCToken: "secret", DBPath: "/tmp/x.db", NumWorkers: 4, BatchSize: 5}
	if err := cfg.Validate(); err != nil {
		t.Errorf("Validate() = %v, want nil", err)
	}
}

// TestDisabledWorkerNeedsNoSecret: a disabled worker has no surface to
// protect, so it must not demand a token.
func TestDisabledWorkerNeedsNoSecret(t *testing.T) {
	cfg := &Config{WorkerEnabled: false}
	if err := cfg.Validate(); err != nil {
		t.Errorf("Validate() on a disabled worker = %v, want nil", err)
	}
}

// TestFromEnvPicksUpSharedQueueKnobs: the worker reads the same
// ATF_QUEUE_* names the Python Core reads, so one deployment configuration
// tunes both engines.
func TestFromEnvPicksUpSharedQueueKnobs(t *testing.T) {
	t.Setenv("ATF_QUEUE_NUM_WORKERS", "7")
	t.Setenv("ATF_QUEUE_BATCH_SIZE", "9")
	t.Setenv("ATF_QUEUE_LEASE_SECONDS", "45")
	t.Setenv("ATF_WORKER_ENABLED", "1")
	t.Setenv("ATF_CORE_RPC_TOKEN", "t")

	cfg, err := FromEnv()
	if err != nil {
		t.Fatalf("FromEnv: %v", err)
	}
	if cfg.NumWorkers != 7 || cfg.BatchSize != 9 {
		t.Errorf("queue knobs = %d/%d, want 7/9", cfg.NumWorkers, cfg.BatchSize)
	}
	if cfg.LeaseSeconds != 45 {
		t.Errorf("lease = %v, want 45", cfg.LeaseSeconds)
	}
	if !cfg.WorkerEnabled {
		t.Error("ATF_WORKER_ENABLED=1 did not enable the worker")
	}
}

// TestFromEnvDefaultsMatchThePythonWorker: the defaults must be the Python
// worker's own, so an unconfigured deployment behaves the same either way.
func TestFromEnvDefaultsMatchThePythonWorker(t *testing.T) {
	for _, k := range []string{
		"ATF_QUEUE_NUM_WORKERS", "ATF_QUEUE_BATCH_SIZE", "ATF_QUEUE_LEASE_SECONDS",
		"ATF_WORKER_ENABLED", "ATF_CORE_RPC_TOKEN", "ATF_DATABASE_URL",
	} {
		t.Setenv(k, "")
	}
	cfg, err := FromEnv()
	if err != nil {
		t.Fatalf("FromEnv: %v", err)
	}
	if cfg.WorkerEnabled {
		t.Error("the worker must default to OFF (strangler-fig default)")
	}
	if cfg.NumWorkers != 4 || cfg.BatchSize != 5 || cfg.LeaseSeconds != 30 {
		t.Errorf("defaults = %d/%d/%v, want the Python worker's", cfg.NumWorkers, cfg.BatchSize, cfg.LeaseSeconds)
	}
}

func TestMain(m *testing.M) {
	// t.Setenv needs no special setup; keep os referenced for clarity.
	_ = os.Setenv
	m.Run()
}
