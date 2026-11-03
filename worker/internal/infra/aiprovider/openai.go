package aiprovider

import (
	"context"
	"strings"

	"atf/worker/internal/domain/aiconfig"
)

// openAICompatibleProvider covers every endpoint that speaks the
// OpenAI /chat/completions contract: openai, groq, deepseek, openrouter,
// 9router, ninerouter, xai, mistral, together, fireworks and any custom
// base_url (providers.py:116).
type openAICompatibleProvider struct {
	http *httpClient
	// defaultBaseURL is the provider-keyed default the adapter falls
	// back to when the config carries no base_url of its own.
	defaultBaseURL string
}

// DefaultBaseURLs is the provider → default endpoint table
// (providers.py:120-133). 9router and ninerouter are the same host: the
// task names the family, the endpoint is the relay either spelling
// points at.
var DefaultBaseURLs = map[string]string{
	"openai":     "https://api.openai.com/v1",
	"groq":       "https://api.groq.com/openai/v1",
	"deepseek":   "https://api.deepseek.com/v1",
	"openrouter": "https://openrouter.ai/api/v1",
	"9router":    "http://sub.legoten.com:4455/v1",
	"ninerouter": "http://sub.legoten.com:4455/v1",
	"xai":        "https://api.x.ai/v1",
	"mistral":    "https://api.mistral.ai/v1",
	"together":   "https://api.together.xyz/v1",
	"fireworks":  "https://api.fireworks.ai/inference/v1",
	"custom":     "",
}

// BaseURL resolves the endpoint (providers.py:134): the config's own
// base_url, else the provider family's default, with /v1 appended when
// the URL does not already end in a versioned or /api suffix.
//
// The /v1 append is load-bearing, not cosmetic: a tenant who pastes
// "https://api.openai.com" gets the versioned endpoint, while a tenant
// who pasted the full URL is not double-suffixed — the same rule the
// Python adapter applies.
func BaseURL(cfg *aiconfig.AIConfig) string {
	if cfg == nil {
		return ""
	}
	url := strings.TrimSpace(cfg.BaseURL)
	if url == "" {
		url = DefaultBaseURLs[strings.ToLower(strings.TrimSpace(cfg.Provider))]
	}
	url = strings.TrimRight(url, "/")
	if url == "" {
		return ""
	}
	for _, suffix := range []string{"/v1", "/v1beta", "/v2", "/v3", "/api"} {
		if strings.HasSuffix(url, suffix) {
			return url
		}
	}
	return url + "/v1"
}

// Headers builds the request headers (providers.py:144): the Bearer
// authorization every OpenAI-compatible endpoint gates on, plus
// OpenRouter's attribution pair (HTTP-Referer / X-Title), which that
// provider reads for ranking and routing and which no other family
// consults.
func Headers(cfg *aiconfig.AIConfig) map[string]string {
	if cfg == nil {
		return map[string]string{}
	}
	headers := map[string]string{
		"Authorization": "Bearer " + cfg.APIKey,
	}
	if strings.EqualFold(strings.TrimSpace(cfg.Provider), "openrouter") {
		headers["HTTP-Referer"] = "https://github.com/Opselon/AutoTelegramForward"
		headers["X-Title"] = "AutoTelegramForward"
	}
	return headers
}

// Rewrite builds the config's prompt pair into a chat-completions
// request (providers.py:153): the model, the temperature, the
// system/user message pair and stream:false.
func (p *openAICompatibleProvider) Rewrite(ctx context.Context, cfg *aiconfig.AIConfig, text string) (string, error) {
	base := BaseURL(cfg)
	if base == "" {
		return "", &Error{Code: InvalidConfig, Retryable: false, Message: "custom provider requires base_url to be set"}
	}
	// The prompt pair comes from the config's own templates: the adapter
	// replaces {text} in the user template the way the Python adapter
	// does, and the transformer's anti-injection envelope is applied by
	// the caller that built `text` — never here.
	user := strings.ReplaceAll(cfg.EffectiveUserPromptTemplate(), "{text}", text)
	messages := []Message{
		{Role: "system", Content: cfg.SystemPrompt},
		{Role: "user", Content: user},
	}
	return p.ChatComplete(ctx, cfg, messages, cfg.Temperature)
}

// ChatComplete posts the messages verbatim and reads
// choices[0].message.content (providers.py:174).
func (p *openAICompatibleProvider) ChatComplete(ctx context.Context, cfg *aiconfig.AIConfig, messages []Message, temperature float64) (string, error) {
	base := BaseURL(cfg)
	if base == "" {
		return "", &Error{Code: InvalidConfig, Retryable: false, Message: "custom provider requires base_url to be set"}
	}
	url := base + "/chat/completions"
	payload := map[string]any{
		"model":       cfg.Model,
		"temperature": temperature,
		"messages":    messages,
		"stream":      false,
	}
	data, err := postJSON(ctx, p.http, url, Headers(cfg), payload)
	if err != nil {
		return "", err
	}
	content, err := chatCompletionsContent(data)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(content), nil
}

// HealthCheck answers whether the endpoint answers at all
// (providers.py:191): one rewrite of the word "ping", success meaning
// the key and the model both work.
func (p *openAICompatibleProvider) HealthCheck(ctx context.Context, cfg *aiconfig.AIConfig) (bool, error) {
	_, err := p.Rewrite(ctx, cfg, "ping")
	if err != nil {
		return false, err
	}
	return true, nil
}

// chatCompletionsContent mines choices[0].message.content out of a
// decoded response (providers.py:170): the KeyError/IndexError family
// in Python becomes a typed PROVIDER_ERROR here, non-retryable because
// a 200 with an empty choices array is a contract violation, not a
// transient failure.
func chatCompletionsContent(data map[string]any) (string, error) {
	if data == nil {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected chat-completions response: empty body"}
	}
	choices, ok := data["choices"].([]any)
	if !ok || len(choices) == 0 {
		// An empty-choices 200 pins the "malformed JSON / empty choices"
		// parity case; the error stays typed and non-retryable.
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected chat-completions response: no choices"}
	}
	first, ok := choices[0].(map[string]any)
	if !ok {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected chat-completions response: choice is not an object"}
	}
	message, ok := first["message"].(map[string]any)
	if !ok {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected chat-completions response: no message"}
	}
	content, ok := message["content"].(string)
	if !ok {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected chat-completions response: no content"}
	}
	return content, nil
}

// Compile-time assertion that the adapter satisfies the contract.
var _ Provider = (*openAICompatibleProvider)(nil)
