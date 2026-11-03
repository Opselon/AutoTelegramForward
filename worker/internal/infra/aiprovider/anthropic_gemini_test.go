package aiprovider

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// anthropic_gemini_test.go — the family adapters against REAL HTTP
// servers, pinning the payload shapes and the content-mining accessors
// that differ per family.

func TestAnthropicPayloadAndHeaders(t *testing.T) {
	var seen struct {
		path, apiKey, version string
		body                  string
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		seen.body = string(raw)
		seen.path = r.URL.Path
		seen.apiKey = r.Header.Get("x-api-key")
		seen.version = r.Header.Get("anthropic-version")
		w.WriteHeader(200)
		_, _ = io.WriteString(w, `{"content":[{"type":"text","text":"  anthropic answer  "}]}`)
	}))
	defer srv.Close()

	p := &anthropicProvider{http: &httpClient{client: srv.Client()}}
	cfg := testCfg("anthropic", srv.URL)
	got, err := p.Rewrite(context.Background(), cfg, "hello")
	if err != nil {
		t.Fatalf("Rewrite: %v", err)
	}
	if got != "anthropic answer" {
		t.Errorf("Rewrite: got %q", got)
	}
	if seen.path != "/messages" {
		t.Errorf("path: got %q, want /messages", seen.path)
	}
	if seen.apiKey != "sk-test" {
		t.Errorf("x-api-key: got %q", seen.apiKey)
	}
	if seen.version != "2023-06-01" {
		t.Errorf("anthropic-version: got %q", seen.version)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(seen.body), &payload); err != nil {
		t.Fatalf("payload: %v", err)
	}
	if payload["model"] != "m" {
		t.Errorf("model: %v", payload["model"])
	}
	// The system prompt is a TOP-LEVEL field on this family, not a message.
	if payload["system"] != "edit" {
		t.Errorf("system: %v", payload["system"])
	}
	if payload["max_tokens"] != float64(4096) {
		t.Errorf("max_tokens: %v", payload["max_tokens"])
	}
	messages, _ := payload["messages"].([]any)
	if len(messages) != 1 {
		t.Fatalf("messages: %v", payload["messages"])
	}
	first, _ := messages[0].(map[string]any)
	if first["role"] != "user" || first["content"] != "hello" {
		t.Errorf("user message: %v", first)
	}
}

func TestAnthropic401IsAuthError(t *testing.T) {
	srv := fixedServer(401, `{"error":"invalid x-api-key"}`)
	defer srv.Close()
	p := &anthropicProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("anthropic", srv.URL), "hello")
	if err == nil {
		t.Fatal("401: expected an error")
	}
	typed := err.(*Error)
	if typed.Code != AuthError || typed.Retryable {
		t.Errorf("401: got %s retryable=%v, want AUTH_ERROR non-retryable", typed.Code, typed.Retryable)
	}
}

func TestAnthropicMalformedContentIsProviderError(t *testing.T) {
	srv := fixedServer(200, `{"content":[]}`)
	defer srv.Close()
	p := &anthropicProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("anthropic", srv.URL), "hello")
	if err == nil {
		t.Fatal("empty content: expected an error")
	}
	if err.(*Error).Code != ProviderError {
		t.Errorf("code %q, want PROVIDER_ERROR", err.(*Error).Code)
	}
}

func TestGeminiPayloadAndKeyInQuery(t *testing.T) {
	var seen struct {
		path, query string
		body        string
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		seen.body = string(raw)
		seen.path = r.URL.Path
		seen.query = r.URL.RawQuery
		w.WriteHeader(200)
		_, _ = io.WriteString(w, `{"candidates":[{"content":{"parts":[{"text":"  gemini answer  "}]}}]}`)
	}))
	defer srv.Close()

	p := &geminiProvider{http: &httpClient{client: srv.Client()}}
	cfg := testCfg("gemini", srv.URL)
	got, err := p.Rewrite(context.Background(), cfg, "hello")
	if err != nil {
		t.Fatalf("Rewrite: %v", err)
	}
	if got != "gemini answer" {
		t.Errorf("Rewrite: got %q", got)
	}
	if seen.path != "/models/m:generateContent" {
		t.Errorf("path: got %q", seen.path)
	}
	if seen.query != "key=sk-test" {
		t.Errorf("query: got %q, want the key as the query parameter", seen.query)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(seen.body), &payload); err != nil {
		t.Fatalf("payload: %v", err)
	}
	sys, _ := payload["systemInstruction"].(map[string]any)
	parts, _ := sys["parts"].([]any)
	if len(parts) != 1 {
		t.Fatalf("systemInstruction parts: %v", sys)
	}
	first, _ := parts[0].(map[string]any)
	if first["text"] != "edit" {
		t.Errorf("systemInstruction text: %v", first["text"])
	}
	contents, _ := payload["contents"].([]any)
	if len(contents) != 1 {
		t.Fatalf("contents: %v", payload["contents"])
	}
	entry, _ := contents[0].(map[string]any)
	if entry["role"] != "user" {
		t.Errorf("contents role: %v", entry["role"])
	}
	gen, _ := payload["generationConfig"].(map[string]any)
	if gen["temperature"] != float64(0.7) {
		t.Errorf("generationConfig.temperature: %v", gen["temperature"])
	}
}

func TestGemini401IsAuthError(t *testing.T) {
	srv := fixedServer(401, `{"error":"API key not valid"}`)
	defer srv.Close()
	p := &geminiProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("gemini", srv.URL), "hello")
	if err == nil {
		t.Fatal("401: expected an error")
	}
	typed := err.(*Error)
	if typed.Code != AuthError || typed.Retryable {
		t.Errorf("401: got %s retryable=%v", typed.Code, typed.Retryable)
	}
}

func TestGeminiMalformedCandidatesIsProviderError(t *testing.T) {
	srv := fixedServer(200, `{"candidates":[]}`)
	defer srv.Close()
	p := &geminiProvider{http: &httpClient{client: srv.Client()}}
	_, err := p.Rewrite(context.Background(), testCfg("gemini", srv.URL), "hello")
	if err == nil {
		t.Fatal("empty candidates: expected an error")
	}
	if err.(*Error).Code != ProviderError {
		t.Errorf("code %q, want PROVIDER_ERROR", err.(*Error).Code)
	}
}
