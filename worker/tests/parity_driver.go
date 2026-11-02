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
	"bytes"
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
	// orderedReplacements carries the replacements in the order the case
	// file wrote them. json.Unmarshal discards JSON object key order, so
	// the plain Replacements field cannot express the order-dependent
	// composition RPL-PRIORITY-01 pins; the ordered decoder fills this
	// field instead.
	orderedReplacements []orderedPair `json:"-"`
	Metadata     map[string]any    `json:"metadata"`
	Header       string            `json:"header"`
	Footer       string            `json:"footer"`
	// header_template cases: the template and the message snapshot it
	// renders against (the Stage 4.4 F-15 fix).
	Template     string            `json:"template"`
	ChatID       string            `json:"chat_id"`
	ChatUsername string            `json:"chat_username"`
	ChatTitle    string            `json:"chat_title"`
	MessageID    int64             `json:"message_id"`
	Date         int64             `json:"date"`
	RuleName     string            `json:"rule_name"`
	SenderName   string            `json:"sender_name"`
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

// orderedEntriesFromMap is the order-preserving twin of entriesFromMap.
// The parity harness reads the case list from JSON, and Python's
// replacements map applies same-priority entries in insertion order
// (RPL-PRIORITY-01) — an order Go's map[string]any throws away. The
// harness relies on its own JSON key order, so this helper takes the pairs
// the ordered decoder produced instead of a decoded map.
func orderedEntriesFromMap(pairs []orderedPair) []cleaning.ReplacementEntry {
	entries := make([]cleaning.ReplacementEntry, 0, len(pairs))
	for _, pair := range pairs {
		entry := cleaning.ReplacementEntry{Find: pair.key}
		opts := optionsMap(pair.value)
		if opts != nil {
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
		} else if text, ok := pair.value.(string); ok {
			entry.Replace = text
		}
		entries = append(entries, entry)
	}
	return entries
}

// optionsMap converts an ordered object's value into the map form the
// string/bool helpers above read, or nil when the value is not an object.
func optionsMap(value any) map[string]any {
	pairs, ok := value.([]orderedPair)
	if !ok {
		return nil
	}
	out := make(map[string]any, len(pairs))
	for _, pair := range pairs {
		switch inner := pair.value.(type) {
		case []orderedPair:
			out[pair.key] = optionsMap(inner)
		default:
			out[pair.key] = inner
		}
	}
	return out
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
		// Order-preserving: same-priority entries apply in the order the
		// case file wrote them, exactly as Python's apply_replacements
		// reads a dict. The plain map field is the fallback for cases that
		// carry no inner objects (where order is unobservable).
		if ordered := orderedCaseReplacements(c); len(ordered) > 0 {
			cfg.Replacements = ordered
		} else {
			cfg.Replacements = entriesFromMap(c.Replacements)
		}
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
	case "header_template":
		// The F-15 fix: the template renders against the message snapshot
		// the case carries, exactly as the delivery engine builds it from
		// the durable payload.
		cfg.Header = c.Template
		cfg.HeaderVars = &cleaning.HeaderVars{
			SourceChatID:       c.ChatID,
			SourceChatUsername: c.ChatUsername,
			SourceChatTitle:    c.ChatTitle,
			SourceMessageID:    c.MessageID,
			SourceDate:         c.Date,
			SenderName:         c.SenderName,
			EffectiveText:      c.Text,
		}
		cfg.HeaderTask = cleaning.HeaderTask{Name: c.RuleName}
	default:
		return ""
	}
	result := engine.Apply(c.Text, cfg, nil)
	return result.Text
}

func main() {
	var cases []testCase
	// Decode the array token-stream style so every case's replacements
	// object keeps the key order the harness wrote it in. json.Unmarshal
	// into []testCase would discard that order — the exact order
	// Python's apply_replacements relies on (RPL-PRIORITY-01).
	dec := json.NewDecoder(os.Stdin)
	if err := readCasesOrdered(dec, &cases); err != nil {
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

// readCasesOrdered decodes the case array, filling each case's
// orderedReplacements from the ordered decoder so the replacements engine
// sees the entries in document order. The structured fields still come
// from a plain decode of the same array, which keeps the driver's schema
// handling in one place.
func readCasesOrdered(dec *json.Decoder, out *[]testCase) error {
	token, err := dec.Token()
	if err != nil {
		return err
	}
	delim, ok := token.(json.Delim)
	if !ok || delim != '[' {
		return fmt.Errorf("expected a JSON array of cases, got %v", token)
	}
	for dec.More() {
		var c testCase
		buffered, err := decodeCaseValue(dec)
		if err != nil {
			return err
		}
		if err := json.Unmarshal(buffered, &c); err != nil {
			return err
		}
		c.orderedReplacements = extractOrderedReplacements(buffered)
		*out = append(*out, c)
	}
	if _, err := dec.Token(); err != nil {
		return err
	}
	return nil
}

// decodeCaseValue reads one array element's raw JSON bytes so the case
// can be decoded twice: once into the struct (for fields) and once
// through the ordered decoder (for key order).
func decodeCaseValue(dec *json.Decoder) ([]byte, error) {
	var raw json.RawMessage
	if err := dec.Decode(&raw); err != nil {
		return nil, err
	}
	return []byte(raw), nil
}

// extractOrderedReplacements reads the "replacements" object of one case
// in document order.
func extractOrderedReplacements(raw []byte) []orderedPair {
	dec := json.NewDecoder(bytes.NewReader(raw))
	pairs, err := decodeOrderedObject(dec)
	if err != nil {
		return nil
	}
	for _, pair := range pairs {
		if pair.key == "replacements" {
			if inner, ok := pair.value.([]orderedPair); ok {
				return inner
			}
			return nil
		}
	}
	return nil
}

// ordered_pair is one JSON object key with its decoded value, in document
// order. json.Unmarshal into map[string]any discards key order, which the
// replacements contract depends on (RPL-PRIORITY-01).
type orderedPair struct {
	key   string
	value any
}

// decodeOrderedObject reads a JSON object as key/value pairs in document
// order. Values that are objects recurse as []orderedPair; scalars decode
// with the plain token reader.
func decodeOrderedObject(dec *json.Decoder) ([]orderedPair, error) {
	token, err := dec.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := token.(json.Delim)
	if !ok || delim != '{' {
		return nil, fmt.Errorf("expected a JSON object, got %v", token)
	}
	pairs := []orderedPair{}
	for dec.More() {
		keyToken, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key, ok := keyToken.(string)
		if !ok {
			return nil, fmt.Errorf("expected an object key, got %v", keyToken)
		}
		value, err := decodeOrderedValue(dec)
		if err != nil {
			return nil, err
		}
		pairs = append(pairs, orderedPair{key: key, value: value})
	}
	if _, err := dec.Token(); err != nil { // the closing '}'
		return nil, err
	}
	return pairs, nil
}

// decodeOrderedValue reads one value; nested objects keep their key order
// as []orderedPair and scalars come back as plain Go values.
func decodeOrderedValue(dec *json.Decoder) (any, error) {
	token, err := dec.Token()
	if err != nil {
		return nil, err
	}
	if delim, ok := token.(json.Delim); ok {
		if delim == '{' {
			return decodeOrderedObjectRest(dec)
		}
		return nil, fmt.Errorf("unexpected JSON array inside the case object")
	}
	switch value := token.(type) {
	case string, bool, nil:
		return value, nil
	default:
		// json.Number and any other scalar token type: hand it through.
		return value, nil
	}
}

// decodeOrderedObjectRest reads an object's body once its '{' is consumed.
func decodeOrderedObjectRest(dec *json.Decoder) ([]orderedPair, error) {
	pairs := []orderedPair{}
	for dec.More() {
		keyToken, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key, ok := keyToken.(string)
		if !ok {
			return nil, fmt.Errorf("expected an object key, got %v", keyToken)
		}
		value, err := decodeOrderedValue(dec)
		if err != nil {
			return nil, err
		}
		pairs = append(pairs, orderedPair{key: key, value: value})
	}
	if _, err := dec.Token(); err != nil { // the closing '}'
		return nil, err
	}
	return pairs, nil
}

// orderedCaseReplacements reads the replacements map from one case in the
// order the case file wrote it, so the parity harness exercises the same
// order-dependent composition Python's apply_replacements does.
func orderedCaseReplacements(c testCase) []cleaning.ReplacementEntry {
	if c.orderedReplacements == nil {
		return nil
	}
	return orderedEntriesFromMap(c.orderedReplacements)
}
