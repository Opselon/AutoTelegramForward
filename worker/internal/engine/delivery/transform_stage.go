package delivery

import (
	"context"
	"os"
	"strings"
	"sync/atomic"

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
	if _, already := j.Payload.Raw()[transformsVersionKey]; already {
		return false
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
func (e *Engine) applyTransformPipeline(ctx context.Context, j *job.DeliveryJob, r *rule.ForwardRule, logger Logger) error {
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
	cfg := transformConfig(j, r)
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
		e.decisions.RecordDecision(ctx, event)
	}

	if result.EmptyAfterClean {
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
func transformConfig(j *job.DeliveryJob, r *rule.ForwardRule) cleaning.Config {
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
