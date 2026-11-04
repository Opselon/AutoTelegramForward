package delivery

import (
	"testing"

	"atf/worker/internal/domain/job"
)

// TestOwnsTransformsNullMarkerIsNotATransformVersion is the regression test
// for a real production defect that broke the whole A->B pipeline:
//
// Python's durable_queue._transform_boundary_payload writes
// (raw_text=<text>, transforms_version=None) when ATF_WORKER_TRANSFORMS=1 —
// the None is its "Python did NOT transform this payload" value, and raw_text
// is the signal that the Go Worker must re-run the stage. The Go gate was a
// key-presence check: a payload carrying transforms_version: null was read as
// "already transformed", so the Worker skipped the transform stage. Python had
// already deferred it. Result: NEITHER engine ran the transform — replacement,
// cleaner, header/footer and AI silently did nothing, while every message was
// still logged "delivered".
//
// The marker is only meaningful when present AND non-empty.
func TestOwnsTransformsNullMarkerIsNotATransformVersion(t *testing.T) {
	t.Setenv("ATF_WORKER_TRANSFORMS", "1")
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	e := &Engine{}

	// A Python-enqueued job under ATF_WORKER_TRANSFORMS=1: raw snapshot
	// present, marker present but null. The Worker MUST own the stage.
	py := job.NewPayload(map[string]any{
		"text":               "body",
		"raw_text":           "body",
		"transforms_version": nil,
	})
	if !e.ownsTransforms(&job.DeliveryJob{Payload: *py}) {
		t.Fatal("a payload with transforms_version: null and a raw snapshot must be transformed by the Worker — Python wrote that null, it did not transform anything")
	}

	// A pre-4.2 Python job that DID transform: real version string. The
	// Worker must NOT re-run the stage (double-apply).
	done := job.NewPayload(map[string]any{
		"text":               "transformed body",
		"transforms_version": "4.2",
	})
	if e.ownsTransforms(&job.DeliveryJob{Payload: *done}) {
		t.Fatal("a payload carrying a real transforms_version is Python-transformed and must be delivered as-is")
	}

	// A pre-Stage-4.2 job: neither key. The pipeline always transformed in
	// that world, so this one is Python-owned too.
	legacy := job.NewPayload(map[string]any{
		"text": "legacy body",
	})
	if e.ownsTransforms(&job.DeliveryJob{Payload: *legacy}) {
		t.Fatal("a payload with neither marker predates the boundary and must be delivered as-is")
	}

	// No raw snapshot: nothing to re-run the pipeline against, whatever the
	// marker says.
	noRaw := job.NewPayload(map[string]any{
		"text":               "body",
		"transforms_version": nil,
	})
	if e.ownsTransforms(&job.DeliveryJob{Payload: *noRaw}) {
		t.Fatal("a payload without a raw snapshot has nothing for the transform pipeline to run against")
	}
}
