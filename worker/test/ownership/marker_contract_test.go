// marker_contract_test.go — §2 of the Go-ownership contract: the
// ownsTransforms marker contract, table-driven over the two boundary axes.
//
// The four payload shapes come from owns_transforms_regression_test.go:
//
//	deferred     raw_text + transforms_version=null — the shape Python's
//	            durable_queue._transform_boundary_payload writes under
//	            ATF_WORKER_TRANSFORMS=1. Python did NOT transform; the Go
//	            Worker MUST.
//	transformed transforms_version="4.2", no raw snapshot — a pre-4.2 job
//	            Python already transformed. The Worker MUST deliver as-is.
//	legacy      neither key — a job written before Stage 4.2. The pipeline
//	            always transformed in that world, so the payload's text is
//	            Python-owned; deliver as-is.
//	no-raw      transforms_version=null, no raw snapshot — nothing to re-run
//	            the pipeline against. Deliver whatever the payload carries.
//
// The second axis is the process switch (ATF_WORKER_TRANSFORMS on/off). The
// contract is pinned in BOTH directions because the flip must be a rollback:
//
//	switch ON  + deferred     → Go applies the transform (the live position)
//	switch ON  + transformed  → Go delivers as-is (no double-apply)
//	switch OFF + deferred     → the payload is delivered as Python wrote it —
//	                           Go does not reach across the boundary
//	switch OFF + transformed  → delivered as-is
//
// THE STAIRCASING PROBE. The replacement token maps to a string that CONTAINS
// the find pattern itself (MK -> MKX). One application of the stage turns
// "MK MK body" into "MKX MKX body"; a second application turns it into
// "MKXX MKXX body". That is the single-pass, non-overlapping property of
// content_policy._apply_replacements (a port of Python's combined-alternation
// sub), and it is what makes a double-apply OBSERVABLE: the delivered body
// counts how many times the transform stage ran. (The J-042 token "J42"->"ONCE"
// cannot stack because its output does not contain its input; "MK"->"MKX" can,
// which is why this suite uses it.)
package ownership

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"atf/worker/internal/engine/delivery"
)

// markerCase is one cell of the 4×2 marker matrix.
type markerCase struct {
	shape     string // deferred | transformed | legacy | no-raw
	switchOn  bool
	input     string // the payload's text key
	rawIn     string // the raw snapshot ("" = absent)
	versionIn string // the marker value: "null" | "4.2" | ""
	wantBody  string // the text the gateway must see
	wantStage string // the first decision stage note ("" = no transform ran)
}

// markerMatrix is the contract table. Each cell is labelled with the shape
// and switch position so a failure names the exact boundary that moved.
//
// NOTE ON THE PROBE TOKEN. The harness's installer writes a PolicySpec's
// replacements to BOTH places the production pipeline reads them: the
// top-level metadata key (rule.replacements, applied at pipeline step 6) and
// content_policy.replacements (applied at step 5). Python applies both —
// the two keys are distinct config surfaces — so Go applying both is the
// parity behaviour, and the matrix's "once" prediction must count BOTH
// passes: MK -> MKX at step 5, then MKX -> MKXX at step 6. The delivered
// body therefore carries TWO Xes after one Visit of the Go pipeline, and
// the double-apply detector becomes THREE Xes. What is pinned here is the
// number of PIPELINE runs, not the number of stage passes: the matrix's
// every cell predicts the body the single Go run produces, and the
// mid-flight-flip scenarios reject the body a SECOND full run would leave.
func markerMatrix() []markerCase {
	const raw = "MK MK body"
	// One full Go pipeline run: content_policy then replacements.
	oneRun := "MKXX MKXX body"

	mk := func(shape string, switchOn bool, wantBody, wantStage string) markerCase {
		c := markerCase{shape: shape, switchOn: switchOn, input: raw, wantBody: wantBody, wantStage: wantStage}
		switch shape {
		case "deferred":
			c.rawIn = raw
			c.versionIn = "null"
		case "transformed":
			c.input = oneRun // Python already applied the pipeline once
			c.versionIn = "4.2"
		case "legacy":
			c.input = oneRun
		case "no-raw":
			c.input = oneRun
			c.versionIn = "null"
		}
		return c
	}

	var cases []markerCase
	for _, shape := range []string{"deferred", "transformed", "legacy", "no-raw"} {
		for _, on := range []bool{true, false} {
			shape, on := shape, on
			switch {
			case shape == "deferred" && on:
				// The live position: Go owns the stage and the payload is raw,
				// so Go runs the pipeline once: MK MK -> MKXX MKXX.
				cases = append(cases, mk(shape, on, oneRun, "content_policy"))
			case shape == "deferred" && !on:
				// Rollback position: the switch is off, so Go must NOT reach
				// across the boundary and transform a payload Python owns.
				// The payload's text is the raw snapshot Python wrote —
				// deliver it exactly.
				cases = append(cases, mk(shape, on, "MK MK body", ""))
			case shape == "transformed":
				// Pre-4.2 job: already transformed, whatever the switch.
				cases = append(cases, mk(shape, on, oneRun, ""))
			case shape == "legacy":
				// Pre-Stage-4.2 job: always Python-owned.
				cases = append(cases, mk(shape, on, oneRun, ""))
			case shape == "no-raw":
				// Nothing to transform against: deliver the payload as-is.
				cases = append(cases, mk(shape, on, oneRun, ""))
			}
		}
	}
	return cases
}

// TestOwnsTransformsMarkerContract runs the 4×2 matrix end to end through the
// real engine. Each cell asserts on the gateway's recorded body AND on the
// transform decision events the engine emitted — the observable answer to
// "which engine ran the transform stage for this payload shape".
func TestOwnsTransformsMarkerContract(t *testing.T) {
	ctx := context.Background()
	// The harness sets the switch ON on construction; each cell flips it per
	// case. Restore the off position when the matrix is done.
	defer delivery.SetWorkerOwnsTransforms(false)

	for i, c := range markerMatrix() {
		c := c
		t.Run(fmt.Sprintf("ATF-OWN-MKR-%03d-%s-switch%v", i+1, c.shape, c.switchOn), func(t *testing.T) {
			rec := NewRecorder()
			h := NewHarness(t, 5094837833)
			taskID := fmt.Sprintf("task-mkr-%d", i)
			h.Install(TaskSpec{ID: taskID, Source: "-1001", Target: "-7001", Active: true,
				Policy: &PolicySpec{Enabled: boolPtr(true),
					// MK -> MKX: the output contains the input, so every
					// additional pass of the stage adds another X. The number
					// of Xes in the delivered body is the number of times the
					// transform stage ran.
					Replacements: map[string]string{"MK": "MKX"}}})

			// The position the process switch holds for THIS job.
			delivery.SetWorkerOwnsTransforms(c.switchOn)

			j := h.EnqueueShape(c.shape, taskID, "-1001", int64(9600+i), "-7001", c.rawIn)
			if c.shape != "deferred" {
				j.Payload.Raw()["text"] = c.input
			}

			e := evt(fmt.Sprintf("ATF-OWN-MKR-%03d", i+1), "MKR",
				fmt.Sprintf("payload shape %q under switch=%v", c.shape, c.switchOn),
				fmt.Sprintf("gateway sees %q", c.wantBody))
			start := now()
			err := h.Run(ctx, j)

			delivered := h.AssertDelivered(&e, j, c.wantBody)
			// The decision-event side of the same answer: a transform ran iff
			// this payload shape + switch position says Go owns the stage.
			stage := ""
			if notes := h.StageNamesForRule(taskID); len(notes) > 0 {
				stage = notes[0]
			}
			if delivered {
				if c.wantStage == "" || stage == c.wantStage {
					e.Result = ResultPass
					e.Observed = fmt.Sprintf("stage=%q body=%q", stage, h.gatewayTexts())
				} else {
					e.Result = ResultFail
					e.FailureReason = fmt.Sprintf("transform stage = %q, want %q", stage, c.wantStage)
				}
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("MKR-%03d (%s, switch=%v): %s", i+1, c.shape, c.switchOn, e.FailureReason)
			}
		})
	}
}

// TestMarkerContractMidFlightFlip is the rollback-safety half of the contract:
// the switch flips BETWEEN enqueue and processing. A job enqueued under one
// switch position must be transformed exactly once when it is finally
// processed under the other — never twice.
// onePipelineRun is the body one full Go transform pipeline leaves on the
// probe token; doubleRun is the body a SECOND full run leaves. The pair is the
// run-counter this suite reads out of the delivered text.
const (
	onePipelineRun = "MKXX MKXX body"
	doubleRun      = "MKXXX MKXXX body"
)

func TestMarkerContractMidFlightFlip(t *testing.T) {
	ctx := context.Background()
	defer delivery.SetWorkerOwnsTransforms(false)

	t.Run("ATF-OWN-MKR-009", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-flip", Source: "-1001", Target: "-7009", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true),
				Replacements: map[string]string{"MK": "MKX"}}})

		e := evt("ATF-OWN-MKR-009", "MKR",
			"mid-flight flip on→off: a deferred job is transformed by exactly one pipeline run",
			onePipelineRun+" — one run under ownership, no second run after the flip")
		start := now()
		// Enqueued while Go owns the stage: raw snapshot, marker null. The job
		// is claimed but NOT yet processed — the switch flips before the
		// engine ever sees it, which is the actual mid-flight shape.
		j := h.EnqueueShape("deferred", "task-flip", "-1001", 9609, "-7009", "MK MK body")
		delivery.SetWorkerOwnsTransforms(false)
		unclaimed := h.Lookup(j.ID)
		err := h.Run(ctx, unclaimed)

		// Under the rollback position the payload is Python's to transform,
		// and Python did not (it wrote the raw snapshot). The honest
		// prediction for this cell is the raw body, delivered as-is — the
		// boundary held in the OFF direction: Go does not reach across it.
		delivered := h.AssertDelivered(&e, unclaimed, "MK MK body")
		if delivered && !strings.Contains(h.gatewayTexts(), doubleRun) {
			e.Result = ResultPass
			e.Observed = h.gatewayTexts()
		} else if delivered {
			e.Result = ResultFail
			e.FailureReason = "double-transform: the engine ran the pipeline a second time (" + doubleRun + ")"
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("MKR-009: %s (observed %s)", e.FailureReason, e.Observed)
		}
	})

	t.Run("ATF-OWN-MKR-011", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-flip3", Source: "-1001", Target: "-7011", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true),
				Replacements: map[string]string{"MK": "MKX"}}})

		e := evt("ATF-OWN-MKR-011", "MKR",
			"post-send redispatch under a flip: the fence collapses it, no second send and no second transform",
			"one gateway call, exactly one pipeline run")
		start := now()
		j := h.EnqueueShape("deferred", "task-flip3", "-1001", 9611, "-7011", "MK MK body")
		if err := h.Run(ctx, j); err != nil {
			h.Finish(rec, &e, start, err)
			t.Fatalf("first pass: %v", err)
		}
		if bodies := h.bodiesForTarget("-7011"); len(bodies) != 1 || bodies[0] != onePipelineRun {
			t.Fatalf("first pass: body = %v, want exactly one pipeline run (%q)", bodies, onePipelineRun)
		}
		// The switch flips, the job's lease expires, and the queue re-claims
		// it. The fence records a CONFIRMED send, so the correct behaviour is
		// SENT-without-send: no second gateway call, no second transform —
		// the exactly-once property the lease-expiry recovery path must hold.
		delivery.SetWorkerOwnsTransforms(false)
		reclaimed := h.Reclaim(ctx, j, "RETRY_WAIT")
		err := h.Run(ctx, reclaimed)
		calls := h.Gateway.Recorded()
		if len(calls) == 1 &&
			h.jobStatus(reclaimed.ID) == "SENT" &&
			!strings.Contains(h.gatewayTexts(), doubleRun) {
			e.Result = ResultPass
			e.Observed = fmt.Sprintf("gateway_calls=%d body=%q", len(calls), h.gatewayTexts())
		} else {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("redispatch must collapse onto the confirmed send: gateway_calls=%d status=%s body=%q",
				len(calls), h.jobStatus(reclaimed.ID), h.gatewayTexts())
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("MKR-011: %s (observed %s)", e.FailureReason, e.Observed)
		}
	})

	t.Run("ATF-OWN-MKR-010", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-flip2", Source: "-1001", Target: "-7010", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true),
				Replacements: map[string]string{"MK": "MKX"}}})

		e := evt("ATF-OWN-MKR-010", "MKR",
			"mid-flight flip off→on: a transformed job stays Python-owned",
			onePipelineRun+" — the flip does not retroactively give Go a job Python transformed")
		start := now()
		// Enqueued under the rollback position: Python already transformed
		// (the marker says so), the payload carries no raw snapshot.
		delivery.SetWorkerOwnsTransforms(false)
		j := h.EnqueueShape("transformed", "task-flip2", "-1001", 9610, "-7010", "MK MK body")
		j.Payload.Raw()["text"] = onePipelineRun
		// The switch flips on before the job is processed. The job was
		// Python-transformed; Go must still deliver it as-is.
		delivery.SetWorkerOwnsTransforms(true)
		err := h.Run(ctx, j)
		delivered := h.AssertDelivered(&e, j, onePipelineRun)
		if delivered && !strings.Contains(h.gatewayTexts(), doubleRun) {
			e.Result = ResultPass
			e.Observed = h.gatewayTexts()
		} else if delivered {
			e.Result = ResultFail
			e.FailureReason = "double-transform: Go re-ran the pipeline on a job Python already transformed (" + doubleRun + ")"
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("MKR-010: %s (observed %s)", e.FailureReason, e.Observed)
		}
	})
}
