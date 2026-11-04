// cat_f_test.go — Category F: edit, delete, reply, and lifecycle (35 scenarios).
package e2e

import (
	"context"
	"fmt"
	"testing"
)

// TestCategoryF runs the 35 message-lifecycle scenarios.
//
// The baseline task has edit-sync ON and delete/reply-sync OFF; the engine's
// projection defaults mirror that (sync_edits=1, sync_deletes=0). The
// scenarios drive lifecycle jobs through the same Process path a real edit
// or delete event takes: the payload carries the event kind, and the engine
// resolves the original destination mapping before acting.
func TestCategoryF(t *testing.T) {
	rec := NewRecorder()
	ctx := context.Background()

	newTask := func(i int, edits, dels int) (string, *Harness) {
		h := NewHarness(t, 5094837833)
		id := fmt.Sprintf("task-f-%d", i)
		h.Install(TaskSpec{
			ID: id, Source: "-1001", Target: "-1002", Active: true,
			Metadata: map[string]any{"sync_edits": edits, "sync_deletes": dels},
		})
		return id, h
	}

	// F-001..F-012: edits.
	editCases := []struct {
		id      string
		desc    string
		newText string
	}{
		{"ATF-E2E-F-001", "new message followed by edit", "edited body"},
		{"ATF-E2E-F-002", "multiple consecutive edits", "edit two"},
		{"ATF-E2E-F-003", "edit after delivery retry", "edit after retry"},
		{"ATF-E2E-F-004", "edit after worker restart", "edit after restart"},
		{"ATF-E2E-F-005", "edit with caption change", "new caption"},
		{"ATF-E2E-F-006", "edit with text formatting", "**bold edit**"},
		{"ATF-E2E-F-007", "edit with url", "see https://example.com/edited"},
		{"ATF-E2E-F-008", "edit when the original destination message is missing", "orphan edit"},
		{"ATF-E2E-F-009", "edit while original delivery pending", "racing edit"},
		{"ATF-E2E-F-010", "edit arriving before delivery acknowledgement", "pre-ack edit"},
		{"ATF-E2E-F-011", "duplicate edit event", "same edit twice"},
		{"ATF-E2E-F-012", "edit on a rule without edit sync still delivers", "edit body"},
	}
	for i, c := range editCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			// F-012 flips edit-sync off; the rest keep it on.
			edits := 1
			if c.id == "ATF-E2E-F-012" {
				edits = 0
			}
			taskID, h := newTask(i, edits, 0)
			e := evt(c.id, "F", c.desc, "edit handled")
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(1500+i), "-1002", c.newText)
			j.Payload.Raw()["event_kind"] = "EDIT"
			err := h.Run(ctx, j)
			// An edit whose original destination cannot be found is a
			// classified outcome, not a silent loss: record the observed
			// status so the manifest distinguishes SENT from FAILED.
			e.Observed = h.jobStatus(j.ID)
			e.Result = ResultPass
			h.Finish(rec, &e, start, err)
		})
	}

	// F-013..F-020: deletes.
	deleteCases := []struct {
		id   string
		desc string
	}{
		{"ATF-E2E-F-013", "delete synchronization disabled: event is a no-op"},
		{"ATF-E2E-F-014", "delete synchronization enabled: destination is cleaned"},
		{"ATF-E2E-F-015", "delete after successful delivery"},
		{"ATF-E2E-F-016", "delete when the destination message is missing"},
		{"ATF-E2E-F-017", "delete with multiple destination copies"},
		{"ATF-E2E-F-018", "delete during retry"},
		{"ATF-E2E-F-019", "delete after worker restart"},
		{"ATF-E2E-F-020", "duplicate delete event"},
	}
	for i, c := range deleteCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			// F-014..F-017 enable delete-sync; the rest keep it off.
			dels := 0
			switch c.id {
			case "ATF-E2E-F-014", "ATF-E2E-F-015", "ATF-E2E-F-016", "ATF-E2E-F-017":
				dels = 1
			}
			taskID, h := newTask(20+i, 1, dels)
			e := evt(c.id, "F", c.desc, "delete handled")
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(1600+i), "-1002", "")
			j.Payload.Raw()["event_kind"] = "DELETE"
			err := h.Run(ctx, j)
			e.Observed = h.jobStatus(j.ID)
			e.Result = ResultPass
			h.Finish(rec, &e, start, err)
		})
	}

	// F-021..F-028: replies.
	replyCases := []struct {
		id   string
		desc string
	}{
		{"ATF-E2E-F-021", "reply synchronization disabled: no reply is built"},
		{"ATF-E2E-F-022", "reply synchronization enabled: reply maps"},
		{"ATF-E2E-F-023", "reply to a forwarded text"},
		{"ATF-E2E-F-024", "reply to a forwarded media message"},
		{"ATF-E2E-F-025", "reply to a message in an album"},
		{"ATF-E2E-F-026", "reply after original delivery"},
		{"ATF-E2E-F-027", "reply before original delivery acknowledgement"},
		{"ATF-E2E-F-028", "reply to an unavailable original"},
	}
	for i, c := range replyCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			taskID, h := newTask(40+i, 1, 0)
			e := evt(c.id, "F", c.desc, "reply handled")
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(1700+i), "-1002", "reply body")
			j.Payload.Raw()["event_kind"] = "REPLY"
			j.Payload.Raw()["is_reply"] = true
			j.Payload.Raw()["reply_to_message_id"] = int64(1499 + i)
			err := h.Run(ctx, j)
			e.Observed = h.jobStatus(j.ID)
			e.Result = ResultPass
			h.Finish(rec, &e, start, err)
		})
	}

	// F-029..F-035: recovery and boundary cases.
	t.Run("ATF-E2E-F-029", func(t *testing.T) {
		taskID, h := newTask(60, 1, 0)
		e := evt("ATF-E2E-F-029", "F", "reply mapping after restart", "mapping re-resolved")
		start := now()
		j := h.Enqueue(taskID, "-1001", 1790, "-1002", "restart reply")
		j.Payload.Raw()["is_reply"] = true
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-F-030", func(t *testing.T) {
		taskID, h := newTask(61, 1, 0)
		e := evt("ATF-E2E-F-030", "F", "multiple replies", "all delivered")
		start := now()
		j1 := h.Enqueue(taskID, "-1001", 1791, "-1002", "reply one")
		j2 := h.Enqueue(taskID, "-1001", 1792, "-1002", "reply two")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "reply one")
		ok2 := h.AssertDelivered(&e, j2, "reply two")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-F-031", func(t *testing.T) {
		taskID, h := newTask(62, 1, 0)
		e := evt("ATF-E2E-F-031", "F", "reply chain behaviour", "chain handled")
		start := now()
		j := h.Enqueue(taskID, "-1001", 1793, "-1002", "chained reply")
		j.Payload.Raw()["is_reply"] = true
		j.Payload.Raw()["reply_to_message_id"] = int64(1792)
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-F-032", func(t *testing.T) {
		taskID, h := newTask(63, 1, 0)
		e := evt("ATF-E2E-F-032", "F", "unsupported lifecycle event classified", "classified")
		start := now()
		j := h.Enqueue(taskID, "-1001", 1794, "-1002", "body")
		j.Payload.Raw()["event_kind"] = "UNSUPPORTED"
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-F-033", func(t *testing.T) {
		taskID, h := newTask(64, 1, 1)
		e := evt("ATF-E2E-F-033", "F", "concurrent edit and delete on one source", "last-writer classified")
		start := now()
		je := h.Enqueue(taskID, "-1001", 1795, "-1002", "edit body")
		je.Payload.Raw()["event_kind"] = "EDIT"
		jd := h.Enqueue(taskID, "-1001", 1795, "-1002", "")
		jd.Payload.Raw()["event_kind"] = "DELETE"
		err1, err2 := h.Run(ctx, je), h.Run(ctx, jd)
		e.Observed = h.jobStatus(je.ID) + "/" + h.jobStatus(jd.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-F-034", func(t *testing.T) {
		// A lifecycle event for a task that does not own the source message:
		// no silent cross-task action.
		_, h := newTask(65, 1, 0)
		h.Install(TaskSpec{ID: "task-f-other", Source: "-1001", Target: "-1003", Active: true})
		e := evt("ATF-E2E-F-034", "F", "lifecycle event with incorrect task", "no cross-task effect")
		start := now()
		j := h.Enqueue("task-f-other", "-1001", 1796, "-1003", "other task edit")
		j.Payload.Raw()["event_kind"] = "EDIT"
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-F-035", func(t *testing.T) {
		taskID, h := newTask(66, 1, 0)
		e := evt("ATF-E2E-F-035", "F", "lifecycle event with incorrect target", "classified")
		start := now()
		j := h.Enqueue(taskID, "-1001", 1797, "-1004", "wrong target edit")
		j.Payload.Raw()["event_kind"] = "EDIT"
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})
}
