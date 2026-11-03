package delivery

import (
	"context"
	"strconv"
	"strings"

	"atf/worker/internal/domain/aiconfig"
	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/prompt"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/infra/aiprovider"
)

// ai_stage.go — the Stage 5 (W4) AI rewrite, wired into the transform
// pipeline the Worker already owns.
//
// THE INVARIANT. There is exactly one transform owner. The Stage 4.2
// work moved the whole ordered _apply_transforms pipeline into the
// Worker (applyTransformPipeline), and the Python pipeline runs the AI
// rewrite INSIDE that ordering — after the content-policy/cleaner/link
// stages, before the final replacements + header/footer
// (pipeline.py:1590-1660). So the AI rewrite here is not a separate
// delivery block: it is a hook on the cleaning.Config that same pipeline
// consumes, placed at the identical position, which is what keeps the
// two engines from diverging on what a rewrite may resurrect or what a
// header may wrap.
//
// THE BOUNDARY. A rule may only spend an AI config owned by the rule's
// owner (transformer.py:108): the id in a rule row is an identifier, not
// proof of ownership, and a rule that names another tenant's config
// (forged callback, imported backup, hand-edited row) must not spend
// that tenant's provider or key. The owner-scoped resolution below is
// the same boundary the Python _resolve_config enforces.
//
// THE LOG CONTRACT. [PIPELINE:AI_REWRITE:*] (pipeline.py:1598-1816):
// START, SUCCESS, FAILED, FALLBACK and OFF all carry the correlation id,
// the rule id, the config id and the error code — never the API key,
// never the full text, never the raw provider output.

// AIMetrics is the delivery-side counters the Python pipeline tracks
// (self.metrics.ai_transforms_* / ai_fallback_*). They are the same
// vocabulary the control room already graphs, so a parity case can pin
// them; nil is "no recorder", the same posture the decision recorder
// takes.
type AIMetrics interface {
	// AITransform marks one attempted rewrite: total, success, failure.
	AITransform(outcome string)
	// AIFallback marks one fallback decision by policy name.
	AIFallback(policy string)
}

// aiRewriteHooks builds the (rewrite, fallback) pair the pipeline runs.
//
// A nil rewrite means "no provider to call": the config store is absent,
// the transformer is absent, or the gate refused. In every one of those
// cases the pipeline keeps the text untransformed and the delivery
// proceeds — the Python pipeline's AI_UNAVAILABLE posture
// (pipeline.py:1700): a worker that has no provider configured delivers
// the original text rather than dropping the message.
//
// The gate is the rule's single AI authority (ai_rewrite_active, the
// Stage 4.4 gate Python consults at pipeline.py:1591): a task whose panel
// switch closed keeps its config attached but does not rewrite, and the
// hooks are never built — the same way Python zeroes the config id and
// skips the whole _run_ai_transform call.
//
// The hooks close over the job and rule, so the closure itself is the
// state; no map lookups happen at rewrite time beyond the transformer's
// own config resolution.
func (e *Engine) aiRewriteHooks(ctx context.Context, j *job.DeliveryJob, r *rule.ForwardRule, logger Logger) (cleaning.AIRewriter, cleaning.AIFallbackPolicy) {
	// The gate: a rule with AI disabled must not build a provider hook,
	// whatever its config id says. Checked here AND by the caller
	// (applyTransformPipeline), because two independent guards are what
	// keep a future caller from wiring the network stage by accident.
	if r == nil || !r.AIRewriteActive || strings.TrimSpace(r.AIConfigID) == "" {
		return nil, nil
	}
	if e.aiTransformer == nil {
		// No transformer wired (a unit-test engine, or a deployment that
		// has not set the master key): deliver the original text, loudly,
		// the same way the Python pipeline treats an uninitialized
		// transformer.
		logger.Warn("AI transformer unavailable — delivering unmodified text",
			"decision", "AI_UNAVAILABLE", "rule_id", r.ID, "cfg", r.AIConfigID)
		if e.aiMetrics != nil {
			e.aiMetrics.AITransform("failed")
		}
		return nil, nil
	}
	return e.aiRewriteFor(ctx, j, r, logger), e.aiFallbackFor(j, r, logger)
}

// targetPromptFor resolves the effective prompt template ID and version
// for this specific delivery target, checking per-target overrides before
// falling back to the task rule's default.
func (e *Engine) targetPromptFor(j *job.DeliveryJob, r *rule.ForwardRule) (string, int) {
	if r == nil {
		return "", 0
	}
	targetKey := goChatKey(j.TargetChatID)
	if targetKey != "" && r.TargetPromptOverrides != nil {
		if override, ok := r.TargetPromptOverrides[targetKey]; ok && strings.TrimSpace(override.PromptID) != "" {
			return strings.TrimSpace(override.PromptID), override.PromptVersion
		}
	}
	return strings.TrimSpace(r.AIPromptID), r.AIPromptVersion
}

// aiRewriteFor builds the rewrite closure: one guarded provider call
// through the orchestrator, mapped onto the pipeline's AIRewriter
// signature. The classification travels back as a cleaning.AIError so
// the fallback callback and the stage note see the Python vocabulary.
func (e *Engine) aiRewriteFor(ctx context.Context, j *job.DeliveryJob, r *rule.ForwardRule, logger Logger) cleaning.AIRewriter {
	return func(ctx context.Context, text string) (string, error) {
		if strings.TrimSpace(text) == "" {
			// Nothing to rewrite: the Python pipeline feeds the whole
			// body, and an empty body is not a provider call worth
			// spending. (Python's own guard is the transformer's: an
			// empty prompt still bills.)
			return text, nil
		}
		timeout := r.AITimeoutSeconds
		if timeout <= 0 {
			timeout = aiprovider.DefaultTimeout
		}

		promptID, promptVer := e.targetPromptFor(j, r)
		systemPrompt := ""
		userTemplate := "{text}"
		targetLang := "auto"

		if e.promptStore != nil && promptID != "" {
			preview, err := prompt.PreviewPrompt(ctx, e.promptStore, promptID, text, promptVer, r.OwnerUserID)
			if err != nil {
				logger.Warn("could not resolve prompt template",
					"stage", "PIPELINE:AI_REWRITE:PROMPT_WARN",
					"correlation_id", j.Payload.CorrelationID(),
					"rule_id", r.ID,
					"prompt", promptID,
					"error", err.Error(),
				)
			} else if preview != nil {
				systemPrompt = preview.SystemPrompt
				userTemplate = preview.RenderedUserPrompt
				promptVer = preview.Version
				if preview.TargetLanguage != "" {
					targetLang = preview.TargetLanguage
				}
			}
		}

		request := aiprovider.Request{
			Text:           text,
			SystemPrompt:   systemPrompt,
			UserTemplate:   userTemplate,
			PromptID:       promptID,
			PromptVersion:  promptVer,
			TargetLanguage: targetLang,
			TimeoutSeconds: timeout,
			CorrelationID:  j.Payload.CorrelationID(),
			IsCaption:      j.Payload.HasMedia(),
			MaxLength:      1024,
		}
		if j.Payload.HasMedia() {
			// The Python pipeline pins 1024 for a media message
			// (pipeline.py:1748): the rewrite's output IS the caption.
			request.MaxLength = 1024
		} else {
			request.MaxLength = 4096
		}
		logger.Info("initiating AI rewrite",
			"stage", "PIPELINE:AI_REWRITE:START",
			"correlation_id", j.Payload.CorrelationID(),
			"rule_id", r.ID,
			"cfg", r.AIConfigID,
			"prompt", orEmpty(promptID, "default"),
			"prompt_version", promptVer,
			"timeout_s", timeout,
			"caption", j.Payload.HasMedia(),
		)
		if e.aiMetrics != nil {
			e.aiMetrics.AITransform("total")
		}
		resp := e.aiTransformer.Transform(ctx, request, r.AIConfigID, r.AISecondaryConfigID, r.OwnerUserID)
		if resp.Success {
			if e.aiMetrics != nil {
				e.aiMetrics.AITransform("success")
			}
			logger.Info("AI rewrite succeeded",
				"stage", "PIPELINE:AI_REWRITE:SUCCESS",
				"correlation_id", j.Payload.CorrelationID(),
				"rule_id", r.ID,
				"cfg", r.AIConfigID,
				"out_len", len(resp.TransformedText),
				"model", orEmpty(resp.ModelUsed, "unknown"),
				"failover", resp.IsFailover,
				"duration_ms", resp.ExecutionDurationMS,
			)
			return resp.TransformedText, nil
		}
		if e.aiMetrics != nil {
			e.aiMetrics.AITransform("failed")
		}
		logger.Warn("AI transform failed",
			"stage", "PIPELINE:AI_REWRITE:FAILED",
			"correlation_id", j.Payload.CorrelationID(),
			"rule_id", r.ID,
			"cfg", r.AIConfigID,
			"error_code", resp.ErrorCode,
			"error", resp.ErrorMessage,
		)
		return "", &cleaning.AIError{Code: resp.ErrorCode, Message: resp.ErrorMessage}
	}
}

// aiFallbackFor builds the fallback callback: the rule's policy, in the
// Python pipeline's own order (pipeline.py:1817-1884). SEND_ORIGINAL
// keeps the pre-rewrite text; DROP, RETRY and QUARANTINE all leave
// nothing sendable — the delivery layer is what distinguishes re-queue
// from hold from discard, and it reads the classification off the
// pipeline Result.
func (e *Engine) aiFallbackFor(j *job.DeliveryJob, r *rule.ForwardRule, logger Logger) cleaning.AIFallbackPolicy {
	return func(ctx context.Context, original string, classification string, message string) (string, bool) {
		policy := r.AIFallbackPolicy
		logger.Warn("AI failed — applying fallback policy",
			"stage", "PIPELINE:AI_REWRITE:FALLBACK",
			"correlation_id", j.Payload.CorrelationID(),
			"rule_id", r.ID,
			"error_code", classification,
			"policy", policy,
			"in_len", len(original),
		)
		if e.aiMetrics != nil {
			e.aiMetrics.AIFallback(policy)
		}
		switch strings.ToUpper(strings.TrimSpace(policy)) {
		case rule.FallbackSendOriginal:
			return original, true
		default:
			// DROP is the fail-closed default, and RETRY/QUARANTINE are
			// terminal here for the same reason Python returns None for
			// both: the re-queue vs. hold vs. discard decision is the
			// delivery layer's, and it reads Result.AIDrop plus the
			// classification.
			return "", false
		}
	}
}

// orEmpty answers the first non-empty string, for the log fields whose
// "unknown" spelling Python prints when the field is absent.
func orEmpty(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}

// --- the engine's AI wiring ---------------------------------------------- //

// SetAITransformer installs the AI rewrite orchestrator. It is a setter
// rather than a New parameter so the engine's existing construction
// sites (and their tests) stay source-compatible: the AI stage is an
// additive stage, not a new constructor arity — the same rule the dedup
// and counters stores follow.
func (e *Engine) SetAITransformer(t *aiprovider.Transformer, configs aiconfig.Store, metrics AIMetrics) {
	e.aiTransformer = t
	e.aiConfigs = configs
	e.aiMetrics = metrics
}

// SetPromptStore installs the prompt template store for prompt_id resolution.
func (e *Engine) SetPromptStore(ps prompt.Store) {
	e.promptStore = ps
}

func goChatKey(chatID string) string {
	trimmed := strings.TrimSpace(chatID)
	if trimmed == "" {
		return ""
	}
	if n, err := strconv.ParseInt(trimmed, 10, 64); err == nil {
		return strconv.FormatInt(n, 10)
	}
	if f, err := strconv.ParseFloat(trimmed, 64); err == nil {
		return strconv.FormatInt(int64(f), 10)
	}
	return trimmed
}

// aiWiring reports whether the AI stage can run at all: a transformer
// plus the config store it resolves through. Used at startup log time so
// an operator sees one line that says whether the stage is live.
func (e *Engine) aiWiring() bool {
	return e.aiTransformer != nil && e.aiConfigs != nil
}

// AIConfigStore is documented here for the reader of the engine struct:
// the field's type is aiconfig.Store, imported above.
