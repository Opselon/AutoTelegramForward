package handlers

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
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

// ProbeServices checks every service in the ATF chain concurrently with short
// timeouts, through the process-wide cache (health_probe.go). Core proves the
// gRPC channel AND the pipeline loop (GetSystemStats), logger proves the log
// store answers, web is a proxy reachability probe of its own /healthz, and api
// is this process.
//
// The per-hop checks take a context bounded by the prober's per-check timeout,
// so a hanging hop costs at most that timeout and never the whole response.
func (h *Handlers) ProbeServices() ([]ServiceProbe, bool) {
	results, allOK, _, _ := h.probeServices(context.Background())
	return results, allOK
}

// probeServices is ProbeServices plus the cache metadata the endpoint reports.
func (h *Handlers) probeServices(ctx context.Context) ([]ServiceProbe, bool, bool, time.Duration) {
	return h.prober().probe(ctx)
}

// prober returns the process-wide prober, building it (and its checks) once.
//
// Tests replace it directly: the checks are the real network calls, so the
// cache and timeout behaviour is tested against a prober with fake checks
// rather than against a live stack.
func (h *Handlers) prober() *serviceProber {
	h.proberMu.Lock()
	defer h.proberMu.Unlock()
	if h.probes != nil {
		return h.probes
	}
	h.probes = newServiceProber(h.liveChecks())
	return h.probes
}

// liveChecks is the deployment's probe set, in the order the UI shows it.
func (h *Handlers) liveChecks() []probeCheck {
	return []probeCheck{
		{name: "core", fn: func(ctx context.Context) (bool, string) {
			if h.c == nil || h.c.System == nil {
				return false, "core client not wired (dial failed at startup)"
			}
			resp, err := h.c.System.GetSystemStats(ctx, &pb.SystemStatsRequest{})
			if err != nil {
				return false, "gRPC: " + err.Error()
			}
			return resp.CoreRunning, fmt.Sprintf("uptime=%ds sessions=%d", resp.UptimeSeconds, resp.ActiveSessionsCount)
		}},
		{name: "logger", fn: func(ctx context.Context) (bool, string) {
			if h.c == nil || h.c.Logs == nil {
				return false, "logger stub not wired (dial failed at startup)"
			}
			// A reachability probe asks the cheapest question that proves the
			// store answers. It used to call LogStats — the whole-archive
			// aggregate — which made the health endpoint's own latency depend on
			// how much history the deployment had (measured 42ms of the 52ms
			// budget). One page of one row proves the same thing for ~0.5ms.
			resp, err := h.c.Logs.QueryLogs(ctx, &pb.QueryLogsRequest{Limit: 1})
			if err != nil {
				return false, "gRPC: " + err.Error()
			}
			return true, fmt.Sprintf("events=%d", resp.Total)
		}},
		{name: "api", fn: func(context.Context) (bool, string) { return true, "this process" }},
		{name: "web", fn: func(ctx context.Context) (bool, string) {
			webURL := os.Getenv("ATF_WEB_HEALTH_URL")
			if webURL == "" {
				webURL = "http://127.0.0.1:8088/healthz"
			}
			// The request carries the per-check deadline, so a web gateway that
			// accepts the connection and then stalls is bounded too — the old
			// client timeout was the only bound and it was twice the probe
			// budget.
			req, err := http.NewRequestWithContext(ctx, http.MethodGet, webURL, nil)
			if err != nil {
				return false, err.Error()
			}
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				return false, err.Error()
			}
			defer resp.Body.Close()
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
			if resp.StatusCode != http.StatusOK {
				return false, "HTTP " + resp.Status
			}
			return true, webURL
		}},
	}
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
//
// It lists what THIS deployment style (docker compose) provides to the api
// container — the addresses it needs to reach its peers and the optional knobs
// the compose file passes. The bot token deliberately is not required here: in
// compose mode the bot session lives in the core container, and listing it as
// required would report a healthy stack as broken.
func (h *Handlers) EnvContract() []EnvCheck {
	checks := []EnvCheck{
		{
			Key:    "ATF_CORE_GRPC",
			Level:  "required",
			Set:    envAnySet("ATF_CORE_GRPC"),
			Reason: "core gRPC address (rules, sessions, pipeline stats)",
			Hint:   "ATF_CORE_GRPC=core:50051 (set by docker-compose.yml)",
		},
		{
			Key:    "ATF_LOGGER_GRPC",
			Level:  "required",
			Set:    envAnySet("ATF_LOGGER_GRPC"),
			Reason: "logger gRPC address (log ingest and query)",
			Hint:   "ATF_LOGGER_GRPC=logger:50052 (set by docker-compose.yml)",
		},
		{
			Key:    "ATF_API_KEY",
			Level:  "recommended",
			Set:    envAnySet("ATF_API_KEY"),
			Reason: "pre-shared key for scripted/CLI clients",
			Hint:   "atf setup generates ATF_API_KEY automatically, then: atf system restart",
		},
		{
			Key:    "ATF_WEB_HEALTH_URL",
			Level:  "recommended",
			Set:    envAnySet("ATF_WEB_HEALTH_URL"),
			Reason: "reachable web gateway health URL, probed by /api/health/services",
			Hint:   "ATF_WEB_HEALTH_URL=http://web:8088/healthz (set by docker-compose.yml)",
		},
		{
			Key:    "ATF_ADDR",
			Level:  "optional",
			Set:    envAnySet("ATF_API_ADDR"),
			Reason: "listen address (defaults to :8080)",
			Hint:   "leave unset unless the port must change",
		},
		{
			Key:    "ATF_BOT_TOKEN",
			Level:  "optional",
			Set:    envAnySet("ATF_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
			Reason: "only for bare-metal API deployments that exchange bot tokens themselves (compose puts the bot in core)",
			Hint:   "not needed with docker compose",
		},
	}
	return append(checks, rateLimitEnvChecks()...)
}

// missingRequired returns the required keys that are not set.
func missingRequired(checks []EnvCheck) []string {
	return missingAtLevel(checks, "required")
}

// missingRecommended returns the recommended keys that are not set.
func missingRecommended(checks []EnvCheck) []string {
	return missingAtLevel(checks, "recommended")
}

func missingAtLevel(checks []EnvCheck, level string) []string {
	out := []string{}
	for _, c := range checks {
		if c.Level == level && !c.Set {
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
		"checks":              checks,
		"missing_required":    missing,
		"missing_recommended": missingRecommended(checks),
		"deployment":          "docker-compose",
		"ok":                  len(missing) == 0,
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

	// Redis is reported but never gates readiness: the limiter degrades to a
	// per-process fallback by design, and calling a deployment that is serving
	// traffic "not ready" because an accelerator is down is how operators learn
	// to ignore the readiness field.
	rlCtx, cancelRL := context.WithTimeout(r.Context(), 2*time.Second)
	rlSummary := h.rateLimitSummary(rlCtx)
	cancelRL()

	writeJSON(w, http.StatusOK, map[string]any{
		"ready":               ready,
		"ratelimit":           rlSummary,
		"time":                time.Now().UTC().Format(time.RFC3339),
		"product_version":     resolveProductVersion(),
		"api_version":         APIVersion,
		"commit_sha":          resolveCommitSHA(),
		"features":            VersionFeatures,
		"deployment":          "docker-compose",
		"checks":              checks,
		"missing_required":    missing,
		"missing_recommended": missingRecommended(checks),
		"services":            probes,
		"next_steps":          nextSteps,
	})
}

// readContractFallback locates the shipped contract relative to the process
// working directory (repo root in production, api/ under `go test`).
func readContractFallback() ([]byte, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return nil, err
	}
	dir := cwd
	for depth := 0; depth < 4; depth++ {
		candidate := filepath.Join(dir, "docs", "openapi.yaml")
		if b, rerr := os.ReadFile(candidate); rerr == nil {
			return b, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return nil, os.ErrNotExist
}

// OpenAPIDoc serves the API's own contract at /api/openapi.yaml. The file is
// baked into the image next to the code that serves it, so the published
// document cannot drift from the binary (a copy in the website was exactly
// the drift the Phase-2 audit found).
func (h *Handlers) OpenAPIDoc(w http.ResponseWriter, r *http.Request) {
	path := os.Getenv("ATF_OPENAPI_PATH")
	if path == "" {
		path = "/etc/atf/openapi.yaml"
	}
	data, err := os.ReadFile(path)
	if err != nil {
		// Tests and dev runs read the repo copy relative to CWD; walk up a
		// couple of levels so the doc resolves whether CWD is the repo root
		// or the api module directory.
		data, err = readContractFallback()
	}
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "openapi document unavailable", "error_code": "openapi_missing"})
		return
	}
	w.Header().Set("Content-Type", "application/yaml")
	w.Header().Set("Cache-Control", "public, max-age=300")
	_, _ = w.Write(data)
}
