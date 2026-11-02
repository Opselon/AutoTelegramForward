package rulestore

import (
	"strings"

	"atf/worker/internal/domain/cleaning"
)

// transform.go resolves the task's text-transform configuration out of the
// metadata blob — the Go counterpart of the Python ForwardRule properties
// DurableMessagePipeline._apply_transforms reads
// (pipeline.py:1401-1580, entities.py:765-1080).
//
// Every field here mirrors a Python property, and the merge order matters:
// cleaner_config is the authoritative home, with the legacy per-flag fields
// acting as the fallback a pre-v2.0 task still relies on
// (ForwardRule.build_cleaner, entities.py:1017-1065). A task that set
// `clean_usernames` before the config blob existed must clean exactly the
// same after the Worker takes over.

// transformConfigFromMetadata builds the cleaning.Config for one task from
// its decoded metadata blob. It never returns an error and never panics: a
// malformed blob means "no transform configured", the same answer Python's
// build_cleaner gives when the config will not coerce.
func transformConfigFromMetadata(meta map[string]any) cleaning.Config {
	cfg := cleaning.Config{}

	if cfgBlob, ok := meta["cleaner_config"].(map[string]any); ok {
		cfg.Cleaner = buildCleanerRule(cfgBlob, meta)
	} else if hasLegacyCleanerFlags(meta) {
		cfg.Cleaner = buildCleanerRule(nil, meta)
	}

	cfg.LinkPolicy = metaStr(meta, "link_policy", "PRESERVE_ALL")
	cfg.RemoveLinks = metaBool(meta, "remove_links", false)
	cfg.LinkReplacement = metaStr(meta, "link_replacement", "")
	cfg.DomainAllowlist = stringListField(meta, "domain_allowlist")
	cfg.DomainBlocklist = stringListField(meta, "domain_blocklist")
	cfg.LinkRewriteMap = mapField(meta, "link_rewrite_map")
	cfg.RemoveEmojis = metaBool(meta, "remove_emojis", false)

	if policyBlob, ok := meta["content_policy"].(map[string]any); ok {
		cfg.ContentPolicy = buildContentPolicy(policyBlob)
	}
	cfg.Replacements = buildReplacements(meta["replacements"])
	cfg.Header = metaStr(meta, "header", "")
	cfg.Footer = metaStr(meta, "footer", "")
	// The Go API stores the panel's header/footer under these names; the
	// Python entity reads custom_header/custom_footer first and falls back
	// to the metadata keys (entities.py:589-632).
	if cfg.Header == "" {
		cfg.Header = metaStr(meta, "custom_header", "")
	}
	if cfg.Footer == "" {
		cfg.Footer = metaStr(meta, "custom_footer", "")
	}
	cfg.Watermark = metaStr(meta, "watermark_text", "")
	if cfg.Watermark == "" {
		cfg.Watermark = metaStr(meta, "watermark", "")
	}
	cfg.LinkButtons = buildLinkButtons(meta["link_buttons"])
	if ak, ok := meta["affiliate_kit"].(map[string]any); ok {
		if metaBool(ak, "enabled", false) {
			cfg.AffiliateKit = &cleaning.AffiliateKit{Prefix: metaStr(ak, "prefix", "")}
		}
	}
	return cfg
}

// hasLegacyCleanerFlags is Python build_cleaner's "any legacy cleaner
// settings present" test (entities.py:1023-1026): the master switch may be
// absent on a pre-v2.0 row, and an active legacy flag still enables the
// cleaner.
func hasLegacyCleanerFlags(meta map[string]any) bool {
	for _, key := range []string{
		"clean_usernames", "clean_hashtags", "clean_phones", "clean_empty_lines",
		"clean_buttons", "remove_links", "remove_emojis",
	} {
		if metaBool(meta, key, false) {
			return true
		}
	}
	if words := stringListField(meta, "clean_custom_words"); len(words) > 0 {
		return true
	}
	return false
}

// buildCleanerRule is ForwardRule.build_cleaner: the config blob is
// authoritative for every key it carries, and a legacy flag fills in only
// the keys the blob left unset.
func buildCleanerRule(cfg map[string]any, meta map[string]any) *cleaning.CleanerRule {
	enabled := metaBool(meta, "cleaner_enabled", false)
	if !enabled && !hasLegacyCleanerFlags(meta) {
		return nil
	}
	if cfg == nil {
		cfg = map[string]any{}
	}

	patterns := stringListField(cfg, "patterns")
	if words := stringListField(meta, "clean_custom_words"); len(words) > 0 {
		seen := make(map[string]bool, len(patterns))
		for _, p := range patterns {
			seen[p] = true
		}
		for _, w := range words {
			trimmed := strings.TrimSpace(w)
			if trimmed != "" && !seen[trimmed] {
				patterns = append(patterns, trimmed)
				seen[trimmed] = true
			}
		}
	}

	rule := &cleaning.CleanerRule{
		Patterns:           patterns,
		UseRegex:           metaBool(cfg, "use_regex", false),
		RemoveURLs:         metaBool(cfg, "remove_urls", metaBool(meta, "remove_links", false)),
		RemoveMentions:     metaBool(cfg, "remove_mentions", metaBool(meta, "clean_usernames", false)),
		RemoveHashtags:     metaBool(cfg, "remove_hashtags", metaBool(meta, "clean_hashtags", false)),
		RemoveEmojis:       metaBool(cfg, "remove_emojis", metaBool(meta, "remove_emojis", false)),
		RemovePhoneNumbers: metaBool(cfg, "remove_phone_numbers", metaBool(meta, "clean_phones", false)),
		RemoveEmptyLines:   metaBool(cfg, "remove_empty_lines", metaBool(meta, "clean_empty_lines", false)),
		StripButtons:       metaBool(cfg, "strip_buttons", metaBool(meta, "clean_buttons", false)),
		ApplyToCaption:     metaBool(cfg, "apply_to_caption", true),
		CaseSensitive:      metaBool(cfg, "case_sensitive", false),
		IsActive:           true,
	}

	hasOption := rule.RemoveURLs || rule.RemoveMentions || rule.RemoveHashtags ||
		rule.RemoveEmojis || rule.RemovePhoneNumbers || rule.RemoveEmptyLines ||
		rule.StripButtons || len(rule.Patterns) > 0
	if !hasOption {
		return nil
	}
	return rule
}

// buildContentPolicy is ContentPolicy.from_dict over the task's policy blob,
// narrowed to the rewrite fields _apply_transforms' transform() applies.
func buildContentPolicy(blob map[string]any) *cleaning.ContentPolicy {
	policy := &cleaning.ContentPolicy{
		RemoveLinks:  metaBool(blob, "remove_links", false),
		StripWords:   stringListField(blob, "strip_words"),
		CaseSensitive: metaBool(blob, "case_sensitive", false),
	}
	if raw, ok := blob["replacements"].(map[string]any); ok {
		policy.Replacements = pairsFromMap(raw)
	}
	return policy
}

// buildReplacements is text_replace.apply_replacements' entry coercion
// (text_replace.py:247-283): a legacy "old": "new" map, a map of option
// dicts, or a list of entries.
func buildReplacements(raw any) []cleaning.ReplacementEntry {
	switch value := raw.(type) {
	case map[string]any:
		entries := make([]cleaning.ReplacementEntry, 0, len(value))
		for source, entry := range value {
			find := source
			if opts, ok := entry.(map[string]any); ok {
				// A dict may carry its own `source`: entries written by the
				// replace-rule manager are keyed by rule id, not by text.
				if stored, ok := opts["source"].(string); ok && stored != "" {
					find = stored
				}
				entries = append(entries, cleaning.ReplacementEntry{
					Find:       find,
					Replace:    metaStr(opts, "to", ""),
					IsRegex:    strings.EqualFold(metaStr(opts, "mode", "literal"), "regex"),
					IgnoreCase: hasFlag(opts, "i"),
					WholeWord:  metaBool(opts, "whole_word", false) || hasFlag(opts, "w"),
				})
				continue
			}
			if text, ok := entry.(string); ok {
				entries = append(entries, cleaning.ReplacementEntry{
					Find:    find,
					Replace: text,
				})
			}
		}
		return entries
	case []any:
		entries := make([]cleaning.ReplacementEntry, 0, len(value))
		for _, entry := range value {
			item, ok := entry.(map[string]any)
			if !ok {
				continue
			}
			entries = append(entries, cleaning.ReplacementEntry{
				Find:       metaStr(item, "source", ""),
				Replace:    metaStr(item, "to", ""),
				IsRegex:    strings.EqualFold(metaStr(item, "mode", "literal"), "regex"),
				IgnoreCase: hasFlag(item, "i"),
				WholeWord:  metaBool(item, "whole_word", false) || hasFlag(item, "w"),
			})
		}
		return entries
	}
	return nil
}

// hasFlag reports whether the entry's flags string carries the letter.
func hasFlag(opts map[string]any, flag string) bool {
	flags, ok := opts["flags"].(string)
	if !ok {
		return false
	}
	return strings.Contains(flags, flag)
}

// pairsFromMap converts the content policy's {find: replace} replacements
// into the ordered pair list _apply_replacements consumes.
func pairsFromMap(raw map[string]any) [][2]string {
	pairs := make([][2]string, 0, len(raw))
	for find, replace := range raw {
		text, ok := replace.(string)
		if !ok {
			continue
		}
		pairs = append(pairs, [2]string{find, text})
	}
	return pairs
}

// buildLinkButtons is the link_buttons add-on's stored shape: a list of
// {text, url} objects.
func buildLinkButtons(raw any) []cleaning.LinkButton {
	list, ok := raw.([]any)
	if !ok {
		return nil
	}
	buttons := make([]cleaning.LinkButton, 0, len(list))
	for _, entry := range list {
		item, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		url := metaStr(item, "url", "")
		if url == "" {
			continue
		}
		buttons = append(buttons, cleaning.LinkButton{
			Text: metaStr(item, "text", ""),
			URL:  url,
		})
	}
	return buttons
}

// stringListField reads a JSON array of strings. Numbers and nested objects
// are skipped, not stringified — the Python reader's `_as_str_list` treats
// a non-string entry as absent rather than coercing it.
func stringListField(blob map[string]any, key string) []string {
	raw, ok := blob[key]
	if !ok {
		return nil
	}
	list, ok := raw.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(list))
	for _, item := range list {
		if text, ok := item.(string); ok {
			trimmed := strings.TrimSpace(text)
			if trimmed != "" {
				out = append(out, trimmed)
			}
		}
	}
	return out
}

// mapField reads a JSON string-keyed map whose values are strings.
func mapField(blob map[string]any, key string) map[string]string {
	raw, ok := blob[key]
	if !ok {
		return nil
	}
	list, ok := raw.(map[string]any)
	if !ok {
		return nil
	}
	out := make(map[string]string, len(list))
	for k, item := range list {
		if text, ok := item.(string); ok {
			out[k] = text
		}
	}
	return out
}
