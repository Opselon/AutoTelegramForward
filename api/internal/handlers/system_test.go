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

func TestEnvContractNeverLeaksValues(t *testing.T) {
	const secret = "123456789:AAveryLongBotTokenValueThatMustNotLeak"
	t.Setenv("ATF_BOT_TOKEN", secret)
	t.Setenv("ATF_API_KEY", "another-secret-value-please-redact")
	t.Setenv("ATF_WEB_URL", "https://example.test")

	h := New(nil)
	rec := httptest.NewRecorder()
	h.EnvContractHandler(rec, httptest.NewRequest(http.MethodGet, "/api/system/env", nil))

	body := rec.Body.String()
	if strings.Contains(body, secret) || strings.Contains(body, "another-secret-value-please-redact") {
		t.Fatalf("env contract leaked a secret value: %s", body)
	}

	var payload struct {
		Checks []EnvCheck `json:"checks"`
		Missing []string `json:"missing_required"`
		OK      bool      `json:"ok"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !payload.OK || len(payload.Missing) != 0 {
		t.Fatalf("expected a complete contract, got ok=%v missing=%v", payload.OK, payload.Missing)
	}
	found := map[string]bool{}
	for _, check := range payload.Checks {
		found[check.Key] = check.Set
	}
	if !found["ATF_BOT_TOKEN"] || !found["ATF_API_KEY"] || !found["ATF_WEB_URL"] {
		t.Fatalf("expected those keys to read as set: %+v", payload.Checks)
	}
}

// Bot tokens are written as ATF_BOT_TOKEN, TELEGRAM_BOT_TOKEN or BOT_TOKEN
// depending on how the deployment was created; any of them means "configured".
func TestEnvContractAcceptsEveryBotTokenName(t *testing.T) {
	for _, name := range []string{"ATF_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN"} {
		t.Run(name, func(t *testing.T) {
			t.Setenv("ATF_BOT_TOKEN", "")
			t.Setenv("TELEGRAM_BOT_TOKEN", "")
			t.Setenv("BOT_TOKEN", "")
			t.Setenv(name, "token-value")

			h := New(nil)
			missing := missingRequired(h.EnvContract())
			if len(missing) != 0 {
				t.Fatalf("bot token via %s was not recognised, missing=%v", name, missing)
			}
		})
	}
}

func TestEnvContractFlagsMissingRequiredKeys(t *testing.T) {
	t.Setenv("ATF_BOT_TOKEN", "")
	t.Setenv("TELEGRAM_BOT_TOKEN", "")
	t.Setenv("BOT_TOKEN", "")

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
	if payload.OK || len(payload.Missing) == 0 {
		t.Fatalf("a deployment without a bot token must not report ready: %s", rec.Body.String())
	}
	for _, check := range New(nil).EnvContract() {
		if check.Key == "ATF_BOT_TOKEN" && check.Hint == "" {
			t.Fatal("a failing check must carry a fix hint")
		}
	}
}

// probe=0 keeps the report instant and dependency-free, which is what the CLI's
// static validator uses when the stack may be half-up.
func TestSetupReportSkipsProbesOnDemand(t *testing.T) {
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
	t.Setenv("ATF_BOT_TOKEN", "")
	t.Setenv("TELEGRAM_BOT_TOKEN", "")
	t.Setenv("BOT_TOKEN", "")

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
