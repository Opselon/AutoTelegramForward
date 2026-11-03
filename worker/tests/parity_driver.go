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
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"

	"atf/worker/internal/domain/album"
	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/dedup"
	"atf/worker/internal/domain/forwardinterval"
	"atf/worker/internal/domain/reply"
	"atf/worker/internal/domain/routing"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/engine/filter"
	"atf/worker/internal/rulestore"
	_ "github.com/mattn/go-sqlite3" // registers the sqlite3 driver the dedup lifecycle opens
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
	orderedReplacements []orderedPair  `json:"-"`
	Metadata            map[string]any `json:"metadata"`
	Header              string         `json:"header"`
	Footer              string         `json:"footer"`
	// header_template cases: the template and the message snapshot it
	// renders against (the Stage 4.4 F-15 fix).
	Template     string `json:"template"`
	ChatID       string `json:"chat_id"`
	ChatUsername string `json:"chat_username"`
	ChatTitle    string `json:"chat_title"`
	MessageID    int64  `json:"message_id"`
	Date         int64  `json:"date"`
	RuleName     string `json:"rule_name"`
	SenderName   string `json:"sender_name"`
	// is_caption marks a cleaner case's text as a media caption, for the
	// F-09 apply_to_caption switch the pipeline honors.
	IsCaption bool `json:"is_caption"`
	// filter_engine cases: the filter's fields and the message's media type.
	Filter map[string]any `json:"filter"`
	// ai_gate cases: the task's AI config id.
	AIConfigID string `json:"ai_config_id"`
	MediaType  string `json:"media_type"`
	// album_window cases: the per-task aggregation window and the number
	// of pieces in the in-flight group.
	Window float64 `json:"window"`
	Items  int     `json:"items"`
	// Stage 4.5 W1 routing cases: the task's routing fields (mirroring the
	// rule.ForwardRule projection) and the message snapshot the decisions
	// run against. ChatID and ChatUsername (above) are the message's
	// source chat id and @username; ForwardOrigin the "Forwarded from
	// <chat>" header snapshot. TargetChatID is the single target a
	// check_loop case decides against and the task's own primary for
	// all_targets cases.
	SourceChatID        string         `json:"source_chat_id"`
	SourceChatName      string         `json:"source_chat_name"`
	TargetChatID        string         `json:"target_chat_id"`
	TargetChatIDs       []string       `json:"target_chat_ids"`
	DetectionCriteria   map[string]any `json:"detection_criteria"`
	IntermediateChannel string         `json:"intermediate_channel_id"`
	ForwardOrigin       map[string]any `json:"forward_origin"`
	// dedup cases: the caption half of the fingerprint seam (text is the
	// shared Text field every engine already reads).
	Caption string `json:"caption"`
	// dedup cases: the content-fingerprint inputs. Text and Caption carry
	// the message body/seam pair, MediaGroupID the album id ("" = no
	// album); the case's op names the fingerprint operation to run so one
	// engine covers the whole normalization family.
	MediaGroupID string `json:"media_group_id"`
	// DedupOp selects the dedup engine operation: "fingerprint" computes
	// the content fingerprint, "claim_release" demonstrates the claim /
	// release lifecycle against a real store.
	DedupOp string `json:"dedup_op"`
	// fen cases: the raw forward_every_n value exactly as a hand-edited
	// row or an older client would store it (any JSON shape), and the
	// 1-based sequence position to test against the interval. The clamp
	// must land every one of these shapes on the same number both
	// engines do.
	FENValue    any `json:"fen_value"`
	FENSequence int `json:"fen_sequence"`
	// fen_slots reports slots_until_next alongside owns_slot, so the
	// harness pins the UI-copy formula too (it never decides delivery,
	// but the two engines must still agree on it).
	FENSlots bool `json:"fen_slots"`
	// effective_settings cases: the settings operation the case asks
	// for, the key(s) it asks about, the bot row(s) a store-backed case
	// seeds the bot_tokens table with, and the owner the scoped query
	// binds (empty = the rule knows no owner, the pre-ownership
	// unscoped read).
	SettingsOp     string         `json:"settings_op"`
	SettingsKey    string         `json:"settings_key"`
	SettingsKeys   []string       `json:"settings_keys"`
	SettingsBots   []botTokenRow  `json:"settings_bots"`
	SettingsOwner  int64          `json:"settings_owner"`

	// BotDefaults is the bot's defaults_json blob for the in-memory ops:
	// null = no bot attached, {} = a bot with no defaults.
	BotDefaults any `json:"bot_defaults"`
}

// botTokenRow is one bot_tokens seed row for a store-backed effective
// settings case: the token id, the owner whose bot it is, and the
// defaults_json blob the Core stores.
type botTokenRow struct {
	TokenID  string         `json:"token_id"`
	Owner    int64          `json:"owner"`
	Defaults map[string]any `json:"defaults"`
}

type result struct {
	Index  int    `json:"index"`
	Output string `json:"output"`
	Error  string `json:"error"`
}

// boolFieldDefault reads a bool with an explicit default, for the flags
// whose Python entity default is not false.
func boolFieldDefault(m map[string]any, key string, def bool) bool {
	if v, ok := m[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return def
}

func boolField(m map[string]any, key string) bool {
	if v, ok := m[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return false
}

// rulestoreProbeTransform runs the case's metadata through the REAL
// rulestore projection (the same transformConfigFromMetadata the delivery
// engine consults), so the parity harness exercises the paid-addon
// entitlement gate production enforces instead of a hand-built Config
// that would silently bypass it. Returns nil when the metadata cannot be
// encoded, in which case the caller leaves the add-ons off (fail closed).
func rulestoreProbeTransform(meta map[string]any) *cleaning.Config {
	raw, err := json.Marshal(meta)
	if err != nil {
		return nil
	}
	cfg := rulestore.ProbeTransform(meta, string(raw))
	return &cfg
}

// applyFilterField sets one FilterRule field from a parity case's raw value.
// Only the keys the cases carry are handled; anything else is a test-author
// error the unknown-key check in the engine would catch in production.
func applyFilterField(f *rule.FilterRule, key string, value any) {
	switch key {
	case "min_message_length":
		if n, ok := value.(float64); ok {
			f.MinMessageLength = int(n)
		}
	case "max_message_length":
		if n, ok := value.(float64); ok {
			f.MaxMessageLength = int(n)
		}
	case "blacklist_keywords":
		f.BlacklistKeywords = stringListValue(value)
	case "whitelist_keywords":
		f.WhitelistKeywords = stringListValue(value)
	case "blocked_media_types":
		f.BlockedMediaTypes = stringListValue(value)
	case "allowed_media_types":
		f.AllowedMediaTypes = stringListValue(value)
	case "is_enabled":
		if b, ok := value.(bool); ok {
			f.IsEnabled = b
		}
	}
}

// stringListValue coerces a JSON-decoded list into []string.
func stringListValue(value any) []string {
	raw, ok := value.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
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
		// Python's entity default is True (entities.py:2366): an absent
		// flag cleans captions, and only an explicit false opts out. That
		// is what the F-09 skip key tests.
		ApplyToCaption: boolFieldDefault(m, "apply_to_caption", true),
		CaseSensitive:  boolField(m, "case_sensitive"),
		IsActive:       true,
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

// routingCaseRule builds the rule.ForwardRule projection a routing case
// describes, the same shape the rule store projects from forward_rules:
// primary source and name, extra sources in detection_criteria, primary
// target and list, and the branding channel the intermediate arm reads.
//
// The extra sources are routed through the real projection accessor
// (rule.ForwardRule.SourceChatIDs) rather than decoded here, so a parity
// case exercises the same path the delivery engine consults.
func routingCaseRule(c testCase) *rule.ForwardRule {
	return &rule.ForwardRule{
		ID:                    "r",
		IsActive:              true,
		SourceChatID:          c.SourceChatID,
		SourceChatName:        c.SourceChatName,
		TargetChatID:          c.TargetChatID,
		TargetChatIDs:         c.TargetChatIDs,
		DetectionCriteria:     c.DetectionCriteria,
		IntermediateChannelID: c.IntermediateChannel,
	}
}

func runCase(c testCase) string {
	engine := cleaning.NewPipeline()
	cfg := cleaning.Config{}
	switch c.Engine {
	case "cleaner":
		// The cleaner engine runs through the pipeline so a caption case
		// exercises the F-09 apply_to_caption switch (the pipeline's
		// other stages are inert when no flag is set, so routing it
		// through does not change any other cleaner case).
		cleaner := cleanerFromMap(c.Cleaner)
		if cleaner == nil {
			return c.Text
		}
		cfg.Cleaner = cleaner
		if c.IsCaption {
			return engine.ApplyText(c.Text, cfg, nil, cleaning.CaptionHint{IsCaption: true}).Text
		}
		return cleaner.Clean(c.Text)
	case "ai_gate":
		// The AI transform gate: the rulestore projection is the authority.
		// Routed through ProbeAIRewriteActive (the real
		// aiRewriteActive the rule store computes) and not a hand-built
		// answer, so a gate regression is a parity failure here.
		meta := map[string]any{}
		if c.Metadata != nil {
			meta = c.Metadata
		}
		return strconv.FormatBool(rulestore.ProbeAIRewriteActive(c.AIConfigID, meta))
	case "dedup":
		// Stage 4.5 (W3): the content fingerprint and claim/release
		// lifecycle, exercised against the REAL dedup package — the same
		// code the delivery engine's W3 preflight consults.
		//
		// "fingerprint" computes PayloadFingerprint over the case's
		// text/caption/media kind/album id and compares it to the Python
		// pipeline's _content_fingerprint byte for byte: a divergence is
		// the two engines disagreeing about which posts are the same post.
		//
		// "claim_release" runs the actual atomic claim, a second claim
		// (which must lose), a release, and a re-claim (which must win)
		// against a real SQLite store, producing the lifecycle string the
		// Python adapter mirrors against the same Core tables.
		switch c.DedupOp {
		case "fingerprint":
			// Python's MessageType defaults to TEXT ("text") when a case
			// omits the field; the Go case field defaults to "". Matching
			// the default keeps the two engines on one key.
			mediaType := c.MediaType
			if mediaType == "" {
				mediaType = "text"
			}
			return dedup.PayloadFingerprint(dedup.FingerprintInput{
				Text:         c.Text,
				Caption:      c.Caption,
				MediaType:    mediaType,
				MediaGroupID: c.MediaGroupID,
			})
		case "claim_release":
			return dedupClaimRelease(c)
		default:
			return ""
		}
	case "filter_engine": // F-11: the reason code IS the decision. Runs the real filter engine
		fr := &rule.FilterRule{}
		for key, value := range c.Filter {
			applyFilterField(fr, key, value)
		}
		// FilterRule.block_links is dead code in Python (no such field), so
		// the Go gate must never fire either — the F-11 alignment.
		fr.BlockLinks = false
		act := filter.New().Evaluate(c.Text, c.MediaType, false, fr)
		return fmt.Sprintf("%v:%s", act.Action, act.Reason)
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
		// The paid-addon entitlement gate is part of this engine's
		// semantics, not an optional extra: Python's
		// apply_content_transforms consults is_addon_enabled before
		// link_buttons or affiliate_kit runs, and a Go side that builds
		// the Config by hand would apply both without the entitlement —
		// a premium-feature bypass the parity harness would hide by
		// construction. Routing the case's metadata through the real
		// rulestore projection makes the harness see what production
		// sees: the gate fails closed.
		//
		// The watermark is not a paid add-on and projects directly.
		cfg.Watermark = strField(c.Metadata, "watermark_text")
		if probed := rulestoreProbeTransform(c.Metadata); probed != nil {
			cfg.LinkButtons = probed.LinkButtons
			cfg.AffiliateKit = probed.AffiliateKit
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
case "keyword":
		// The keyword-reply classifier, called through the REAL rule
		// projection (rule.ForwardRule.KeywordSettings on the case's
		// metadata blob) and not a hand-built reply.Settings — the same
		// projection the delivery engine's W4 block consults, so a
		// divergence between the metadata reader and the classifier is a
		// parity failure here rather than a silent agreement.
		r := &rule.ForwardRule{ID: "r", IsActive: true, Metadata: c.Metadata}
		if out := reply.Classify(c.Text, r.KeywordSettings()); out == "" {
			return "none"
		} else {
			return out
		}
	case "album_window":
		// The album aggregation decision. The per-task window is the
		// INPUT (F-02: dispatcher.py hard-codes 2.0 and never reads the
		// field), so the case pins the decision the field's contract
		// implies: the album limit, the window arithmetic, the defaults.
		s := album.NewSettings(c.Window, album.DefaultMaxActiveAlbums)
		group := album.Group{Items: c.Items, CreatedAt: 0}
		d := s.Evaluate(group, 0)
		completeByLimit := d.Reason == "album_limit" || c.Items >= album.MaxGroupItems
		return fmt.Sprintf("limit=%v deadline_window=%.3f items=%d",
			completeByLimit, s.Window, c.Items)
case "chat_key":
		// _chat_key: the normalisation every routing equality is built on.
		return routing.ChatKey(c.ChatID)
	case "matches_source":
		// ForwardRule.matches_source: the multi-source membership decision,
		// run against the same projection the delivery preflight builds.
		return strconv.FormatBool(routing.MatchesSource(routingCaseRule(c), c.ChatID))
	case "all_targets":
		// ForwardRule.all_targets: every target, primary first, deduped.
		return strings.Join(routing.AllTargets(routingCaseRule(c)), ",")
	case "check_loop":
		// SmartRoutingEngine.check_loop, per target — the decision the
		// delivery preflight consults before any send.
		isLoop, reason := routing.CheckLoop(routing.Input{
			ChatID:        c.ChatID,
			ChatUsername:  c.ChatUsername,
			ForwardOrigin: c.ForwardOrigin,
		}, routingCaseRule(c), c.TargetChatID)
		return strconv.FormatBool(isLoop) + ":" + reason
	case "fen":
		// The forward-every-N cadence arithmetic. Both the clamp (any
		// JSON shape -> a legal interval) and the slot arithmetic run
		// through the real production functions the delivery gate
		// consults, never a hand-built answer — a clamp that disagreed
		// would silently change which messages a task forwards.
		everyN := forwardinterval.Clamp(c.FENValue)
		owns := forwardinterval.OwnsSlot(c.FENSequence, everyN)
		if c.FENSlots {
			return fmt.Sprintf("clamp=%d owns=%t slots=%d", everyN, owns,
				forwardinterval.SlotsUntilNext(c.FENSequence, everyN))
		}
		return fmt.Sprintf("clamp=%d owns=%t", everyN, owns)

	case "effective_settings":
		// The configuration-ownership plane (Stage 5 W2): which value a
		// task actually runs with when a bot carries defaults for it.
		// Runs through the REAL settings registry and (for the
		// store-backed ops) the REAL owner-scoped bot_tokens read, so a
		// divergence is a parity failure rather than two agreed
		// fixtures.
		return runEffectiveSettingsCase(c)

	default:
		return ""
	}
	// F-09: a caption case runs through ApplyText so the pipeline can
	// honor apply_to_caption=False; a body case keeps the plain entry
	// point (which is ApplyText with a zero hint).
	result := engine.ApplyText(c.Text, cfg, nil, cleaning.CaptionHint{IsCaption: c.IsCaption})
	return result.Text
}

// dedupClaimRelease runs the content-claim lifecycle against a REAL dedup
// store (the same Core tables the production pipeline uses), returning the
// win/lose/win string the Python adapter mirrors. One store per case keeps
// cases independent; the fingerprint the case acts on is derived from the
// case's own text so two cases never collide on one (rule, hash).
//
// The lifecycle this pins is the one pipeline.py:781 documents: a claim is
// earned on delivery acceptance and must be released when the message never
// reached a target, or the next legitimate identical post is silently
// dropped.
func dedupClaimRelease(c testCase) string {
	dir, err := os.MkdirTemp("", "parity-dedup")
	if err != nil {
		return "mkdir: " + err.Error()
	}
	defer os.RemoveAll(dir)
	path := dir + "/dedup.db"
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		return "open: " + err.Error()
	}
	if _, err := db.Exec(parityDedupSchema); err != nil {
		db.Close()
		return "schema: " + err.Error()
	}
	db.Close()
	store, err := dedup.NewSQLite(context.Background(), path+"?_busy_timeout=10000")
	if err != nil {
		return "new store: " + err.Error()
	}
	defer store.Close()

	ctx := context.Background()
	ruleID := strField(c.Metadata, "rule_id")
	if ruleID == "" {
		ruleID = "parity-rule"
	}
	hash := dedup.PayloadFingerprint(dedup.FingerprintInput{Text: c.Text, MediaType: "text"})

	var b strings.Builder
	first, err := store.ClaimContentHash(ctx, ruleID, hash)
	if err != nil {
		return "claim1: " + err.Error()
	}
	b.WriteString(boolWord(first))
	second, err := store.ClaimContentHash(ctx, ruleID, hash)
	if err != nil {
		return "claim2: " + err.Error()
	}
	b.WriteString("/" + boolWord(second))
	if err := store.ReleaseContentHash(ctx, ruleID, hash); err != nil {
		return "release: " + err.Error()
	}
	again, err := store.ClaimContentHash(ctx, ruleID, hash)
	if err != nil {
		return "reclaim: " + err.Error()
	}
	b.WriteString("/" + boolWord(again))
	return b.String()
}

// parityDedupSchema is the Core's content_dedup DDL
// (core/infrastructure/persistence/database.py), so the lifecycle runs
// against the real table shape rather than a look-alike.
const parityDedupSchema = `
CREATE TABLE IF NOT EXISTS content_dedup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(rule_id, content_hash)
);
`

func boolWord(b bool) string {
	if b {
		return "true"
	}
	return "false"
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
