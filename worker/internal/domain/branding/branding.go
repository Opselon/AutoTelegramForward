// Package branding is the Go Worker's port of the Python branding-hop
// decision: whether one message should take the A -> C -> B hop.
//
// This is a *port of a decision*, not a re-imagining of it. The Python
// reference implementation lives in core/domain/branding.py
// (branding.should_hop) and core/domain/services.py
// (RoutingService.should_brand_hop, vip_criteria_configured, is_vip_origin);
// every rule the Python decision applies must hold here too, because the two
// engines execute the same queue and an operator must not see a different
// route depending on which engine picked the job up.
//
// Parity is enforced, not asserted: worker/internal/parity holds shared JSON
// fixtures that exercise the REAL Python implementation and this one, and CI
// regenerates the committed reference so a Python behavior change nobody
// re-referenced fails the build.
//
// Rules ported verbatim from the Python reference:
//
//   - no channel C configured -> never hop ("branding_disabled:no_channel")
//   - the hop flag must be on, either the first-class use_intermediate
//     toggle or a legacy intermediate_mode spelling
//     (VIA_INTERMEDIATE / MASK_ORIGIN) -> otherwise
//     "branding_disabled:flag_off"
//   - with triggers disabled, a task that configured VIP criteria hops only
//     for a message that matched them; without criteria the hop is
//     unconditional ("branding_always")
//   - require_forward is a GATE, not a check: it refuses a message that is
//     not itself a forward, whatever match_mode says
//   - origin ids match the forward-origin chat id or its @username;
//     origin titles match the forward-origin channel title, case-insensitively
//     in BOTH directions (the Python reference tests `wanted in title or
//     title in wanted`)
//   - keywords match the message text through the Persian/Arabic text fold
//     (the same fold the content engine applies)
//   - match_mode "all" requires every configured condition;
//     "any" (default) requires at least one
//   - triggers enabled but nothing configured -> hop
//     ("branding_no_triggers_configured")
package branding

import (
	"strings"
)

// MatchMode is the trigger-combination semantics.
type MatchMode string

const (
	ModeAny MatchMode = "any"
	ModeAll MatchMode = "all"
)

// legacy hop-mode spellings older rules carry instead of use_intermediate.
var legacyHopModes = map[string]struct{}{
	"VIA_INTERMEDIATE": {},
	"MASK_ORIGIN":      {},
}

// Config is the normalised branding_hop configuration carried in the rule's
// metadata blob. Every field degrades to its pre-feature default, exactly as
// Python's read_config does.
type Config struct {
	TriggersEnabled bool
	OriginIDs       []string
	OriginTitles    []string
	Keywords        []string
	MatchMode       MatchMode
	RequireForward  bool
	ForceForward    bool
}

// DefaultConfig is the Python DEFAULTS dict: no triggers (unconditional
// hop), force_forward on, any-match semantics.
func DefaultConfig() Config {
	return Config{
		MatchMode:    ModeAny,
		ForceForward: true,
	}
}

// metaKey is the metadata blob key the Python side reads.
const metaKey = "branding_hop"

// BrandingKey is the metadata key that identifies a rule's branding config.
func BrandingKey() string { return metaKey }

// ReadConfig reads the branding configuration out of a rule's metadata blob.
//
// The blob is the same map the Python read_config normalises: unknown keys
// are ignored, list keys are coerced to de-duplicated non-empty string
// lists, booleans are coerced, and match_mode falls back to "any" when it
// is anything other than "all"/"any".
func ReadConfig(metadata map[string]any) Config {
	cfg := DefaultConfig()
	raw := metadataBlob(metadata, metaKey)
	if raw != nil {
		for key, value := range raw {
			switch key {
			case "triggers_enabled":
				cfg.TriggersEnabled = asBool(value)
			case "require_forward":
				cfg.RequireForward = asBool(value)
			case "force_forward":
				cfg.ForceForward = asBool(value)
			case "origin_ids":
				cfg.OriginIDs = asStringList(value)
			case "origin_titles":
				cfg.OriginTitles = asStringList(value)
			case "keywords":
				cfg.Keywords = asStringList(value)
			case "match_mode":
				cfg.MatchMode = asMatchMode(value)
			}
		}
	}
	if cfg.MatchMode != ModeAll {
		cfg.MatchMode = ModeAny
	}
	return cfg
}

// Decision is the answer to "should this message take the hop?".
type Decision struct {
	TakeHop bool
	Reason  string
}

// MessageView is the message-side input the decision reads: the text (the
// effective text the transforms produced) and the forward-origin channel.
//
// It deliberately mirrors the Python payload attributes should_hop consults:
// effective_text, text, raw_text, caption (in that preference order) and
// forward_origin's from_chat_id / from_chat_username / from_chat_title.
type MessageView struct {
	Text          string
	ForwardOrigin *ForwardOrigin
}

// ForwardOrigin is the "Forwarded from <channel>" header of a message.
type ForwardOrigin struct {
	FromChatID       string
	FromChatUsername string
	FromChatTitle    string
}

// RuleView is the task-side input: the branding channel, the enable flag(s)
// and the VIP criteria the hop decision consults.
type RuleView struct {
	// IntermediateChannelID is channel C (numeric id, @username or link).
	IntermediateChannelID string
	// UseIntermediate is the dashboard/bot/mobile enable flag.
	UseIntermediate bool
	// IntermediateMode carries the legacy spellings
	// (VIA_INTERMEDIATE / MASK_ORIGIN).
	IntermediateMode string
	// MessageCategory is the VIP / REGULAR gate.
	MessageCategory string
	// DetectionCriteria is the task's own origin/VIP criteria blob.
	DetectionCriteria map[string]any
	// Metadata carries the branding_hop sub-blob.
	Metadata map[string]any
}

// Enabled reports whether this task routes through C at all: an enable flag
// AND a channel, in either spelling the product has written.
func Enabled(r RuleView) bool {
	if strings.TrimSpace(r.IntermediateChannelID) == "" {
		return false
	}
	if r.UseIntermediate {
		return true
	}
	mode := strings.ToUpper(strings.TrimSpace(r.IntermediateMode))
	_, ok := legacyHopModes[mode]
	return ok
}

// VIPOutcome is the (matched, reason) pair Python's is_vip_origin returns.
type VIPOutcome struct {
	Matched bool
	Reason  string
}

// ShouldHop decides whether one message takes the A -> C -> B hop.
//
// The signature mirrors Python's should_hop(payload, rule, vip,
// vip_configured): the caller resolves the task's VIP criteria itself
// (VIPOf) and passes the result in, so the decision has exactly one
// implementation of every rule.
func ShouldHop(msg MessageView, r RuleView, vip VIPOutcome, vipConfigured bool) Decision {
	if strings.TrimSpace(r.IntermediateChannelID) == "" {
		return Decision{false, "branding_disabled:no_channel"}
	}
	if !Enabled(r) {
		return Decision{false, "branding_disabled:flag_off"}
	}

	cfg := ReadConfig(r.Metadata)
	if !cfg.TriggersEnabled {
		// Legacy behaviour, preserved: a task with VIP criteria hops only for
		// a message that matched them; without criteria the hop is
		// unconditional.
		if vipConfigured {
			if vip.Matched {
				return Decision{true, "branding_vip_matched:" + vip.Reason}
			}
			reason := vip.Reason
			if reason == "" {
				reason = "no_match"
			}
			return Decision{false, "branding_vip_unmatched:" + reason}
		}
		return Decision{true, "branding_always"}
	}

	// A "require" is a gate and must hold on its own, whatever match_mode
	// says.
	if cfg.RequireForward && msg.ForwardOrigin == nil {
		return Decision{false, "branding_requires_forward_origin"}
	}

	var conditions, matched []string
	if len(cfg.OriginIDs) > 0 || len(cfg.OriginTitles) > 0 {
		conditions = append(conditions, "origin")
		if matchOrigin(msg.ForwardOrigin, cfg.OriginIDs, cfg.OriginTitles) {
			matched = append(matched, "origin")
		}
	}
	if len(cfg.Keywords) > 0 {
		conditions = append(conditions, "keywords")
		if matchKeywords(msg.Text, cfg.Keywords) {
			matched = append(matched, "keywords")
		}
	}
	if vipConfigured {
		conditions = append(conditions, "vip")
		if vip.Matched {
			matched = append(matched, "vip")
		}
	}

	if len(conditions) == 0 {
		// Triggers were switched on but nothing was configured to test.
		return Decision{true, "branding_no_triggers_configured"}
	}

	satisfied := len(matched) == len(conditions)
	if cfg.MatchMode == ModeAny {
		satisfied = len(matched) > 0
	}
	if satisfied {
		return Decision{true, "branding_triggers_matched:" + strings.Join(matched, ",")}
	}
	detail := strings.Join(matched, ",")
	if detail == "" {
		detail = "none"
	}
	return Decision{false, "branding_triggers_unmatched:" + detail}
}

// VIPConfigured reports whether a task declared ANY VIP/origin criteria of
// its own. Mirrors Python's RoutingService.vip_criteria_configured: the
// detection-criteria keys, or a message_category of VIP/VIP_ONLY.
func VIPConfigured(r RuleView) bool {
	criteria := r.DetectionCriteria
	for _, key := range []string{
		"forward_origin_chat_ids", "origin_chat_ids",
		"forward_origin_titles", "origin_titles",
		"text_contains", "keywords", "regex_pattern", "text_regex",
	} {
		if len(criteriaSlice(criteria, key)) > 0 {
			return true
		}
		if s, ok := criteria[key].(string); ok && s != "" {
			return true
		}
	}
	if v, ok := criteria["require_forward_origin"]; ok && asBool(v) {
		return true
	}
	cat := strings.ToUpper(strings.TrimSpace(r.MessageCategory))
	return cat == "VIP" || cat == "VIP_ONLY"
}

// VIPOf evaluates the task's own VIP criteria against a message, mirroring
// Python's RoutingService.is_vip_origin: origin ids/titles/username, text
// keywords and a regex, combined per match_mode (ALL requires every
// configured condition, ANY at least one). With no explicit criteria at
// all, any verified forward is a VIP.
func VIPOf(msg MessageView, r RuleView) VIPOutcome {
	criteria := r.DetectionCriteria
	mode := strings.ToUpper(asString(criteria["match_mode"], "ANY"))

	originIDs := criteriaSlice(criteria, "forward_origin_chat_ids")
	if len(originIDs) == 0 {
		originIDs = criteriaSlice(criteria, "origin_chat_ids")
	}
	originTitles := criteriaSlice(criteria, "forward_origin_titles")
	if len(originTitles) == 0 {
		originTitles = criteriaSlice(criteria, "origin_titles")
	}
	textKeywords := criteriaSlice(criteria, "text_contains")
	if len(textKeywords) == 0 {
		textKeywords = criteriaSlice(criteria, "keywords")
	}
	regexPattern := asString(criteria["regex_pattern"], "")
	if regexPattern == "" {
		regexPattern = asString(criteria["text_regex"], "")
	}

	checked := 0
	var matched []string

	if len(originIDs) > 0 || len(originTitles) > 0 || asBool(criteria["require_forward_origin"]) {
		checked++
		if msg.ForwardOrigin == nil {
			if mode == "ALL" {
				return VIPOutcome{false, "UNKNOWN_ORIGIN:missing_forward_metadata"}
			}
		} else {
			if matchOrigin(msg.ForwardOrigin, originIDs, originTitles) {
				matched = append(matched, "origin_matched")
			} else if mode == "ALL" {
				return VIPOutcome{false, "origin_unmatched"}
			}
		}
	}

	if len(textKeywords) > 0 {
		checked++
		folded := strings.ToLower(msg.Text)
		hit := false
		for _, kw := range textKeywords {
			if strings.Contains(folded, strings.ToLower(kw)) {
				hit = true
				break
			}
		}
		if hit {
			matched = append(matched, "text_contains_matched")
		} else if mode == "ALL" {
			return VIPOutcome{false, "text_contains_unmatched"}
		}
	}

	if regexPattern != "" {
		checked++
		if matchRegex(msg.Text, regexPattern) {
			matched = append(matched, "regex_matched")
		} else if mode == "ALL" {
			return VIPOutcome{false, "regex_unmatched"}
		}
	}

	if checked == 0 {
		// No explicit criteria: any verified forward is a VIP.
		if msg.ForwardOrigin != nil && msg.ForwardOrigin.FromChatID != "" {
			return VIPOutcome{true, "forward_chat_id_match:" + msg.ForwardOrigin.FromChatID}
		}
		return VIPOutcome{false, "UNKNOWN_ORIGIN"}
	}

	if mode == "ALL" {
		if len(matched) == checked {
			return VIPOutcome{true, "all_matched:" + strings.Join(matched, ",")}
		}
		return VIPOutcome{false, "all_criteria_not_satisfied"}
	}
	if len(matched) > 0 {
		return VIPOutcome{true, "any_matched:" + strings.Join(matched, ",")}
	}
	return VIPOutcome{false, "no_criteria_matched"}
}

// matchOrigin matches the forward-origin channel against the configured ids
// and titles. Ids match the origin chat id or its @username (case-folded);
// titles match the channel title case-insensitively in BOTH directions — the
// Python reference tests `wanted in title or title in wanted`, so a
// configured prefix or suffix of the real title also matches.
func matchOrigin(origin *ForwardOrigin, ids, titles []string) bool {
	if origin == nil {
		return false
	}
	fwdID := strings.TrimSpace(origin.FromChatID)
	fwdUsername := strings.TrimPrefix(strings.ToLower(strings.TrimSpace(origin.FromChatUsername)), "@")
	fwdTitle := strings.ToLower(strings.TrimSpace(origin.FromChatTitle))

	for _, wanted := range ids {
		w := strings.TrimSpace(wanted)
		if w == "" {
			continue
		}
		if fwdID == w {
			return true
		}
		if fwdUsername != "" && fwdUsername == strings.TrimPrefix(strings.ToLower(w), "@") {
			return true
		}
	}
	for _, wanted := range titles {
		w := strings.ToLower(strings.TrimSpace(wanted))
		if w == "" {
			continue
		}
		if strings.Contains(fwdTitle, w) || strings.Contains(w, fwdTitle) {
			return true
		}
	}
	return false
}

// matchKeywords reports whether any keyword appears in the text, after the
// Persian/Arabic fold the Python content engine applies.
//
// The fold is the one non-trivial piece of the decision, and it is ported
// character-for-character from core/domain/content_policy.py: NFC
// normalization, stripping harakat/tatweel/zero-width/bidi controls,
// unifying Arabic yeh/kaf/alef variants, then case-folding.
func matchKeywords(text string, keywords []string) bool {
	if strings.TrimSpace(text) == "" {
		return false
	}
	folded := foldText(text)
	for _, word := range keywords {
		needle := strings.TrimSpace(word)
		if needle == "" {
			continue
		}
		if strings.Contains(folded, foldText(needle)) {
			return true
		}
	}
	return false
}
