// Package rule carries the small read-only slice of the forward_rules /
// filter_rules tables the Go Worker needs to execute a delivery. The Worker
// never writes these tables — the API and the pipeline own configuration.
package rule

// ForwardRule is the execution-relevant projection of a forward_rules row.
//
// Only the fields the *delivery* decision reads are carried. Anything else
// (display metadata, ownership bookkeeping, audit fields) stays in Python's
// own objects; duplicating them here would create a second source of truth
// for the same business state.
type ForwardRule struct {
	ID             string
	SessionID      string
	IsActive       bool
	IsPaused       bool
	PausedUntil    int64
	OwnerUserID    int64
	ForwardMode    string // DIRECT_FORWARD | COPY_MESSAGE | CUSTOM_HEADER_COPY
	ContentMode    string // AUTO | TEXT_ONLY | MEDIA_ONLY | TEXT_AND_MEDIA
	FallbackEnabled bool
	// AutoPause is set by the failure policy when a source/target died.
	AutoPauseReason string
}

// FilterRule is the execution-relevant projection of a filter_rules row.
// It is the *only* persisted filter the worker consults at delivery time —
// the pipeline's per-task content policies already ran on the ingest side
// and their decision is baked into the job payload.
type FilterRule struct {
	ID                 string
	IsEnabled          bool
	WhitelistKeywords  []string
	BlacklistKeywords  []string
	RegexBlock         []string
	RegexRequire       []string
	RegexPatterns      []string // legacy compatibility
	BlockedMediaTypes  []string
	AllowedMediaTypes  []string
	DropService        bool
	MinMessageLength   int
	MaxMessageLength   int
	BlockLinks         bool
}

// Store is the read-only interface the queue worker uses to re-check a task
// at delivery time (the Python worker does the same before dispatching).
type Store interface {
	// GetByID returns the rule and its attached filter, or nil when the
	// task was deleted while its job sat in the queue.
	GetByID(ruleID string) (*ForwardRule, *FilterRule, error)
}
