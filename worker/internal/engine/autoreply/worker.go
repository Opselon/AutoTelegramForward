package autoreply

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"atf/worker/internal/domain/aiconfig"
	"atf/worker/internal/domain/autoreply"
	"atf/worker/internal/infra/aiprovider"
	"atf/worker/internal/infra/autoreply/store"
	"atf/worker/internal/telegraph"
)

// Default lease duration and backoff parameters mirroring Python worker.py
const (
	DefaultLeaseSeconds = 45
	DefaultPollInterval = 1500 * time.Millisecond
	MaxRecoveries       = 4
)

// Permanent error codes that dead-letter immediately instead of retrying.
var permanentErrorCodes = map[string]bool{
	"no_ai_config":      true,
	"send_refused":      true,
	"chat_unavailable":  true,
	"auth_error":        true,
	"invalid_config":    true,
	"unauthorized":      true,
}

// Sender delivers replies to Telegram. Defaults to telegraph.Gateway.
type Sender interface {
	SendMessage(ctx context.Context, sessionID string, targetChatID int64, text string, threadID int64) (int64, *telegraph.DeliveryFailure)
}

// Worker is the durable queue consumer for AI auto-reply.
type Worker struct {
	store           store.Store
	aiStore         aiconfig.Store
	providerFactory *aiprovider.Factory
	sender          Sender
	workerID        string
	concurrency     int
	pollInterval    time.Duration
	leaseSeconds    int
	mu              sync.Mutex
	activeLanes     map[string]bool // per-chat lane guard
}

// Options configures the AutoReply worker.
type Options struct {
	Concurrency  int
	PollInterval time.Duration
	LeaseSeconds int
}

// NewWorker constructs an AutoReply worker.
func NewWorker(
	st store.Store,
	aiSt aiconfig.Store,
	pf *aiprovider.Factory,
	snd Sender,
	workerID string,
	opts Options,
) *Worker {
	c := opts.Concurrency
	if c <= 0 {
		c = 3
	}
	poll := opts.PollInterval
	if poll <= 0 {
		poll = DefaultPollInterval
	}
	lease := opts.LeaseSeconds
	if lease <= 0 {
		lease = DefaultLeaseSeconds
	}
	return &Worker{
		store:           st,
		aiStore:         aiSt,
		providerFactory: pf,
		sender:          snd,
		workerID:        workerID,
		concurrency:     c,
		pollInterval:    poll,
		leaseSeconds:    lease,
		activeLanes:     make(map[string]bool),
	}
}

// ProcessJob generates and sends an auto-reply for one claimed job.
func (w *Worker) ProcessJob(ctx context.Context, job *autoreply.AutoReplyJob) (bool, error) {
	if job == nil {
		return false, nil
	}
	start := time.Now()

	// 1. Resolve Settings
	settings, err := w.store.GetSettings(ctx, job.OwnerUserID)
	if err != nil {
		return false, w.failOrRetry(ctx, job, fmt.Sprintf("get settings: %v", err), "db_error", false)
	}
	if settings == nil || !settings.Enabled {
		w.store.MarkSkipped(ctx, job.ID, "settings disabled")
		return false, nil
	}

	// 2. Resolve Prompt
	promptID := job.PromptID
	if promptID == "" {
		promptID = settings.PromptID
	}
	var prompt *autoreply.AutoReplyPrompt
	if promptID != "" {
		prompt, _ = w.store.GetPrompt(ctx, promptID, job.OwnerUserID)
	}

	// 3. Resolve AI Config
	aiCfgID := job.AIConfigID
	if aiCfgID == "" {
		aiCfgID = settings.AIConfigID
	}
	if aiCfgID == "" {
		return false, w.failOrRetry(ctx, job, "no AI config configured", "no_ai_config", true)
	}
	cfg, err := w.aiStore.GetOwned(ctx, aiCfgID, job.OwnerUserID)
	if err != nil || cfg == nil || !cfg.IsEnabled {
		return false, w.failOrRetry(ctx, job, fmt.Sprintf("AI config %s unavailable or not owned", aiCfgID), "invalid_config", true)
	}

	// 4. Build Context Messages
	historyRows, _ := w.store.ListMessages(ctx, job.OwnerUserID, job.ChatID, settings.ContextMessages)
	facts, _ := w.store.ListMemory(ctx, job.OwnerUserID, job.ChatID, settings.MemoryMaxFacts)

	systemPrompt := "You are a helpful assistant."
	if prompt != nil && prompt.SystemPrompt != "" {
		systemPrompt = prompt.SystemPrompt
	} else if settings.ExtraInstructions != "" {
		systemPrompt += " " + settings.ExtraInstructions
	}

	// Add memory facts into system prompt if available
	if len(facts) > 0 {
		var factTexts []string
		for _, f := range facts {
			factTexts = append(factTexts, "- "+f.Content)
		}
		systemPrompt += "\nKnown facts about user:\n" + strings.Join(factTexts, "\n")
	}

	var chatMsgs []aiprovider.Message
	chatMsgs = append(chatMsgs, aiprovider.Message{
		Role:    "system",
		Content: autoreply.RedactForPrompt(systemPrompt),
	})

	for _, m := range historyRows {
		role := m.Role
		if role != "user" && role != "assistant" {
			role = "user"
		}
		chatMsgs = append(chatMsgs, aiprovider.Message{
			Role:    role,
			Content: autoreply.RedactForPrompt(m.Content),
		})
	}

	// Append the inbound text
	inboundText := autoreply.RedactForPrompt(job.InboundText)
	if prompt != nil && prompt.UserPromptTemplate != "" && prompt.UserPromptTemplate != "{text}" {
		inboundText = strings.ReplaceAll(prompt.UserPromptTemplate, "{text}", inboundText)
	}
	chatMsgs = append(chatMsgs, aiprovider.Message{
		Role:    "user",
		Content: inboundText,
	})

	// 5. Call Provider ChatComplete
	provider, err := w.providerFactory.Get(cfg)
	if err != nil {
		return false, w.failOrRetry(ctx, job, fmt.Sprintf("provider init: %v", err), "provider_error", false)
	}

	temp := cfg.Temperature
	if temp <= 0 {
		temp = settings.Temperature
	}
	replyText, err := provider.ChatComplete(ctx, cfg, chatMsgs, temp)
	if err != nil {
		return false, w.failOrRetry(ctx, job, fmt.Sprintf("chat completion: %v", err), "provider_error", false)
	}
	replyText = strings.TrimSpace(replyText)
	if replyText == "" {
		if settings.FallbackText != "" {
			replyText = settings.FallbackText
		} else {
			return false, w.failOrRetry(ctx, job, "empty reply from provider", "empty_output", false)
		}
	}

	// Enforce max reply chars
	if settings.MaxReplyChars > 0 && len(replyText) > settings.MaxReplyChars {
		replyText = replyText[:settings.MaxReplyChars]
	}

	// 6. Deliver reply via Telegram Sender
	targetChat, _ := strconv.ParseInt(job.ChatID, 10, 64)
	sentID, fail := w.sender.SendMessage(ctx, job.SessionID, targetChat, replyText, 0)
	if fail != nil {
		code := fail.Code
		deadLetter := !fail.IsTransient()
		return false, w.failOrRetry(ctx, job, fail.ErrorType, code, deadLetter)
	}

	latencyMS := int(time.Since(start).Milliseconds())

	// 7. Complete job in store & append to history
	ok, err := w.store.CompleteSent(ctx, job.ID, job.LeaseToken, sentID, replyText, latencyMS)
	if err != nil {
		return false, fmt.Errorf("complete job: %w", err)
	}

	// Append assistant response to message history
	_ = w.store.AppendMessage(ctx, job.OwnerUserID, job.ChatID, "assistant", replyText, sentID)

	// Touch prompt usage if prompt was used
	if prompt != nil {
		_ = w.store.TouchPromptUsage(ctx, prompt.ID, job.OwnerUserID)
	}

	return ok, nil
}

func (w *Worker) failOrRetry(ctx context.Context, job *autoreply.AutoReplyJob, errMsg, errCode string, deadLetter bool) error {
	if deadLetter || permanentErrorCodes[errCode] || job.Attempt >= job.MaxAttempts {
		_, err := w.store.MarkFailed(ctx, job.ID, job.LeaseToken, errMsg, errCode, true)
		return err
	}
	backoff := 5.0 * float64(job.Attempt+1)
	_, err := w.store.MarkRetry(ctx, job.ID, job.LeaseToken, errMsg, errCode, backoff, job.Attempt+1)
	return err
}

// Step claims and processes up to one job for the given owner.
func (w *Worker) Step(ctx context.Context, ownerUserID int64) (bool, error) {
	now := time.Now().Unix()
	job, err := w.store.ClaimNext(ctx, ownerUserID, w.workerID, now, w.leaseSeconds)
	if err != nil || job == nil {
		return false, err
	}

	laneKey := fmt.Sprintf("%d:%s", job.OwnerUserID, job.ChatID)
	w.mu.Lock()
	if w.activeLanes[laneKey] {
		w.mu.Unlock()
		return false, nil // another worker goroutine is servicing this chat
	}
	w.activeLanes[laneKey] = true
	w.mu.Unlock()

	defer func() {
		w.mu.Lock()
		delete(w.activeLanes, laneKey)
		w.mu.Unlock()
	}()

	return w.ProcessJob(ctx, job)
}

// RecoverLeases triggers the maintenance sweep across all expired jobs.
func (w *Worker) RecoverLeases(ctx context.Context) (int, error) {
	return w.store.RecoverExpiredLeases(ctx, time.Now().Unix())
}
