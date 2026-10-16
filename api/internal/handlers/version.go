package handlers

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

// APIVersion is the SemVer contract owned by this Go module. Bump MINOR when
// adding backwards-compatible endpoints, MAJOR on breaking changes.
const APIVersion = "1.0.0"

// MinSupportedClient is the oldest mobile/web client guaranteed to work
// against this API.
const MinSupportedClient = "1.0.0"

// ProductVersion and CommitSHA are build-time overrides:
//
//	go build -ldflags "-X autoforward/internal/handlers.ProductVersion=1.5.0 -X autoforward/internal/handlers.CommitSHA=abc123"
var (
	ProductVersion = "dev"
	CommitSHA      = "unknown"
)

// VersionFeatures lists the capability keys clients can probe.
var VersionFeatures = []string{"rules", "filters", "ai_configs", "sessions", "logs", "dlq", "backup", "system_setup", "env_contract"}

type versionResponse struct {
	ProductVersion     string   `json:"product_version"`
	APIVersion         string   `json:"api_version"`
	MinSupportedClient string   `json:"min_supported_client"`
	CommitSHA          string   `json:"commit_sha"`
	Features           []string `json:"features"`
}

// resolveProductVersion reads the repo-root VERSION file at runtime so the
// shipped binary always reports the released product version. Falls back to
// the -ldflags injected ProductVersion, then to "dev".
func resolveProductVersion() string {
	if p := strings.TrimSpace(os.Getenv("ATF_VERSION_FILE")); p != "" {
		if v := readVersionFile(p); v != "" {
			return v
		}
	}
	candidates := []string{}
	if cwd, err := os.Getwd(); err == nil {
		dir := cwd
		for range 5 {
			candidates = append(candidates, filepath.Join(dir, "VERSION"))
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		candidates = append(candidates,
			filepath.Join(dir, "VERSION"),
			filepath.Join(dir, "..", "VERSION"),
		)
	}
	for _, p := range candidates {
		if v := readVersionFile(p); v != "" {
			return v
		}
	}
	if ProductVersion != "" {
		return ProductVersion
	}
	return "dev"
}

func readVersionFile(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func resolveCommitSHA() string {
	if v := strings.TrimSpace(os.Getenv("ATF_COMMIT_SHA")); v != "" {
		return v
	}
	if CommitSHA != "" {
		return CommitSHA
	}
	return "unknown"
}

// Version is a PUBLIC unauthenticated endpoint: no Bearer token required.
// Reports the product/API versions and capability keys so mobile/web clients
// can gate features without authenticating first.
func (h *Handlers) Version(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, versionResponse{
		ProductVersion:     resolveProductVersion(),
		APIVersion:         APIVersion,
		MinSupportedClient: MinSupportedClient,
		CommitSHA:          resolveCommitSHA(),
		Features:           VersionFeatures,
	})
}
