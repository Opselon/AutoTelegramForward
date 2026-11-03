// Package rule carries the small read-only slice of the forward_rules /
// filter_rules tables the Go Worker needs to execute a delivery. The Worker
// never writes these tables — the API and the pipeline own configuration.
package rule

import (
	"strconv"
	"strings"
	"time"

	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/reply"
)

// ForwardRule is the execution-relevant projection of a forward_rules row.
//
// Only the fields the *delivery* decision reads are carried. Anything else
// (display metadata, ownership bookkeeping, audit fields) stays in Python's
// own objects; duplicating them here would create a second source of truth
// for the same business state.
type ForwardRule struct {
	ID        string
	SessionID string
	IsActive  bool
	// IsPaused reports whether the owner paused the task. Whether the pause
	// still *holds* is a separate question: Python's ForwardRule.is_currently_paused()
	// reports False once paused_until has passed, so an expired pause must
	// not keep blocking deliveries.
	IsPaused        bool
	PausedUntil     int64
	OwnerUserID     int64
	ForwardMode     string // DIRECT_FORWARD | COPY_MESSAGE | CUSTOM_HEADER_COPY
	ContentMode     string // AUTO | TEXT_ONLY | MEDIA_ONLY | TEXT_AND_MEDIA
	FallbackEnabled bool
	// AutoPause is set by the failure policy when a source/target died.
	AutoPauseReason string

	// --- Stage 3: transport + branding hop -------------------------------- //

	// DeliveryTransport is USER (owner account, the default) or BOT (post
	// through the bot token the owner attached).
	DeliveryTransport string
	// BotTokenID names the stored bot token for DeliveryTransport=BOT. It is
	// only ever an id: the token itself stays in the Python Runtime, which
	// resolves it through the owner-scoped repository.
	BotTokenID string

	// IntermediateChannelID is the branding channel C (numeric id, @username
	// or link), "" when the task does not route through C.
	IntermediateChannelID string
	// UseIntermediate mirrors the dashboard/bot/mobile enable flag; the
	// legacy spellings (VIA_INTERMEDIATE / MASK_ORIGIN) live in
	// IntermediateMode.
	UseIntermediate  bool
	IntermediateMode string
	// MessageCategory is the VIP/REGULAR gate the hop decision consults.
	MessageCategory string
	// DetectionCriteria carries the task's own origin/VIP criteria JSON.
	DetectionCriteria map[string]any
	// CustomCaptionTemplate / SplitLongCaption feed the stage-1 post's
	// caption handling, mirroring core.application.branding_hop.
	CustomCaptionTemplate string
	SplitLongCaption      bool
	// AlbumMode is album | first | split for multi-photo tasks.
	AlbumMode string
	// TopicID is the forum thread (0 = General / none).
	TopicID int64

	// --- Stage 4.5 W1: routing + loop prevention ------------------------- //

	// SourceChatID is the primary source chat the task routes from. Extra
	// sources live in SourceChatIDs (mirrored out of the task's
	// detection_criteria["source_chat_ids"]); the pair mirrors the Python
	// ForwardRule's source_chat_id column / source_chat_ids property,
	// where the column exists but the list lives in the JSON criteria.
	SourceChatID string
	// SourceChatName is the source chat's @username or title: a task can
	// be configured by @username instead of a numeric id, and
	// matches_source compares it too (entities.py:1994).
	SourceChatName string
	// TargetChatID is the primary target (the legacy single-target
	// spelling, also the task's destination_chat_id).
	TargetChatID string
	// TargetChatIDs are the multi-target fan-out targets. The primary is
	// first by construction, so all_targets (primary first, deduped) reads
	// the pair the same way the Python ForwardRule property does.
	TargetChatIDs []string

	// Name is the task's display name, resolved out of the metadata blob
	// the same way the Python ForwardRule's name field is
	// (sqlite_repositories.py: name=str(meta.get("name") or "")). The
	// header placeholder renderer needs it: {rule_name} falls back to
	// "Rule-<id[:8]>" when it is empty, exactly as Python does.
	Name string

	// Transform is the task's text-transform configuration — cleaner, link
	// policy, emoji flag, content-policy rewrite, replacements,
	// header/footer and advanced transforms — resolved out of the metadata
	// blob by the rule store the same way the Python ForwardRule resolves it
	// in _apply_transforms. The delivery engine consumes it when the Worker
	// owns the transform stage (Stage 4.2).
	Transform cleaning.Config

	// AIConfigID names the AI provider configuration the task rewrites
	// through ("" = no AI transform). A real forward_rules column, both
	// backends. The task rewrites at all only when AIRewriteActive says so:
	// a config id alone used to mean "rewrite", which ignored the owner's
	// panel switch and the paid-addon veto (the bug ai_rewrite_active was
	// written to fix — entities.py:1681).
	AIConfigID string
	// AIRewriteActive is ForwardRule.ai_rewrite_active (entities.py:1681):
	// the single authority for "does this task rewrite text with an LLM?".
	// A config id alone means "rewrite with this config" (API clients,
	// backups and the AI menu rely on that), with the paid-addons switch as
	// the explicit veto on top.
	AIRewriteActive bool

	// --- Stage 5 (W4): the AI execution pair ---------------------------- //

	// AISecondaryConfigID is the failover config (a real forward_rules
	// column on both backends, database.py:336): when the primary config's
	// circuit is OPEN, the rewrite falls over to this one. Owned by the
	// same rule owner — a failover to a config the owner does not own
	// would be a privilege escalation, not a recovery.
	AISecondaryConfigID string
	// AIFallbackPolicy is what the delivery does when the rewrite fails
	// (a real column, default DROP — pipeline.py:1817): deliver the
	// original text, drop the message, re-queue it, or quarantine it.
	// QUARANTINE and RETRY are both terminal drops on the Go execution
	// side: the Python pipeline records the stage and returns None either
	// way, and the re-queue decision is the queue's, not the transform's.
	AIFallbackPolicy string
	// AIPromptVersion pins the prompt template version the rewrite used
	// (a real column, default 0 = latest active version). The Worker has
	// no prompt-template service, so the version is carried for
	// attribution only: the prompt pair itself is read off the config
	// row, the same way the Python pipeline does when no prompt_id is
	// configured (pipeline.py:1730).
	AIPromptVersion int
	// AITimeoutSeconds is the per-rewrite network budget (a real column,
	// default 15.0 — entities.py:429).
	AITimeoutSeconds float64

	// --- Stage 5 (W4): the metadata-only prompt tailoring triple ------ //
	// (NEVER a column on either backend; see the store's projection note.)

	// AIPromptID names the prompt-template pair the task configured, a
	// metadata key (pipeline.py:1724).
	AIPromptID string
	// TargetPromptOverrides stores per-target prompt overrides keyed by target chat key.
	TargetPromptOverrides map[string]TargetPrompt
	// AICreativity is the sampling knob (metadata key, default 0.7 —
	// entities.py:1629). Carried on the projection so a parity harness
	// can pin the two engines' read of it; the Worker's provider payload
	// uses the config row's temperature, the field Python's own provider
	// adapter sends.
	AICreativity float64
	// AIMaxTokens is the response budget (metadata key, default 600 —
	// entities.py:1644), the same attribution-only posture.
	AIMaxTokens int

	// BlockLinks mirrors the task-level metadata flag the Python pipeline
	// gates at pipeline.py:957 via media_filter.link_block_reason, which
	// reads ForwardRule.block_links — itself the metadata key
	// `block_links` (entities.py), NOT a column. The persisted
	// filter_rules row has no block_links column in any migration, so this
	// flag is projected at load time from the task's metadata blob by the
	// rule store. It must stay on the task projection: Python applies it
	// whether or not the task has an attached FilterRule, and the delivery
	// preflight must do the same or a task with no filter would forward
	// links its owner blocked.
	BlockLinks bool

	// ForwardEveryN is the task's forward-every-N cadence (Stage 4.5 W2),
	// the projection of ForwardRule.forward_every_n (entities.py:345):
	// 1 means "forward every message", which is also the absence of the
	// feature and what an unconfigured task keeps doing.
	//
	// Like BlockLinks this is a metadata-resolved value, never a column,
	// and the rule store resolves it through the SAME clamp the Python
	// property applies on read — so a hand-edited row or a float from JSON
	// reads identically on both engines. The delivery preflight consumes
	// it to decide whether a job's message owns a slot.
	ForwardEveryN int

	// Metadata is the task's metadata JSON blob. It carries the
	// branding_hop sub-blob and the metadata-resolved transport/shape fields
	// the Core's ForwardRule entity reads out of it, so the worker reads the
	// same blob the Python entity reads.
	Metadata map[string]any
}

// KeywordSettings projects the keyword-reply add-on's inputs out of the
// task's metadata blob, exactly as the Python ForwardRule's own
// accessors resolve them:
//
//   - keyword_confirmation / keyword_reply are metadata keys
//     (entities.py:1787, 1799);
//   - the paid-addon entitlement is is_addon_enabled("keyword_reply")
//     (entities.py:1843);
//   - the word list is ForwardRule.whitelist (entities.py:1467): the
//     content policy's whitelist_words, with the legacy pre-policy
//     metadata["whitelist"] list as the fallback that keeps a task
//     configured before the policy existed on its configured behaviour.
//
// The projection lives on the rule because that is where Python resolves
// it; the delivery engine's keyword-reply block (Stage 4.5, W4) consumes
// it as a reply.Settings and never re-derives any of it.
func (r *ForwardRule) KeywordSettings() reply.Settings {
	if r == nil {
		return reply.Settings{}
	}
	meta := r.Metadata
	if meta == nil {
		meta = map[string]any{}
	}
	return reply.Settings{
		KeywordConfirmation: ruleMetaBool(meta, "keyword_confirmation", false),
		KeywordReply:        ruleMetaStr(meta, "keyword_reply", ""),
		Whitelist:           whitelistWords(meta),
		AddonEnabled:        ruleAddonEnabled(meta, reply.AddonKey),
	}
}

// ruleMetaBool reads a boolean metadata field with the Python entity's
// own semantics (bool(self.metadata.get(key, def))). A value the reader
// cannot interpret falls back to the default rather than silently
// switching the flag off. It duplicates the rulestore helper on purpose:
// the rule package may not import the store (the store depends on the
// rule), so the narrow readers the projection needs stay here.
func ruleMetaBool(meta map[string]any, key string, def bool) bool {
	v, ok := meta[key]
	if !ok {
		return def
	}
	switch b := v.(type) {
	case bool:
		return b
	case float64:
		return b != 0
	case int:
		return b != 0
	case int64:
		return b != 0
	case string:
		switch strings.ToLower(strings.TrimSpace(b)) {
		case "true":
			return true
		case "false":
			return false
		}
	}
	return def
}

// ruleMetaStr reads a string metadata field, mirroring the Python
// accessor str(self.metadata.get(key) or default).
func ruleMetaStr(meta map[string]any, key, def string) string {
	if v, ok := meta[key]; ok {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return def
}

// ruleAddonEnabled is ForwardRule.is_addon_enabled (entities.py:1843):
// the addon must be marked enabled in the persisted metadata's
// paid_addons block. A client-supplied flag alone is never sufficient,
// and a non-dict paid_addons block means no entitlement at all.
func ruleAddonEnabled(meta map[string]any, key string) bool {
	addons, ok := meta["paid_addons"].(map[string]any)
	if !ok {
		return false
	}
	return ruleMetaBool(addons, key, false)
}

// whitelistWords is ForwardRule.whitelist (entities.py:1467): the
// content policy's whitelist_words first, with the legacy pre-policy
// metadata["whitelist"] list as the fallback. A task configured before
// the content policy existed keeps forwarding on the words it configured
// instead of silently forwarding everything.
//
// The policy comes first, exactly as the Python property orders it: a
// non-empty policy list wins outright, and the legacy list is only read
// when the policy carries no words.
func whitelistWords(meta map[string]any) []string {
	if policy, ok := meta["content_policy"].(map[string]any); ok {
		if words := stringListField(policy, "whitelist_words"); len(words) > 0 {
			return words
		}
	}
	if legacy, ok := meta["whitelist"].([]any); ok {
		out := make([]string, 0, len(legacy))
		for _, word := range legacy {
			if s, ok := word.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

// stringListField coerces a JSON-decoded list field into []string,
// dropping entries that are not strings (Python's [str(x) for x in ...]
// coerces them; no parity case ever feeds one, so dropping is the
// conservative read).
func stringListField(meta map[string]any, key string) []string {

	raw, ok := meta[key]
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

// AIFallbackPolicy values the delivery stage's fallback contract speaks
// (value_objects.AIFallbackPolicy, pipeline.py:1817). The strings are the
// Python enum's values: a Go spelling that differed would read the
// column's value as "unknown policy" and fail-closed to DROP — which is
// the same default the Python loader applies to an illegible value.
const (
	FallbackSendOriginal = "SEND_ORIGINAL"
	FallbackDrop         = "DROP"
	FallbackRetry        = "RETRY"
	FallbackQuarantine   = "QUARANTINE"
)

// ResolveAIFallbackPolicy maps the persisted string onto the delivery
// stage's vocabulary (entities.py:500-506): an unknown or empty value is
// DROP, the Python __post_init__ default — the fail-closed posture, so
// a rule saved with a policy the Worker cannot spell never silently
// forwards text its owner expected rewritten.
func ResolveAIFallbackPolicy(raw string) string {
	switch strings.ToUpper(strings.TrimSpace(raw)) {
	case FallbackSendOriginal:
		return FallbackSendOriginal
	case FallbackDrop:
		return FallbackDrop
	case FallbackRetry:
		return FallbackRetry
	case FallbackQuarantine:
		return FallbackQuarantine
	}
	return FallbackDrop
}

// PauseStillActiveAt reports whether an explicit pause was in force at the
// given instant. It mirrors core/domain/entities.py::is_currently_paused():
// a pause whose deadline has elapsed is no longer a pause, so a task does
// not stay blocked forever because its owner paused it for an hour three
// days ago.
func (r *ForwardRule) PauseStillActiveAt(now time.Time) bool {
	if !r.IsPaused {
		return false
	}
	if r.PausedUntil > 0 && now.Unix() >= r.PausedUntil {
		return false
	}
	return true
}

// FilterRule is the execution-relevant projection of a filter_rules row.
// It is the *only* persisted filter the worker consults at delivery time —
// the pipeline's per-task content policies already ran on the ingest side
// and their decision is baked into the job payload.
type FilterRule struct {
	ID                string
	IsEnabled         bool
	WhitelistKeywords []string
	BlacklistKeywords []string
	RegexBlock        []string
	RegexRequire      []string
	RegexPatterns     []string // legacy compatibility
	BlockedMediaTypes []string
	AllowedMediaTypes []string
	DropService       bool
	MinMessageLength  int
	MaxMessageLength  int
	// OwnerUserID is the filter's tenant. The rule store enforces that a task
	// may only be filtered by a filter it owns (owner 0 is a pre-ownership
	// row, kept as the task's own — pipeline.py:930-943). Carried on the
	// projection so a parity harness can assert the boundary.
	OwnerUserID int64
	// BlockLinks on a FilterRule mirrors the task's own flag projected at
	// load time (see ForwardRule.BlockLinks) — it is NOT a filter_rules
	// column: no migration ever added one, and Python's FilterRule entity
	// has no such field (its getattr(filter_rule,"block_links",False) at
	// services.py:79 therefore always reads False). The engine still
	// honours it for the attached-filter path, but the authoritative task
	// gate is the delivery preflight's ForwardRule.BlockLinks check, which
	// fires whether or not a filter is attached — matching the Python
	// pipeline's link_block_reason gate at pipeline.py:957.
	BlockLinks bool
}

// SourceChatIDs is every source chat the task routes from.
//
// Port of the Python ForwardRule.source_chat_ids property
// (entities.py:1873): the extra sources live in
// detection_criteria["source_chat_ids"], and the property falls back to
// the mirrored primary when the list is empty — so a task built the legacy
// way (source_chat_id only) still answers with a one-element list and
// matches_source need not special-case it.
func (r *ForwardRule) SourceChatIDs() []string {
	if r == nil {
		return nil
	}
	raw := criteriaList(r.DetectionCriteria, "source_chat_ids")
	if len(raw) == 0 && r.SourceChatID != "" {
		return []string{r.SourceChatID}
	}
	return raw
}

// criteriaList reads a string list out of the task's detection_criteria
// JSON blob, tolerating every spelling the blob can carry: the list is
// written by Python as a JSON array of strings, but a task saved by an
// older path can hold a single string or a list of numbers, and the
// Python property's str(...) coercion on each entry accepts both.
func criteriaList(criteria map[string]any, key string) []string {
	if criteria == nil {
		return nil
	}
	raw, ok := criteria[key]
	if !ok {
		return nil
	}
	switch v := raw.(type) {
	case []any:
		out := make([]string, 0, len(v))
		for _, item := range v {
			if s := coercionString(item); s != "" {
				out = append(out, s)
			}
		}
		return out
	case string:
		if s := strings.TrimSpace(v); s != "" {
			return []string{s}
		}
	}
	return nil
}

// coercionString stringifies one detection_criteria entry the way the
// Python property's str(...) does: numbers and bools become their decimal
// spelling, and a non-string non-number is skipped (str() would render
// it, but such an entry is not a chat id either engine can match).
func coercionString(value any) string {
	switch v := value.(type) {
	case string:
		return strings.TrimSpace(v)
	case float64:
		// The Python str(int(x)) path: a JSON-decoded -100123.0 is
		// "-100123", matching _chat_key's normalisation of the same value.
		return strconv.FormatInt(int64(v), 10)
	case int:
		return strconv.Itoa(v)
	case int64:
		return strconv.FormatInt(v, 10)
	case bool:
		if v {
			return "true"
		}
		return "false"
	}
	return ""
}

// AllTargets is every target chat id for the task, primary first, deduped.
//
// Port of the Python ForwardRule.all_targets property (entities.py:554).
// The primary (target_chat_id) comes first and the list repeats it, so
// without dedup a task whose primary is also the first list entry would
// deliver to the same target twice.
func (r *ForwardRule) AllTargets() []string {
	if r == nil {
		return nil
	}
	seen := map[string]struct{}{}
	out := make([]string, 0, 1+len(r.TargetChatIDs))
	for _, tid := range append([]string{r.TargetChatID}, r.TargetChatIDs...) {
		if tid == "" {
			continue
		}
		if _, dup := seen[tid]; dup {
			continue
		}
		seen[tid] = struct{}{}
		out = append(out, tid)
	}
	return out
}

// TargetPrompt defines a target-scoped prompt template override.
type TargetPrompt struct {
	PromptID      string
	PromptVersion int
}

// Store is the read-only interface the queue worker uses to re-check a task
// at delivery time (the Python worker does the same before dispatching).
type Store interface {
	// GetByID returns the rule and its attached filter, or nil when the
	// task was deleted while its job sat in the queue.
	GetByID(ruleID string) (*ForwardRule, *FilterRule, error)
}
