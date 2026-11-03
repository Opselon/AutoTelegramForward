// Package rule carries the small read-only slice of the forward_rules /
// filter_rules tables the Go Worker needs to execute a delivery. The Worker
// never writes these tables — the API and the pipeline own configuration.
package rule

import "time"

import "atf/worker/internal/domain/cleaning"

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

	// Metadata is the task's metadata JSON blob. It carries the
	// branding_hop sub-blob and the metadata-resolved transport/shape fields
	// the Core's ForwardRule entity reads out of it, so the worker reads the
	// same blob the Python entity reads.
	Metadata map[string]any
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
