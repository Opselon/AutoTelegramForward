package opsstore_test

import (
	"testing"

	"atf/worker/internal/ops"
)

// testCollectorOps builds the collector the same way the worker's main
// does: identity baked from config, stages enabled for the pipeline the
// process owns.
func testCollectorOps() *ops.Collector {
	c := ops.NewCollector(ops.Config{
		WorkerID:     "w-test",
		CommitSHA:    "82f1982",
		Version:      "1.2.3",
		NumWorkers:   4,
		MaxQueueSize: 1000,
		QueueBackend: "sqlite:/app/data/atf.db",
	})
	for _, stage := range ops.StageNames {
		c.StageEnabled(stage, true)
	}
	return c
}
