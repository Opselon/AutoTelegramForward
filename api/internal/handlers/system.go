package handlers

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	pb "autoforward/proto"
)

// This file answers one question for an operator and for tooling: "is the
// running deployment actually configured, and is every service in the chain
// answering?" It is deliberately self-contained — no database, no core RPC
// required for the contract report — so it still works when the stack is only
// half up, which is exactly when the answer matters.

// ServiceProbe is one microservice's live reachability.
type ServiceProbe struct {
	Name      string `json:"name"`
	Online    bool   `json:"online"`
	Detail    string `json:"detail"`
	LatencyMs int64  `json:"latency_ms"`
}

// ProbeServices checks every service in the ATF chain in parallel with short
// timeouts. Core proves the gRPC channel AND the pipeline loop (GetSystemStats),
// logger proves the log store answers, web is a proxy reachability probe of its
// own /healthz, and api is this process.
func (h *Handlers) ProbeServices() ([]ServiceProbe, bool) {
	results := make([]ServiceProbe, 0, 4)
	var mu sync.Mutex
	check := func(name string, fn func() (bool, string)) {
		start := time.Now()
		ok, detail := false, ""
		done := make(chan struct{})
		go func() { ok, detail = fn(); close(done) }()
		select {
		case <-done:
		case <-time.After(4 * time.Second):
			ok, detail = false, "probe timeout (4s)"
		}
		mu.Lock()
		results = append(results, ServiceProbe{
			Name:      name,
			Online:    ok,
			Detail:    detail,
			LatencyMs: time.Since(start).Milliseconds(),
		})
		mu.Unlock()
	}

	go check("core", func() (bool, string) {
		ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer cancel()
		resp, err := h.c.System.GetSystemStats(ctx, &pb.SystemStatsRequest{})
		if err != nil {
			return false, "gRPC: " + err.Error()
		}
		return resp.CoreRunning, fmt.Sprintf("uptime=%ds sessions=%d", resp.UptimeSeconds, resp.ActiveSessionsCount)
	})

	go check("logger", func() (bool, string) {
		if h.c.Logs == nil {
			return false, "logger stub not wired (dial failed at startup)"
		}
		ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer cancel()
		resp, err := h.c.Logs.LogStats(ctx, &pb.LogStatsRequest{})
		if err != nil {
			return false, "gRPC: " + err.Error()
		}
		return true, fmt.Sprintf("events=%d", resp.Total)
	})

	go check("api", func() (bool, string) { return true, "this process" })

	go check("web", func() (bool, string) {
		webURL := os.Getenv("ATF_WEB_HEALTH_URL")
		if webURL == "" {
			webURL = "http://127.0.0.1:8088/healthz"
		}
		client := &http.Client{Timeout: 4 * time.Second}
		resp, err := client.Get(webURL)
		if err != nil {
			return false, err.Error()
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return false, "HTTP " + resp.Status
		}
		return true, webURL
	})

	deadline := time.After(4500 * time.Millisecond)
	for len(results) < 4 {
		select {
		case <-deadline:
			mu.Lock()
			if len(results) < 4 {
				results = append(results, ServiceProbe{Name: "?", Online: false, Detail: "probe pending"})
			}
			mu.Unlock()
			deadline = time.After(0)
		case <-time.After(50 * time.Millisecond):
		}
	}

	// Deterministic order for the UI: core → logger → api → web.
	order := map[string]int{"core": 0, "logger": 1, "api": 2, "web": 3}
	for i := 0; i < len(results); i++ {
		for j := i + 1; j < len(results); j++ {
			if order[results[j].Name] < order[results[i].Name] {
				results[i], results[j] = results[j], results[i]
			}
		}
	}

	allOK := true
	for _, s := range results {
		if !s.Online {
			allOK = false
		}
	}
	return results, allOK
}

// EnvCheck is one configuration input this process depends on.
//
// `Set` is a boolean on purpose: an operator (or a log) must be able to see that
// a secret is present without the value ever leaving the process. `Hint` carries
// a fix command, never data.
type EnvCheck struct {
	Key    string `json:"key"`
	Level  string `json:"level"` // required | recommended | optional
	Set    bool   `json:"set"`
	Reason string `json:"reason"`
	Hint   string `json:"hint,omitempty"`
}

// envAnySet reports whether any of the alternative names is set. Deployments in
// the wild use ATF_BOT_TOKEN, TELEGRAM_BOT_TOKEN and BOT_TOKEN interchangeably,
// so a check must accept all of them instead of declaring a working deployment
// broken.
func envAnySet(names ...string) bool {
	for _, name := range names {
		if strings.TrimSpace(os.Getenv(name)) != "" {
			return true
		}
	}
	return false
}

// EnvContract is the API's own configuration contract, as booleans.
func (h *Handlers) EnvContract() []EnvCheck {
	return []EnvCheck{
		{
			Key:    "ATF_BOT_TOKEN",
			Level:  "required",
			Set:    envAnySet("ATF_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
			Reason: "bot token exchange and Telegram Mini App auth",
			Hint:   "set ATF_BOT_TOKEN in .env and restart (atf system restart)",
		},
		{
			Key:    "ATF_API_KEY",
			Level:  "recommended",
			Set:    envAnySet("ATF_API_KEY"),
			Reason: "pre-shared CLI/service key for the admin surface",
			Hint:   "atf setup generates this automatically",
		},
		{
			Key:    "ATF_WEB_URL",
			Level:  "recommended",
			Set:    envAnySet("ATF_WEB_URL"),
			Reason: "absolute links in bot messages and the dashboard",
			Hint:   "ATF_WEB_URL=https://your-domain (or http://127.0.0.1:8088)",
		},
		{
			Key:    "ATF_TLS_DOMAIN",
			Level:  "optional",
			Set:    envAnySet("ATF_TLS_DOMAIN"),
			Reason: "reverse proxy terminates TLS for this domain",
			Hint:   "leave empty for a local-only dashboard",
		},
		{
			Key:    "ATF_TLS_EMAIL",
			Level:  "optional",
			Set:    envAnySet("ATF_TLS_EMAIL"),
			Reason: "Let's Encrypt contact address",
			Hint:   "set it once ATF_TLS_DOMAIN is set",
		},
	}
}

// missingRequired returns the required keys that are not set.
func missingRequired(checks []EnvCheck) []string {
	out := []string{}
	for _, c := range checks {
		if c.Level == "required" && !c.Set {
			out = append(out, c.Key)
		}
	}
	return out
}

// EnvContractHandler serves the contract alone — fast and dependency-free, so a
// CLI validator can compare the host .env against what the container actually
// received.
func (h *Handlers) EnvContractHandler(w http.ResponseWriter, r *http.Request) {
	checks := h.EnvContract()
	missing := missingRequired(checks)
	writeJSON(w, http.StatusOK, map[string]any{
		"checks":           checks,
		"missing_required": missing,
		"ok":               len(missing) == 0,
	})
}

// SetupReport answers "is this deployment ready?" with the version block, the
// env contract, and (unless ?probe=0) the live service probes.
func (h *Handlers) SetupReport(w http.ResponseWriter, r *http.Request) {
	checks := h.EnvContract()
	missing := missingRequired(checks)

	probes := []ServiceProbe{}
	allOnline := true
	if r.URL.Query().Get("probe") != "0" {
		probes, allOnline = h.ProbeServices()
	}

	ready := len(missing) == 0 && (len(probes) == 0 || allOnline)
	nextSteps := []string{}
	for _, key := range missing {
		for _, c := range checks {
			if c.Key == key && c.Hint != "" {
				nextSteps = append(nextSteps, c.Hint)
			}
		}
	}
	if len(probes) > 0 && !allOnline {
		for _, p := range probes {
			if !p.Online {
				nextSteps = append(nextSteps, fmt.Sprintf("%s is not answering (%s) — check: docker logs --tail 50 atf-%s", p.Name, p.Detail, p.Name))
			}
		}
	}
	if ready {
		nextSteps = append(nextSteps, "atf validate --runtime — full health re-check of the live stack")
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"ready":            ready,
		"time":             time.Now().UTC().Format(time.RFC3339),
		"product_version":  resolveProductVersion(),
		"api_version":      APIVersion,
		"commit_sha":       resolveCommitSHA(),
		"features":         VersionFeatures,
		"checks":           checks,
		"missing_required": missing,
		"services":         probes,
		"next_steps":       nextSteps,
	})
}
