package aiprovider

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"atf/worker/internal/domain/aiconfig"
)

// openai_test.go — the OpenAI-compatible adapter against a REAL HTTP
// server (httptest), the fixture set the parity contract requires:
// 200 happy, 401, 429, 500, timeout, malformed JSON, empty choices.
//
// The adapter is driven through the production Provider methods, never a
// hand-built request, so a fixture exercises the code path the worker
// runs.

// fixedServer answers every POST with one canned handler.
func fixedServer(status int, body string) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
}

// recordingServer records the requests and answers 200 with a canned body.
func recordingServer(body string, seen *map[string]string, bodies *[]string) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		(*bodies) = append(*bodies, string(raw))
		(*seen)["path"] = r.URL.Path
		(*seen)["auth"] = r.Header.Get("Authorization")
		(*seen)["referer"] = r.Header.Get("HTTP-Referer")
		(*seen)["title"] = r.Header.Get("X-Title")
		(*seen)["content_type"] = r.Header.Get("Content-Type")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, body)
	}))
}

func chatResponse(content string) string {
	return `{"choices":[{"message":{"role":"assistant","content":` + jsonQuote(content) + `}}]}`
}

func jsonQuote(s string) string {
	out, _ := json.Marshal(s)
	return string(out)
}

func testCfg(provider, baseURL string) *aiconfig.AIConfig {
	return &aiconfig.AIConfig{
		ID: "cfg-1", Provider: provider, Model: "m", APIKey: "sk-test",
		BaseURL: baseURL, SystemPrompt: "edit", UserPromptTemplate: "{text}",
		Temperature: 0.7, IsEnabled: true, OwnerUserID: 1,
	}
}

func TestOpenAIHappyPath(t *testing.T) {
	srv := fixedServer(200, chatResponse("  rewritten text  "))
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	got, err := p.Rewrite(context.Background(), testCfg("openai", srv.URL), "original")
	if err != nil {
		t.Fatalf("Rewrite: %v", err)
	}
	// providers.py:170 returns .strip() — the adapter trims the answer.
	if got != "rewritten text" {
		t.Errorf("Rewrite: got %q, want the trimmed content", got)
	}
}

func TestOpenAIRequestShapeAndHeaders(t *testing.T) {
	seen := map[string]string{}
	var bodies []string
	srv := recordingServer(chatResponse("ok"), &seen, &bodies)
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("openai", srv.URL), "hello")
	if err != nil {
		t.Fatalf("Rewrite: %v", err)
	}
	if seen["path"] != "/v1/chat/completions" {
		// The adapter appends /v1 to a bare test URL (BaseURL's contract,
		// providers.py:140), so the fixture sees the versioned path.
		t.Errorf("path: got %q, want /v1/chat/completions", seen["path"])
	}
	if seen["auth"] != "Bearer sk-test" {
		t.Errorf("auth: got %q", seen["auth"])
	}
	if seen["content_type"] != "application/json" {
		t.Errorf("content type: got %q", seen["content_type"])
	}
	if len(bodies) != 1 {
		t.Fatalf("wanted 1 request, got %d", len(bodies))
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(bodies[0]), &payload); err != nil {
		t.Fatalf("payload is not JSON: %v", err)
	}
	if payload["model"] != "m" {
		t.Errorf("model: got %v", payload["model"])
	}
	if payload["stream"] != false {
		t.Errorf("stream: got %v, want false", payload["stream"])
	}
	messages, ok := payload["messages"].([]any)
	if !ok || len(messages) != 2 {
		t.Fatalf("messages: got %v", payload["messages"])
	}
	first, _ := messages[0].(map[string]any)
	second, _ := messages[1].(map[string]any)
	if first["role"] != "system" || first["content"] != "edit" {
		t.Errorf("system message: got %v", first)
	}
	if second["role"] != "user" || second["content"] != "hello" {
		t.Errorf("user message: got %v", second)
	}
}

func TestOpenAIOpenRouterAttributionHeaders(t *testing.T) {
	seen := map[string]string{}
	var bodies []string
	srv := recordingServer(chatResponse("ok"), &seen, &bodies)
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("openrouter", srv.URL), "hello")
	if err != nil {
		t.Fatalf("Rewrite: %v", err)
	}
	if seen["referer"] != "https://github.com/Opselon/AutoTelegramForward" {
		t.Errorf("HTTP-Referer: got %q", seen["referer"])
	}
	if seen["title"] != "AutoTelegramForward" {
		t.Errorf("X-Title: got %q", seen["title"])
	}
}

func TestOpenAINonOpenRouterHasNoAttributionHeaders(t *testing.T) {
	seen := map[string]string{}
	var bodies []string
	srv := recordingServer(chatResponse("ok"), &seen, &bodies)
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	_, _ = p.Rewrite(context.Background(), testCfg("openai", srv.URL), "hello")
	if seen["referer"] != "" || seen["title"] != "" {
		t.Errorf("attribution headers must be OpenRouter-only: %v", seen)
	}
}

func TestOpenAI401IsNonRetryableAuthError(t *testing.T) {
	srv := fixedServer(401, `{"error":"Incorrect API key"}`)
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("openai", srv.URL), "hello")
	if err == nil {
		t.Fatal("401: expected an error")
	}
	typed, ok := err.(*Error)
	if !ok {
		t.Fatalf("401: expected *Error, got %T", err)
	}
	if typed.Code != AuthError {
		t.Errorf("401: code %q, want AUTH_ERROR", typed.Code)
	}
	if typed.Retryable {
		t.Error("401: must be non-retryable — a retry will not fix a bad key")
	}
	if typed.Status != 401 {
		t.Errorf("401: status %d", typed.Status)
	}
}

func TestOpenAI429IsRetryableRateLimit(t *testing.T) {
	srv := fixedServer(429, `{"error":"rate limited"}`)
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("openai", srv.URL), "hello")
	if err == nil {
		t.Fatal("429: expected an error")
	}
	typed := err.(*Error)
	if typed.Code != RateLimit {
		t.Errorf("429: code %q, want RATE_LIMIT", typed.Code)
	}
	if !typed.Retryable {
		t.Error("429: must be retryable")
	}
}

func TestOpenAI500RetriesThenFailsAsProviderError(t *testing.T) {
	// A 500 on every attempt: the retry loop exhausts its budget and
	// surfaces the typed error. The retry sleeps are capped small here
	// because the loop's own backoff (2s, 4s) is the production
	// posture; the test just pays it.
	srv := fixedServer(500, "internal error")
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	start := time.Now()
	_, err := p.Rewrite(context.Background(), testCfg("openai", srv.URL), "hello")
	if err == nil {
		t.Fatal("500: expected an error")
	}
	typed := err.(*Error)
	if typed.Code != ProviderError {
		t.Errorf("500: code %q, want PROVIDER_ERROR", typed.Code)
	}
	// The backoff the port applies is capped at 8s; with two retries the
	// floor is 2s+4s=6s. Allow slack but catch a missing backoff (the
	// whole point of the retry budget) at the low end.
	if elapsed := time.Since(start); elapsed < 5*time.Second {
		t.Errorf("500: elapsed %s — the retry backoff did not run", elapsed)
	}
}

func TestOpenAIMalformedJSONIsProviderError(t *testing.T) {
	srv := fixedServer(200, "this is not json at all")
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("openai", srv.URL), "hello")
	if err == nil {
		t.Fatal("malformed body: expected an error")
	}
	typed := err.(*Error)
	if typed.Code != ProviderError {
		t.Errorf("malformed body: code %q, want PROVIDER_ERROR", typed.Code)
	}
	if typed.Retryable {
		t.Error("malformed body: a 200 with a broken body is a contract violation, not a transient blip")
	}
}

func TestOpenAIEmptyChoicesIsProviderError(t *testing.T) {
	srv := fixedServer(200, `{"choices":[]}`)
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("openai", srv.URL), "hello")
	if err == nil {
		t.Fatal("empty choices: expected an error")
	}
	typed := err.(*Error)
	if typed.Code != ProviderError {
		t.Errorf("empty choices: code %q, want PROVIDER_ERROR", typed.Code)
	}
	if !strings.Contains(typed.Message, "no choices") && !strings.Contains(typed.Message, "choices") {
		t.Errorf("empty choices: message %q should name the missing choices", typed.Message)
	}
}

func TestOpenAITimeoutIsTimeoutCode(t *testing.T) {
	// A server that never answers: the request deadline expires inside
	// the client and the adapter classifies it TIMEOUT.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(2 * time.Second)
		w.WriteHeader(200)
	}))
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: &http.Client{Timeout: 100 * time.Millisecond}}}
	_, err := p.Rewrite(context.Background(), testCfg("openai", srv.URL), "hello")
	if err == nil {
		t.Fatal("timeout: expected an error")
	}
	typed, ok := err.(*Error)
	if !ok {
		t.Fatalf("timeout: expected *Error, got %T: %v", err, err)
	}
	if typed.Code != Timeout {
		t.Errorf("timeout: code %q, want TIMEOUT", typed.Code)
	}
}

func TestOpenAICustomWithoutBaseURLIsInvalidConfig(t *testing.T) {
	p := &openAICompatibleProvider{http: newHTTPClient()}
	_, err := p.Rewrite(context.Background(), testCfg("custom", ""), "hello")
	if err == nil {
		t.Fatal("custom with no base_url: expected an error")
	}
	typed := err.(*Error)
	if typed.Code != InvalidConfig {
		t.Errorf("custom: code %q, want INVALID_CONFIG", typed.Code)
	}
}

func TestBaseURLResolution(t *testing.T) {
	cases := []struct {
		name, provider, in, want string
	}{
		{"config url wins", "openai", "https://example.com", "https://example.com/v1"},
		{"no suffix gets /v1", "openai", "https://api.openai.com", "https://api.openai.com/v1"},
		{"trailing slash trimmed", "openai", "https://api.openai.com/", "https://api.openai.com/v1"},
		{"v1 kept as-is", "openai", "https://api.openai.com/v1", "https://api.openai.com/v1"},
		{"v1beta kept", "gemini", "https://host/v1beta", "https://host/v1beta"},
		{"v2 kept", "openai", "https://host/v2", "https://host/v2"},
		{"api kept", "openai", "https://host/api", "https://host/api"},
		{"default for 9router", "9router", "", "http://sub.legoten.com:4455/v1"},
		{"default for ninerouter", "ninerouter", "", "http://sub.legoten.com:4455/v1"},
		{"default for groq", "groq", "", "https://api.groq.com/openai/v1"},
		{"custom with nothing is empty", "custom", "", ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := BaseURL(&aiconfig.AIConfig{Provider: c.provider, BaseURL: c.in})
			if got != c.want {
				t.Errorf("BaseURL(%q, %q): got %q, want %q", c.provider, c.in, got, c.want)
			}
		})
	}
}

func TestFactoryDispatch(t *testing.T) {
	f := NewFactory()
	for _, name := range []string{"openai", "groq", "deepseek", "openrouter", "9router", "ninerouter", "xai", "mistral", "together", "fireworks", "custom"} {
		adapter, err := f.Get(&aiconfig.AIConfig{Provider: name, BaseURL: "https://example.com"})
		if err != nil {
			t.Errorf("Get(%q): %v", name, err)
			continue
		}
		if _, ok := adapter.(*openAICompatibleProvider); !ok {
			t.Errorf("Get(%q): got %T, want the OpenAI-compatible adapter", name, adapter)
		}
	}
	for _, name := range []string{"anthropic", "gemini"} {
		adapter, err := f.Get(&aiconfig.AIConfig{Provider: name})
		if err != nil {
			t.Errorf("Get(%q): %v", name, err)
			continue
		}
		switch adapter.(type) {
		case *anthropicProvider, *geminiProvider:
		default:
			t.Errorf("Get(%q): got %T, want the family adapter", name, adapter)
		}
	}
}

func TestFactoryUnknownProviderIsInvalidConfig(t *testing.T) {
	f := NewFactory()
	_, err := f.Get(&aiconfig.AIConfig{Provider: "not-a-provider"})
	if err == nil {
		t.Fatal("unknown provider: expected an error")
	}
	typed := err.(*Error)
	if typed.Code != InvalidConfig {
		t.Errorf("unknown provider: code %q, want INVALID_CONFIG", typed.Code)
	}
	if !strings.Contains(typed.Message, "not-a-provider") {
		t.Errorf("unknown provider: message %q should name the provider", typed.Message)
	}
}

func TestFactoryRegistersAndLists(t *testing.T) {
	f := NewFactory()
	f.Register("probe", func() Provider { return &openAICompatibleProvider{http: newHTTPClient()} })
	names := f.Providers()
	found := false
	for _, name := range names {
		if name == "probe" {
			found = true
		}
	}
	if !found {
		t.Errorf("Providers(): %v does not include the registered probe", names)
	}
	adapter, err := f.Get(&aiconfig.AIConfig{Provider: "probe", BaseURL: "https://example.com"})
	if err != nil || adapter == nil {
		t.Errorf("Get(probe): got %v, %v", adapter, err)
	}
	if !f.KnownProvider("probe") || f.KnownProvider("nope") {
		t.Errorf("KnownProvider disagreed")
	}
}

func TestClassifyStatusContract(t *testing.T) {
	cases := []struct {
		status    int
		wantCode  ErrorCode
		wantRetry bool
	}{
		{429, RateLimit, true},
		{401, AuthError, false},
		{403, AuthError, false},
		{408, ProviderError, true},
		{425, ProviderError, true},
		{500, ProviderError, true},
		{502, ProviderError, true},
		{503, ProviderError, true},
		{504, ProviderError, true},
		// A 4xx that is neither 401/403/429: not retryable (a retry
		// cannot fix a malformed request), classified PROVIDER_ERROR.
		{400, ProviderError, false},
		{404, ProviderError, false},
	}
	for _, c := range cases {
		got := ClassifyStatus(c.status, "detail")
		if got.Code != c.wantCode || got.Retryable != c.wantRetry {
			t.Errorf("ClassifyStatus(%d): got %s retryable=%v, want %s retryable=%v",
				c.status, got.Code, got.Retryable, c.wantCode, c.wantRetry)
		}
	}
}

func TestChatCompleteRawMessages(t *testing.T) {
	seen := map[string]string{}
	var bodies []string
	srv := recordingServer(chatResponse("raw"), &seen, &bodies)
	defer srv.Close()

	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	messages := []Message{
		{Role: "system", Content: "sys"},
		{Role: "user", Content: "u1"},
		{Role: "assistant", Content: "a1"},
		{Role: "user", Content: "u2"},
	}
	got, err := p.ChatComplete(context.Background(), testCfg("openai", srv.URL), messages, 0.2)
	if err != nil {
		t.Fatalf("ChatComplete: %v", err)
	}
	if got != "raw" {
		t.Errorf("ChatComplete: got %q", got)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(bodies[0]), &payload); err != nil {
		t.Fatalf("payload: %v", err)
	}
	list, _ := payload["messages"].([]any)
	if len(list) != 4 {
		t.Errorf("messages: got %d, want the four verbatim", len(list))
	}
	if payload["temperature"] != 0.2 {
		t.Errorf("temperature: got %v, want 0.2 (the caller's, not the config's)", payload["temperature"])
	}
}

func TestHealthCheck(t *testing.T) {
	srv := fixedServer(200, chatResponse("pong"))
	defer srv.Close()
	p := &openAICompatibleProvider{http: &httpClient{client: srv.Client()}}
	ok, err := p.HealthCheck(context.Background(), testCfg("openai", srv.URL))
	if err != nil || !ok {
		t.Errorf("HealthCheck: ok=%v err=%v", ok, err)
	}
}
