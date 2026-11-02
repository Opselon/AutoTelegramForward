package replace

import (
	"regexp"
	"sort"
	"strings"
)

// Entry defines a single replacement rule ported from text_replace.py.
type Entry struct {
	ID          string
	FindPattern string
	ReplaceWith string
	IsRegex     bool
	IgnoreCase  bool
	WholeWord   bool
	Priority    int
	IsEnabled   bool
}

// Service applies a slice of replacement entries in prioritized, deterministic order.
type Service struct{}

// New returns a new Replace Service.
func New() *Service {
	return &Service{}
}

// Apply executes all enabled replacements in priority order (higher priority first).
func (s *Service) Apply(text string, entries []Entry) (string, error) {
	if text == "" || len(entries) == 0 {
		return text, nil
	}

	// Filter and sort entries by Priority desc, preserving original index on tie
	type indexedEntry struct {
		entry Entry
		index int
	}
	var active []indexedEntry
	for i, e := range entries {
		if e.IsEnabled && e.FindPattern != "" {
			active = append(active, indexedEntry{entry: e, index: i})
		}
	}

	sort.SliceStable(active, func(i, j int) bool {
		if active[i].entry.Priority != active[j].entry.Priority {
			return active[i].entry.Priority > active[j].entry.Priority
		}
		return active[i].index < active[j].index
	})

	out := text
	for _, item := range active {
		e := item.entry
		if e.IsRegex {
			prefix := ""
			if e.IgnoreCase {
				prefix = "(?i)"
			}
			pat := prefix + e.FindPattern
			if e.WholeWord {
				pat = `\b(?:` + pat + `)\b`
			}
			re, err := regexp.Compile(pat)
			if err != nil {
				// Invalid regex in replace must not crash processing
				continue
			}
			out = re.ReplaceAllString(out, e.ReplaceWith)
		} else {
			if e.WholeWord {
				prefix := ""
				if e.IgnoreCase {
					prefix = "(?i)"
				}
				pat := prefix + `\b` + regexp.QuoteMeta(e.FindPattern) + `\b`
				re, err := regexp.Compile(pat)
				if err == nil {
					out = re.ReplaceAllString(out, e.ReplaceWith)
				}
			} else if e.IgnoreCase {
				re, err := regexp.Compile("(?i)" + regexp.QuoteMeta(e.FindPattern))
				if err == nil {
					out = re.ReplaceAllString(out, e.ReplaceWith)
				}
			} else {
				out = strings.ReplaceAll(out, e.FindPattern, e.ReplaceWith)
			}
		}
	}

	return out, nil
}
