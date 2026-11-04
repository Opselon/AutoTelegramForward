package ops

// NOTE ON THE IMPORT: this file deliberately does NOT import
// internal/engine/delivery. The ops package is imported BY the queue store
// (its read-only aggregations live there), and the delivery engine imports
// the queue — so ops importing delivery would be a cycle
// (ops → delivery → queue → ops). Instead, the adapter satisfies the
// engine's StageObserver structurally: the compile-time check lives in
// cmd/worker (see wireStageObserver there), where the cycle cannot reach.

// stageHook is the engine's StageObserver surface, copied by hand from
// delivery.StageObserver. The two must stay in sync — the compile-time
// assertion in cmd/worker/main.go is what catches a drift, and the
// "ops tests" cover the mapping itself.
type stageHook interface {
	StageRecord(stage string, latencyMs float64)
	StageRecordSkipped(stage string, latencyMs float64)
	StageFail(stage, msg string)
	StageEnabled(stage string, enabled bool)
	DeliveryOutcome(kind, ruleID string, latencyMs float64)
	RecordError(component, severity, msg string, fields map[string]string)
}

// EngineObserver adapts the Collector to delivery.StageObserver.
//
// WHY AN ADAPTER RATHER THAN THE COLLECTOR ITSELF: the delivery package
// must not import internal/ops (the queue store already does, and the
// engine imports the queue — that would close the cycle). The engine
// therefore declares StageObserver against ITS OWN contract, and this
// small adapter is the one place the two vocabularies meet.
//
// The stage names the engine emits are the contract's fixed 11; the
// collector accepts exactly those (StageRecord ignores anything else), so
// a name that drifts is visible at compile time in cmd/worker, and an
// unknown name is a no-op rather than a crash.
type EngineObserver struct {
	collector *Collector
}

// NewEngineObserver wires the collector as the engine's stage observer.
func NewEngineObserver(c *Collector) *EngineObserver { return &EngineObserver{collector: c} }

// Compile-time guarantee of the LOCAL contract above; the engine-facing
// one lives in cmd/worker (wireStageObserver).
var _ stageHook = (*EngineObserver)(nil)

// StageRecord marks one processed item of the named stage.
func (o *EngineObserver) StageRecord(stage string, latencyMs float64) {
	o.collector.StageRecord(stage, latencyMs)
}

// StageRecordSkipped marks one item the stage declined to pass.
func (o *EngineObserver) StageRecordSkipped(stage string, latencyMs float64) {
	o.collector.StageRecordSkipped(stage, latencyMs)
}

// StageFail records a stage failure.
func (o *EngineObserver) StageFail(stage, msg string) {
	o.collector.StageFail(stage, msg)
}

// StageEnabled sets the stage's configured state.
func (o *EngineObserver) StageEnabled(stage string, enabled bool) {
	o.collector.StageEnabled(stage, enabled)
}

// DeliveryOutcome records one job's terminal outcome together with its
// end-to-end latency (claim → outcome) and feeds the per-task rolling
// throughput/latency under the job's rule.
//
// The outcomeKind vocabulary is the Metrics.Outcome one (sent|retried|
// failed|unknown) — the collector's Outcome method already counted it;
// this call adds the LATENCY and the PER-TASK view, so the counters and
// the rings stay consistent without a second counting site.
func (o *EngineObserver) DeliveryOutcome(kind, ruleID string, latencyMs float64) {
	c := o.collector
	c.ObserveE2E(latencyMs / 1000.0)
	if kind == "sent" {
		c.TaskDelivery(ruleID, latencyMs)
	}
}

// RecordError maps a classified Telegram failure onto the ring through the
// same funnel the log feed uses.
func (o *EngineObserver) RecordError(component, severity, msg string, fields map[string]string) {
	e := LogEntry{
		Severity:      severity,
		Component:     component,
		Message:       msg,
		Code:          fields["code"],
		Reason:        fields["reason_code"],
		RuleID:        fields["rule_id"],
		JobID:         fields["job_id"],
		CorrelationID: fields["correlation_id"],
	}
	if v, ok := fields["error_type"]; ok && e.Code == "" {
		e.Code = v
	}
	o.collector.RecordError(e)
}

