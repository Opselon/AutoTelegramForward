package ownership

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// TaskSpec describes a task the scenario installs. Fields map 1:1 onto the
// forward_rules columns the production rulestore reads — the same installer
// shape the e2e harness uses, kept identical so a configuration that passes
// here is a configuration the live Go Worker loads the same way.
type TaskSpec struct {
	ID           string
	Source       string
	SourceName   string
	Target       string
	TargetName   string
	Active       bool
	FilterID     string
	AIConfigID   string
	Replacements map[string]map[string]any
	Metadata     map[string]any
	Header       string
	Footer       string
	Policy       *PolicySpec
}

// PolicySpec is the metadata.content_policy shape the Python pipeline and the
// Go transform pipeline both read.
type PolicySpec struct {
	Enabled        *bool
	BlacklistWords []string
	WhitelistWords []string
	WhitelistMode  string
	BlockLinks     bool
	RemoveLinks    bool
	StripWords     []string
	Replacements        map[string]string
	OrderedReplacements [][2]string
	RegexBlock          []string
	RegexRequire        []string
	MinLength           int
	MaxLength           int
	AllowedMedia        []string
	BlockedMedia        []string
	CaseSensitive       bool
	WordBoundary        bool
	Cleaner             *CleanerSpec
}

// CleanerSpec is the metadata.cleaner shape: which normalizations run.
type CleanerSpec struct {
	Enabled    bool
	Emoji      bool
	Urls       bool
	Mentions   bool
	Hashtags   bool
	Phones     bool
	Whitespace bool
	Newlines   bool
}

func (c *CleanerSpec) asMap() map[string]any {
	return map[string]any{
		"enabled": c.Enabled, "emoji": c.Emoji, "urls": c.Urls,
		"mentions": c.Mentions, "hashtags": c.Hashtags, "phones": c.Phones,
		"whitespace": c.Whitespace, "newlines": c.Newlines,
	}
}

func (p *PolicySpec) asMap() map[string]any {
	m := map[string]any{}
	if p.Enabled != nil {
		m["enabled"] = *p.Enabled
	}
	if len(p.BlacklistWords) > 0 {
		m["blacklist"] = p.BlacklistWords
	}
	if len(p.WhitelistWords) > 0 {
		m["whitelist"] = p.WhitelistWords
	}
	if p.WhitelistMode != "" {
		m["whitelist_mode"] = p.WhitelistMode
	}
	if p.BlockLinks {
		m["block_links"] = true
	}
	if p.RemoveLinks {
		m["remove_links"] = true
	}
	if len(p.StripWords) > 0 {
		m["strip_words"] = p.StripWords
	}
	if len(p.Replacements) > 0 {
		m["replacements"] = p.Replacements
	}
	if len(p.OrderedReplacements) > 0 {
		m["ordered_replacements"] = p.OrderedReplacements
	}
	if len(p.RegexBlock) > 0 {
		m["regex_block"] = p.RegexBlock
	}
	if len(p.RegexRequire) > 0 {
		m["regex_require"] = p.RegexRequire
	}
	if p.MinLength > 0 {
		m["min_length"] = p.MinLength
	}
	if p.MaxLength > 0 {
		m["max_length"] = p.MaxLength
	}
	if len(p.AllowedMedia) > 0 {
		m["allowed_media"] = p.AllowedMedia
	}
	if len(p.BlockedMedia) > 0 {
		m["blocked_media"] = p.BlockedMedia
	}
	if p.CaseSensitive {
		m["case_sensitive"] = true
	}
	if p.WordBoundary {
		m["word_boundary"] = true
	}
	return m
}

// Install writes the task as real forward_rules (+ filter_rules when a
// FilterID is set) rows into the harness DB the engine loads.
func (h *Harness) Install(spec TaskSpec) {
	h.t.Helper()
	if spec.ID == "" {
		h.t.Fatal("TaskSpec needs an ID")
	}
	meta := map[string]any{}
	for k, v := range spec.Metadata {
		meta[k] = v
	}
	if spec.Replacements != nil {
		meta["replacements"] = spec.Replacements
	}
	if spec.Policy != nil && (len(spec.Policy.Replacements) > 0 || len(spec.Policy.OrderedReplacements) > 0) {
		// The transforms pipeline reads the top-level metadata key; the
		// content_policy copy is the policy-restricted view. Preserve entry
		// order: the store's ordered decoder keeps same-priority entries in
		// the order the task wrote them, and overlapping rules are
		// order-sensitive.
		if len(spec.Policy.OrderedReplacements) > 0 {
			var sb strings.Builder
			sb.WriteByte('{')
			for i, kv := range spec.Policy.OrderedReplacements {
				if i > 0 {
					sb.WriteByte(',')
				}
				k, _ := json.Marshal(kv[0])
				v, _ := json.Marshal(kv[1])
				sb.Write(k)
				sb.WriteByte(':')
				sb.Write(v)
			}
			sb.WriteByte('}')
			meta["replacements"] = json.RawMessage(sb.String())
		} else {
			reps := map[string]any{}
			for k, v := range spec.Policy.Replacements {
				reps[k] = v
			}
			meta["replacements"] = reps
		}
	}
	if spec.Policy != nil {
		meta["content_policy"] = spec.Policy.asMap()
		if spec.Policy.Cleaner != nil {
			// The production split: the enable flag is a top-level task
			// metadata key (cleaner_enabled); the option flags live under
			// cleaner_config.
			meta["cleaner_enabled"] = spec.Policy.Cleaner.Enabled
			meta["cleaner_config"] = spec.Policy.Cleaner.asMap()
		}
	}
	metaJSON, _ := json.Marshal(meta)
	repsJSON, _ := json.Marshal(spec.Replacements)
	now := time.Now().Unix()

	// Every column the live schema carries, with a typed default so an
	// older or newer column list always binds cleanly.
	vals := map[string]any{
		"id": spec.ID, "session_id": "sess-ownership", "source_chat_id": spec.Source,
		"source_chat_name": spec.SourceName, "target_chat_id": spec.Target,
		"target_chat_name": spec.TargetName, "routing_type": "CHANNEL_TO_CHANNEL",
		"forward_mode": "COPY_MESSAGE", "is_active": boolToInt(spec.Active),
		"filter_rule_id": spec.FilterID, "ai_config_id": spec.AIConfigID,
		"remove_links": 0, "custom_caption_template": "", "created_at": now,
		"updated_at": now, "target_chat_ids": "[]", "delay_seconds": 0.0,
		"skip_history": 1, "since_ts": 0, "ignore_edits": 0,
		"trigger_events": `["NEW_MESSAGE"]`, "content_mode": "AUTO",
		"metadata": string(metaJSON), "chain_of": "",
		"replacements": string(repsJSON), "header": spec.Header, "footer": spec.Footer,
		"sync_edits": 1, "sync_deletes": 0, "allow_senders": "[]",
		"block_senders": "[]", "target_topic_id": 0, "version": 33,
		"ai_fallback_policy": "SEND_ORIGINAL", "ai_prompt_version": 0,
		"ai_timeout_seconds": 15.0, "ai_secondary_config_id": nil,
		"message_category": "ALL", "intermediate_channel_id": "",
		"intermediate_channel_name": "", "intermediate_target_chat_id": "",
		"use_intermediate": 0, "fallback_mode": "COPY_MESSAGE",
		"fallback_enabled": 1, "detection_criteria": "{}", "priority": 10,
		"execution_order": 0, "multi_route": 0, "media_handling": "AUTO",
		"dedupe_policy": "STRICT", "max_retries": 3, "retry_backoff_base": 2.0,
		"rate_limit_per_minute": 25, "rate_limit_burst": 0, "custom_header": "",
		"custom_footer": "", "header_enabled": 0, "template_text": "",
		"template_media": "", "template_album": "", "caption_max_length": 0,
		"preserve_signature": 0, "is_paused": 0, "paused_until": 0,
		"link_policy": "PRESERVE_ALL", "domain_allowlist": "[]",
		"domain_blocklist": "[]", "link_rewrite_map": "{}",
		"allowed_media_types": "[]", "split_long_caption": 1,
		"owner_user_id": h.owner,
	}

	rows, err := h.DB().Query(`PRAGMA table_info(forward_rules)`)
	if err != nil {
		h.t.Fatalf("table info: %v", err)
	}
	defer rows.Close()

	var cols []string
	var args []any
	var placeholders []string
	for rows.Next() {
		var cid int
		var name, ctype string
		var notnull, pk int
		var dflt any
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			continue
		}
		if v, ok := vals[name]; ok {
			cols = append(cols, name)
			args = append(args, v)
			placeholders = append(placeholders, "?")
		}
	}

	query := fmt.Sprintf("INSERT INTO forward_rules (%s) VALUES (%s)",
		strings.Join(cols, ","), strings.Join(placeholders, ","))
	if _, err := h.DB().Exec(query, args...); err != nil {
		h.t.Fatalf("install task %s: %v", spec.ID, err)
	}
}

// FilterSpec maps onto filter_rules the production engine reads.
type FilterSpec struct {
	ID           string
	Enabled      bool
	Whitelist    []string
	Blacklist    []string
	RegexBlock   []string
	RegexRequire []string
	AllowedMedia []string
	BlockedMedia []string
	MinLen       int
	MaxLen       int
	DropService  bool
}

// InstallFilter writes a real filter_rules row owned by this harness tenant.
func (h *Harness) InstallFilter(f FilterSpec) {
	h.t.Helper()
	wl, _ := json.Marshal(f.Whitelist)
	bl, _ := json.Marshal(f.Blacklist)
	rb, _ := json.Marshal(f.RegexBlock)
	rr, _ := json.Marshal(f.RegexRequire)
	am, _ := json.Marshal(f.AllowedMedia)
	bm, _ := json.Marshal(f.BlockedMedia)
	_, err := h.DB().Exec(`INSERT INTO filter_rules
 (id, name, is_enabled, whitelist_keywords, blacklist_keywords, regex_patterns,
  allowed_media_types, blocked_media_types, min_message_length, max_message_length,
  owner_user_id, regex_block, regex_require, drop_service_messages)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		f.ID, "ownership-filter", boolToInt(f.Enabled), string(wl), string(bl), "[]",
		string(am), string(bm), f.MinLen, f.MaxLen, h.owner, string(rb), string(rr),
		boolToInt(f.DropService))
	if err != nil {
		h.t.Fatalf("install filter %s: %v", f.ID, err)
	}
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func boolPtr(b bool) *bool { return &b }
