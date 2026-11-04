package ownership

import (
	"context"
	"fmt"
	"testing"
)

func TestProbeMeta(t *testing.T) {
	h := NewHarness(t, 5094837833)
	h.Install(TaskSpec{ID: "task-mkr-p", Source: "-1001", Target: "-7001", Active: true,
		Policy: &PolicySpec{Enabled: boolPtr(true),
			Replacements: map[string]string{"MK": "MKX"}}})
	var meta, reps string
	h.DB().QueryRow(`SELECT metadata, replacements FROM forward_rules WHERE id='task-mkr-p'`).Scan(&meta, &reps)
	fmt.Printf("meta=%s\nreps=%s\n", meta, reps)
	j := h.EnqueueShape("deferred", "task-mkr-p", "-1001", 1, "-7001", "MK MK body")
	err := h.Run(context.Background(), j)
	t.Logf("err=%v body=%v stages=%v", err, h.bodiesForTarget("-7001"), h.StageNamesForRule("task-mkr-p"))
}
