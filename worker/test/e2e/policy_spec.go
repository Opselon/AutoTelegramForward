package e2e

// asMap renders the policy as the metadata.content_policy blob the pipeline reads.
func (p *PolicySpec) asMap() map[string]any {
	m := map[string]any{
		"enabled":         true,
		"blacklist_words": p.BlacklistWords,
		"whitelist_words": p.WhitelistWords,
		"whitelist_mode":  "any",
		"block_links":     p.BlockLinks,
		"remove_links":    p.RemoveLinks,
		"strip_words":     p.StripWords,
		"regex_block":     p.RegexBlock,
		"regex_require":   p.RegexRequire,
		"min_length":      p.MinLength,
		"max_length":      p.MaxLength,
		"allowed_media":   p.AllowedMedia,
		"blocked_media":   p.BlockedMedia,
		"case_sensitive":  p.CaseSensitive,
		"word_boundary":   p.WordBoundary,
	}
	if p.WhitelistMode != "" {
		m["whitelist_mode"] = p.WhitelistMode
	}
	if p.Enabled != nil {
		m["enabled"] = *p.Enabled
	}

	return m
}

// asMap renders the cleaner_config blob the transform projection reads:
// the enable flag lives at the TOP level of the task metadata
// (cleaner_enabled), the per-option flags live under cleaner_config.
// Whitespace normalization maps onto the collapse flag; newline
// normalization onto remove_empty_lines.
func (c *CleanerSpec) asMap() map[string]any {
	m := map[string]any{
		"remove_urls":          c.Urls,
		"remove_mentions":      c.Mentions,
		"remove_hashtags":      c.Hashtags,
		"remove_emojis":        c.Emoji,
		"remove_phone_numbers": c.Phones,
	}
	if c.Whitespace {
		m["collapse_whitespace"] = true
	}
	if c.Newlines {
		m["remove_empty_lines"] = true
	}
	return m
}
