// Package rule carries the small read-only slice of the forward_rules /
// filter_rules tables the Go Worker needs to execute a delivery. The Worker
// never writes these tables — the API and the pipeline own configuration.
package rule

import "time"

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
	BlockLinks        bool
}

// Store is the read-only interface the queue worker uses to re-check a task
// at delivery time (the Python worker does the same before dispatching).
type Store interface {
	// GetByID returns the rule and its attached filter, or nil when the
	// task was deleted while its job sat in the queue.
	GetByID(ruleID string) (*ForwardRule, *FilterRule, error)
}
