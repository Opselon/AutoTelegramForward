package delivery

import (
	"context"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
)

// transform_stage.go — the Stage 4.2 execution-boundary change.
//
// THE INVARIANT. For every operation there is exactly one authoritative
// execution path. Before this stage the Python ingest pipeline owned the
// text transforms: it ran _apply_transforms on the raw source message and
// wrote the RESULT into the job payload as "text". The Worker delivered
// that baked text, which is correct but leaves Python as the business
// owner of half the pipeline.
//
// After this stage the Worker owns the same pipeline. The enqueue path
// (Python, unchanged) snapshots the RAW message — "effective_text" — and
// the Worker re-runs the identical ordered stages against that raw form.
// The output is what gets sent. Exactly one engine runs the transform, so
// the message is never double-transformed, and each side's copy of the
// work is restart-safe because the input is a durable snapshot, not an
// in-memory view.
//
// THE GATE. The handover is not a code drop, it is a switch:
//
//	ATF_WORKER_TRANSFORMS=1   the Worker owns the transform stage
//	                          (unset)   the Python ingest pipeline keeps it
//
// A job enqueued BEFORE the flip carries "transforms_version" in its
// payload, which says "the ingest side already applied the transform
// stage — do not run it again". A job enqueued AFTER the flip carries
// "raw_text" (the untransformed snapshot) and no version marker. Both
// shapes execute correctly under either switch position, which is what
// makes the flip safe to set and unset at any time.

// transformsVersionKey marks a payload whose transform stage the Python
// ingest pipeline already applied. Its presence is the "do not
// re-transform" contract.
const transformsVersionKey = "transforms_version"

// rawTextKey carries the raw source text when the enqueue path expects the
// Worker to own the transform stage.
const rawTextKey = "raw_text"

// ownsTransforms reports whether this Worker process owns the transform
// stage for this job. It is the conjunction of the process switch and the
// job's own marker: a pre-4.2 job (already transformed by Python) never
// gets a second pass, whatever the process switch says.
func (e *Engine) ownsTransforms(j *job.DeliveryJob) bool {
	if !workerOwnsTransforms.Load() {
		return false
	}
	// A job is "already transformed" only when the marker is PRESENT and
	// non-empty. Python writes transforms_version: null alongside raw_text
	// when ATF_WORKER_TRANSFORMS=1 (durable_queue._transform_boundary_payload
	// returns (raw, None)) — the null is its "Python did NOT transform"
	// value, not a version. Treating key-presence as the marker made every
	// Python-enqueued job look pre-transformed, so neither engine applied
	// the stage: replacements, cleaner, header/footer and AI all silently
	// did nothing while the worker still logged "delivered".
	if v, already := j.Payload.Raw()[transformsVersionKey]; already {
		if s, ok := v.(string); ok && s != "" {
			return false
		}
	}
	// Only a job that actually carries a raw snapshot can be transformed
	// here. Anything else has nothing to re-run the pipeline against.
	_, hasRaw := j.Payload.Raw()[rawTextKey]
	return hasRaw
}

// workerOwnsTransforms is the process-level switch behind
// ATF_WORKER_TRANSFORMS. It is read on every job, so the cost matters: an
// atomic is the cheapest correct thing (no mutex, no map lookup).
var workerOwnsTransforms atomic.Bool

// SetWorkerOwnsTransforms configures the process-level transform
// ownership. The Worker main calls it once at startup from
// ATF_WORKER_TRANSFORMS; tests call it to exercise both positions without
// an env round-trip.
func SetWorkerOwnsTransforms(enabled bool) {
	workerOwnsTransforms.Store(enabled)
}

// loadTransformSwitch reads ATF_WORKER_TRANSFORMS once. Centralizing the
// parsing keeps one spelling of "on" for the whole process.
func loadTransformSwitch() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("ATF_WORKER_TRANSFORMS"))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// applyTransformPipeline runs the Stage 4.2 transform pipeline for one job
// and records the decision. It mutates the job's payload text in place —
// the job is CLAIMED and is never concurrently processed by another worker
// (the queue's lane-isolation predicate guarantees it), so the in-place
// update is the single-writer case, not a shared-mutable bug.
//
// This is a preflight stage for the same reasons the filter and
// block_links gates are: a drop here never touches Telegram, so it must
// never leave a fence claim behind. MarkFailed, not Defer.
//
// Stage 5 (W4): this is also the AI stage's only owner. The rule's
// ai_config_id pair, the fallback policy and the timeout resolve INTO the
// cleaning.Config the pipeline below already consumes (cfg.AIRewrite /
// cfg.AIFallback), so the provider rewrite runs at the exact position the
// Python pipeline runs it — inside the ordered transform, after the
// policy stages and before the final replacements + header/footer — and
// there is exactly one transform owner, never two.
func (e *Engine) applyTransformPipeline(ctx context.Context, j *job.DeliveryJob, r *rule.ForwardRule, logger Logger) error {
	tStart := time.Now()
	defer func() {
		// The transform pipeline IS the text stages (cleaner, link
		// policy/replacement, header/footer). They run as one ordered
		// ApplyText call, so one observation per stage in the aggregate is
		// the honest measurement: splitting them would mean re-timing a
		// library call that reports no per-stage clock.
		//
		// "ai" is NOT recorded here: most jobs carry no AI rewrite at all,
		// and counting them made the stage read healthy-with-usage it never
		// had — while jobs that DID rewrite were counted twice (once here,
		// once per attempt by the AIMetricsObserver). The AI stage counts
		// through its own observer only.
		ms := float64(time.Since(tStart).Microseconds()) / 1000.0
		e.stageRecord("cleaner", tStart)
		e.stageRecord("replacement", tStart)
		e.stageRecord("header_footer", tStart)
		_ = ms
	}()
	raw := payloadRawText(j)
	if raw == "" {
		// Nothing to transform and nothing to drop: a snapshot of an empty
		// message can only stay empty, and an opinion about it is not a
		// business decision worth recording.
		return nil
	}

	ch := cleaning.CaptionHint{
		// The effective text for a media message IS its caption
		// (payload.effective_text, entities.py:2498). The enqueue path
		// snapshots that text under raw_text, so the only way the pipeline
		// can tell a caption from a body is the payload's own has_media
		// flag — the same field Python's effective_text consults.
		IsCaption: j.Payload.HasMedia(),
	}
	cfg := e.transformConfig(j, r)

	// Stage 5 (W4): install the AI rewrite hook when the task actually
	// asks for one. The gate is the rule's single AI authority
	// (ai_rewrite_active, Stage 4.4): a config id alone is not enough,
	// because the owner's panel switch or the paid-addon veto can be
	// closed while the config stays attached — and Python logs exactly
	// that ([PIPELINE:AI_REWRITE:OFF]) before delivering the text
	// untransformed.
	if r != nil && r.AIRewriteActive && r.AIConfigID != "" {
		rewrite, fallback := e.aiRewriteHooks(ctx, j, r, logger)
		if rewrite != nil {
			cfg.AIRewrite = rewrite
			cfg.AIFallback = fallback
		}
	}

	result := e.transformsPipeline.ApplyText(raw, cfg, nil, ch)

	// Stage 4.4 (F-16): the link policy governs the source's inline URL
	// buttons as well as its body. The payload carries the keyboard
	// snapshot; the policy that just governed the body decides whether the
	// buttons survive, and the decision rides on the payload so the
	// delivery side never re-derives it.
	if decision := buttonDecisionFor(j, cfg); decision.RemoveAll {
		j.Payload.MarkDropSourceButtons()
	}

	// Record the decision — even the null result, because "which stages
	// ran" is the answer to "was any message silently skipped?".
	if e.decisions != nil {
		event := cleaning.DecisionEvent{
			Category:      "transform",
			Level:         "INFO",
			Message:       "transform pipeline applied",
			RuleID:        j.RuleID,
			CorrelationID: j.Payload.CorrelationID(),
			OwnerUserID:   j.Payload.OwnerUserID(),
			SourceID:      j.SourceChatID,
			TargetID:      j.TargetChatID,
			Decision:      "APPLY",
			StageNames:    result.StageNotes,
			Detail: map[string]any{
				"in_len":  len(raw),
				"out_len": len(result.Text),
			},
		}
		if result.EmptyAfterClean {
			event.Decision = "DROP"
			event.ReasonCode = "EMPTY_AFTER_CLEAN"
			event.Level = "WARN"
			event.Message = "message dropped: nothing left after cleaning"
		}
		// Stage 5 (W4): the AI-refusal drop is its own reason code, because
		// "the rewrite failed and the policy dropped the message" is a
		// different operator question than "the cleaner stripped the body".
		// The Python pipeline records the same distinction
		// (ai_drop:<error_code>, pipeline.py:1831) and the worker's reason
		// vocabulary must match it or the two engines' audit feeds cannot
		// be co-read.
		if result.AIDrop {
			event.Decision = "DROP"
			event.ReasonCode = "AI_" + result.AIClassification
			event.Level = "WARN"
			event.Message = "message dropped: AI rewrite failed and the fallback policy refused to send"
		}
		e.decisions.RecordDecision(ctx, event)
	}

	if result.EmptyAfterClean && !result.AIDrop {
		e.stageFail("cleaner", "EMPTY_AFTER_CLEAN")
		// The only transform-internal drop: the stages stripped the whole
		// body of a message that had content. The Python pipeline audits
		// exactly this decision (pipeline.py:1467-1486); the Worker makes
		// it a FAILED job with the same reason code so an operator sees the
		// same vocabulary from either engine.
		logger.Warn("message dropped — nothing left after cleaning",
			"decision", "DROP", "reason_code", "EMPTY_AFTER_CLEAN",
			"stages", strings.Join(result.StageNotes, ","))
		_ = e.store.MarkFailed(ctx, j.ID, "Filter: empty_after_clean")
		e.metrics.Outcome("failed")
		return errTransformDrop
	}
	if result.AIDrop {
		e.stageFail("ai", result.AIClassification)
		// Stage 5 (W4): the AI rewrite failed and the rule's fallback
		// policy is DROP, RETRY or QUARANTINE. The three share one
		// terminal outcome on the transform side — nothing is sendable —
		// and the delivery layer distinguishes them: RETRY re-queues the
		// job for another attempt, QUARANTINE holds it, DROP discards it.
		// The job's reason string carries the policy and the error code
		// so the queue's dead-letter surface can say which is which.
		policy := "DROP"
		if r != nil && r.AIFallbackPolicy != "" {
			policy = r.AIFallbackPolicy
		}
		switch strings.ToUpper(strings.TrimSpace(policy)) {
		case rule.FallbackRetry:
			logger.Warn("AI rewrite failed — re-queuing per fallback policy",
				"decision", "RETRY", "reason_code", "AI_"+result.AIClassification,
				"policy", policy, "rule_id", j.RuleID)
			_ = e.store.MarkRetry(ctx, j.ID, "AI: "+result.AIClassification, aiRetryBackoff)
			e.metrics.Outcome("retried")
		case rule.FallbackQuarantine:
			logger.Warn("AI rewrite failed — quarantining per fallback policy",
				"decision", "QUARANTINE", "reason_code", "AI_"+result.AIClassification,
				"policy", policy, "rule_id", j.RuleID)
			_ = e.store.MarkFailed(ctx, j.ID, "AI: quarantine:"+result.AIClassification)
			e.metrics.Outcome("failed")
		default:
			logger.Warn("AI rewrite failed — dropping per fallback policy",
				"decision", "DROP", "reason_code", "AI_"+result.AIClassification,
				"policy", policy, "rule_id", j.RuleID)
			_ = e.store.MarkFailed(ctx, j.ID, "AI: "+result.AIClassification)
			e.metrics.Outcome("failed")
		}
		return errAIDrop
	}

	// Hand the transformed text to the dispatch stage. The payload's own
	// accessor is the single read point, so the mutation is written once,
	// where every later reader (album caption, branding post, keyword
	// reply) sees it.
	setPayloadText(j, result.Text)
	if result.Text != raw {
		logger.Info("transform pipeline applied",
			"stages", strings.Join(result.StageNotes, ","),
			"in_len", len(raw), "out_len", len(result.Text))
	}
	return nil
}

// errTransformDrop is the sentinel that unwinds a preflight drop out of
// Process without touching the fence. It is never wrapped: the caller has
// already recorded the terminal state on the job row.
var errTransformDrop = sentinel("transform pipeline dropped the message")

// errAIDrop is the Stage 5 (W4) AI-refusal sentinel: the rewrite failed,
// the fallback policy refused to send, and the job's terminal state is
// already recorded (MarkFailed / MarkRetry). It unwinds out of Process
// the same way errTransformDrop does — never a fence claim, never a
// Telegram call.
var errAIDrop = sentinel("AI rewrite failed and the fallback policy dropped the message")

// aiRetryBackoff is the re-queue backoff a fallback policy of RETRY
// asks for: a short, fixed window, because the failure classification
// already decided the retry is worthwhile (RATE_LIMIT, TIMEOUT,
// PROVIDER_ERROR) and the circuit breaker is tracking the provider's
// health independently. Python's RETRY posture leaves the re-queue to
// the delivery layer too (pipeline.py:1854 returns None), so the exact
// window is this layer's to choose.
const aiRetryBackoff = 5 * time.Second

// sentinel is a string error type, so a decision drop reads like a
// condition and not like an infrastructure failure.
type sentinel string

func (s sentinel) Error() string { return string(s) }

// --- payload accessors kept local to this stage ------------------------------

// payloadRawText is the raw source snapshot the pipeline transforms. The
// raw form is the effective text — the caption when the source carried
// media, the body otherwise — exactly what the Python pipeline's first
// line of _apply_transforms reads (pipeline.py:1404).
func payloadRawText(j *job.DeliveryJob) string {
	raw, ok := j.Payload.Raw()[rawTextKey].(string)
	if !ok {
		return ""
	}
	return raw
}

// setPayloadText writes the transformed text back onto the payload. The
// payload is a map wrapper, so the write goes through the map the same way
// the Python enqueue path wrote the original key.
func setPayloadText(j *job.DeliveryJob, text string) {
	j.Payload.Raw()["text"] = text
}

// transformConfig builds the cleaning.Config for one job. Every field that
// can be resolved from the durable job payload is (the payload is the
// snapshot the enqueue path took, so it is the truth for this delivery);
// the live rule supplies only the configuration the payload does not carry
// — the transform settings, which Python never baked into the payload
// because it was the one applying them.
//
// Stage 5 (W4): the method lives on the engine because the AI hook
// resolves through the engine's transformer wiring. The AI fields are
// installed by the caller (applyTransformPipeline) after this returns —
// the transform config stays the deterministic pre-W4 shape it always
// was, and the caller gates the network stage.
func (e *Engine) transformConfig(j *job.DeliveryJob, r *rule.ForwardRule) cleaning.Config {
	if r == nil {
		return cleaning.Config{}
	}
	cfg := r.Transform
	cfg.RuleID = r.ID
	cfg.OwnerUserID = r.OwnerUserID
	cfg.HasMedia = j.Payload.HasMedia()
	// The target-scoped override applies to this target's copy alone, so
	// the pipeline has to know which target's text it is transforming.
	cfg.TargetChatID = j.TargetChatID

	// Stage 4.4: the header/footer templates render against the message
	// snapshot the enqueue path took. The payload carries every input the
	// renderer reads, so a restart renders the same header the ingest
	// path would have — and the same one the preview showed.
	if cfg.Header != "" || cfg.Footer != "" {
		p := &j.Payload
		cfg.HeaderVars = &cleaning.HeaderVars{
			SourceChatID:       p.SourceChatID(),
			SourceChatUsername: p.SourceChatUsername(),
			SourceChatTitle:    p.SourceChatTitle(),
			SourceMessageID:    j.SourceMessageID,
			SourceDate:         p.SourceDate(),
			SenderName:         p.SenderName(),
			EffectiveText:      p.EffectiveText(),
		}
		if origin := p.ForwardOrigin(); origin != nil {
			if from, ok := origin["from_chat_id"].(string); ok {
				cfg.HeaderVars.ForwardFromChat = from
			}
		}
		cfg.HeaderTask = cleaning.HeaderTask{
			Name:     ruleName(r),
			Category: r.MessageCategory,
		}
	}
	return cfg
}

// ruleName is the {rule_name} placeholder's value: the task's display name,
// or Python's f"Rule-{rule.id[:8]}" fallback when the task has none
// (services.py:359). It lives next to the only caller rather than on the
// rule projection because the fallback is a rendering decision, not a
// property of the task.
func ruleName(r *rule.ForwardRule) string {
	if r == nil {
		return ""
	}
	if name := strings.TrimSpace(r.Name); name != "" {
		return name
	}
	id := r.ID
	if len(id) > 8 {
		id = id[:8]
	}
	return "Rule-" + id
}
