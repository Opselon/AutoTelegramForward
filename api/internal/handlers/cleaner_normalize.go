package handlers

import "encoding/json"

// maxCleanerPatterns bounds the number of removal patterns a client can
// configure. Without it the cleaner config is an unbounded array.
const maxCleanerPatterns = 256

// normalizeCleanerConfig validates the nested cleaner_config object.
//
// The cleaner is a removal-only transform, so an over-broad or malformed
// configuration cannot corrupt a message — but it can still make a task_useless
// (strip everything) or bloat the metadata row. This keeps the typed contract:
// known fields are coerced, unknown fields are kept for forward compatibility,
// and the pattern array is bounded and string-only.
func normalizeCleanerConfig(meta map[string]any) {
	raw, ok := meta["cleaner_config"]
	if !ok {
		return
	}

	// A non-object cleaner_config is meaningless: drop it rather than persist
	// garbage that build_cleaner() would reject anyway.
	obj, ok := raw.(map[string]any)
	if !ok {
		delete(meta, "cleaner_config")
		return
	}

	// Booleans.
	for _, k := range []string{
		"use_regex", "remove_urls", "remove_mentions", "remove_hashtags",
		"remove_emojis", "apply_to_caption", "case_sensitive",
	} {
		if v, ok := obj[k]; ok {
			obj[k] = toBool(v)
		}
	}

	// patterns: bounded, string-only. Non-string entries are dropped rather
	// than stringified — a numeric "pattern" is a client bug, not a config.
	if pats, ok := obj["patterns"].([]any); ok {
		out := make([]any, 0, len(pats))
		for _, p := range pats {
			if s, ok := p.(string); ok {
				if s = trimSpaceASCII(s); s != "" {
					out = append(out, s)
					if len(out) >= maxCleanerPatterns {
						break
					}
				}
			}
		}
		obj["patterns"] = out
	}

	// name: bounded string.
	if name, ok := obj["name"].(string); ok && len(name) > 200 {
		obj["name"] = name[:200]
	}

	// Keep the normalized object marshalled deterministically.
	if b, err := json.Marshal(obj); err == nil {
		_ = b // shape already validated; the map is stored by reference
	}
}

// trimSpaceASCII strips ASCII whitespace without pulling in unicode helpers.
func trimSpaceASCII(s string) string {
	start, end := 0, len(s)
	for start < end && (s[start] == ' ' || s[start] == '\t' || s[start] == '\n' || s[start] == '\r') {
		start++
	}
	for end > start && (s[end-1] == ' ' || s[end-1] == '\t' || s[end-1] == '\n' || s[end-1] == '\r') {
		end--
	}
	return s[start:end]
}
