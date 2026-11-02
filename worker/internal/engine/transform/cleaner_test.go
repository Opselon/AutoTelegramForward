package transform

import (
	"testing"
)

func TestCleanerRule_AllFeatures(t *testing.T) {
	rule := &CleanerRule{
		Patterns:           []string{"forbidden"},
		UseRegex:           false,
		RemoveURLs:         true,
		RemoveMentions:     true,
		RemoveHashtags:     true,
		RemoveEmojis:       true,
		RemovePhoneNumbers: true,
		RemoveEmptyLines:   true,
		IsActive:           true,
	}

	input := "Hello @user! Check this https://example.com #cool forbidden 🚀 Call +12345678901\n\n\nNext line."
	out := rule.Clean(input)

	if out == "" {
		t.Fatalf("expected non-empty output")
	}
	if HasLink(out) {
		t.Errorf("output still has link: %s", out)
	}
}
