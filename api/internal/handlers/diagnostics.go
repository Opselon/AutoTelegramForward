package handlers

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"

	pb "autoforward/proto"
)

// ---------------------------------------------------------------------------
// Preflight diagnostics — the "catch problems before they bite" algorithm.
//
// The dashboard calls GET /api/v1/diagnostics on load. It walks the caller's
// live configuration (rules, sessions, AI configs) plus service reachability
// and returns ranked findings with a severity and a remediation hint, so a
// silent misconfiguration (an AI rewrite pointing at a deleted config, a rule
// forwarding a channel onto itself, a task whose session is gone) surfaces as
// an explicit, actionable card instead of a forward that quietly never lands.
// ---------------------------------------------------------------------------

// Finding is one diagnostic observation about the configured system.
type Finding struct {
	ID          string `json:"id"`
	Severity    string `json:"severity"` // error | warning | info
	Scope       string `json:"scope"`    // service | session | ai | rule
	ScopeID     string `json:"scope_id,omitempty"`
	Title       string `json:"title"`
	Detail      string `json:"detail"`
	Remediation string `json:"remediation"`
}

// severityRank orders findings most-severe-first and scores the health grade.
func severityRank(sev string) int {
	switch sev {
	case "error":
		return 0
	case "warning":
		return 1
	default:
		return 2
	}
}

// analyzeRules is the pure core of the diagnostics engine: given a user's
// rules, the set of session ids they own, and their AI configs (by id), it
// returns every configuration defect it can prove. Kept free of I/O so it is
// deterministic and unit-testable.
func analyzeRules(
	rules []*pb.ForwardRule,
	ownedSessions map[string]bool,
	configs map[string]*pb.AIConfig,
) []Finding {
	findings := make([]Finding, 0, len(rules))

	for _, r := range rules {
		if r == nil {
			continue
		}
		label := r.Name
		if label == "" {
			label = r.Id
		}

		// 1. A task forwarding a chat onto itself can never terminate; it
		//    re-ingests its own output.
		if r.SourceChatId != "" && r.SourceChatId == r.TargetChatId {
			findings = append(findings, Finding{
				ID:          "rule.self_loop:" + r.Id,
				Severity:    "error",
				Scope:       "rule",
				ScopeID:     r.Id,
				Title:       fmt.Sprintf("%q forwards a chat onto itself", label),
				Detail:      "source and target chat are both " + r.SourceChatId + ", so each forwarded message is re-ingested.",
				Remediation: "Point the task at a different target chat, or disable it.",
			})
		}

		// 2. A task with no destination silently swallows every matched message.
		if r.TargetChatId == "" && len(r.TargetChatIds) == 0 {
			findings = append(findings, Finding{
				ID:          "rule.no_target:" + r.Id,
				Severity:    "error",
				Scope:       "rule",
				ScopeID:     r.Id,
				Title:       fmt.Sprintf("%q has no target chat", label),
				Detail:      "matched messages have nowhere to go and are dropped after evaluation.",
				Remediation: "Set a target chat for this task.",
			})
		}

		// 3. The session powering a task may have been terminated.
		if r.SessionId != "" && !ownedSessions[r.SessionId] {
			findings = append(findings, Finding{
				ID:          "rule.dead_session:" + r.Id,
				Severity:    "error",
				Scope:       "rule",
				ScopeID:     r.Id,
				Title:       fmt.Sprintf("%q is bound to a missing session", label),
				Detail:      "session " + r.SessionId + " is not among your active sessions; the task will never receive updates.",
				Remediation: "Re-link the session in the dashboard, or rebind the task to a live session.",
			})
		}

		// 4. AI rewrite enabled but pointed at a config that is gone, which
		//    makes the whole feature a confusing no-op.
		aiEnabled := r.AiConfigId != ""
		if aiEnabled {
			cfg, ok := configs[r.AiConfigId]
			switch {
			case !ok:
				findings = append(findings, Finding{
					ID:          "rule.ai_missing:" + r.Id,
					Severity:    "error",
					Scope:       "ai",
					ScopeID:     r.Id,
					Title:       fmt.Sprintf("%q references a deleted AI config", label),
					Detail:      "ai_config_id " + r.AiConfigId + " no longer exists; messages forward unrewritten.",
					Remediation: "Pick an existing AI config for this task, or turn AI rewrite off.",
				})
			case !cfg.IsEnabled:
				findings = append(findings, Finding{
					ID:          "rule.ai_disabled:" + r.Id,
					Severity:    "warning",
					Scope:       "ai",
					ScopeID:     r.Id,
					Title:       fmt.Sprintf("%q uses a disabled AI config", label),
					Detail:      "config " + cfg.Name + " is switched off, so the rewrite step is skipped at runtime.",
					Remediation: "Enable the AI config, or assign an active one to this task.",
				})
			case strings.TrimSpace(cfg.Model) == "":
				findings = append(findings, Finding{
					ID:          "rule.ai_no_model:" + r.Id,
					Severity:    "error",
					Scope:       "ai",
					ScopeID:     r.Id,
					Title:       fmt.Sprintf("%q uses an AI config with no model", label),
					Detail:      "config " + cfg.Name + " has an empty model, so every rewrite call will fail.",
					Remediation: "Set a model on the AI config (e.g. gpt-4o-mini).",
				})
			}
		}

		// 5. DROP + AI: if the provider is briefly down, messages vanish.
		if aiEnabled && strings.EqualFold(strings.TrimSpace(r.AiFallbackPolicy), "DROP") {
			findings = append(findings, Finding{
				ID:          "rule.ai_drop:" + r.Id,
				Severity:    "warning",
				Scope:       "ai",
				ScopeID:     r.Id,
				Title:       fmt.Sprintf("%q drops messages when AI rewrite fails", label),
				Detail:      "fallback policy is DROP, so a provider outage silently discards traffic instead of forwarding it unchanged.",
				Remediation: "Switch the fallback policy to SEND_ORIGINAL (or RETRY) unless dropping is intentional.",
			})
		}

		// 6. AI rewrite configured but the task is paused — easy to misread as
		//    "AI is on" while nothing forwards.
		if aiEnabled && !r.IsActive {
			findings = append(findings, Finding{
				ID:          "rule.ai_inactive:" + r.Id,
				Severity:    "info",
				Scope:       "rule",
				ScopeID:     r.Id,
				Title:       fmt.Sprintf("%q has AI rewrite but is paused", label),
				Detail:      "the task is inactive, so its AI settings have no effect until it is resumed.",
				Remediation: "Resume the task if AI rewriting should be live.",
			})
		}
	}

	sort.SliceStable(findings, func(i, j int) bool {
		ri, rj := severityRank(findings[i].Severity), severityRank(findings[j].Severity)
		if ri != rj {
			return ri < rj
		}
		return findings[i].ID < findings[j].ID
	})
	return findings
}

// probeAIEndpoint does a short TCP reachability check against an AI config's
// base_url. A self-hosted gateway that is bound to another docker network (or
// simply down) is the single most common cause of "AI rewrite does nothing",
// because every rewrite call fails and the fallback policy then decides what
// happens to the message. Catching it here turns a silent outage into a card.
func probeAIEndpoint(baseURL string, timeout time.Duration) error {
	raw := strings.TrimSpace(baseURL)
	if raw == "" {
		return nil // provider default endpoint; nothing to probe
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return fmt.Errorf("base_url %q is not a valid URL", raw)
	}
	host := u.Hostname()
	port := u.Port()
	if port == "" {
		if u.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	conn, err := net.DialTimeout("tcp", net.JoinHostPort(host, port), timeout)
	if err != nil {
		return fmt.Errorf("%s is unreachable: %v", net.JoinHostPort(host, port), err)
	}
	_ = conn.Close()
	return nil
}

// isNameResolutionFailure reports whether a dial error is the container failing
// to resolve the hostname rather than the target refusing the connection. The
// two need different advice: a sibling docker stack's hostname only resolves for
// containers actually attached to its network.
func isNameResolutionFailure(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	for _, marker := range []string{
		"no such host",
		"server misbehaving",
		"temporary failure in name resolution",
		"name resolution",
		"nodename nor servname provided",
		"lookup ",
	} {
		if strings.Contains(msg, marker) {
			return true
		}
	}
	return false
}

// aiReachabilityRemediation turns a probe failure into advice a human can act
// on. A hostname that does not resolve from the probing container is almost
// always a missing docker network attachment, which the generic "fix the
// address" hint would send the operator chasing the wrong thing.
func aiReachabilityRemediation(err error) string {
	if isNameResolutionFailure(err) {
		return "The provider hostname does not resolve from the api container. If the gateway runs as a sibling docker stack, attach this compose service to that stack's network (declare it `external: true` and add it under the service's `networks:`); otherwise use a hostname or address this container can reach."
	}
	return "Fix the provider address/port, or point the AI config at a reachable gateway."
}

// grade collapses findings into a single headline status + 0-100 score.
func grade(findings []Finding) (string, int) {
	score := 100
	errs, warns := 0, 0
	for _, f := range findings {
		switch f.Severity {
		case "error":
			errs++
			score -= 20
		case "warning":
			warns++
			score -= 7
		}
	}
	if score < 0 {
		score = 0
	}
	switch {
	case errs > 0:
		return "critical", score
	case warns > 0:
		return "warning", score
	default:
		return "healthy", score
	}
}

// Diagnostics runs the full preflight and returns ranked, actionable findings.
func (h *Handlers) Diagnostics(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	id := identity(r)

	findings := make([]Finding, 0, 8)

	// --- service reachability (cheap, parallel-ish: short per-call timeouts) ---
	services := make([]map[string]any, 0, 3)

	coreOK := false
	if resp, err := h.c.System.GetSystemStats(ctx, &pb.SystemStatsRequest{}); err == nil && resp.CoreRunning {
		coreOK = true
		services = append(services, map[string]any{"name": "core", "online": true})
	} else {
		detail := "gRPC core unreachable"
		if err != nil {
			detail = err.Error()
		}
		services = append(services, map[string]any{"name": "core", "online": false, "detail": detail})
		findings = append(findings, Finding{
			ID: "service.core_down", Severity: "error", Scope: "service",
			Title:       "Core engine is unreachable",
			Detail:      "the Python pipeline did not answer GetSystemStats: " + detail,
			Remediation: "Check that the atf-core container is running and healthy.",
		})
	}

	// --- sessions ---
	ownedSessions := map[string]bool{}
	if resp, err := h.c.Sessions.ListSessions(ctx, &pb.ListSessionsRequest{OwnerUserId: id.UserID}); err == nil {
		for _, s := range resp.Sessions {
			if s != nil {
				ownedSessions[s.Id] = true
			}
		}
	}
	if len(ownedSessions) == 0 {
		findings = append(findings, Finding{
			ID: "session.none", Severity: "info", Scope: "session",
			Title:       "No active sessions",
			Detail:      "no Telegram account is currently linked, so no task can receive messages.",
			Remediation: "Link an account from the dashboard (Scan / login) to start forwarding.",
		})
	}

	// --- AI configs ---
	configs := map[string]*pb.AIConfig{}
	if coreOK {
		if resp, err := h.c.AI.ListAIConfigs(ctx, &pb.ListAIConfigsRequest{OwnerUserId: id.UserID}); err == nil {
			for _, c := range resp.Configs {
				if c != nil {
					configs[c.Id] = c
				}
			}
		}
	}

	// --- AI provider reachability (the #1 cause of "AI rewrite does nothing") ---
	for _, c := range configs {
		if !c.IsEnabled {
			continue
		}
		if err := probeAIEndpoint(c.BaseUrl, 3*time.Second); err != nil {
			findings = append(findings, Finding{
				ID:          "ai.endpoint_unreachable:" + c.Id,
				Severity:    "error",
				Scope:       "ai",
				ScopeID:     c.Id,
				Title:       fmt.Sprintf("AI config %q cannot reach its provider", c.Name),
				Detail:      "base_url " + c.BaseUrl + " — " + err.Error() + "; every rewrite on tasks using this config will fail.",
				Remediation: aiReachabilityRemediation(err),
			})
		}
	}

	// --- rules ---
	var rules []*pb.ForwardRule
	if coreOK {
		if resp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{OwnerUserId: id.UserID}); err == nil {
			rules = resp.Rules
		}
	}
	findings = append(findings, analyzeRules(rules, ownedSessions, configs)...)

	status, score := grade(findings)
	counts := map[string]int{"error": 0, "warning": 0, "info": 0}
	for _, f := range findings {
		counts[f.Severity]++
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"status":       status,
		"score":        score,
		"generated_at": time.Now().UTC().Format(time.RFC3339),
		"counts":       counts,
		"services":     services,
		"scanned": map[string]int{
			"rules":      len(rules),
			"sessions":   len(ownedSessions),
			"ai_configs": len(configs),
		},
		"findings": findings,
	})
}

// keep context import used even if build tags change.
var _ = context.Background
