package config

import "testing"

// activation_test.go — the strangler-fig execution boundary.
//
// The Python Core's queue loop stands down ONLY when ATF_WORKER_ENABLED is set
// (durable_queue.py:221). So the Go worker must claim jobs only when the same
// flag is set — an ungated claimer puts both engines on one queue at once.
// That is the "no hidden fallback / no simultaneous processing" boundary the
// migration depends on, and these tests are the regression net for it.

func TestClaimsEnabledRequiresTheSwitch(t *testing.T) {
	if ClaimsEnabled(&Config{}) {
		t.Error("a worker with ATF_WORKER_ENABLED unset must not claim jobs — the Python Core owns delivery execution")
	}
	if ClaimsEnabled(&Config{WorkerEnabled: true}) != true {
		t.Error("an enabled worker must be allowed to claim")
	}
}

func TestValidateFailsClosedOnAnEmptyRPCTokenWhenEnabled(t *testing.T) {
	// Enabled + empty token is a refuse-to-start condition, not degraded mode:
	// an unauthenticated internal RPC would let any caller on the network
	// drive Telegram sessions.
	if err := (&Config{WorkerEnabled: true, CoreRPCToken: ""}).Validate(); err == nil {
		t.Error("enabled worker with an empty RPC token must refuse to start")
	}
	// Disabled workers have nothing to protect.
	if err := (&Config{WorkerEnabled: false, CoreRPCToken: ""}).Validate(); err != nil {
		t.Errorf("disabled worker must start without a token, got %v", err)
	}
}
