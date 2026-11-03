package aiprovider

import (
	"context"
	"strings"

	"atf/worker/internal/domain/aiconfig"
)

// anthropicProvider speaks the Anthropic /messages contract
// (providers.py:196). The differences that matter: the key rides an
// x-api-key header with a pinned anthropic-version, the system prompt is
// a top-level field instead of a message, and the answer is the
// content[0].text array element.
type anthropicProvider struct {
	http *httpClient
}

// AnthropicDefaultURL is the family's default endpoint (providers.py:197).
const AnthropicDefaultURL = "https://api.anthropic.com/v1"

// AnthropicAPIVersion is the pinned protocol version every request
// carries (providers.py:205). Anthropic rejects a request without it.
const AnthropicAPIVersion = "2023-06-01"

func (p *anthropicProvider) endpoint(cfg *aiconfig.AIConfig) string {
	base := AnthropicDefaultURL
	if cfg != nil && strings.TrimSpace(cfg.BaseURL) != "" {
		base = strings.TrimRight(strings.TrimSpace(cfg.BaseURL), "/")
	}
	return base + "/messages"
}

func (p *anthropicProvider) headers(cfg *aiconfig.AIConfig) map[string]string {
	return map[string]string{
		"x-api-key":         cfg.APIKey,
		"anthropic-version": AnthropicAPIVersion,
	}
}

func (p *anthropicProvider) Rewrite(ctx context.Context, cfg *aiconfig.AIConfig, text string) (string, error) {
	user := strings.ReplaceAll(cfg.EffectiveUserPromptTemplate(), "{text}", text)
	messages := []Message{{Role: "user", Content: user}}
	return p.ChatComplete(ctx, cfg, messages, cfg.Temperature)
}

// ChatComplete posts the /messages payload. Anthropic has no
// chat-completions endpoint; the payload is the /messages shape
// (model / max_tokens / temperature / system / messages) and the
// adapter reads content[0].text from the answer.
func (p *anthropicProvider) ChatComplete(ctx context.Context, cfg *aiconfig.AIConfig, messages []Message, temperature float64) (string, error) {
	url := p.endpoint(cfg)
	payload := map[string]any{
		"model":       cfg.Model,
		"max_tokens":  4096,
		"temperature": temperature,
		"system":      cfg.SystemPrompt,
		"messages":    messages,
	}
	data, err := postJSON(ctx, p.http, url, p.headers(cfg), payload)
	if err != nil {
		return "", err
	}
	return anthropicContent(data)
}

// HealthCheck is the same single-word probe the Python adapter makes
// (providers.py:221).
func (p *anthropicProvider) HealthCheck(ctx context.Context, cfg *aiconfig.AIConfig) (bool, error) {
	_, err := p.Rewrite(ctx, cfg, "ping")
	if err != nil {
		return false, err
	}
	return true, nil
}

// anthropicContent mines data["content"][0]["text"] (providers.py:217).
func anthropicContent(data map[string]any) (string, error) {
	if data == nil {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Anthropic response: empty body"}
	}
	blocks, ok := data["content"].([]any)
	if !ok || len(blocks) == 0 {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Anthropic response: no content blocks"}
	}
	first, ok := blocks[0].(map[string]any)
	if !ok {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Anthropic response: content block is not an object"}
	}
	text, ok := first["text"].(string)
	if !ok {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Anthropic response: no text"}
	}
	return strings.TrimSpace(text), nil
}

var _ Provider = (*anthropicProvider)(nil)

// geminiProvider speaks the Gemini generateContent contract
// (providers.py:226): the key is a query parameter, the prompt pair is
// systemInstruction + contents.parts, and the answer comes out of
// candidates[0].content.parts[0].text.
type geminiProvider struct {
	http *httpClient
}

// GeminiDefaultURL is the family's default endpoint (providers.py:227).
const GeminiDefaultURL = "https://generativelanguage.googleapis.com/v1beta"

func (p *geminiProvider) endpoint(cfg *aiconfig.AIConfig) string {
	base := GeminiDefaultURL
	if cfg != nil && strings.TrimSpace(cfg.BaseURL) != "" {
		base = strings.TrimRight(strings.TrimSpace(cfg.BaseURL), "/")
	}
	return base + "/models/" + cfg.Model + ":generateContent?key=" + cfg.APIKey
}

func (p *geminiProvider) Rewrite(ctx context.Context, cfg *aiconfig.AIConfig, text string) (string, error) {
	user := strings.ReplaceAll(cfg.EffectiveUserPromptTemplate(), "{text}", text)
	messages := []Message{{Role: "user", Content: user}}
	return p.ChatComplete(ctx, cfg, messages, cfg.Temperature)
}

// ChatComplete posts the generateContent payload. The messages arrive in
// the chat-completions shape and are carried into Gemini's contents
// array; only the roles the contract accepts are meaningful, the same
// leniency the Python adapter has (it posts a single user message).
func (p *geminiProvider) ChatComplete(ctx context.Context, cfg *aiconfig.AIConfig, messages []Message, temperature float64) (string, error) {
	url := p.endpoint(cfg)
	contents := make([]map[string]any, 0, len(messages))
	for _, m := range messages {
		contents = append(contents, map[string]any{
			"role":  m.Role,
			"parts": []map[string]any{{"text": m.Content}},
		})
	}
	payload := map[string]any{
		"systemInstruction": map[string]any{
			"parts": []map[string]any{{"text": cfg.SystemPrompt}},
		},
		"contents": contents,
		"generationConfig": map[string]any{
			"temperature": temperature,
		},
	}
	data, err := postJSON(ctx, p.http, url, map[string]string{}, payload)
	if err != nil {
		return "", err
	}
	return geminiContent(data)
}

// HealthCheck is the same single-word probe the Python adapter makes
// (providers.py:247).
func (p *geminiProvider) HealthCheck(ctx context.Context, cfg *aiconfig.AIConfig) (bool, error) {
	_, err := p.Rewrite(ctx, cfg, "ping")
	if err != nil {
		return false, err
	}
	return true, nil
}

// geminiContent mines data["candidates"][0]["content"]["parts"][0]["text"]
// (providers.py:243).
func geminiContent(data map[string]any) (string, error) {
	if data == nil {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Gemini response: empty body"}
	}
	candidates, ok := data["candidates"].([]any)
	if !ok || len(candidates) == 0 {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Gemini response: no candidates"}
	}
	first, ok := candidates[0].(map[string]any)
	if !ok {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Gemini response: candidate is not an object"}
	}
	content, ok := first["content"].(map[string]any)
	if !ok {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Gemini response: no content"}
	}
	parts, ok := content["parts"].([]any)
	if !ok || len(parts) == 0 {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Gemini response: no parts"}
	}
	part, ok := parts[0].(map[string]any)
	if !ok {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Gemini response: part is not an object"}
	}
	text, ok := part["text"].(string)
	if !ok {
		return "", &Error{Code: ProviderError, Retryable: false, Message: "unexpected Gemini response: no text"}
	}
	return strings.TrimSpace(text), nil
}

var _ Provider = (*geminiProvider)(nil)
