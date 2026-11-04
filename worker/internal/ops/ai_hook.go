package ops

import "time"

// now is the single clock the AI hook consults — a seam the tests set so
// the provider-outcome timestamps are deterministic without freezing the
// whole package's time.
var now = time.Now

// AIMetricsObserver adapts the Collector to delivery.AIMetrics.
//
// The engine's AI stage reports outcomes in the Python pipeline's own
// vocabulary (ai_transforms_total/success/failed, ai_fallback_<policy>).
// The /ops surface answers the operator's question differently: "does the
// AI stage work?" is settled by SUCCESS ≥ 1 (the contract's "never mark AI
// healthy because the task enables it — healthy only after ≥1 success").
//
// This adapter maps the one onto the other:
//
//	total       → the ai stage processed one unit (its health derivation
//	              needs "processed > 0"), with no success claimed
//	success     → the stage succeeded; a success also clears the
//	              last-failure flag so the stage can read degraded→healthy
//	failed      → the stage failed; the ai stage's health reflects it
//	(plus)     → the provider-outcome probe: a success marks the provider
//	              reachable (ai_connected), the same "last provider
//	              outcome" the contract's connectivity field names
type AIMetricsObserver struct {
	collector *Collector
}

// NewAIMetricsObserver wires the collector as the AI stage's metrics
// recorder.
func NewAIMetricsObserver(c *Collector) *AIMetricsObserver {
	return &AIMetricsObserver{collector: c}
}

// AITransform records one attempted rewrite (total/success/failed).
func (o *AIMetricsObserver) AITransform(outcome string) {
	c := o.collector
	switch outcome {
	case "total":
		// An attempt: processed, with no success or failure claimed yet.
		c.StageRecord("ai", 0)
	case "success":
		c.StageRecord("ai", 0)
		c.RecordAI(true, now())
		c.StageClearFailure("ai")
	case "failed":
		c.StageRecord("ai", 0)
		c.StageFail("ai", "AI_TRANSFORM_FAILED")
		c.RecordAI(false, now())
	}
}

// AIFallback records one fallback decision by policy name.
func (o *AIMetricsObserver) AIFallback(policy string) {
	if policy == "" {
		return
	}
	o.collector.RecordError(LogEntry{
		Severity:  "WARN",
		Component: "worker.delivery.ai",
		Message:   "AI rewrite failed — fallback policy applied",
		Reason:    "AI_FALLBACK_" + policy,
	})
}
