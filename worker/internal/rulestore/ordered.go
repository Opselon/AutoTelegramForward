package rulestore

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"

	"atf/worker/internal/domain/cleaning"
)

// ordered.go — JSON object key order, recovered.
//
// Go's encoding/json decodes every JSON object into map[string]any, which
// discards the order the keys were written in. That order is part of the
// replacements contract: Python's dict preserves insertion order and
// text_replace.apply_replacements applies same-priority entries in it
// (RPL-PRIORITY-01: {"#":"##","##":"x"} yields "x sale"; stored in the
// other order it yields "## sale"). Without recovery, the Go Worker would
// re-order a task's same-priority replacements and silently change what
// the task forwards.
//
// The decoder below token-streams the metadata blob and returns each
// object's values alongside the key order the document wrote. Only the
// replacements map needs this; every other field the transform reads is
// order-independent.

// orderedPair is one JSON object key with its decoded value, in document
// order. A nested object appears as []orderedPair recursively; arrays and
// scalars decode as plain Go values.
type orderedPair struct {
	key   string
	value any
}

// arrayMarker is the value an ordered read returns for a JSON array: key
// order does not apply, and the caller reports "present" so the
// decoded-map fallback handles the list form instead of dropping it.
const arrayMarker = "[ordered:array]"

// decodeOrderedObject reads a JSON object as a slice of (key, value)
// pairs in document order.
func decodeOrderedObject(dec *json.Decoder) ([]orderedPair, error) {
	token, err := dec.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := token.(json.Delim)
	if !ok || delim != '{' {
		return nil, fmt.Errorf("expected a JSON object, got %v", token)
	}
	return decodeOrderedObjectRest(dec)
}

// decodeOrderedObjectRest reads an object's body once its opening '{' has
// been consumed, returning the pairs in key order.
func decodeOrderedObjectRest(dec *json.Decoder) ([]orderedPair, error) {
	var pairs []orderedPair
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
	// Consume the closing '}'.
	if _, err := dec.Token(); err != nil {
		return nil, err
	}
	return pairs, nil
}

// decodeOrderedValue reads one JSON value. A nested object recurses
// through the ordered reader; an array is drained and marked, because key
// order is meaningless for it but the caller must still see "present";
// every scalar arrives as its own token.
func decodeOrderedValue(dec *json.Decoder) (any, error) {
	token, err := dec.Token()
	if err != nil {
		return nil, err
	}
	if delim, ok := token.(json.Delim); ok {
		if delim == '{' {
			return decodeOrderedObjectRest(dec)
		}
		if delim == '[' {
			if err := drainArray(dec); err != nil {
				return nil, err
			}
			return arrayMarker, nil
		}
		return nil, fmt.Errorf("unexpected JSON delimiter %q", delim)
	}
	// Scalars: strings, numbers, bools and null all arrive as their own
	// token. Anything the json package hands out is handed through; the
	// replacements reader only consumes strings and objects.
	return token, nil
}

// drainArray consumes the remainder of a JSON array the caller already
// opened, keeping the decoder's token stream in sync.
func drainArray(dec *json.Decoder) error {
	depth := 1
	for depth > 0 {
		token, err := dec.Token()
		if err != nil {
			return err
		}
		if delim, ok := token.(json.Delim); ok {
			switch delim {
			case '[':
				depth++
			case ']':
				depth--
			}
		}
	}
	return nil
}

// orderedReplacements decodes the replacements map from the raw metadata
// JSON, in the order the task stored it. It returns the entries and
// whether the field was present.
//
// A malformed blob, an empty blob, or an absent field yields
// (nil, false): the caller falls back to the decoded-map path, whose
// iteration order is arbitrary but whose entries are still correct when
// the task's entries do not share a priority. A present-but-not-an-object
// field yields (nil, true) so the caller takes the same fallback rather
// than dropping the entries.
func orderedReplacements(metadataJSON, field string) ([]cleaning.ReplacementEntry, bool) {
	if strings.TrimSpace(metadataJSON) == "" {
		return nil, false
	}
	dec := json.NewDecoder(bytes.NewReader([]byte(metadataJSON)))
	pairs, err := decodeOrderedObject(dec)
	if err != nil || pairs == nil {
		return nil, false
	}
	for _, pair := range pairs {
		if pair.key != field {
			continue
		}
		inner, ok := pair.value.([]orderedPair)
		if !ok {
			// Present but not an object (a list, a scalar): report
			// "present" so the caller uses the decoded-map fallback.
			return nil, true
		}
		return entriesFromOrderedPairs(inner), true
	}
	return nil, false
}

// entriesFromOrderedPairs converts the ordered replacements object into
// entries, in the object's key order. Each value is the same shape
// buildReplacements accepts: a plain string, or an options dict.
func entriesFromOrderedPairs(pairs []orderedPair) []cleaning.ReplacementEntry {
	entries := make([]cleaning.ReplacementEntry, 0, len(pairs))
	for _, pair := range pairs {
		entry := cleaning.ReplacementEntry{Find: pair.key}
		switch value := pair.value.(type) {
		case []orderedPair:
			opts := optionsMap(value)
			entry.Replace = metaStr(opts, "to", "")
			// A dict may carry its own `source`: entries written by the
			// replace-rule manager are keyed by rule id, not by text.
			if stored, ok := opts["source"].(string); ok && stored != "" {
				entry.Find = stored
			}
			if mode := metaStr(opts, "mode", "literal"); strings.EqualFold(mode, "regex") {
				entry.IsRegex = true
			}
			entry.IgnoreCase = hasFlag(opts, "i")
			entry.WholeWord = metaBool(opts, "whole_word", false) || hasFlag(opts, "w")
		case string:
			entry.Replace = value
		default:
			continue // an unreadable value is skipped, never stored as junk
		}
		entries = append(entries, entry)
	}
	return entries
}

// optionsMap converts an ordered object's pairs into the map form the
// shared metaStr/metaBool helpers read. The pairs carry the order; the map
// is only a lookup convenience for option keys, whose order does not
// matter.
func optionsMap(pairs []orderedPair) map[string]any {
	out := make(map[string]any, len(pairs))
	for _, pair := range pairs {
		switch value := pair.value.(type) {
		case []orderedPair:
			out[pair.key] = optionsMap(value)
		default:
			out[pair.key] = value
		}
	}
	return out
}
