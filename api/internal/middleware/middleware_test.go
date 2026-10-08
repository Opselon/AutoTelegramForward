package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestAPIKeyAuth_RejectsMissing(t *testing.T) {
	h := APIKeyAuth("secret", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("handler must not run without a key")
	}))
	req := httptest.NewRequest("GET", "/api/v1/sessions", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d", rec.Code)
	}
}

func TestAPIKeyAuth_AcceptsHeader(t *testing.T) {
	called := false
	h := APIKeyAuth("secret", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set("X-API-Key", "secret")
	h.ServeHTTP(httptest.NewRecorder(), req)
	if !called {
		t.Fatal("handler should run with a valid key")
	}
}

func TestAPIKeyAuth_AcceptsBearer(t *testing.T) {
	called := false
	h := APIKeyAuth("secret", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set("Authorization", "Bearer secret")
	h.ServeHTTP(httptest.NewRecorder(), req)
	if !called {
		t.Fatal("handler should run with a valid bearer token")
	}
}

func TestRequestLogger_PassesThrough(t *testing.T) {
	h := RequestLogger(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTeapot)
	}))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	if rec.Code != http.StatusTeapot {
		t.Fatalf("status passthrough broken: %d", rec.Code)
	}
}
