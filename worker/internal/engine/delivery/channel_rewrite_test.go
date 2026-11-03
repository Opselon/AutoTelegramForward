package delivery

import (
	"context"
	"strings"
	"testing"

	"atf/worker/internal/domain/aiconfig"
	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/prompt"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/infra/aiprovider"
	"atf/worker/internal/infra/aiprovider/circuit"
	"atf/worker/internal/infra/aiprovider/validator"
)

// fakePromptStore implements prompt.Store in memory for tests.
type fakePromptStore struct {
	templates map[string]*prompt.PromptTemplate
	versions  map[string]map[int]*prompt.PromptVersion
}

func newFakePromptStore() *fakePromptStore {
	return &fakePromptStore{
		templates: make(map[string]*prompt.PromptTemplate),
		versions:  make(map[string]map[int]*prompt.PromptVersion),
	}
}

func (s *fakePromptStore) addTemplate(t *prompt.PromptTemplate, versions ...*prompt.PromptVersion) {
	s.templates[t.ID] = t
	if len(versions) > 0 {
		if s.versions[t.ID] == nil {
			s.versions[t.ID] = make(map[int]*prompt.PromptVersion)
		}
		for _, v := range versions {
			s.versions[t.ID][v.Version] = v
		}
	}
}

func (s *fakePromptStore) GetTemplate(ctx context.Context, id string, owner int64) (*prompt.PromptTemplate, error) {
	t, ok := s.templates[id]
	if !ok {
		return nil, nil
	}
	if !t.IsSystem && t.OwnerUserID != owner {
		return nil, nil
	}
	return t, nil
}

func (s *fakePromptStore) GetVersion(ctx context.Context, promptID string, ver int) (*prompt.PromptVersion, error) {
	vm, ok := s.versions[promptID]
	if !ok {
		return nil, nil
	}
	v, ok := vm[ver]
	if !ok {
		return nil, nil
	}
	return v, nil
}

func setupRewriteEngine(t *testing.T, outcomes ...fakeOutcome) (*Engine, *fakeProvider, *fakePromptStore) {
	t.Helper()
	fake := newFakeProvider(outcomes...)
	factory := aiprovider.NewFactory()
	factory.Register("openai", func() aiprovider.Provider { return fake })
	registry := circuit.NewRegistry()
	configs := map[string]*aiconfig.AIConfig{
		"cfg-ai": {
			ID: "cfg-ai", Provider: "openai", Model: "gpt-test",
			APIKey: "key-ai", SystemPrompt: "default system", UserPromptTemplate: "{text}",
			Temperature: 0.7, IsEnabled: true, OwnerUserID: 42,
		},
	}
	transformer := aiprovider.NewTransformer(&fakeAIConfigStore{configs: configs}, registry, validator.New(), factory)
	promptStore := newFakePromptStore()

	h := newHarness(t)
	h.engine.SetAITransformer(transformer, &fakeAIConfigStore{configs: configs}, nil)
	h.engine.SetPromptStore(promptStore)
	return h.engine, fake, promptStore
}

// TestChannelRewriteMultipleTargetsIndependentPrompts verifies that when one source
// feeds multiple targets with different effective prompts (via target-scoped overrides),
// each target gets its own prompt resolution, independent provider call, and independent
// transformed text without cross-target leakage.
func TestChannelRewriteMultipleTargetsIndependentPrompts(t *testing.T) {
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	engine, fake, promptStore := setupRewriteEngine(t)

	// Register two prompt templates:
	// p-french for Target A: translates/rewrites to French style
	promptStore.addTemplate(&prompt.PromptTemplate{
		ID:                 "p-french",
		Name:               "French Rewrite",
		SystemPrompt:       "Translate to French",
		UserPromptTemplate: "Texte: {text}",
		CurrentVersion:     1,
		OwnerUserID:        42,
	})
	// p-spanish for Target B: translates/rewrites to Spanish style (v2)
	promptStore.addTemplate(
		&prompt.PromptTemplate{
			ID:                 "p-spanish",
			Name:               "Spanish Rewrite",
			SystemPrompt:       "Translate to Spanish v2",
			UserPromptTemplate: "Texto: {text}",
			CurrentVersion:     2,
			OwnerUserID:        42,
		},
		&prompt.PromptVersion{
			ID:                 "v1",
			PromptID:           "p-spanish",
			Version:            1,
			SystemPrompt:       "Translate to Spanish v1",
			UserPromptTemplate: "Texto v1: {text}",
		},
		&prompt.PromptVersion{
			ID:                 "v2",
			PromptID:           "p-spanish",
			Version:            2,
			SystemPrompt:       "Translate to Spanish v2",
			UserPromptTemplate: "Texto v2: {text}",
		},
	)

	// Configure provider script:
	// First call (Target A with p-french) returns French output
	// Second call (Target B with p-spanish) returns Spanish output
	fake.script = []fakeOutcome{
		{Text: "Bonjour le monde"},
		{Text: "Hola mundo"},
	}

	// Task rule has default prompt p-french, but Target B (-1003) overrides to p-spanish (pinned to version 1)
	r := &rule.ForwardRule{
		ID:               "rule-1",
		IsActive:         true,
		OwnerUserID:      42,
		AIConfigID:       "cfg-ai",
		AIRewriteActive:  true,
		AIPromptID:       "p-french",
		AIPromptVersion:  1,
		AIFallbackPolicy: rule.FallbackSendOriginal,
		TargetPromptOverrides: map[string]rule.TargetPrompt{
			"-1003": {PromptID: "p-spanish", PromptVersion: 1},
		},
	}

	rawText := "Hello world"

	// Create Job A for Target -1002 (should use p-french)
	jobA := &job.DeliveryJob{
		ID:              "rule-1:src:1:-1002",
		RuleID:          "rule-1",
		SourceChatID:    "src",
		SourceMessageID: 1,
		TargetChatID:    "-1002",
		Payload: *job.NewPayload(map[string]any{
			"raw_text":       rawText,
			"text":           rawText,
			"correlation_id": "corr-target-a",
			"owner_user_id":  int64(42),
		}),
	}

	// Create Job B for Target -1003 (should use p-spanish version 1)
	jobB := &job.DeliveryJob{
		ID:              "rule-1:src:1:-1003",
		RuleID:          "rule-1",
		SourceChatID:    "src",
		SourceMessageID: 1,
		TargetChatID:    "-1003",
		Payload: *job.NewPayload(map[string]any{
			"raw_text":       rawText,
			"text":           rawText,
			"correlation_id": "corr-target-b",
			"owner_user_id":  int64(42),
		}),
	}

	logger := &testLogger{}

	// Execute Transform for Target A
	if err := engine.applyTransformPipeline(context.Background(), jobA, r, logger); err != nil {
		t.Fatalf("applyTransformPipeline jobA failed: %v", err)
	}

	// Execute Transform for Target B
	if err := engine.applyTransformPipeline(context.Background(), jobB, r, logger); err != nil {
		t.Fatalf("applyTransformPipeline jobB failed: %v", err)
	}

	// 1. Verify provider call count
	if fake.Calls() != 2 {
		t.Fatalf("expected 2 provider calls, got %d", fake.Calls())
	}

	// 2. Verify payloads passed to provider
	if len(fake.payloads) != 2 {
		t.Fatalf("expected 2 recorded payloads, got %d", len(fake.payloads))
	}
	// Call 1 (Target A) used p-french
	if !strings.Contains(fake.payloads[0], "Translate to French") {
		t.Errorf("call 0 expected French system prompt, got: %s", fake.payloads[0])
	}
	// Call 2 (Target B) used p-spanish version 1
	if !strings.Contains(fake.payloads[1], "Translate to Spanish v1") {
		t.Errorf("call 1 expected Spanish v1 system prompt, got: %s", fake.payloads[1])
	}

	// 3. Verify distinct transformed text in each job
	textA, _ := jobA.Payload.Raw()["text"].(string)
	textB, _ := jobB.Payload.Raw()["text"].(string)

	if textA != "Bonjour le monde" {
		t.Errorf("jobA text mismatch: got %q, want 'Bonjour le monde'", textA)
	}
	if textB != "Hola mundo" {
		t.Errorf("jobB text mismatch: got %q, want 'Hola mundo'", textB)
	}

	// 4. Verify original payload raw_text remains immutable
	rawA, _ := jobA.Payload.Raw()["raw_text"].(string)
	rawB, _ := jobB.Payload.Raw()["raw_text"].(string)
	if rawA != "Hello world" || rawB != "Hello world" {
		t.Errorf("original raw_text was mutated: rawA=%q, rawB=%q", rawA, rawB)
	}
}

// TestChannelRewritePipelineOrdering verifies the Stage 4.2+ pipeline ordering:
// Cleaner -> AI Rewrite -> Replacements -> Header/Footer.
func TestChannelRewritePipelineOrdering(t *testing.T) {
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	engine, fake, _ := setupRewriteEngine(t)
	fake.script = []fakeOutcome{
		{Text: "rewritten body with target_replace"},
	}

	r := &rule.ForwardRule{
		ID:              "rule-order",
		IsActive:        true,
		OwnerUserID:     42,
		AIConfigID:      "cfg-ai",
		AIRewriteActive: true,
		Transform: cleaning.Config{
			Cleaner: &cleaning.CleanerRule{
				IsActive: true,
				Patterns: []string{"unwanted_junk"},
			},
			Replacements: []cleaning.ReplacementEntry{
				{Find: "target_replace", Replace: "FINAL_DONE"},
			},
			Header: "HEADER\n",
			Footer: "\nFOOTER",
		},
	}

	j := &job.DeliveryJob{
		ID:           "job-order",
		RuleID:       "rule-order",
		TargetChatID: "-1002",
		Payload: *job.NewPayload(map[string]any{
			"raw_text":       "unwanted_junk please rewrite me",
			"text":           "unwanted_junk please rewrite me",
			"correlation_id": "corr-order",
		}),
	}

	if err := engine.applyTransformPipeline(context.Background(), j, r, &testLogger{}); err != nil {
		t.Fatalf("applyTransformPipeline failed: %v", err)
	}

	// Provider should have seen cleaned text ("please rewrite me") without "unwanted_junk"
	if len(fake.payloads) != 1 {
		t.Fatalf("expected 1 provider call, got %d", len(fake.payloads))
	}
	if strings.Contains(fake.payloads[0], "unwanted_junk") {
		t.Errorf("AI provider saw uncleaned text: %s", fake.payloads[0])
	}
	if !strings.Contains(fake.payloads[0], "please rewrite me") {
		t.Errorf("AI provider did not see cleaned text: %s", fake.payloads[0])
	}

	// Result text should have replacements applied to the AI output, wrapped in header/footer
	outText, _ := j.Payload.Raw()["text"].(string)
	want := "HEADER\n\nrewritten body with FINAL_DONE\n\nFOOTER"
	if outText != want {
		t.Errorf("ordering mismatch:\ngot:  %q\nwant: %q", outText, want)
	}
}

// TestChannelRewriteOwnerScopedPromptRefusal verifies that a prompt owned by another
// tenant (not system) fails resolution with a warning and falls back to default prompt
// rather than dropping the message.
func TestChannelRewriteOwnerScopedPromptRefusal(t *testing.T) {
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	engine, fake, promptStore := setupRewriteEngine(t)
	fake.script = []fakeOutcome{
		{Text: "rewritten with fallback prompt"},
	}

	// Foreign prompt owned by 99
	promptStore.addTemplate(&prompt.PromptTemplate{
		ID:                 "p-foreign",
		Name:               "Foreign Prompt",
		SystemPrompt:       "Foreign Secret System",
		UserPromptTemplate: "{text}",
		CurrentVersion:     1,
		OwnerUserID:        99,
	})

	r := &rule.ForwardRule{
		ID:              "rule-foreign",
		IsActive:        true,
		OwnerUserID:     42, // Owner is 42, not 99
		AIConfigID:      "cfg-ai",
		AIRewriteActive: true,
		AIPromptID:      "p-foreign",
	}

	j := &job.DeliveryJob{
		ID:           "job-foreign",
		RuleID:       "rule-foreign",
		TargetChatID: "-1002",
		Payload: *job.NewPayload(map[string]any{
			"raw_text":       "original message",
			"text":           "original message",
			"correlation_id": "corr-foreign",
		}),
	}

	logger := &testLogger{}
	if err := engine.applyTransformPipeline(context.Background(), j, r, logger); err != nil {
		t.Fatalf("applyTransformPipeline failed: %v", err)
	}

	// Should not have used the foreign secret prompt
	if len(fake.payloads) != 1 {
		t.Fatalf("expected 1 call, got %d", len(fake.payloads))
	}
	if strings.Contains(fake.payloads[0], "Foreign Secret System") {
		t.Errorf("foreign secret system prompt leaked: %s", fake.payloads[0])
	}

	// Warning should be logged
	foundWarn := false
	for _, l := range logger.lines {
		if strings.Contains(l, "could not resolve prompt template") {
			foundWarn = true
			break
		}
	}
	if !foundWarn {
		t.Errorf("expected warning log for unresolved foreign prompt, got logs: %v", logger.lines)
	}

	outText, _ := j.Payload.Raw()["text"].(string)
	if outText != "rewritten with fallback prompt" {
		t.Errorf("got %q, want 'rewritten with fallback prompt'", outText)
	}
}
