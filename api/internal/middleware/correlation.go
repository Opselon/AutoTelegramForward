package middleware

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"regexp"
	"strings"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

type correlationContextKey struct{}

var (
	validIDRegex = regexp.MustCompile(`^[a-zA-Z0-9_\-\.:]{8,128}$`)
)

// GenerateCorrelationID produces a secure random identifier.
func GenerateCorrelationID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		// Fallback should rand.Read fail
		return "corr-fallback-id-000000000000"
	}
	return "corr-" + hex.EncodeToString(b)
}

// IsValidCorrelationID checks whether the given ID matches the length and character constraints.
func IsValidCorrelationID(id string) bool {
	if len(id) < 8 || len(id) > 128 {
		return false
	}
	return validIDRegex.MatchString(id)
}

// ContextWithCorrelationID returns a new context containing the correlation ID.
func ContextWithCorrelationID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, correlationContextKey{}, id)
}

// CorrelationIDFromContext extracts the correlation ID if present.
func CorrelationIDFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(correlationContextKey{}).(string); ok {
		return v
	}
	return ""
}

// CorrelationID is an HTTP middleware that extracts, validates, or generates a correlation ID,
// attaching it to the request context and setting standard response headers.
func CorrelationID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var incomingID string
		for _, h := range []string{"X-Correlation-ID", "X-Request-ID", "traceparent"} {
			if val := strings.TrimSpace(r.Header.Get(h)); val != "" {
				incomingID = val
				break
			}
		}

		effectiveID := incomingID
		if !IsValidCorrelationID(effectiveID) {
			effectiveID = GenerateCorrelationID()
		}

		ctx := ContextWithCorrelationID(r.Context(), effectiveID)
		w.Header().Set("X-Correlation-ID", effectiveID)
		w.Header().Set("X-Request-ID", effectiveID)

		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// CorrelationUnaryClientInterceptor injects the correlation ID from context into outgoing gRPC calls.
func CorrelationUnaryClientInterceptor() grpc.UnaryClientInterceptor {
	return func(
		ctx context.Context,
		method string,
		req, reply any,
		cc *grpc.ClientConn,
		invoker grpc.UnaryInvoker,
		opts ...grpc.CallOption,
	) error {
		if corrID := CorrelationIDFromContext(ctx); corrID != "" {
			ctx = metadata.AppendToOutgoingContext(ctx, "x-correlation-id", corrID)
		}
		return invoker(ctx, method, req, reply, cc, opts...)
	}
}
