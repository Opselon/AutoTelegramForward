package handlers

import (
	"errors"
	"strings"
	"testing"
	"time"

	pb "autoforward/proto"
)

func TestAnalyzeRulesCatchesSelfLoop(t *testing.T) {
	rules := []*pb.ForwardRule{{
		Id: "r1", Name: "loop", SourceChatId: "-100", TargetChatId: "-100", SessionId: "s1",
	}}
	f := analyzeRules(rules, map[string]bool{"s1": true}, nil)
	if len(f) != 1 || f[0].Severity != "error" || f[0].ID != "rule.self_loop:r1" {
		t.Fatalf("expected one self-loop error, got %+v", f)
	}
}

func TestAnalyzeRulesCatchesMissingAIConfig(t *testing.T) {
	rules := []*pb.ForwardRule{{
		Id: "r2", Name: "ai", SourceChatId: "-1", TargetChatId: "-2", SessionId: "s1",
		AiConfigId: "ghost", IsActive: true,
	}}
	f := analyzeRules(rules, map[string]bool{"s1": true}, map[string]*pb.AIConfig{})
	if len(f) != 1 || f[0].ID != "rule.ai_missing:r2" || f[0].Severity != "error" {
		t.Fatalf("expected missing-AI error, got %+v", f)
	}
}

func TestAnalyzeRulesFlagsDropFallback(t *testing.T) {
	rules := []*pb.ForwardRule{{
		Id: "r3", Name: "ai", SourceChatId: "-1", TargetChatId: "-2", SessionId: "s1",
		AiConfigId: "cfg", AiFallbackPolicy: "DROP", IsActive: true,
	}}
	cfgs := map[string]*pb.AIConfig{"cfg": {Id: "cfg", Name: "ok", IsEnabled: true, Model: "gpt-4o-mini"}}
	f := analyzeRules(rules, map[string]bool{"s1": true}, cfgs)
	if len(f) != 1 || f[0].ID != "rule.ai_drop:r3" || f[0].Severity != "warning" {
		t.Fatalf("expected DROP warning, got %+v", f)
	}
}

// The whole point of per-task AI settings: one task with AI must never leak a
// finding onto a sibling task that has AI off.
func TestAnalyzeRulesIsPerTask(t *testing.T) {
	rules := []*pb.ForwardRule{
		{Id: "ai-on", Name: "needs ai", SourceChatId: "-1", TargetChatId: "-2", SessionId: "s1",
			AiConfigId: "cfg", AiFallbackPolicy: "SEND_ORIGINAL", IsActive: true},
		{Id: "ai-off", Name: "plain", SourceChatId: "-1", TargetChatId: "-3", SessionId: "s1"},
	}
	cfgs := map[string]*pb.AIConfig{"cfg": {Id: "cfg", Name: "ok", IsEnabled: true, Model: "gpt-4o-mini"}}
	f := analyzeRules(rules, map[string]bool{"s1": true}, cfgs)
	if len(f) != 0 {
		t.Fatalf("healthy per-task config should produce no findings, got %+v", f)
	}
}

func TestGrade(t *testing.T) {
	if s, sc := grade(nil); s != "healthy" || sc != 100 {
		t.Fatalf("empty => healthy/100, got %s/%d", s, sc)
	}
	if s, _ := grade([]Finding{{Severity: "warning"}}); s != "warning" {
		t.Fatalf("warning expected, got %s", s)
	}
	if s, sc := grade([]Finding{{Severity: "error"}}); s != "critical" || sc != 80 {
		t.Fatalf("error => critical/80, got %s/%d", s, sc)
	}
	// A pile of errors must not drive the score below zero.
	many := make([]Finding, 0, 10)
	for i := 0; i < 10; i++ {
		many = append(many, Finding{Severity: "error"})
	}
	if _, sc := grade(many); sc != 0 {
		t.Fatalf("score floor should be 0, got %d", sc)
	}
}

func TestProbeAIEndpoint(t *testing.T) {
	// Empty base_url means "provider default" — nothing to probe, no finding.
	if err := probeAIEndpoint("", time.Second); err != nil {
		t.Fatalf("empty base_url should be skipped, got %v", err)
	}
	// A closed port on loopback must be reported as unreachable.
	if err := probeAIEndpoint("http://127.0.0.1:1/v1", time.Second); err == nil {
		t.Fatal("expected unreachable error for closed port")
	}
	// A malformed URL is reported, not swallowed.
	if err := probeAIEndpoint("not a url", time.Second); err == nil {
		t.Fatal("expected error for malformed base_url")
	}
}

// A hostname the container cannot resolve must produce docker-network advice,
// not the generic "fix the address" hint — that misdiagnosis is what made a
// sibling-stack gateway look like a broken provider address.
func TestIsNameResolutionFailure(t *testing.T) {
	resolution := []string{
		"dial tcp: lookup 9router on 127.0.0.11:53: server misbehaving",
		`dial tcp: lookup nope: no such host`,
		"dial tcp: Temporary failure in name resolution",
	}
	for _, msg := range resolution {
		if !isNameResolutionFailure(errors.New(msg)) {
			t.Errorf("should be a resolution failure: %q", msg)
		}
	}
	refused := []string{
		"dial tcp 127.0.0.1:4455: connect: connection refused",
		"dial tcp 10.0.0.5:20128: i/o timeout",
	}
	for _, msg := range refused {
		if isNameResolutionFailure(errors.New(msg)) {
			t.Errorf("should NOT be a resolution failure: %q", msg)
		}
	}
	if isNameResolutionFailure(nil) {
		t.Error("nil error is not a resolution failure")
	}
}

func TestAIRemediationSplitsAdvice(t *testing.T) {
	dns := aiReachabilityRemediation(errors.New("dial tcp: lookup 9router on 127.0.0.11:53: server misbehaving"))
	if !strings.Contains(dns, "network") || !strings.Contains(dns, "external") {
		t.Fatalf("DNS failure should advise a network attachment, got %q", dns)
	}
	refused := aiReachabilityRemediation(errors.New("dial tcp 127.0.0.1:4455: connect: connection refused"))
	if strings.Contains(refused, "external") {
		t.Fatalf("a refusing endpoint should get the address hint, got %q", refused)
	}
	if !strings.Contains(refused, "address/port") {
		t.Fatalf("expected the generic address hint, got %q", refused)
	}
}
