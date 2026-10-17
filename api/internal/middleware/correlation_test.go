package middleware

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

func TestCorrelationID_GeneratesWhenMissing(t *testing.T) {
	var capturedID string
	h := CorrelationID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		capturedID = CorrelationIDFromContext(r.Context())
		w.WriteHeader(http.StatusOK)
	}))

	rec := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/rules", nil)
	h.ServeHTTP(rec, req)

	if capturedID == "" {
		t.Fatal("expected correlation ID to be generated in context")
	}
	if !strings.HasPrefix(capturedID, "corr-") {
		t.Fatalf("expected generated ID to start with 'corr-', got %q", capturedID)
	}
	if rec.Header().Get("X-Correlation-ID") != capturedID {
		t.Fatalf("expected X-Correlation-ID header %q, got %q", capturedID, rec.Header().Get("X-Correlation-ID"))
	}
	if rec.Header().Get("X-Request-ID") != capturedID {
		t.Fatalf("expected X-Request-ID header %q, got %q", capturedID, rec.Header().Get("X-Request-ID"))
	}
}

func TestCorrelationID_PreservesValidIncoming(t *testing.T) {
	validIDs := []string{
		"my-valid-correlation-id-12345",
		"trace-uuid-11223344-5566-7788",
		"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", // W3C traceparent
	}

	for _, id := range validIDs {
		var capturedID string
		h := CorrelationID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			capturedID = CorrelationIDFromContext(r.Context())
			w.WriteHeader(http.StatusOK)
		}))

		rec := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/api/rules", nil)
		req.Header.Set("X-Correlation-ID", id)
		h.ServeHTTP(rec, req)

		if capturedID != id {
			t.Fatalf("expected preserved ID %q, got %q", id, capturedID)
		}
		if rec.Header().Get("X-Correlation-ID") != id {
			t.Fatalf("expected response header %q, got %q", id, rec.Header().Get("X-Correlation-ID"))
		}
	}
}

func TestCorrelationID_RejectsMalformedAndOversized(t *testing.T) {
	badIDs := []string{
		"short",                        // < 8 chars
		strings.Repeat("a", 130),       // > 128 chars
		"bad<script>alert(1)</script>", // dangerous characters
		"newline\nid\r\nvalue",         // header injection attempt
		"bad space in id",              // spaces not allowed
	}

	for _, bad := range badIDs {
		var capturedID string
		h := CorrelationID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			capturedID = CorrelationIDFromContext(r.Context())
			w.WriteHeader(http.StatusOK)
		}))

		rec := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/api/rules", nil)
		req.Header.Set("X-Correlation-ID", bad)
		h.ServeHTTP(rec, req)

		if capturedID == bad {
			t.Fatalf("malformed ID %q should have been rejected/replaced", bad)
		}
		if !strings.HasPrefix(capturedID, "corr-") {
			t.Fatalf("expected sanitized replacement ID starting with corr-, got %q", capturedID)
		}
	}
}

func TestCorrelationUnaryClientInterceptor(t *testing.T) {
	interceptor := CorrelationUnaryClientInterceptor()
	ctx := ContextWithCorrelationID(context.Background(), "corr-test-trace-12345")

	var invokedMD metadata.MD
	dummyInvoker := func(
		callCtx context.Context,
		method string,
		req, reply any,
		cc *grpc.ClientConn,
		opts ...grpc.CallOption,
	) error {
		md, ok := metadata.FromOutgoingContext(callCtx)
		if ok {
			invokedMD = md
		}
		return nil
	}

	err := interceptor(ctx, "/autoforward.RuleService/ListRules", nil, nil, nil, dummyInvoker)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	vals := invokedMD.Get("x-correlation-id")
	if len(vals) == 0 || vals[0] != "corr-test-trace-12345" {
		t.Fatalf("expected x-correlation-id metadata 'corr-test-trace-12345', got %v", vals)
	}
}
