package replace

import (
	"testing"
)

func TestService_Apply(t *testing.T) {
	svc := New()

	entries := []Entry{
		{
			ID:          "1",
			FindPattern: "foo",
			ReplaceWith: "bar",
			Priority:    10,
			IsEnabled:   true,
		},
		{
			ID:          "2",
			FindPattern: "bar",
			ReplaceWith: "baz",
			Priority:    20, // Should run first!
			IsEnabled:   true,
		},
	}

	// Priority 20 runs first: "foo bar" -> "foo baz", then priority 10 runs: "foo baz" -> "bar baz"
	out, err := svc.Apply("foo bar", entries)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != "bar baz" {
		t.Errorf("expected 'bar baz', got %q", out)
	}

	// Whole word test
	wwEntries := []Entry{
		{
			FindPattern: "cat",
			ReplaceWith: "dog",
			WholeWord:   true,
			IsEnabled:   true,
		},
	}
	out2, _ := svc.Apply("concatenate the cat", wwEntries)
	if out2 != "concatenate the dog" {
		t.Errorf("expected 'concatenate the dog', got %q", out2)
	}
}
