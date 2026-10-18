package handlers

import (
	"net/http"
	"net/http/httptest"
	"testing"

	grpcclient "autoforward/internal/grpcclient"
)

// The contract document is public by design: an unauthenticated caller must be
// able to read it (it describes HOW to authenticate). This pins the routing so
// the mux precedence can never silently push it behind RequireAuth.
func TestOpenAPIDocRoutesWithoutAuth(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{}}
	rec := httptest.NewRecorder()
	h.OpenAPIDoc(rec, httptest.NewRequest("GET", "/api/openapi.yaml", nil))
	if rec.Code == http.StatusUnauthorized {
		t.Fatalf("openapi must not require auth, got 401: %s", rec.Body.String())
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/yaml" {
		t.Fatalf("content type: %q", ct)
	}
}
