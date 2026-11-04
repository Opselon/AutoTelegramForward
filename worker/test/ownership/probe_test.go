package ownership

import (
	"context"
	"testing"

	"atf/worker/internal/engine/delivery"
)

func TestProbeReclaim3(t *testing.T) {
	h := NewHarness(t, 5094837833)
	h.Install(TaskSpec{ID: "task-flip", Source: "-1001", Target: "-7009", Active: true,
		Policy: &PolicySpec{Enabled: boolPtr(true),
			Replacements: map[string]string{"MK": "MKX"}}})
	j := h.EnqueueShape("deferred", "task-flip", "-1001", 9609, "-7009", "MK MK body")
	_ = h.Run(context.Background(), j)
	t.Logf("pass1 status=%s", h.jobStatus(j.ID))
	h.ResetCalls()
	delivery.SetWorkerOwnsTransforms(false)
	r := h.Reclaim(context.Background(), j, "RETRY_WAIT")
	t.Logf("reclaimed: status=%s claimedFrom=%v", r.Status, r.ClaimedFrom)
	err2 := h.Run(context.Background(), r)
	t.Logf("pass2: err=%v status=%s calls=%d", err2, h.jobStatus(r.ID), len(h.Gateway.Recorded()))
}
