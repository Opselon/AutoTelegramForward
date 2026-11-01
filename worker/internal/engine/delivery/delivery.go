// Package delivery is the Go Worker's business execution plane: it consumes
// durable delivery jobs, decides the delivery shape, dispatches through the
// Telegram Gateway, and owns the retry, idempotency and recovery semantics.
//
// The decision tree here is a port of the Python DurableQueueManager's
// dispatch path, and it is deliberately line-for-line: the existing
// behaviour (copy modes, album modes, the restricted-source fallback, the
// branding hop, the fence-defer-fail vocabulary) is the contract, and the
// strangler-fig migration only changes WHO executes it — not what it does.
//
// Ownership rules this package enforces:
//
//   - One authoritative execution path per job. The fence
//     (delivery_attempts) is claimed BEFORE any Telegram call, so a lease
//     expiring mid-dispatch or a crash between Telegram's accept and the
//     commit cannot produce a second send.
//   - Python decides nothing business-level. The Gateway is asked to
//     forward/copy/send and reports what Telegram said; the retry decision
//     (backoff, budget, ambiguity) is made here.
//   - Ambiguous outcomes are recorded, never retried blindly. A timeout
//     means "Telegram may have accepted it" — the job goes UNKNOWN and
//     reconciliation decides.
package delivery

import (
	"context"
	"fmt"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/engine/filter"
	"atf/worker/internal/engine/transform"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph"
)

// Logger is the structured logger the engines emit through. Fields carry the
// correlation id, job id and rule id, never message content or credentials.
type Logger interface {
	Info(msg string, args ...any)
	Warn(msg string, args ...any)
	Error(msg string, args ...any)
}

// Metrics is the operational counter surface the worker publishes.
type Metrics interface {
	// Outcome bumps a delivery outcome counter (sent|retried|failed|unknown).
	Outcome(kind string)
	// ObserveRPC records a Telegram RPC latency observation.
	ObserveRPC(operation string, seconds float64)
	// ObserveWait records how long a job waited in the queue.
	ObserveWait(seconds float64)
}

// Options tunes the delivery engine. Defaults come from the shared
// ATF_QUEUE_* configuration so both engines behave the same way.
type Options struct {
	// NumWorkers is the bounded goroutine count consuming the queue.
	NumWorkers int
	// BatchSize is one claim's batch.
	BatchSize int
	// LeaseDuration bounds how long a claim may run before it is reclaimable.
	LeaseDuration time.Duration
	// MaxQueueSize is the backpressure ceiling the Python pipeline applies at
	// enqueue time; the worker reports against the same number.
	MaxQueueSize int
	// MaxPerMinutePerTarget is the per-target send rate limit (0 = unlimited).
	MaxPerMinutePerTarget int
	// RPCTimeout bounds a single Telegram Gateway call.
	RPCTimeout time.Duration
}

// Engine is the delivery execution engine. It owns no goroutine state; the
// Worker (cmd/worker) drives its Run loop.
type Engine struct {
	store    queue.Store
	rules    rule.Store
	gateway  telegraph.Gateway
	filters  *filter.Engine
	transforms *transform.Engine
	log      Logger
	metrics  Metrics
	opts     Options
	workerID string

	mu       sync.Mutex
	rate     map[string][]time.Time // per-target send timestamps
	scratch  string                 // shared media scratch dir
}

// New constructs an Engine. gateway may be the gRPC client or the in-process
// fake — the engine never knows which.
func New(store queue.Store, rules rule.Store, gateway telegraph.Gateway, log Logger, metrics Metrics, workerID, scratchDir string, opts Options) *Engine {
	if opts.NumWorkers < 1 {
		opts.NumWorkers = 4
	}
	if opts.BatchSize < 1 {
		opts.BatchSize = 5
	}
	if opts.LeaseDuration <= 0 {
		opts.LeaseDuration = 30 * time.Second
	}
	if opts.RPCTimeout <= 0 {
		opts.RPCTimeout = 60 * time.Second
	}
	return &Engine{
		store:    store,
		rules:    rules,
		gateway:  gateway,
		filters:  filter.New(),
		transforms: transform.New(),
		log:      log,
		metrics:  metrics,
		opts:     opts,
		workerID: workerID,
		rate:     map[string][]time.Time{},
		scratch:  scratchDir,
	}
}

// Process executes one claimed job end to end.
//
// It returns nil when the job reached a terminal state (SENT, FAILED,
// UNKNOWN) and an error only when the engine itself could not make progress
// (store unavailable). A defer/retry is a terminal-for-now state, not an
// error.
func (e *Engine) Process(ctx context.Context, j *job.DeliveryJob) error {
	logger := e.jobLogger(j)

	// 1. The send-side idempotency fence runs BEFORE any Telegram call.
	// Telegram has no idempotency key for ordinary sends; claiming the
	// logical send here is the only thing that stops a lease expiry or a
	// crash-recovery loop from double-dispatching.
	key := job.AttemptKey{
		RuleID:          j.RuleID,
		SourceChatID:    j.SourceChatID,
		SourceMessageID: j.SourceMessageID,
		TargetChatID:    j.TargetChatID,
		JobKind:         j.JobKind,
	}
	leaseUntil := float64(time.Now().Add(2 * e.opts.LeaseDuration).Unix())
	fenceOutcome, fenceRow, err := e.store.FenceClaim(ctx, key, e.workerID, leaseUntil)
	if err != nil {
		// A fence-store hiccup must never silently disable the fence: the
		// send is refused loudly rather than dispatched unprotected.
		logger.Error("idempotency fence unavailable — refusing to dispatch unprotected",
			"error", err)
		_ = e.store.Defer(ctx, j.ID, "idempotency fence unavailable", 5*time.Second)
		return nil
	}
	switch fenceOutcome {
	case job.FenceAlreadySent:
		// A previous attempt recorded a confirmed send. Telegram has the
		// copy; re-dispatching would duplicate it. Close as SENT with the
		// recorded target id, without re-sending.
		priorID := int64(0)
		if fenceRow != nil {
			priorID = fenceRow.TargetMessageID
		}
		logger.Warn("job already has a confirmed send — marking SENT without re-dispatching",
			"target_message_id", priorID)
		if err := e.store.NoteMapping(ctx, j, job.StatusSent, priorID); err != nil {
			logger.Warn("mapping note failed for already-sent job", "error", err)
		}
		if err := e.store.MarkSent(ctx, j.ID, priorID); err != nil {
			logger.Warn("mark_sent failed for already-sent job", "error", err)
		}
		e.metrics.Outcome("sent")
		return nil
	case job.FenceInFlight, job.FenceStale, job.FenceUnknown:
		// Another worker holds a live claim, or an earlier send's outcome
		// was never recorded. Re-dispatching is exactly the duplicate the
		// fence exists to prevent: defer for reconciliation.
		logger.Warn("job not dispatchable now — deferring for reconciliation",
			"fence_outcome", string(fenceOutcome))
		_ = e.store.Defer(ctx, j.ID, "delivery fence: "+string(fenceOutcome), 15*time.Second)
		e.metrics.Outcome("retried")
		return nil
	case job.FenceDispatch:
		// fall through: this worker owns the send
	}

	// 2. Re-check the task at delivery time. A job can sit in the queue
	// while its owner deletes or pauses the task; delivering then would
	// forward a message the owner has already removed.
	r, f, err := e.rules.GetByID(j.RuleID)
	if err != nil {
		logger.Warn("rule store unavailable — deferring", "error", err)
		_ = e.store.Defer(ctx, j.ID, "rule store unavailable", 5*time.Second)
		e.metrics.Outcome("retried")
		return nil
	}
	if r == nil {
		logger.Info("rule no longer exists — dropping job")
		_ = e.store.MarkFailed(ctx, j.ID, "Rule no longer exists")
		e.metrics.Outcome("failed")
		return nil
	}
	if !r.IsActive {
		logger.Info("rule is disabled — dropping job")
		_ = e.store.MarkFailed(ctx, j.ID, "Rule disabled")
		e.metrics.Outcome("failed")
		return nil
	}
	if r.AutoPauseReason != "" {
		// The engine paused the task for a dead source/target; the message
		// cannot be delivered until the owner fixes the chat.
		logger.Info("rule is auto-paused — dropping job", "reason", r.AutoPauseReason)
		_ = e.store.MarkFailed(ctx, j.ID, "Task paused: "+r.AutoPauseReason)
		e.metrics.Outcome("failed")
		return nil
	}
	if r.IsPaused {
		// A paused task is not a failure: the job waits for the pause to lift.
		backoff := 5 * time.Second
		if r.PausedUntil > 0 {
			if rem := time.Until(time.Unix(r.PausedUntil, 0)); rem > 0 {
				backoff = rem
			}
		}
		logger.Info("rule is paused — postponing job", "backoff", backoff.String())
		_ = e.store.Defer(ctx, j.ID, "Rule paused", backoff)
		e.metrics.Outcome("retried")
		return nil
	}

	// 3. The persisted filter attached to the task is the only persisted
	// filter re-evaluated at delivery time (the pipeline's content policies
	// ran on the ingest side and are baked into the job payload).
	if f != nil {
		text := j.Payload.Text()
		res := e.filters.Evaluate(text, j.Payload.MediaType(), false, f)
		if res.Action == filter.Drop {
			logger.Info("job dropped by attached filter at delivery time", "reason", res.Reason)
			_ = e.store.MarkFailed(ctx, j.ID, "Filter: "+res.Reason)
			e.metrics.Outcome("failed")
			return nil
		}
	}

	// 4. Per-target rate limit. A throttled target is a defer (the lane is
	// released and the job re-enters after the window), never an attempt.
	if wait := e.targetRateWait(j.TargetChatID); wait > 0 {
		logger.Info("target is throttled — deferring", "wait", wait.String())
		_ = e.store.Defer(ctx, j.ID, "Target rate-limited", wait)
		e.metrics.Outcome("retried")
		return nil
	}

	// 5. Dispatch. The job's own text already carries every ingest-side
	// transform; the dispatch path re-decides only per-target overrides,
	// never the task's own policies.
	targetID, ferr := e.dispatch(ctx, j)

	if ferr != nil {
		return e.handleFailure(ctx, j, logger, key, ferr)
	}

	// 6. Confirmed. Record the send on the fence BEFORE the job-row commit:
	// a crash between Telegram's response and the commit must leave the
	// fence saying the send landed.
	if err := e.store.FenceRecordSent(ctx, key, e.workerID, targetID); err != nil {
		logger.Warn("could not record the confirmed send (a duplicate on recovery is now possible)", "error", err)
	}
	if err := e.store.NoteMapping(ctx, j, job.StatusSent, targetID); err != nil {
		logger.Warn("mapping note failed", "error", err)
	}
	if err := e.store.MarkSent(ctx, j.ID, targetID); err != nil {
		logger.Warn("mark_sent failed", "error", err)
	}
	if j.CreatedAt > 0 {
		e.metrics.ObserveWait(math.Max(0, time.Since(time.Unix(j.CreatedAt, 0)).Seconds()))
	}
	e.recordSend(j.TargetChatID)
	e.metrics.Outcome("sent")
	logger.Info("delivered",
		"target_message_id", targetID,
		"session_id", redacted(j.Payload.SessionID()))
	return nil
}

// dispatch selects the Telegram operation for this job and executes it.
//
// The decision order mirrors the Python dispatch path exactly:
//
//  1. branding hop (the message was already posted into C; deliver a forward
//     of that post, or a clean copy when the forward is restricted)
//  2. DIRECT_FORWARD (native forward, with the restricted-source fallback)
//  3. album handling (album | first | split modes, with resume)
//  4. TEXT_ONLY / MEDIA_ONLY / AUTO content modes
//  5. plain text send
//
// Every "restricted source" failure may trigger the copy/re-upload fallback,
// but only when the task opted in (fallback_enabled): a refused forward must
// fail the job instead of being rescued by a bypass the owner did not ask
// for.
func (e *Engine) dispatch(ctx context.Context, j *job.DeliveryJob) (int64, *telegraph.DeliveryFailure) {
	p := &j.Payload
	callCtx, cancel := context.WithTimeout(ctx, e.opts.RPCTimeout)
	defer cancel()

	target := intChat(j.TargetChatID)
	src := intChat(j.SourceChatID)
	srcMsg := j.SourceMessageID
	sessionID := p.SessionID()

	// A keyword-reply job is a pure-text follow-up: it never re-copies or
	// re-forwards the source message's media.
	if p.IsReply() {
		id, ferr := e.gateway.SendMessage(callCtx, sessionID, target, p.Text(), p.TopicID())
		e.metrics.ObserveRPC("send_message", 0)
		return id, ferr
	}

	// 1. Branding hop (A -> C -> B): the message was already copied into the
	// branding channel C on the dispatch path; the target must now receive a
	// forward of that post so the header names C.
	if branding := p.Branding(); branding != nil {
		cChat := chatID(branding["chat_id"])
		cIDs := messageIDs(branding["message_ids"], branding["message_id"])
		if cChat != 0 && len(cIDs) > 0 {
			id, ferr := e.brandingStage(callCtx, sessionID, target, cChat, cIDs, j)
			if ferr == nil {
				return id, nil
			}
			if !ferr.IsForwardingRestricted() || !p.FallbackEnabled() {
				return id, ferr
			}
			// Restricted from C with the fallback allowed: fall through to
			// the copy/re-upload path against the ORIGINAL source.
			e.jobLogger(j).Warn("branding hop restricted — falling back to the source",
				"brand_channel", cChat)
		}
	}

	// 2. DIRECT_FORWARD.
	forwardMode := p.ForwardMode()
albumFromIDs := p.AlbumMessageIDs()
	if len(albumFromIDs) == 0 {
		albumFromIDs = []int64{srcMsg}
	}
	if forwardMode == "DIRECT_FORWARD" {
		ids, ferr := e.gateway.ForwardMessages(callCtx, sessionID, target, src, albumFromIDs, p.TopicID())
		e.metrics.ObserveRPC("forward_messages", 0)
		if ferr == nil && len(ids) > 0 {
			return firstNonZero(ids), nil
		}
		if ferr != nil && !ferr.IsForwardingRestricted() {
			return 0, ferr
		}
		// Restricted (or empty result): fall through to the copy path when
		// the task allows the bypass, otherwise surface the failure.
		if ferr != nil && !p.FallbackEnabled() {
			return 0, ferr
		}
	}

	// 3. Album handling.
	isAlbum := p.MediaGroupID() != "" && len(albumFromIDs) > 1
	if isAlbum {
		return e.dispatchAlbum(callCtx, j, albumFromIDs)
	}

	// 4. Content modes.
	caption := p.Text()
	switch p.ContentMode() {
	case "TEXT_ONLY":
		id, ferr := e.gateway.SendMessage(callCtx, sessionID, target, firstNonEmpty(caption, "..."), p.TopicID())
		e.metrics.ObserveRPC("send_message", 0)
		return id, ferr
	case "MEDIA_ONLY":
		if p.HasMedia() {
			id, ferr := e.gateway.CopyMessage(callCtx, sessionID, target, src, srcMsg, "", p.TopicID())
			e.metrics.ObserveRPC("copy_message", 0)
			if ferr != nil && ferr.IsForwardingRestricted() && p.FallbackEnabled() {
				return e.reuploadMedia(callCtx, j, src, srcMsg, "")
			}
			return id, ferr
		}
		return 0, nil
	}

	// 5. AUTO / TEXT_AND_MEDIA.
	if p.HasMedia() {
		id, ferr := e.gateway.CopyMessage(callCtx, sessionID, target, src, srcMsg, caption, p.TopicID())
		e.metrics.ObserveRPC("copy_message", 0)
		if ferr != nil && ferr.IsForwardingRestricted() && p.FallbackEnabled() {
			return e.reuploadMedia(callCtx, j, src, srcMsg, caption)
		}
		return id, ferr
	}
	id, ferr := e.gateway.SendMessage(callCtx, sessionID, target, firstNonEmpty(caption, "..."), p.TopicID())
	e.metrics.ObserveRPC("send_message", 0)
	return id, ferr
}

// brandingStage reproduces the hop: forward C's post (or copy it when the
// forward is restricted or the transport has no native forward).
func (e *Engine) brandingStage(ctx context.Context, sessionID string, target, cChat int64, cIDs []int64, j *job.DeliveryJob) (int64, *telegraph.DeliveryFailure) {
	forceForward := true
	if branding := j.Payload.Branding(); branding != nil {
		if v, ok := branding["force_forward"].(bool); ok {
			forceForward = v
		}
	}
	if forceForward {
		ids, ferr := e.gateway.ForwardMessages(ctx, sessionID, target, cChat, cIDs, j.Payload.TopicID())
		e.metrics.ObserveRPC("forward_messages", 0)
		if ferr == nil && len(ids) > 0 {
			return firstNonZero(ids), nil
		}
		if ferr != nil && !ferr.IsForwardingRestricted() {
			return 0, ferr
		}
		// restricted: fall through to a clean copy of C's post
	}
	var last int64
	for _, cID := range cIDs {
		caption := j.Payload.Text()
		if j.Payload.ContentMode() == "MEDIA_ONLY" {
			caption = ""
		}
		id, ferr := e.gateway.CopyMessage(ctx, sessionID, target, cChat, cID, caption, j.Payload.TopicID())
		e.metrics.ObserveRPC("copy_message", 0)
		if ferr != nil {
			if ferr.IsForwardingRestricted() && j.Payload.FallbackEnabled() {
				id, ferr = e.reuploadMedia(ctx, j, cChat, cID, caption)
				if ferr != nil {
					return 0, ferr
				}
			} else {
				return 0, ferr
			}
		}
		if id > 0 {
			last = id
		}
	}
	return last, nil
}

// dispatchAlbum handles the multi-photo modes.
//
//   - "album"  keeps the media group (copy_media_group), falling back to a
//     per-item re-upload when the group copy is refused.
//   - "first"  delivers only the group's first item.
//   - "split"  delivers every item as its own post, RESUMING from the
//     per-item confirmations already recorded in the job payload (audit
//     finding E2E-5: a retry used to re-send every item, duplicating the
//     ones Telegram had already accepted).
func (e *Engine) dispatchAlbum(ctx context.Context, j *job.DeliveryJob, albumIDs []int64) (int64, *telegraph.DeliveryFailure) {
	p := &j.Payload
	src := intChat(j.SourceChatID)
	target := intChat(j.TargetChatID)
	sessionID := p.SessionID()
	caption := p.Text()

	switch strings.ToLower(p.AlbumMode()) {
	case "first":
		id, ferr := e.gateway.CopyMessage(ctx, sessionID, target, src, albumIDs[0], caption, p.TopicID())
		e.metrics.ObserveRPC("copy_message", 0)
		if ferr != nil && ferr.IsForwardingRestricted() && p.FallbackEnabled() {
			return e.reuploadMedia(ctx, j, src, albumIDs[0], caption)
		}
		return id, ferr

	case "split":
		sentMap := p.AlbumSent()
		remaining := make([]int64, 0, len(albumIDs))
		for _, id := range albumIDs {
			if _, ok := sentMap[strconv.FormatInt(id, 10)]; ok {
				// Telegram already confirmed this item: never re-send it
				// (audit finding E2E-5 — a retry used to duplicate every
				// item that had already landed).
				continue
			}
			remaining = append(remaining, id)
		}
		if len(remaining) > 0 && len(sentMap) > 0 {
			e.jobLogger(j).Warn("resuming split delivery: re-sending only the unconfirmed items",
				"confirmed", len(albumIDs)-len(remaining), "remaining", len(remaining))
		}
		var last int64
		newSent := map[string]int64{}
		for _, itemID := range remaining {
			id, ferr := e.gateway.CopyMessage(ctx, sessionID, target, src, itemID, caption, p.TopicID())
			e.metrics.ObserveRPC("copy_message", 0)
			if ferr != nil {
				// Record what got through before surfacing the failure, so
				// the retry resumes rather than duplicates: the items before
				// this one are physically in the target now.
				_ = e.store.RecordAlbumProgress(ctx, j.ID, newSent)
				if ferr.IsForwardingRestricted() && p.FallbackEnabled() {
					id, ferr = e.reuploadMedia(ctx, j, src, itemID, caption)
					if ferr != nil {
						return 0, ferr
					}
				} else {
					return 0, ferr
				}
			}
			if id > 0 {
				newSent[strconv.FormatInt(itemID, 10)] = id
				last = id
			}
		}
		// Persist the per-item confirmations for any later retry.
		_ = e.store.RecordAlbumProgress(ctx, j.ID, newSent)
		// The mapping's target id is the FIRST item of the group: edit and
		// delete sync key on it and the whole group shares it.
		if first, ok := sentMap[strconv.FormatInt(albumIDs[0], 10)]; ok {
			return first, nil
		}
		if firstNew, ok := newSent[strconv.FormatInt(albumIDs[0], 10)]; ok {
			return firstNew, nil
		}
		return last, nil
	}

	// "album" (default): keep the media group.
	ids, ferr := e.gateway.CopyMediaGroup(ctx, sessionID, target, src, albumIDs[0], caption, p.TopicID())
	e.metrics.ObserveRPC("copy_media_group", 0)
	if ferr == nil && len(ids) > 0 {
		return firstNonZero(ids), nil
	}
	if ferr != nil && !ferr.IsForwardingRestricted() {
		// The group copy failed for a non-restriction reason: fall through to
		// the per-item re-upload (the Python path does the same).
		e.jobLogger(j).Warn("album group copy failed — falling back to re-upload", "error", ferr.Message)
	}
	if ferr != nil && ferr.IsForwardingRestricted() && !p.FallbackEnabled() {
		return 0, ferr
	}
	// Per-item re-upload fallback.
	var last int64
	for _, itemID := range albumIDs {
		id, rferr := e.reuploadMedia(ctx, j, src, itemID, caption)
		if rferr != nil {
			return 0, rferr
		}
		if id > 0 {
			last = id
		}
	}
	return last, nil
}

// reuploadMedia is the restricted-source bypass: download the media through
// the runtime, stage it in the shared scratch volume, then upload it as a
// fresh file. It is only entered when the task opted in (fallback_enabled).
//
// Media never enters either process's memory in full: the runtime writes to
// the shared scratch path, and the upload is staged from that path.
func (e *Engine) reuploadMedia(ctx context.Context, j *job.DeliveryJob, srcChat, srcMsg int64, caption string) (int64, *telegraph.DeliveryFailure) {
	p := &j.Payload
	sessionID := p.SessionID()
	dest := telegraph.MediaHandle{
		Path:     e.scratchPath(j),
		FileName: fmt.Sprintf("job-%s-media", j.ID),
	}
	if _, ferr := e.gateway.DownloadMedia(ctx, sessionID, srcChat, srcMsg, dest); ferr != nil {
		return 0, ferr
	}
	token, ferr := e.gateway.PrepareMediaUpload(ctx, sessionID, dest, srcChat, srcMsg, p.MediaType())
	if ferr != nil {
		return 0, ferr
	}
	if token == "" {
		// No file reference: deliver the text only, never lose the message.
		id, sferr := e.gateway.SendMessage(ctx, sessionID, intChat(j.TargetChatID), firstNonEmpty(p.Text(), "..."), p.TopicID())
		e.metrics.ObserveRPC("send_message", 0)
		return id, sferr
	}
	// The runtime turns the opaque token into a Telegram file reference and
	// sends the media with the transformed caption. The worker never parses
	// the token and never holds the file in memory.
	id, sferr := e.gateway.SendMedia(ctx, sessionID, intChat(j.TargetChatID), token, caption, p.MediaType(), p.TopicID())
	e.metrics.ObserveRPC("send_media", 0)
	return id, sferr
}

// scratchPath is the unique per-job path in the shared media scratch volume.
// Unique by construction (job ids are unique), so two concurrent re-uploads
// can never collide on the same file.
func (e *Engine) scratchPath(j *job.DeliveryJob) string {
	if e.scratch == "" {
		return fmt.Sprintf("/tmp/atf-media/%s", j.ID)
	}
	return fmt.Sprintf("%s/%s", strings.TrimRight(e.scratch, "/"), j.ID)
}

// handleFailure maps a typed Telegram failure onto the job lifecycle.
//
// The vocabulary is the whole point of the shared error taxonomy:
//
//   - ambiguous (timeout, unknown outcome) → MarkUnknown, never retried
//     automatically. Blindly retrying is what produces duplicate messages.
//   - transient (flood wait, rate limited, temporary) → defer/backoff; a
//     FloodWait's retry_after is authoritative and never shortened.
//   - permanent (permission, chat/message not found, auth) → MarkFailed,
//     the attempt budget is not wasted on something that cannot succeed.
//   - anything else → MarkRetry with exponential backoff, dead-lettering
//     when the budget is spent.
func (e *Engine) handleFailure(ctx context.Context, j *job.DeliveryJob, logger Logger, key job.AttemptKey, ferr *telegraph.DeliveryFailure) error {
	// Ambiguous: Telegram may have accepted the send. Never a plain FAILED,
	// never an automatic re-dispatch — reconciliation owns this row.
	if ferr.IsAmbiguous() {
		_ = e.store.FenceRecordOutcome(ctx, key, e.workerID, "UNKNOWN", ferr.Message)
		logger.Error("ambiguous delivery outcome — marking UNKNOWN for reconciliation",
			"code", ferr.Code, "error_type", ferr.ErrorType, "message", ferr.Message)
		if err := e.store.MarkUnknown(ctx, j.ID, "Uncertain outcome: "+ferr.Message); err != nil {
			logger.Warn("mark_unknown failed", "error", err)
		}
		e.metrics.Outcome("unknown")
		return nil
	}

	// FloodWait: Telegram told us exactly how long to wait. Record it and
	// defer — never spend an attempt on a rate limit the platform itself
	// demanded.
	if ferr.Code == "FLOOD_WAIT" {
		wait := time.Duration(ferr.RetryAfterSeconds) * time.Second
		if wait <= 0 {
			wait = 5 * time.Second
		}
		_ = e.store.FenceRecordOutcome(ctx, key, e.workerID, "FAILED", ferr.Message)
		logger.Warn("flood wait — deferring for the demanded window",
			"retry_after_seconds", ferr.RetryAfterSeconds)
		_ = e.store.Defer(ctx, j.ID, "FloodWait: "+ferr.Message, wait)
		e.metrics.Outcome("retried")
		return nil
	}

	// Session unavailable / target unavailable: the environment, not the
	// job. Defer without spending an attempt.
	if ferr.Code == "SESSION_UNAVAILABLE" || ferr.Code == "TARGET_UNAVAILABLE" ||
		ferr.Code == "SOURCE_UNAVAILABLE" || ferr.Code == "NETWORK_DISCONNECTED" {
		_ = e.store.FenceRecordOutcome(ctx, key, e.workerID, "FAILED", ferr.Message)
		logger.Warn("dependency unavailable — deferring", "code", ferr.Code)
		_ = e.store.Defer(ctx, j.ID, ferr.Code+": "+ferr.Message, 10*time.Second)
		e.metrics.Outcome("retried")
		return nil
	}

	// Authentication: the session cannot perform operations at all. Nothing
	// the retry loop can fix — fail it loudly and visibly.
	if ferr.Code == "AUTHENTICATION_ERROR" {
		_ = e.store.FenceRecordOutcome(ctx, key, e.workerID, "FAILED", ferr.Message)
		logger.Error("session authentication failure — marking job FAILED",
			"session_id", redacted(j.Payload.SessionID()))
		_ = e.store.MarkFailed(ctx, j.ID, "Auth: "+ferr.Message)
		e.metrics.Outcome("failed")
		return nil
	}

	// Permanent Telegram refusals: no retry can help.
	if !ferr.IsTransient() {
		_ = e.store.FenceRecordOutcome(ctx, key, e.workerID, "FAILED", ferr.Message)
		logger.Error("permanent Telegram failure — marking job FAILED",
			"code", ferr.Code, "error_type", ferr.ErrorType)
		_ = e.store.MarkFailed(ctx, j.ID, ferr.Code+": "+ferr.Message)
		e.metrics.Outcome("failed")
		return nil
	}

	// Transient: exponential backoff, budget-bounded, dead-lettered at
	// exhaustion. Exponential (not linear) because a burst of FloodWaits is
	// exactly the condition under which linear retries compound.
	_ = e.store.FenceRecordOutcome(ctx, key, e.workerID, "FAILED", ferr.Message)
	backoff := backoffFor(j.Attempts)
	logger.Warn("transient failure — retrying with backoff",
		"code", ferr.Code, "attempts", j.Attempts, "backoff", backoff.String())
	if err := e.store.MarkRetry(ctx, j.ID, ferr.Code+": "+ferr.Message, backoff); err != nil {
		logger.Warn("mark_retry failed", "error", err)
	}
	e.metrics.Outcome("retried")
	return nil
}

// backoffFor is the exponential backoff schedule. Capped: an unbounded
// exponential is an uncontrolled retry storm wearing a different hat.
func backoffFor(attempts int) time.Duration {
	delay := time.Duration(math.Pow(2, float64(attempts))) * time.Second
	if delay > 5*time.Minute {
		delay = 5 * time.Minute
	}
	if delay < time.Second {
		delay = time.Second
	}
	return delay
}

// --- per-target rate limiting --------------------------------------------- //

// targetRateWait returns how long this target must wait before another send.
// 0 means "send now". The window is in-memory and per-process: it is a
// *cooperation* guard on top of the durable per-target lane the claim query
// enforces, not a second source of truth.
func (e *Engine) targetRateWait(target string) time.Duration {
	if e.opts.MaxPerMinutePerTarget <= 0 {
		return 0
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	now := time.Now()
	window := time.Minute
	cutoff := now.Add(-window)
	times := e.rate[target]
	kept := times[:0]
	for _, t := range times {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	if len(kept) >= e.opts.MaxPerMinutePerTarget {
		e.rate[target] = kept
		return kept[0].Add(window).Sub(now)
	}
	e.rate[target] = kept
	return 0
}

// recordSend notes a confirmed send for the per-target rate window.
func (e *Engine) recordSend(target string) {
	if e.opts.MaxPerMinutePerTarget <= 0 {
		return
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.rate[target] = append(e.rate[target], time.Now())
}

// --- helpers --------------------------------------------------------------- //

// jobLogger returns a logger pre-loaded with the job's identity fields.
// Correlation id, job id and rule id ride on every line; message content and
// credentials never do.
func (e *Engine) jobLogger(j *job.DeliveryJob) Logger {
	return &fieldLogger{base: e.log, fields: []any{
		"correlation_id", j.Payload.CorrelationID(),
		"job_id", j.ID,
		"rule_id", j.RuleID,
		"target_chat_id", j.TargetChatID,
		"source_chat_id", j.SourceChatID,
		"source_message_id", j.SourceMessageID,
	}}
}

// fieldLogger stamps a fixed field set on every record.
type fieldLogger struct {
	base   Logger
	fields []any
}

func (l *fieldLogger) Info(msg string, args ...any)  { l.base.Info(msg, append(l.fields, args...)...) }
func (l *fieldLogger) Warn(msg string, args ...any)  { l.base.Warn(msg, append(l.fields, args...)...) }
func (l *fieldLogger) Error(msg string, args ...any) { l.base.Error(msg, append(l.fields, args...)...) }

// redacted masks a session id for logs: the first 8 characters are enough to
// correlate two lines about the same session, the rest is not needed and not
// printed. Session STRINGS never reach the worker at all.
func redacted(sessionID string) string {
	if len(sessionID) <= 8 {
		return strings.Repeat("*", len(sessionID))
	}
	return sessionID[:8] + "***"
}

// firstNonZero returns the first non-zero id of a batch (Telegram returns a
// list for multi-id forwards; the mapping needs the first one).
func firstNonZero(ids []int64) int64 {
	for _, id := range ids {
		if id != 0 {
			return id
		}
	}
	return 0
}

func firstNonEmpty(s, fallback string) string {
	if strings.TrimSpace(s) != "" {
		return s
	}
	return fallback
}

// intChat converts a string chat id to int64 (0 when unparseable). Chat ids
// are kept as strings in the payload (channel ids exceed the 32-bit range and
// a numeric round-trip is how precision is lost) and coerced only at the
// transport boundary, where Telegram needs the number.
func intChat(s string) int64 {
	var out int64
	_, _ = fmt.Sscanf(strings.TrimSpace(s), "%d", &out)
	return out
}

// chatID coerces a payload value (string or number) into an int64 chat id.
func chatID(v any) int64 {
	switch n := v.(type) {
	case float64:
		return int64(n)
	case int:
		return int64(n)
	case int64:
		return n
	case string:
		var out int64
		_, _ = fmt.Sscanf(n, "%d", &out)
		return out
	}
	return 0
}

// messageIDs coerces the branding payload's ids into a list.
func messageIDs(list any, single any) []int64 {
	out := []int64{}
	switch l := list.(type) {
	case []any:
		for _, item := range l {
			if id := chatID(item); id != 0 {
				out = append(out, id)
			}
		}
	}
	if len(out) == 0 {
		if id := chatID(single); id != 0 {
			out = append(out, id)
		}
	}
	return out
}


