//go:build stage42_parity
// +build stage42_parity

// Package main is the Go half of scripts/test_stage42_parity.py.
//
// This is a GENERATED driver: the parity harness writes it into
// worker/tests, runs it once over the case list on stdin, and deletes it.
// It exists so the parity harness executes the REAL production Go packages
// (atf/worker/internal/domain/cleaning) — the same code the Worker binary
// runs — instead of a fixture file or a reimplementation.
//
// The case schema is the harness's contract; see
// scripts/test_stage42_parity.py for the authoritative list and the Python
// adapters each case is compared against.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"atf/worker/internal/domain/cleaning"
)

type testCase struct {
	Engine       string            `json:"engine"`
	Text         string            `json:"text"`
	Cleaner      map[string]any    `json:"cleaner"`
	Policy       string            `json:"policy"`
	Allowlist    []string          `json:"allowlist"`
	Blocklist    []string          `json:"blocklist"`
	RewriteMap   map[string]string `json:"rewrite_map"`
	Replacement  string            `json:"replacement"`
	Replacements map[string]any    `json:"replacements"`
	Metadata     map[string]any    `json:"metadata"`
	Header       string            `json:"header"`
	Footer       string            `json:"footer"`
}

type result struct {
	Index  int    `json:"index"`
	Output string `json:"output"`
	Error  string `json:"error"`
}

func boolField(m map[string]any, key string) bool {
	if v, ok := m[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return false
}

func strField(m map[string]any, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func listField(m map[string]any, key string) []string {
	raw, ok := m[key]
	if !ok {
		return nil
	}
	list, ok := raw.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(list))
	for _, item := range list {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func cleanerFromMap(m map[string]any) *cleaning.CleanerRule {
	if m == nil {
		return nil
	}
	return &cleaning.CleanerRule{
		Patterns:           listField(m, "patterns"),
		UseRegex:           boolField(m, "use_regex"),
		RemoveURLs:         boolField(m, "remove_urls"),
		RemoveMentions:     boolField(m, "remove_mentions"),
		RemoveHashtags:     boolField(m, "remove_hashtags"),
		RemoveEmojis:       boolField(m, "remove_emojis"),
		RemovePhoneNumbers: boolField(m, "remove_phone_numbers"),
		RemoveEmptyLines:   boolField(m, "remove_empty_lines"),
		StripButtons:       boolField(m, "strip_buttons"),
		CaseSensitive:      boolField(m, "case_sensitive"),
		IsActive:           true,
	}
}

func entriesFromMap(raw map[string]any) []cleaning.ReplacementEntry {
	entries := make([]cleaning.ReplacementEntry, 0, len(raw))
	for find, value := range raw {
		entry := cleaning.ReplacementEntry{Find: find}
		if opts, ok := value.(map[string]any); ok {
			entry.Replace = strField(opts, "to")
			if mode := strField(opts, "mode"); mode == "regex" {
				entry.IsRegex = true
			}
			if flags, ok := opts["flags"].(string); ok {
				for _, ch := range flags {
					if ch == 'i' {
						entry.IgnoreCase = true
					}
				}
			}
			if boolField(opts, "whole_word") {
				entry.WholeWord = true
			}
		} else if text, ok := value.(string); ok {
			entry.Replace = text
		}
		entries = append(entries, entry)
	}
	return entries
}

func runCase(c testCase) string {
	engine := cleaning.NewPipeline()
	cfg := cleaning.Config{}
	switch c.Engine {
	case "cleaner":
		// The cleaner engine is exercised directly: the pipeline's other
		// stages are inert when no flag is set.
		cleaner := cleanerFromMap(c.Cleaner)
		if cleaner == nil {
			return c.Text
		}
		return cleaner.Clean(c.Text)
	case "emoji":
		cfg.RemoveEmojis = true
	case "link_policy":
		cfg.LinkPolicy = c.Policy
		cfg.DomainAllowlist = c.Allowlist
		cfg.DomainBlocklist = c.Blocklist
		cfg.LinkRewriteMap = c.RewriteMap
	case "remove_urls":
		cfg.ContentPolicy = &cleaning.ContentPolicy{RemoveLinks: true}
	case "replace_links":
		cfg.LinkReplacement = c.Replacement
	case "replacements":
		cfg.Replacements = entriesFromMap(c.Replacements)
	case "advanced":
		cfg.Watermark = strField(c.Metadata, "watermark_text")
		if buttons, ok := c.Metadata["link_buttons"].([]any); ok {
			for _, item := range buttons {
				if m, ok := item.(map[string]any); ok {
					cfg.LinkButtons = append(cfg.LinkButtons, cleaning.LinkButton{
						Text: strField(m, "text"), URL: strField(m, "url"),
					})
				}
			}
		}
		if ak, ok := c.Metadata["affiliate_kit"].(map[string]any); ok && boolField(ak, "enabled") {
			cfg.AffiliateKit = &cleaning.AffiliateKit{Prefix: strField(ak, "prefix")}
		}
	case "header_footer":
		cfg.Header = c.Header
		cfg.Footer = c.Footer
	default:
		return ""
	}
	result := engine.Apply(c.Text, cfg, nil)
	return result.Text
}

func main() {
	var cases []testCase
	dec := json.NewDecoder(os.Stdin)
	if err := dec.Decode(&cases); err != nil {
		fmt.Fprintln(os.Stderr, "decode cases:", err)
		os.Exit(2)
	}
	results := make([]result, 0, len(cases))
	for i, c := range cases {
		out := runCase(c)
		results = append(results, result{Index: i, Output: out})
	}
	enc := json.NewEncoder(os.Stdout)
	if err := enc.Encode(results); err != nil {
		fmt.Fprintln(os.Stderr, "encode results:", err)
		os.Exit(2)
	}
}
