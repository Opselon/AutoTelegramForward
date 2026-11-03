package autoreply

import (
	"context"
	"fmt"
	"os"
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
	// dbErrorBackoff is the brief pause after a storage fault: a broken
	// claim is not "no work", but a tight retry loop would hammer the DB.
	dbErrorBackoff = 500 * time.Millisecond
	// retentionSeconds bounds the rolling conversation window's retention
	// sweep (Python's IDLE_POLL/retention contract: keep history bounded).
	retentionSeconds = 86400 // 24h
	// shutdownGrace bounds how long Run waits for in-flight replies during
	// a shutdown drain: a pending reply is a promise, but a stuck provider
	// must not hold a rolling restart hostage (and the job's own lease
	// lets a later claimant finish the work).
	shutdownGrace = 90 * time.Second
	// maxJobGrace bounds one in-flight job that survived Run cancellation:
	// the Python worker's 45s provider ceiling doubled, so the drain is
	// generous without being unbounded.
	maxJobGrace = 90 * time.Second
)

// Permanent error codes that dead-letter immediately instead of retrying.
var permanentErrorCodes = map[string]bool{
	"no_ai_config":     true,
	"send_refused":     true,
	"chat_unavailable": true,
	"auth_error":       true,
	"invalid_config":   true,
	"unauthorized":     true,
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

// maintenanceSweep runs lease expiry recovery + retention cleanup. Runs are
// safe concurrently with claiming: lease recovery is compare-and-set on the
// lease_until column, so an in-flight job is never requeued under a live lease.
func (w *Worker) maintenanceSweep(ctx context.Context) {
	now := time.Now().Unix()
	if _, err := w.store.RecoverExpiredLeases(ctx, now); err != nil {
		// Never let a maintenance fault take the consumer down.
		fmt.Fprintf(os.Stderr, "autoreply: lease recovery failed: %v\n", err)
	}
	if _, err := w.store.CleanupMessages(ctx, int64(retentionSeconds)); err != nil {
		fmt.Fprintf(os.Stderr, "autoreply: message retention failed: %v\n", err)
	}
	if _, err := w.store.CleanupExpiredMemory(ctx, now); err != nil {
		fmt.Fprintf(os.Stderr, "autoreply: memory expiry failed: %v\n", err)
	}
}

// Run is the durable queue consumer. It owns the claim loop, the bounded
// worker pool, per-chat lane ordering, and the maintenance sweep, and it
// exits cleanly on ctx cancellation after DRAINING in-flight jobs — a
// pending reply is a promise the user made to their contact, so an
// in-flight generation completes rather than being aborted by a rolling
// restart (worker.py's shutdown contract).
//
// ownerUserID scopes the claim when non-zero (single-tenant embedding);
// 0 means "claim across every tenant", which the pool deployment uses.
func (w *Worker) Run(ctx context.Context, ownerUserID int64) error {
	sem := make(chan struct{}, w.concurrency)
	var wg sync.WaitGroup

	// Seed the sweep clock to now so the claim loop does not fire a
	// maintenance goroutine on every single iteration: with the zero time,
	// now.Sub(lastSweep) >= interval is trivially true on cycle one and a
	// sweep goroutine is spawned per iteration — a real goroutine leak.
	lastSweep := time.Now()
	sweepInterval := w.maintenanceInterval()

	drainAndWait := func() error {
		// Bounded drain: in-flight work runs on a context that survives
		// cancellation, so Wait() alone could hang a shutdown on a stuck
		// provider. Cap it, then report.
		done := make(chan struct{})
		go func() { wg.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(shutdownGrace):
		}
		return ctx.Err()
	}

	for {
		// Cancelled? Drain in-flight, then exit. Never leave a runnable
		// reply behind, and never keep a goroutine alive past shutdown.
		if ctx.Err() != nil {
			return drainAndWait()
		}

		// Periodic maintenance: lease recovery + retention sweeps. Checked
		// on the claim cycle itself (not a select with `default`) so the
		// sweep cannot be starved by a busy queue.
		if now := time.Now(); now.Sub(lastSweep) >= sweepInterval {
			lastSweep = now // guarded: a sweep can only start once per interval
			wg.Add(1)
			go func() {
				defer wg.Done()
				w.maintenanceSweep(ctx)
			}()
		}

		// Acquire a pool slot. When the pool is saturated, wait for a slot
		// or shutdown — never spin, never queue unbounded goroutines.
		select {
		case sem <- struct{}{}:
		case <-ctx.Done():
			return drainAndWait()
		}

		// Claim one job. ClaimNext is atomic and per-chat ordered, so
		// racing claimants never take the same job.
		now := time.Now().Unix()
		job, err := w.store.ClaimNext(ctx, ownerUserID, w.workerID, now, w.leaseSeconds)
		if err != nil {
			<-sem
			// A storage fault is not "no work": back off briefly and retry.
			select {
			case <-ctx.Done():
				return drainAndWait()
			case <-time.After(dbErrorBackoff):
			}
			continue
		}
		if job == nil {
			<-sem
			// Empty queue: poll at the configured interval.
			select {
			case <-ctx.Done():
				return drainAndWait()
			case <-time.After(w.pollInterval):
			}
			continue
		}

		// Cancelled between the claim and the dispatch? Requeue the job in
		// order (no attempt spent) — it is still a promise, just not ours
		// to keep in this process lifetime.
		if ctx.Err() != nil {
			w.requeueUnclaimed(ctx, job)
			<-sem
			return drainAndWait()
		}

		wg.Add(1)
		go func(j *autoreply.AutoReplyJob) {
			defer wg.Done()
			defer func() { <-sem }()

			// In-flight work survives Run's cancellation: the job's context
			// is detached from the loop's but deadline-bounded, so a stuck
			// provider can hold neither the shutdown nor a slot forever.
			jobCtx, cancelJob := context.WithTimeout(
				context.WithoutCancel(ctx), maxJobGrace)
			defer cancelJob()

			laneKey := fmt.Sprintf("%d:%s", j.OwnerUserID, j.ChatID)
			w.mu.Lock()
			if w.activeLanes[laneKey] {
				w.mu.Unlock()
				// Another goroutine is already servicing this chat. Requeue
				// without spending an attempt; per-chat ordering is
				// preserved because ClaimNext refuses to claim past a
				// waiting job for the same chat.
				w.requeueUnclaimed(jobCtx, j)
				return
			}
			w.activeLanes[laneKey] = true
			w.mu.Unlock()

			defer func() {
				w.mu.Lock()
				delete(w.activeLanes, laneKey)
				w.mu.Unlock()
			}()

			if _, err := w.ProcessJob(jobCtx, j); err != nil {
				fmt.Fprintf(os.Stderr, "autoreply: job %s failed: %v\n", j.ID, err)
			}
		}(job)
	}
}

// requeueUnclaimed releases a lane-conflicted job so the next claim cycle
// can take it in order. It never spends an attempt: the job was never
// processed.
func (w *Worker) requeueUnclaimed(ctx context.Context, j *autoreply.AutoReplyJob) {
	// Release the lease without spending an attempt: a lane conflict means
	// "try again in order", not "this attempt failed".
	if _, err := w.store.MarkRetry(ctx, j.ID, j.LeaseToken, "lane busy; requeued in order", "lane_conflict", 0, j.Attempt); err != nil {
		fmt.Fprintf(os.Stderr, "autoreply: lane requeue failed for %s: %v\n", j.ID, err)
	}
}

func (w *Worker) maintenanceInterval() time.Duration {
	// MAINTENANCE_EVERY_SECONDS from worker.py: 30s.
	return 30 * time.Second
}
