package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestVersion_PublicShape(t *testing.T) {
	h := &Handlers{}
	req := httptest.NewRequest("GET", "/api/version", nil) // no identity: public
	rec := httptest.NewRecorder()
	h.Version(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var body struct {
		ProductVersion     string   `json:"product_version"`
		APIVersion         string   `json:"api_version"`
		MinSupportedClient string   `json:"min_supported_client"`
		CommitSHA          string   `json:"commit_sha"`
		Features           []string `json:"features"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.ProductVersion == "" || body.APIVersion == "" || body.MinSupportedClient == "" || body.CommitSHA == "" {
		t.Fatalf("version fields must be non-empty: %+v", body)
	}
	want := map[string]bool{"rules": true, "filters": true, "ai_configs": true, "sessions": true, "logs": true, "dlq": true, "backup": true}
	for _, f := range body.Features {
		delete(want, f)
	}
	if len(want) != 0 {
		t.Fatalf("missing capability keys %v in %v", want, body.Features)
	}
}

func TestVersion_ReadsVersionFileAtRuntime(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "VERSION"), []byte("9.9.9\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("ATF_VERSION_FILE", filepath.Join(dir, "VERSION"))
	if got := resolveProductVersion(); got != "9.9.9" {
		t.Fatalf("want 9.9.9 from VERSION file, got %q", got)
	}
}

func TestVersion_FallsBackToLdflagsOrDev(t *testing.T) {
	t.Setenv("ATF_VERSION_FILE", filepath.Join(t.TempDir(), "does-not-exist"))
	old := ProductVersion
	ProductVersion = "ldflags-1.2.3"
	defer func() { ProductVersion = old }()
	// Point CWD away: resolveProductVersion only checks relative candidates.
	cwd, _ := os.Getwd()
	if err := os.Chdir(t.TempDir()); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = os.Chdir(cwd) }()
	if got := resolveProductVersion(); got != "ldflags-1.2.3" {
		t.Fatalf("want ldflags fallback, got %q", got)
	}
}
