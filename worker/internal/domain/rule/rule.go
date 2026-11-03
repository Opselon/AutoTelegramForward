// Package rule carries the small read-only slice of the forward_rules /
// filter_rules tables the Go Worker needs to execute a delivery. The Worker
// never writes these tables — the API and the pipeline own configuration.
package rule

import (
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

// Store is the read-only interface the queue worker uses to re-check a task
// at delivery time (the Python worker does the same before dispatching).
type Store interface {
	// GetByID returns the rule and its attached filter, or nil when the
	// task was deleted while its job sat in the queue.
	GetByID(ruleID string) (*ForwardRule, *FilterRule, error)
}
