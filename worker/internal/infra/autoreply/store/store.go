// Package store provides SQLite and PostgreSQL persistence for the auto-reply aggregate.
//
// Invariant: owner_user_id scopes EVERY query and mutation. Cross-tenant leakage
// is impossible at the SQL level.
package store

import (
	"context"
	"errors"

	"atf/worker/internal/domain/autoreply"
)

var (
	ErrNotFound       = errors.New("autoreply: row not found")
	ErrOwnerRequired  = errors.New("autoreply: owner_user_id is required and must be positive")
	ErrNameRequired   = errors.New("autoreply: name is required")
	ErrNameTaken      = errors.New("autoreply: another prompt already uses that name")
	ErrChatIDRequired = errors.New("autoreply: chat_id is required")
)

func clampFloat(val, low, high, def float64) float64 {
	if val != val {
		return def
	}
	if val < low {
		return low
	}
	if val > high {
		return high
	}
	return val
}

type Store interface {
	// Settings
	GetSettings(ctx context.Context, ownerUserID int64) (*autoreply.AutoReplySettings, error)
	SaveSettings(ctx context.Context, settings *autoreply.AutoReplySettings) (*autoreply.AutoReplySettings, error)

	// Prompts
	GetPrompt(ctx context.Context, promptID string, ownerUserID int64) (*autoreply.AutoReplyPrompt, error)
	ListPrompts(ctx context.Context, ownerUserID int64, includeArchived bool, search, category string) ([]*autoreply.AutoReplyPrompt, error)
	SavePrompt(ctx context.Context, prompt *autoreply.AutoReplyPrompt) (*autoreply.AutoReplyPrompt, error)
	DuplicatePrompt(ctx context.Context, promptID string, ownerUserID int64, name string) (*autoreply.AutoReplyPrompt, error)
	DeletePrompt(ctx context.Context, promptID string, ownerUserID int64) (bool, error)
	CountPrompts(ctx context.Context, ownerUserID int64) (int, error)
	TouchPromptUsage(ctx context.Context, promptID string, ownerUserID int64) error

	// Targets
	GetTargetByChat(ctx context.Context, chatID string, ownerUserID int64) (*autoreply.ContactTarget, error)
	ListTargets(ctx context.Context, ownerUserID int64, search string, enabledOnly bool) ([]*autoreply.ContactTarget, error)
	UpsertTarget(ctx context.Context, target *autoreply.ContactTarget) (*autoreply.ContactTarget, error)
	SetTargetEnabled(ctx context.Context, chatID string, ownerUserID int64, enabled bool) (bool, error)
	DeleteTarget(ctx context.Context, chatID string, ownerUserID int64) (bool, error)
	BulkSetTargets(ctx context.Context, ownerUserID int64, targets []*autoreply.ContactTarget, replace bool) ([]*autoreply.ContactTarget, error)

	// Rules
	GetRule(ctx context.Context, ruleID string, ownerUserID int64) (*autoreply.AutoReplyRule, error)
	ListRules(ctx context.Context, ownerUserID int64) ([]*autoreply.AutoReplyRule, error)
	SaveRule(ctx context.Context, rule *autoreply.AutoReplyRule) (*autoreply.AutoReplyRule, error)
	DeleteRule(ctx context.Context, ruleID string, ownerUserID int64) (bool, error)

	// Context Messages
	AppendMessage(ctx context.Context, ownerUserID int64, chatID, role, content string, messageID int64) error
	ListMessages(ctx context.Context, ownerUserID int64, chatID string, limit int) ([]autoreply.MessageHistory, error)
	CountMessages(ctx context.Context, ownerUserID int64, chatID string) (int, error)
	ClearMessages(ctx context.Context, ownerUserID int64, chatID string) (int, error)
	CleanupMessages(ctx context.Context, maxAgeSeconds int64) (int, error)

	// Memory Facts
	Remember(ctx context.Context, ownerUserID int64, chatID, content string, kind autoreply.MemoryKind, ttlSeconds int64) (string, error)
	ListMemory(ctx context.Context, ownerUserID int64, chatID string, limit int) ([]autoreply.MemoryFact, error)
	Forget(ctx context.Context, ownerUserID int64, chatID, memoryID string) (int, error)
	CleanupExpiredMemory(ctx context.Context, now int64) (int, error)

	// Durable Queue Jobs
	Enqueue(ctx context.Context, job *autoreply.AutoReplyJob) (bool, *autoreply.AutoReplyJob, error)
	ClaimNext(ctx context.Context, ownerUserID int64, workerID string, now int64, leaseSeconds int) (*autoreply.AutoReplyJob, error)
	CompleteSent(ctx context.Context, jobID, leaseToken string, sentMessageID int64, replyText string, latencyMS int) (bool, error)
	MarkRetry(ctx context.Context, jobID, leaseToken, errStr, errCode string, backoffSeconds float64, attempt int) (bool, error)
	MarkFailed(ctx context.Context, jobID, leaseToken, errStr, errCode string, deadLetter bool) (bool, error)
	MarkSkipped(ctx context.Context, jobID, reason string) (bool, error)
	Cancel(ctx context.Context, jobID string, ownerUserID int64) (bool, error)
	Replay(ctx context.Context, jobID string, ownerUserID int64) (bool, error)
	RecoverExpiredLeases(ctx context.Context, now int64) (int, error)
	ListJobs(ctx context.Context, ownerUserID int64, status, chatID string, limit, offset int) ([]*autoreply.AutoReplyJob, int, error)
	ListDeadLetter(ctx context.Context, ownerUserID int64, limit, offset int) ([]*autoreply.AutoReplyJob, int, error)
	GetJob(ctx context.Context, jobID string, ownerUserID int64) (*autoreply.AutoReplyJob, error)
	PendingCounts(ctx context.Context, ownerUserID int64) (map[string]int, error)
	RecentSends(ctx context.Context, ownerUserID int64, chatID string, sinceTS int64) (int, error)
	LastSentAt(ctx context.Context, ownerUserID int64, chatID string) (int64, error)
	CleanupTerminal(ctx context.Context, retentionSeconds int64) (int, error)

	Close() error
}
