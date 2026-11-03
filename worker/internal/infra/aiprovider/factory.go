package aiprovider

import (
	"fmt"
	"strings"

	"atf/worker/internal/domain/aiconfig"
)

// Factory is the registry-based dispatch (providers.py:252): a provider
// name resolves to the adapter that speaks its contract. Registering a
// new family never touches a caller, which is the whole point of the
// Python registry and preserved here.
type Factory struct {
	// registry maps the provider key to the constructor that builds its
	// adapter. Constructors, not instances: the adapters are stateless,
	// so a new one per call is free and a mutated test override cannot
	// leak across calls.
	registry map[string]func() Provider
}

// NewFactory builds the default registry (providers.py:273): every
// OpenAI-compatible family maps to the same adapter, with its own
// default base_url; anthropic and gemini map to their own.
func NewFactory() *Factory {
	openAICompatible := func() Provider {
		return &openAICompatibleProvider{http: newHTTPClient()}
	}
	return &Factory{
		registry: map[string]func() Provider{
			"openai":     openAICompatible,
			"groq":       openAICompatible,
			"deepseek":   openAICompatible,
			"openrouter": openAICompatible,
			"9router":    openAICompatible,
			"ninerouter": openAICompatible,
			"xai":        openAICompatible,
			"mistral":    openAICompatible,
			"together":   openAICompatible,
			"fireworks":  openAICompatible,
			"custom":     openAICompatible,
			"anthropic": func() Provider {
				return &anthropicProvider{http: newHTTPClient()}
			},
			"gemini": func() Provider {
				return &geminiProvider{http: newHTTPClient()}
			},
		},
	}
}

// Register installs (or replaces) one provider's adapter constructor.
// The parity harness uses it to swap in the deterministic in-process
// fake provider without touching production code paths.
func (f *Factory) Register(name string, build func() Provider) {
	if f == nil || f.registry == nil {
		return
	}
	f.registry[strings.ToLower(strings.TrimSpace(name))] = build
}

// Providers lists every registered family, sorted, mirroring the
// Python factory's providers() (used by the bot UI's picker).
func (f *Factory) Providers() []string {
	if f == nil || f.registry == nil {
		return nil
	}
	out := make([]string, 0, len(f.registry))
	for name := range f.registry {
		out = append(out, name)
	}
	sortStrings(out)
	return out
}

// Get returns the adapter for the config's provider
// (providers.py:298). An unknown name is a typed INVALID_CONFIG: the
// rewrite orchestrator treats it as non-retryable, because no amount of
// retrying makes "anthropicx" a real provider.
func (f *Factory) Get(cfg *aiconfig.AIConfig) (Provider, error) {
	if f == nil || cfg == nil {
		return nil, &Error{Code: InvalidConfig, Retryable: false, Message: "no AI config"}
	}
	key := strings.ToLower(strings.TrimSpace(cfg.Provider))
	build, ok := f.registry[key]
	if !ok {
		return nil, &Error{
			Code:      InvalidConfig,
			Retryable: false,
			Message:   fmt.Sprintf("unknown AI provider: %s", key),
		}
	}
	return build(), nil
}

// ModelsFor is the family → model list the bot UI's picker shows
// (providers.py:256). The Worker itself never consults it — the model
// comes from the config row — but it is the vocabulary of legal
// families, so it lives beside the registry that defines them.
var ModelsFor = map[string][]string{
	"openai":     {"gpt-5-mini", "gpt-5", "gpt-4.1-mini", "gpt-4o-mini"},
	"anthropic":  {"claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"},
	"gemini":     {"gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"},
	"groq":       {"llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct"},
	"deepseek":   {"deepseek-chat", "deepseek-reasoner"},
	"openrouter": {"openai/gpt-5-mini", "anthropic/claude-sonnet-4-5", "google/gemini-2.5-flash"},
	"9router":    {"coding", "auto", "gemini/gemini-3.8-flash", "mistral", "Free"},
	"ninerouter": {"coding", "auto", "gemini/gemini-3.8-flash", "mistral", "Free"},
	"xai":        {"grok-4", "grok-3-mini"},
	"mistral":    {"mistral-large-latest", "mistral-small-latest"},
	"together":   {"meta-llama/Llama-3.3-70B-Instruct-Turbo"},
	"fireworks":  {"accounts/fireworks/models/llama-v3p3-70b-instruct"},
	"custom":     {"coding", "gpt-4o-mini", "model"},
}

// KnownProvider reports whether the factory can build an adapter for the
// name. The rewrite orchestrator consults it before it resolves a row,
// so an unknown family is refused before any network is spent.
func (f *Factory) KnownProvider(name string) bool {
	if f == nil || f.registry == nil {
		return false
	}
	_, ok := f.registry[strings.ToLower(strings.TrimSpace(name))]
	return ok
}

// sortStrings is a local sort so the package does not pull in sort for
// one call site; the lists are tiny (13 families).
func sortStrings(list []string) {
	for i := 1; i < len(list); i++ {
		for j := i; j > 0 && list[j-1] > list[j]; j-- {
			list[j-1], list[j] = list[j], list[j-1]
		}
	}
}
