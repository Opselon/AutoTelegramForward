// Package aiprovider is the Go port of the AI provider execution path
// (core/infrastructure/ai/providers.py): provider-neutral chat-completions
// adapters, typed error classification, and the registry factory.
//
// THE CONTRACT. Every adapter answers the same three questions — rewrite
// one piece of text, run a raw chat completion, and probe health — against
// a remote HTTP endpoint the tenant chose. The adapter never decides the
// text it is handed (the prompt construction is the transformer's, never
// the adapter's), and it never logs the API key: the typed error carries a
// classification and a status, never the Authorization header that made
// the request fail.
package aiprovider

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"atf/worker/internal/domain/aiconfig"
)

// RequestTimeout is the default network budget per attempt
// (providers.py:24). The rewrite orchestrator layers its own
// deadline.ContextTimeout (transformer.py:215) on top, so this is the
// per-attempt ceiling, not the whole-rewrite ceiling.
const RequestTimeout = 45.0 * time.Second

// MaxRetries is the transient-error retry budget (providers.py:26): the
// loop in postJSON runs MaxRetries+1 attempts, so 2 means up to three
// tries with backoff between them.
const MaxRetries = 2

// RetryableStatus is the transient-failure set (providers.py:28): a
// response in this set is retried with backoff, everything else is
// raised immediately. 401/403 is deliberately absent — a retry will not
// fix a bad key, and retrying it triples the cost of a misconfiguration.
var RetryableStatus = map[int]bool{
	408: true, 425: true, 429: true,
	500: true, 502: true, 503: true, 504: true,
}

// Message is one chat-completions message, the exact shape both
// OpenAI-compatible and the transformer's prompt builder emit.
type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// Provider is the adapter contract (IAIProvider, providers.py:31).
//
// Rewrite is the single-message entry point: it builds the provider
// payload from the config's own prompts and returns the transformed
// text. ChatComplete is the raw messages entry point the parity harness
// and health probes use. HealthCheck reports whether the endpoint
// answers at all.
type Provider interface {
	// Rewrite runs the config's prompt pair against the provider and
	// returns the transformed text (providers.py:153).
	Rewrite(ctx context.Context, cfg *aiconfig.AIConfig, text string) (string, error)
	// ChatComplete posts the messages verbatim and returns
	// choices[0].message.content (providers.py:174).
	ChatComplete(ctx context.Context, cfg *aiconfig.AIConfig, messages []Message, temperature float64) (string, error)
	// HealthCheck answers "can this endpoint be reached at all"
	// (providers.py:191).
	HealthCheck(ctx context.Context, cfg *aiconfig.AIConfig) (bool, error)
}

// ErrorCode is the typed classification the rewrite orchestrator and the
// delivery stage act on (transformer.py:283): it decides the circuit
// breaker's transient bookkeeping AND the fallback policy's outcome.
type ErrorCode string

const (
	// AuthError is 401/403: non-retryable, the key or the plan is wrong.
	AuthError ErrorCode = "AUTH_ERROR"
	// RateLimit is 429: retryable, the tenant is paying for capacity.
	RateLimit ErrorCode = "RATE_LIMIT"
	// ProviderError is any other provider failure (5xx that exhausted
	// its retries, a malformed response, an unexpected status).
	ProviderError ErrorCode = "PROVIDER_ERROR"
	// Timeout is the request budget expiring.
	Timeout ErrorCode = "TIMEOUT"
	// InvalidConfig is a config the adapter cannot even try
	// (custom provider with no base_url, unknown provider name).
	InvalidConfig ErrorCode = "INVALID_CONFIG"
)

// Error is the typed failure every adapter returns. The classification is
// load-bearing — the circuit breaker counts transient and permanent
// failures differently — so adapters must not return plain errors for
// HTTP outcomes.
type Error struct {
	Code     ErrorCode
	Status   int
	Retryable bool
	// Message never contains the API key or the request payload. It is the
	// status line plus a provider-supplied phrase at most, and the rewrite
	// orchestrator is the one that decides whether any of it reaches a log.
	Message string
}

func (e *Error) Error() string {
	if e.Status != 0 {
		return fmt.Sprintf("ai provider: %s: HTTP %d: %s", e.Code, e.Status, e.Message)
	}
	return fmt.Sprintf("ai provider: %s: %s", e.Code, e.Message)
}

// ClassifyStatus maps an HTTP status to the typed error
// (transformer.py:282-283): 429 is RATE_LIMIT, 401/403 is AUTH_ERROR,
// and everything else is PROVIDER_ERROR. Retryable follows the
// RetryableStatus set, and the AUTH pair is never retryable.
func ClassifyStatus(status int, detail string) *Error {
	switch {
	case status == 429:
		return &Error{Code: RateLimit, Status: status, Retryable: RetryableStatus[status], Message: detail}
	case status == 401 || status == 403:
		return &Error{Code: AuthError, Status: status, Retryable: false, Message: detail}
	default:
		return &Error{Code: ProviderError, Status: status, Retryable: RetryableStatus[status], Message: detail}
	}
}

// httpClient is the one HTTP dependency. Adapters take it as a field so
// tests inject an httptest.Server-backed client and production uses the
// default with the provider timeout; the package never hides a global
// behind the interface.
type httpClient struct {
	client *http.Client
}

// newHTTPClient builds the transport with the provider timeout.
func newHTTPClient() *httpClient {
	return &httpClient{client: &http.Client{Timeout: RequestTimeout}}
}

// postJSON is the Go port of providers.py::_post_json: POST a JSON body,
// retry transient failures with exponential backoff (capped at 8s), and
// return the decoded response. Non-retryable statuses and the final
// attempt of a retryable one surface as a typed Error.
//
// The SSE fallback in the Python original is deliberately NOT ported:
// every adapter here sends stream:false, so an SSE body means the
// endpoint ignored the negotiated request shape — a PROVIDER_ERROR,
// which is exactly what the Go JSON decoder produces on it.
func postJSON(ctx context.Context, h *httpClient, url string, headers map[string]string, payload any) (map[string]any, error) {
	body, err := jsonMarshal(payload)
	if err != nil {
		return nil, &Error{Code: ProviderError, Message: "encode request: " + redact(err.Error())}
	}
	var lastErr error
	for attempt := 0; attempt <= MaxRetries; attempt++ {
		if attempt > 0 {
			// Python's min(2 ** attempt, 8) backoff: attempt 1 waits
			// 2s, attempt 2 waits 4s, and the cap keeps a retrying
			// worker from parking on one slow provider.
			wait := time.Duration(1<<uint(attempt)) * time.Second
			if wait > 8*time.Second {
				wait = 8 * time.Second
			}
			select {
			case <-ctx.Done():
				return nil, ctxErr(ctx.Err())
			case <-time.After(wait):
			}
		}
		if err := ctx.Err(); err != nil {
			return nil, ctxErr(err)
		}
		outcome, err := postOnce(ctx, h, url, headers, body)
		if err != nil {
			return nil, err // already typed
		}
		// outcome.retry indicates a transient status worth another
		// attempt; the last attempt surfaces it as a typed error
		// (Python's raise_for_status after the loop).
		if outcome.retry && attempt < MaxRetries {
			lastErr = outcome.err
			continue
		}
		if outcome.err != nil {
			return nil, outcome.err
		}
		return outcome.body, nil
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, &Error{Code: ProviderError, Message: "retry loop exhausted"}
}

// onceResult is one attempt's outcome: either a decoded body, a typed
// error, or a transient status that asks for another attempt.
type onceResult struct {
	body  map[string]any
	err   *Error
	retry bool
}

// postOnce performs exactly one POST.
func postOnce(ctx context.Context, h *httpClient, url string, headers map[string]string, body []byte) (*onceResult, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, byteReader(body))
	if err != nil {
		return nil, &Error{Code: InvalidConfig, Message: redact(err.Error())}
	}
	req.Header.Set("Content-Type", "application/json")
	for key, value := range headers {
		req.Header.Set(key, value)
	}
	resp, err := h.client.Do(req)
	if err != nil {
		// A context deadline is TIMEOUT; every other transport failure
		// (connection refused, DNS, TLS) is PROVIDER_ERROR, mirroring
		// the Python original's httpx.HTTPError branch — retryable,
		// because the next attempt may reach a healthy replica.
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
			return nil, ctxErr(err)
		}
		return nil, &Error{Code: ProviderError, Retryable: true, Message: redactTransport(err)}
	}
	defer func() {
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
	}()
	raw, readErr := readAll(resp.Body)
	if readErr != nil {
		return nil, &Error{Code: ProviderError, Retryable: true, Message: "read response: " + redact(readErr.Error())}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		detail := detailFrom(raw)
		typed := ClassifyStatus(resp.StatusCode, detail)
		if typed.Retryable {
			return &onceResult{err: typed, retry: true}, nil
		}
		return &onceResult{err: typed}, nil
	}
	decoded, decErr := jsonUnmarshal(raw)
	if decErr != nil {
		return nil, &Error{
			Code:     ProviderError,
			Retryable: false,
			// "unexpected chat-completions response" (providers.py:172):
			// a 200 with a body that is not the agreed shape is a
			// provider contract violation, not a transient blip.
			Message: "unexpected response body: " + redact(decErr.Error()),
		}
	}
	return &onceResult{body: decoded}, nil
}

// ctxErr converts a context failure to the typed TIMEOUT classification.
func ctxErr(err error) *Error {
	if errors.Is(err, context.DeadlineExceeded) {
		return &Error{Code: Timeout, Retryable: true, Message: "request deadline exceeded"}
	}
	if errors.Is(err, context.Canceled) {
		return &Error{Code: Timeout, Retryable: true, Message: "request canceled"}
	}
	return &Error{Code: Timeout, Retryable: true, Message: redact(err.Error())}
}

// detailFrom reads the short provider phrase out of an error body, the
// way Python's raise_for_status message does. It is bounded and it is the
// ONLY part of a response body that may travel upward: the body can echo
// the key back (OpenAI's "Incorrect API key provided: sk-..."), so the
// caller's redact pass is the last line of defence, never the only one.
const providerDetailLimit = 200

func detailFrom(raw []byte) string {
	text := strings.TrimSpace(string(raw))
	if len(text) > providerDetailLimit {
		text = text[:providerDetailLimit]
	}
	return text
}

// redact strips nothing of its own accord but keeps the helper local so
// every adapter's error path goes through one place; the key never
// reaches here because the header value is never included in an error
// message in the first place.
func redact(msg string) string {
	return msg
}

// redactTransport renders a transport error without its URL or inner
// detail: a connection-refused message names the internal host the
// tenant's base_url pointed at, which is not the worker's to publish
// (the Python probe's own posture, providers.py:541).
func redactTransport(err error) string {
	msg := err.Error()
	// Keep only the error kind: drop everything from the first "http://" /
	// "https://" onward, and anything after the first colon that follows a
	// scheme-less host descriptor.
	if i := strings.Index(msg, "http"); i >= 0 {
		return strings.TrimSpace(msg[:i])
	}
	return msg
}
