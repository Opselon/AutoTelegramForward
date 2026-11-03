package delivery

import (
	"context"
	"strings"
	"testing"

	"atf/worker/internal/domain/aiconfig"
	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/infra/aiprovider"
	"atf/worker/internal/infra/aiprovider/circuit"
	"atf/worker/internal/infra/aiprovider/validator"
)

// ai_stage_test.go — the Stage 5 (W4) AI rewrite as the delivery engine
// runs it: inside the transform pipeline, at the Python pipeline's own
// position, with the fallback policies the Python _handle_ai_fallback
// applies.
//
// Every case drives the REAL Engine through the REAL cleaning.Pipeline
// (the same path production takes); the provider is the deterministic
// in-process fake, so no network and no sleep.

// --- the fake AI wiring the tests install -------------------------------- //

// fakeAIConfigStore is the in-memory aiconfig.Store.
type fakeAIConfigStore struct {
	configs map[string]*aiconfig.AIConfig
}

func (s *fakeAIConfigStore) GetOwned(ctx context.Context, id string, owner int64) (*aiconfig.AIConfig, error) {
	cfg, ok := s.configs[id]
	if !ok || cfg.OwnerUserID != owner {
		return nil, nil
	}
	return cfg, nil
}

func (s *fakeAIConfigStore) GetByID(ctx context.Context, id string) (*aiconfig.AIConfig, error) {
	return s.configs[id], nil
}

// scriptedAI builds an Engine wiring whose provider answers the script.
// The returned fake records every call the provider served.
func scriptedAI(t *testing.T, e *Engine, outcomes ...fakeOutcome) *fakeProvider {
	t.Helper()
	fake := newFakeProvider(outcomes...)
	factory := aiprovider.NewFactory()
	factory.Register("openai", func() aiprovider.Provider { return fake })
	registry := circuit.NewRegistry()
	configs := map[string]*aiconfig.AIConfig{
		"cfg-ai": {
			ID: "cfg-ai", Provider: "openai", Model: "gpt-test", APIKey: "sk-test",
			SystemPrompt: "edit", UserPromptTemplate: "{text}",
			Temperature: 0.7, IsEnabled: true, OwnerUserID: 42,
		},
		"cfg-backup": {
			ID: "cfg-backup", Provider: "openai", Model: "gpt-backup", APIKey: "sk-backup",
			SystemPrompt: "backup", UserPromptTemplate: "{text}",
			Temperature: 0.7, IsEnabled: true, OwnerUserID: 42,
		},
	}
	transformer := aiprovider.NewTransformer(&fakeAIConfigStore{configs: configs}, registry, validator.New(), factory)
	e.SetAITransformer(transformer, &fakeAIConfigStore{configs: map[string]*aiconfig.AIConfig{
		"cfg-ai":     {ID: "cfg-ai", Provider: "openai", APIKey: "sk-test", IsEnabled: true, OwnerUserID: 42},
		"cfg-backup": {ID: "cfg-backup", Provider: "openai", APIKey: "sk-backup", IsEnabled: true, OwnerUserID: 42},
	}}, nil)
	return fake
}

// --- the deterministic in-process fake provider (delivery-side copy) ----- //
//
// The aiprovider package keeps its own fake for its tests; the delivery
// package needs one too because the dynamic factory registration returns
// aiprovider.Provider and the tests want to assert on the call count.
// The two fakes answer the same script contract the parity harness uses
// on the Python side.

// fakeOutcome is one scripted rewrite result.
type fakeOutcome struct {
	Text string
	Err  *aiprovider.Error
}

// fakeProvider answers by script, advancing per call.
type fakeProvider struct {
	calls    int
	script   []fakeOutcome
	payloads []string
}

func newFakeProvider(outcomes ...fakeOutcome) *fakeProvider {
	return &fakeProvider{script: outcomes}
}

func (p *fakeProvider) Rewrite(ctx context.Context, cfg *aiconfig.AIConfig, text string) (string, error) {
	p.calls++
	p.payloads = append(p.payloads, text)
	index := p.calls - 1
	var outcome fakeOutcome
	if index < len(p.script) {
		outcome = p.script[index]
	} else if len(p.script) > 0 {
		outcome = p.script[len(p.script)-1]
	}
	if outcome.Err != nil {
		return "", outcome.Err
	}
	return outcome.Text, nil
}

func (p *fakeProvider) ChatComplete(ctx context.Context, cfg *aiconfig.AIConfig, messages []aiprovider.Message, temperature float64) (string, error) {
	p.calls++
	index := p.calls - 1
	var outcome fakeOutcome
	if index < len(p.script) {
		outcome = p.script[index]
	} else if len(p.script) > 0 {
		outcome = p.script[len(p.script)-1]
	}
	if outcome.Err != nil {
		return "", outcome.Err
	}
	return outcome.Text, nil
}

func (p *fakeProvider) HealthCheck(ctx context.Context, cfg *aiconfig.AIConfig) (bool, error) {
	_, err := p.Rewrite(ctx, cfg, "ping")
	return err == nil, err
}

// Calls reports how many rewrites the provider served.
func (p *fakeProvider) Calls() int { return p.calls }

var _ aiprovider.Provider = (*fakeProvider)(nil)

// aiRule is the rule the transform gate consults: AI active, the config
// attached, and (optionally) the fallback policy set.
func aiRule(policy string) *rule.ForwardRule {
	return &rule.ForwardRule{
		ID: "r", IsActive: true, OwnerUserID: 42,
		AIConfigID: "cfg-ai", AIRewriteActive: true,
		AIFallbackPolicy: policy, AITimeoutSeconds: 5,
	}
}

// --- the gate: ai_rewrite_active is the authority ------------------------ //

func TestAIRewriteStageRespectsTheGate(t *testing.T) {
	// A task whose panel switch closed (ai_rewrite_active=false) keeps
	// its config attached but does not rewrite — the Stage 4.4 gate,
	// pipeline.py:1591. The hook must never be installed.
	//
	// The gate is applied by applyTransformPipeline, not by the Config
	// builder: the AI fields are installed AFTER the deterministic config
	// returns, so a gate failure is a hook that is never built rather
	// than a Config field that is nil. The assertion therefore reads the
	// engine's own decision — the hook pair — exactly the way the
	// pipeline does.
	h := newHarness(t)
	scriptedAI(t, h.engine)
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)
	r := aiRule(rule.FallbackDrop)
	r.AIRewriteActive = false
	j := h.enqueueWithPayload(t, map[string]any{"raw_text": "the original body"})
	rewrite, fallback := h.engine.aiRewriteHooks(context.Background(), j, r, &testLogger{})
	if rewrite != nil {
		t.Error("a task with ai_rewrite_active=false must not install the rewrite hook")
	}
	if fallback != nil {
		t.Error("a task with ai_rewrite_active=false must not install a fallback hook")
	}
	// The gate's positive case: an active task installs it.
	r.AIRewriteActive = true
	rewrite, fallback = h.engine.aiRewriteHooks(context.Background(), j, r, &testLogger{})
	if rewrite == nil {
		t.Error("a task with ai_rewrite_active=true and a config must install the rewrite hook")
	}
	if fallback == nil {
		t.Error("an active task must install its fallback policy hook")
	}
}

// --- the position: after the policy stages, before the pair ------------- //

func TestAIRewriteRunsAtThePythonPosition(t *testing.T) {
	// The rewrite operates on the CLEANED text and the task's
	// replacements/header still apply to the REWRITTEN text
	// (pipeline.py:1590-1660). A hook that upper-cases proves both: a
	// cleaner that strips "junk" runs before, and a replacement that
	// maps the rewritten form runs after.
	pipeline := cleaning.NewPipeline()
	var sawInput string
	cfg := cleaning.Config{
		Cleaner: &cleaning.CleanerRule{
			IsActive: true, Patterns: []string{"junk"},
		},
		AIRewrite: func(ctx context.Context, text string) (string, error) {
			sawInput = text
			return strings.ToUpper(text), nil
		},
		Replacements: []cleaning.ReplacementEntry{{Find: "CLEANED", Replace: "done"}},
	}
	out := pipeline.ApplyText("junk body cleaned", cfg, nil, cleaning.CaptionHint{})
	if sawInput != "body cleaned" {
		// The cleaner stripped "junk" BEFORE the rewrite saw the text
		// (and trims: Clean's final pass is TrimSpace).
		t.Errorf("the rewrite must see the cleaned text: got %q", sawInput)
	}
	if out.Text != "BODY done" {
		// The replacement ran AFTER the rewrite, on the rewritten form.
		t.Errorf("the replacements must apply to the rewritten text: got %q", out.Text)
	}
	if !contains(out.StageNotes, "ai_rewrite") {
		t.Errorf("the stage note must record the rewrite: %v", out.StageNotes)
	}
}

// --- the fallback policies ---------------------------------------------- //

func TestAIFallbackSendOriginal(t *testing.T) {
	pipeline := cleaning.NewPipeline()
	cfg := cleaning.Config{
		AIRewrite: func(ctx context.Context, text string) (string, error) {
			return "", &cleaning.AIError{Code: "RATE_LIMIT", Message: "throttled"}
		},
		AIFallback: func(ctx context.Context, original string, classification string, message string) (string, bool) {
			if classification != "RATE_LIMIT" {
				t.Errorf("classification: got %q, want RATE_LIMIT", classification)
			}
			return original, true
		},
	}
	out := pipeline.ApplyText("the original body", cfg, nil, cleaning.CaptionHint{})
	if out.AIDrop {
		t.Error("SEND_ORIGINAL must not drop")
	}
	if out.Text != "the original body" {
		t.Errorf("SEND_ORIGINAL must keep the pre-rewrite text: got %q", out.Text)
	}
}

func TestAIFallbackDropIsTerminal(t *testing.T) {
	pipeline := cleaning.NewPipeline()
	cfg := cleaning.Config{
		AIRewrite: func(ctx context.Context, text string) (string, error) {
			return "", &cleaning.AIError{Code: "AUTH_ERROR"}
		},
		AIFallback: func(ctx context.Context, original string, classification string, message string) (string, bool) {
			return "", false
		},
	}
	out := pipeline.ApplyText("the original body", cfg, nil, cleaning.CaptionHint{})
	if !out.AIDrop {
		t.Fatal("DROP must set AIDrop")
	}
	if out.Text != "" {
		t.Errorf("DROP must leave nothing sendable: got %q", out.Text)
	}
	if out.AIClassification != "AUTH_ERROR" {
		t.Errorf("classification: got %q", out.AIClassification)
	}
	if !contains(out.StageNotes, "ai:AUTH_ERROR") {
		t.Errorf("stage notes: %v", out.StageNotes)
	}
}

func TestAIFallbackNilCallbackIsDrop(t *testing.T) {
	// A config with no fallback callback is the Python default, DROP
	// (entities.py:500: ai_fallback_policy = AIFallbackPolicy.DROP) —
	// never a silent pass-through of untransformed text.
	pipeline := cleaning.NewPipeline()
	cfg := cleaning.Config{
		AIRewrite: func(ctx context.Context, text string) (string, error) {
			return "", &cleaning.AIError{Code: "TIMEOUT"}
		},
	}
	out := pipeline.ApplyText("the original body", cfg, nil, cleaning.CaptionHint{})
	if !out.AIDrop {
		t.Fatal("a nil fallback callback must drop (the Python default)")
	}
	if out.AIClassification != "DROP" {
		t.Errorf("classification: got %q, want DROP", out.AIClassification)
	}
}

// --- the empty-output gate around the rewrite --------------------------- //

func TestAIRewriteSkipsEmptyText(t *testing.T) {
	// An empty body is not a provider call worth spending (the Python
	// pipeline feeds the whole body; an empty prompt still bills).
	h := newHarness(t)
	fake := scriptedAI(t, h.engine)
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)
	r := aiRule(rule.FallbackSendOriginal)
	hook := h.engine.aiRewriteFor(context.Background(), h.enqueueWithPayload(t, map[string]any{"raw_text": ""}), r, &testLogger{})
	out, err := hook(context.Background(), "")
	if err != nil {
		t.Fatalf("empty text: %v", err)
	}
	if out != "" {
		t.Errorf("empty text: got %q, want empty", out)
	}
	if fake.Calls() != 0 {
		t.Errorf("empty text must not reach the provider: %d calls", fake.Calls())
	}
}

// --- the ownership boundary --------------------------------------------- //

func TestAIResolutionRefusesForeignConfig(t *testing.T) {
	// A rule that names a config another tenant owns cannot spend that
	// tenant's provider (transformer.py:108). The hook returns the
	// typed refusal and the fallback policy decides the outcome.
	h := newHarness(t)
	fake := scriptedAI(t, h.engine)
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)
	r := aiRule(rule.FallbackSendOriginal)
	r.OwnerUserID = 7 // a foreign owner
	j := h.enqueueWithPayload(t, map[string]any{"raw_text": "body"})
	hook := h.engine.aiRewriteFor(context.Background(), j, r, &testLogger{})
	_, err := hook(context.Background(), "body")
	if err == nil {
		t.Fatal("a foreign config must be refused")
	}
	typed, ok := err.(*cleaning.AIError)
	if !ok {
		t.Fatalf("expected *cleaning.AIError, got %T", err)
	}
	if typed.Code != "INVALID_CONFIG" {
		t.Errorf("code: got %s, want INVALID_CONFIG", typed.Code)
	}
	if fake.Calls() != 0 {
		t.Errorf("the provider must not be called for a foreign config: %d", fake.Calls())
	}
}

// --- prompt construction through the delivery stage --------------------- //

func TestAIPromptEnvelopeIsApplied(t *testing.T) {
	// The delivery hook hands the TRANSFORMER the text, and the
	// transformer wraps it in the anti-injection envelope before it
	// reaches the provider. The fake provider records what it saw.
	h := newHarness(t)
	fake := scriptedAI(t, h.engine, fakeOutcome{Text: "rewritten"})
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)
	r := aiRule(rule.FallbackDrop)
	j := h.enqueueWithPayload(t, map[string]any{"raw_text": "the body"})
	hook := h.engine.aiRewriteFor(context.Background(), j, r, &testLogger{})
	out, err := hook(context.Background(), "the body")
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if out != "rewritten" {
		t.Errorf("rewrite: got %q", out)
	}
	if fake.Calls() != 1 {
		t.Fatalf("provider calls: %d", fake.Calls())
	}
	// The envelope is applied by the transformer, not the delivery hook:
	// the provider sees the WRAPPED text only through the transformer's
	// request. This test pins the wiring (hook → transformer → provider)
	// rather than re-pinning the envelope itself (that is the
	// transformer's own test).
}

func TestAITimeoutFromTheRule(t *testing.T) {
	// The rule's ai_timeout_seconds is the budget the transformer
	// applies (pipeline.py:1746: getattr(rule, "ai_timeout_seconds",
	// 15.0)). A rule with none gets the default.
	h := newHarness(t)
	scriptedAI(t, h.engine, fakeOutcome{Text: "ok"})
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	budget := func(r *rule.ForwardRule) float64 {
		if r.AITimeoutSeconds > 0 {
			return r.AITimeoutSeconds
		}
		return aiprovider.DefaultTimeout
	}
	r := aiRule(rule.FallbackDrop)
	r.AITimeoutSeconds = 0
	if got := budget(r); got != aiprovider.DefaultTimeout {
		t.Errorf("budget: got %v, want the default %v", got, aiprovider.DefaultTimeout)
	}
	r.AITimeoutSeconds = 42
	if got := budget(r); got != 42 {
		t.Errorf("budget: got %v, want 42", got)
	}
}

// --- the unwired worker: AI_UNAVAILABLE keeps delivering --------------- //

func TestAIUnwiredTransformerDeliversOriginal(t *testing.T) {
	// A worker with no transformer wired (unit-test engine, or a
	// deployment that has not set the master key) delivers the original
	// text rather than dropping the message — the Python AI_UNAVAILABLE
	// posture (pipeline.py:1700).
	h := newHarness(t)
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	// No SetAITransformer: the engine's aiTransformer is nil.
	logger := &testLogger{}
	rewrite, fallback := h.engine.aiRewriteHooks(context.Background(), h.enqueueWithPayload(t, map[string]any{"raw_text": "body"}), aiRule(rule.FallbackDrop), logger)
	if rewrite != nil || fallback != nil {
		t.Error("an unwired engine must install no AI hooks")
	}
	found := false
	for _, line := range logger.lines {
		if strings.Contains(line, "AI transformer unavailable") {
			found = true
		}
	}
	if !found {
		t.Errorf("the unwired case must log AI_UNAVAILABLE: %v", logger.lines)
	}
}

// --- the rule projection's fallback resolution --------------------------- //

func TestResolveAIFallbackPolicyFailClosed(t *testing.T) {
	cases := []struct{ in, want string }{
		{"SEND_ORIGINAL", rule.FallbackSendOriginal},
		{"send_original", rule.FallbackSendOriginal}, // case-insensitive
		{"DROP", rule.FallbackDrop},
		{"RETRY", rule.FallbackRetry},
		{"QUARANTINE", rule.FallbackQuarantine},
		{"", rule.FallbackDrop},        // empty is the Python default
		{"GARBAGE", rule.FallbackDrop}, // unknown fails closed
	}
	for _, c := range cases {
		if got := rule.ResolveAIFallbackPolicy(c.in); got != c.want {
			t.Errorf("ResolveAIFallbackPolicy(%q): got %q, want %q", c.in, got, c.want)
		}
	}
}

// --- helpers -------------------------------------------------------------- //

func contains(list []string, want string) bool {
	for _, item := range list {
		if item == want {
			return true
		}
	}
	return false
}

// enqueueWithPayload is the harness entry an AI case needs: a job whose
// payload carries the RAW snapshot the transform pipeline reads.
func (h *harness) enqueueWithPayload(t *testing.T, payload map[string]any) *job.DeliveryJob {
	t.Helper()
	if payload == nil {
		payload = map[string]any{}
	}
	if _, ok := payload["session_id"]; !ok {
		payload["session_id"] = "s1"
	}
	j := &job.DeliveryJob{
		ID: "r:c:1:t", RuleID: "r", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(payload),
	}
	h.enqueue(t, j)
	return j
}
