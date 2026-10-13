package handlers

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRequireAdmin_RejectsUnauthenticated(t *testing.T) {
	h := &Handlers{}
	called := false
	target := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	})

	handler := h.RequireAdmin(target)
	req := httptest.NewRequest(http.MethodGet, "/overview", nil)
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403 Forbidden, got %d", rec.Code)
	}
	if called {
		t.Fatalf("target handler must not be called")
	}
}

func TestRequireAdmin_RejectsStandardUser(t *testing.T) {
	h := &Handlers{}
	called := false
	target := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	})

	handler := h.RequireAdmin(target)
	req := httptest.NewRequest(http.MethodGet, "/overview", nil)
	// Inject normal client identity (IsAdmin = false)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{
		UserID:   5094837833,
		Username: "client",
		IsAdmin:  false,
	}))
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403 Forbidden for non-admin, got %d", rec.Code)
	}
	if called {
		t.Fatalf("target handler must not be called for standard user")
	}
}

func TestRequireAdmin_AllowsVerifiedAdmin(t *testing.T) {
	h := &Handlers{}
	called := false
	target := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	})

	handler := h.RequireAdmin(target)
	req := httptest.NewRequest(http.MethodGet, "/overview", nil)
	// Inject admin identity (IsAdmin = true)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{
		UserID:   5094837833,
		Username: "admin_owner",
		IsAdmin:  true,
	}))
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 OK for admin, got %d", rec.Code)
	}
	if !called {
		t.Fatalf("target handler should have been called for verified admin")
	}
}
