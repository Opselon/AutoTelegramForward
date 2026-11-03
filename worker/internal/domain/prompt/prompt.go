// Package prompt manages prompt templates and version pinning for AI message
// rewriting. It provides owner-scoped template resolution, immutable version
// snapshots, and preview rendering matching core/application/prompt_service.py.
package prompt

import (
	"context"
	"fmt"
	"strings"
)

// PromptTemplate represents a prompt template record from prompt_templates.
type PromptTemplate struct {
	ID                 string
	Name               string
	Description        string
	SystemPrompt       string
	UserPromptTemplate string
	TargetLanguage     string
	CurrentVersion     int
	IsSystem           bool
	OwnerUserID        int64
	CreatedAt          int64
	UpdatedAt          int64
}

// PromptVersion represents an immutable historical version from prompt_versions.
type PromptVersion struct {
	ID                 string
	PromptID           string
	Version            int
	SystemPrompt       string
	UserPromptTemplate string
	ChangeSummary      string
	IsActive           bool
	CreatedAt          int64
}

// PreviewResult contains the rendered prompt and resolved version metadata.
type PreviewResult struct {
	TemplateID         string
	Version            int
	Name               string
	SystemPrompt       string
	RenderedUserPrompt string
	TargetLanguage     string
}

// Store defines the owner-scoped prompt template and version access contract.
type Store interface {
	// GetTemplate returns the template if owned by ownerUserID or if is_system=1.
	// Returns nil, nil if no such template exists or if access is refused.
	GetTemplate(ctx context.Context, templateID string, ownerUserID int64) (*PromptTemplate, error)
	// GetVersion returns a specific version of a prompt template.
	GetVersion(ctx context.Context, promptID string, version int) (*PromptVersion, error)
}

// PreviewPrompt resolves the prompt template (owner-scoped) and version pinning,
// rendering the user prompt with sampleText matching Python's preview_prompt
// (core/application/prompt_service.py:150-185).
func PreviewPrompt(ctx context.Context, store Store, templateID string, sampleText string, version int, ownerUserID int64) (*PreviewResult, error) {
	if store == nil {
		return nil, fmt.Errorf("prompt store unavailable")
	}
	template, err := store.GetTemplate(ctx, templateID, ownerUserID)
	if err != nil {
		return nil, err
	}
	if template == nil {
		return nil, fmt.Errorf("prompt template '%s' not found", templateID)
	}

	sysPrompt := template.SystemPrompt
	userTpl := template.UserPromptTemplate
	ver := template.CurrentVersion

	if version > 0 && version != template.CurrentVersion {
		vObj, vErr := store.GetVersion(ctx, templateID, version)
		if vErr == nil && vObj != nil {
			sysPrompt = vObj.SystemPrompt
			userTpl = vObj.UserPromptTemplate
			ver = vObj.Version
		}
	}

	var renderedUser string
	if strings.Contains(userTpl, "{text}") {
		renderedUser = strings.ReplaceAll(userTpl, "{text}", sampleText)
	} else {
		renderedUser = userTpl + "\n\n" + sampleText
	}

	return &PreviewResult{
		TemplateID:         templateID,
		Version:            ver,
		Name:               template.Name,
		SystemPrompt:       sysPrompt,
		RenderedUserPrompt: renderedUser,
		TargetLanguage:     template.TargetLanguage,
	}, nil
}
