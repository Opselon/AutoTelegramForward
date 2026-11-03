package rulestore

import (
	"strconv"
	"strings"

	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/rule"
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
//
// The raw blob string is taken alongside the decoded map because JSON
// object key order is load-bearing for the replacements map: Python's dict
// preserves insertion order and text_replace applies same-priority entries
// in it (RPL-PRIORITY-01 is the pinned evidence), while Go's
// map[string]any discards it at decode time. Replacements are therefore
// re-decoded from the raw string with order kept.
func transformConfigFromMetadata(meta map[string]any, metadataJSON string) cleaning.Config {
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

	policyBlob, _ := meta["content_policy"].(map[string]any)
	if policyBlob != nil {
		cfg.ContentPolicy = buildContentPolicy(policyBlob)
	}
	cfg.TargetOverrides = buildTargetOverrides(policyBlob, meta)
	// The ordered re-decode: same-priority entries keep the order the task
	// stored them in, the order the Python engine applies them in.
	if replacements, ordered := orderedReplacements(metadataJSON, "replacements"); len(replacements) > 0 || ordered {
		cfg.Replacements = replacements
	} else {
		cfg.Replacements = buildReplacements(meta["replacements"])
	}
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
	// The paid-addon entitlement gate (entities.py:1843 is_addon_enabled):
	// a client-supplied flag is never trusted on its own — the addon must
	// be marked enabled in the persisted rule metadata, and this projection
	// is the single enforcement point the Go pipeline consults. Fail
	// closed: an absent or non-dict paid_addons block means the transform
	// never runs, so a request that claims an entitlement it was not
	// granted cannot reach the target text.
	cfg.LinkButtons = buildLinkButtons(meta["link_buttons"])
	if !addonEnabled(meta, "link_buttons") {
		cfg.LinkButtons = nil
	}
	if ak, ok := meta["affiliate_kit"].(map[string]any); ok {
		if metaBool(ak, "enabled", false) && addonEnabled(meta, "affiliate_kit") {
			cfg.AffiliateKit = &cleaning.AffiliateKit{Prefix: metaStr(ak, "prefix", "")}
		}
	}
	return cfg
}

// addonEnabled is ForwardRule.is_addon_enabled (entities.py:1843): the
// addon must be marked enabled in the persisted metadata's paid_addons
// block. A client-supplied flag alone is never sufficient.
func addonEnabled(meta map[string]any, key string) bool {
	addons, ok := meta["paid_addons"].(map[string]any)
	if !ok {
		return false
	}
	return metaBool(addons, key, false)
}

// ProbeTransform exposes the real metadata projection for cross-language
// parity testing. The differential harness must exercise the same code path
// the delivery engine consults — including the paid-addon entitlement gate —
// rather than a hand-built Config that would silently bypass it. It is
// exported for that purpose only; production resolves the projection
// through the rulestore's own rule loading.
func ProbeTransform(meta map[string]any, metadataJSON string) cleaning.Config {
	return transformConfigFromMetadata(meta, metadataJSON)
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

// buildTargetOverrides reads the target-scoped policy overrides. They live
// inside the content policy blob as
// metadata["content_policy"]["target_policies"]["<chat id>"] = {...}
// (entities.py:1256-1262, policy_chain's target lookup at :1403-1408).
//
// The flat metadata keys are NOT consulted: a target override is always
// nested, because the flat form has no way to say which target it belongs
// to. An override that is not a dict is ignored rather than coerced —
// Python's ContentPolicy.from_dict would reject it the same way.
func buildTargetOverrides(policyBlob map[string]any, meta map[string]any) map[string]*cleaning.ContentPolicy {
	raw := nestedPolicyKey(policyBlob, meta, "target_policies")
	if raw == nil {
		return nil
	}
	out := make(map[string]*cleaning.ContentPolicy, len(raw))
	for key, item := range raw {
		blob, ok := item.(map[string]any)
		if !ok {
			continue
		}
		out[goChatKey(key)] = buildContentPolicy(blob)
	}
	return out
}

// buildTargetPromptOverrides reads per-target prompt overrides from target_policies
// or target_prompts in task metadata.
func buildTargetPromptOverrides(policyBlob map[string]any, meta map[string]any) map[string]rule.TargetPrompt {
	out := make(map[string]rule.TargetPrompt)

	if raw := nestedPolicyKey(policyBlob, meta, "target_policies"); raw != nil {
		for key, item := range raw {
			blob, ok := item.(map[string]any)
			if !ok {
				continue
			}
			pid := metaStr(blob, "prompt_id", "")
			if pid != "" {
				ver := metaIntDefault(blob, "ai_prompt_version", 0)
				out[goChatKey(key)] = rule.TargetPrompt{PromptID: pid, PromptVersion: ver}
			}
		}
	}

	if raw := nestedPolicyKey(nil, meta, "target_prompts"); raw != nil {
		for key, item := range raw {
			switch v := item.(type) {
			case string:
				if strings.TrimSpace(v) != "" {
					out[goChatKey(key)] = rule.TargetPrompt{PromptID: strings.TrimSpace(v), PromptVersion: 0}
				}
			case map[string]any:
				pid := metaStr(v, "prompt_id", "")
				if pid != "" {
					ver := metaIntDefault(v, "ai_prompt_version", 0)
					if ver == 0 {
						ver = metaIntDefault(v, "prompt_version", 0)
					}
					out[goChatKey(key)] = rule.TargetPrompt{PromptID: pid, PromptVersion: ver}
				}
			}
		}
	}

	if len(out) == 0 {
		return nil
	}
	return out
}

// nestedPolicyKey reads target_policies from the policy blob, tolerating a
// blob that never carried the key (the common case: no target overrides).
func nestedPolicyKey(policyBlob map[string]any, meta map[string]any, key string) map[string]any {
	if policyBlob != nil {
		if raw, ok := policyBlob[key].(map[string]any); ok {
			return raw
		}
	}
	if meta != nil {
		if raw, ok := meta[key].(map[string]any); ok {
			return raw
		}
	}
	return nil
}

// goChatKey mirrors cleaning.chatKey (Python _chat_key). Duplicated here
// rather than exported because the rulestore builds the map and the
// pipeline looks it up: two call sites of one rule, and keeping the lookup
// key algorithm next to each side is what makes the pair obviously equal.
func goChatKey(chatID string) string {
	trimmed := strings.TrimSpace(chatID)
	if trimmed == "" {
		return ""
	}
	if n, err := strconv.ParseInt(trimmed, 10, 64); err == nil {
		return strconv.FormatInt(n, 10)
	}
	if f, err := strconv.ParseFloat(trimmed, 64); err == nil {
		return strconv.FormatInt(int64(f), 10)
	}
	return trimmed
}

// ProbeAIRewriteActive exposes the ai_rewrite_active gate for cross-language
// parity testing, the same purpose ProbeTransform serves.
func ProbeAIRewriteActive(aiConfigID string, meta map[string]any) bool {
	return aiRewriteActive(aiConfigID, meta)
}
