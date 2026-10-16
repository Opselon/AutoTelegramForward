package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The setup surface exists so an operator (and the CLI validator) can ask a
// running deployment two questions without shell access: "what are you
// configured with?" and "is every service answering?". These tests pin the
// contract that makes it safe to expose publicly: booleans, never values.

// setRequiredEnv gives the contract what docker-compose.yml gives the container.
func setRequiredEnv(t *testing.T) {
	t.Helper()
	t.Setenv("ATF_CORE_GRPC", "core:50051")
	t.Setenv("ATF_LOGGER_GRPC", "logger:50052")
}

func TestEnvContractNeverLeaksValues(t *testing.T) {
	// Assembled at runtime on purpose: a credential-shaped literal in the repo
	// trips the project's own secrets scanner (atf validate --runtime).
	secret := "123456789:" + strings.Repeat("A", 38)
	setRequiredEnv(t)
	t.Setenv("ATF_BOT_TOKEN", secret)
	t.Setenv("ATF_API_KEY", "another-secret-value-please-redact")
	t.Setenv("ATF_WEB_HEALTH_URL", "http://web:8088/healthz")

	h := New(nil)
	rec := httptest.NewRecorder()
	h.EnvContractHandler(rec, httptest.NewRequest(http.MethodGet, "/api/system/env", nil))

	body := rec.Body.String()
	if strings.Contains(body, secret) || strings.Contains(body, "another-secret-value-please-redact") {
		t.Fatalf("env contract leaked a secret value: %s", body)
	}

	var payload struct {
		Checks   []EnvCheck `json:"checks"`
		Missing  []string   `json:"missing_required"`
		MissingR []string   `json:"missing_recommended"`
		Deploy   string     `json:"deployment"`
		OK       bool       `json:"ok"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !payload.OK || len(payload.Missing) != 0 || len(payload.MissingR) != 0 {
		t.Fatalf("expected a complete contract, got ok=%v missing=%v/%v", payload.OK, payload.Missing, payload.MissingR)
	}
	if payload.Deploy != "docker-compose" {
		t.Fatalf("the contract must say which deployment style it describes, got %q", payload.Deploy)
	}
	found := map[string]bool{}
	for _, check := range payload.Checks {
		found[check.Key] = check.Set
	}
	for _, key := range []string{"ATF_CORE_GRPC", "ATF_LOGGER_GRPC", "ATF_API_KEY", "ATF_WEB_HEALTH_URL", "ATF_BOT_TOKEN"} {
		if !found[key] {
			t.Fatalf("expected %s to read as set: %+v", key, payload.Checks)
		}
	}
}

// Bot tokens are written as ATF_BOT_TOKEN, TELEGRAM_BOT_TOKEN or BOT_TOKEN
// depending on how the deployment was created; any of them means "configured".
func TestEnvContractAcceptsEveryBotTokenName(t *testing.T) {
	for _, name := range []string{"ATF_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN"} {
		t.Run(name, func(t *testing.T) {
			setRequiredEnv(t)
			t.Setenv("ATF_BOT_TOKEN", "")
			t.Setenv("TELEGRAM_BOT_TOKEN", "")
			t.Setenv("BOT_TOKEN", "")
			t.Setenv(name, "token-value")

			botSet := false
			for _, check := range New(nil).EnvContract() {
				if check.Key == "ATF_BOT_TOKEN" {
					botSet = check.Set
				}
			}
			if !botSet {
				t.Fatalf("bot token via %s was not recognised", name)
			}
		})
	}
}

// A container that never received the peer addresses cannot work at all, so the
// missing required keys must be reported with a fix hint (and a bot token, which
// compose keeps in core, must NOT make a healthy stack look broken).
func TestEnvContractFlagsMissingRequiredKeys(t *testing.T) {
	t.Setenv("ATF_CORE_GRPC", "")
	t.Setenv("ATF_LOGGER_GRPC", "")
	t.Setenv("ATF_BOT_TOKEN", "")

	h := New(nil)
	rec := httptest.NewRecorder()
	h.EnvContractHandler(rec, httptest.NewRequest(http.MethodGet, "/api/system/env", nil))

	var payload struct {
		Missing []string `json:"missing_required"`
		OK      bool     `json:"ok"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if payload.OK {
		t.Fatalf("a container without its peer addresses must not report ready: %s", rec.Body.String())
	}
	for _, want := range []string{"ATF_CORE_GRPC", "ATF_LOGGER_GRPC"} {
		found := false
		for _, key := range payload.Missing {
			if key == want {
				found = true
			}
		}
		if !found {
			t.Fatalf("expected %s in missing_required, got %v", want, payload.Missing)
		}
	}
	for _, check := range New(nil).EnvContract() {
		if check.Level == "required" && check.Hint == "" {
			t.Fatalf("%s must carry a fix hint", check.Key)
		}
	}
	if missingRequired(New(nil).EnvContract()) == nil {
		t.Fatal("missing_required must be an empty list, never nil, so JSON stays stable")
	}
}

// probe=0 keeps the report instant and dependency-free, which is what the CLI's
// static validator uses when the stack may be half-up.
func TestSetupReportSkipsProbesOnDemand(t *testing.T) {
	setRequiredEnv(t)
	t.Setenv("ATF_BOT_TOKEN", "token-value")

	h := New(nil)
	rec := httptest.NewRecorder()
	h.SetupReport(rec, httptest.NewRequest(http.MethodGet, "/api/system/setup?probe=0", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	var payload struct {
		Ready    bool           `json:"ready"`
		Services []ServiceProbe `json:"services"`
		Version  string         `json:"product_version"`
		API      string         `json:"api_version"`
		Features []string       `json:"features"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(payload.Services) != 0 {
		t.Fatalf("probe=0 must not probe, got %d services", len(payload.Services))
	}
	if !payload.Ready {
		t.Fatalf("a complete contract with probes skipped must be ready: %s", rec.Body.String())
	}
	if payload.API == "" || payload.Version == "" {
		t.Fatalf("version block missing: %s", rec.Body.String())
	}
	want := map[string]bool{"system_setup": true, "env_contract": true}
	for _, feature := range payload.Features {
		delete(want, feature)
	}
	if len(want) != 0 {
		t.Fatalf("new capabilities must be advertised for clients to probe: missing %v", want)
	}
}

// A missing required key must surface as a fixable next step, never as a bare
// failure an operator cannot act on.
func TestSetupReportNextStepsAreActionable(t *testing.T) {
	t.Setenv("ATF_CORE_GRPC", "")
	t.Setenv("ATF_LOGGER_GRPC", "")

	h := New(nil)
	rec := httptest.NewRecorder()
	h.SetupReport(rec, httptest.NewRequest(http.MethodGet, "/api/system/setup?probe=0", nil))

	var payload struct {
		Ready     bool     `json:"ready"`
		NextSteps []string `json:"next_steps"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if payload.Ready {
		t.Fatal("ready must be false without a bot token")
	}
	if len(payload.NextSteps) == 0 {
		t.Fatal("expected a fix hint in next_steps")
	}
}
