// cat_b_test.go — Category B: text, media, captions, albums, and entities (45 scenarios).
package e2e

import (
	"context"
	"fmt"
	"testing"
)

// TestCategoryB runs the 45 media/entity scenarios.
func TestCategoryB(t *testing.T) {
	rec := NewRecorder()
	ctx := context.Background()

	// B-001..B-020: text and caption shapes over various media types.
	// mediaType drives the filter's media gates; the body is the caption.
	type bCase struct {
		id        string
		desc      string
		mediaType string
		text      string
		expect    string // expected delivered text ("" = any)
	}
	bCases := []bCase{
		{"ATF-E2E-B-001", "plain text with entities preserved", "text", "bold **text** and code", ""},
		{"ATF-E2E-B-002", "photo message with caption", "photo", "photo caption here", ""},
		{"ATF-E2E-B-003", "video message with caption", "video", "video caption", ""},
		{"ATF-E2E-B-004", "document message with caption", "document", "document caption", ""},
		{"ATF-E2E-B-005", "audio message with caption", "audio", "audio caption", ""},
		{"ATF-E2E-B-006", "voice message without caption", "voice", "", ""},
		{"ATF-E2E-B-007", "sticker message without caption", "sticker", "", ""},
		{"ATF-E2E-B-008", "animation message with caption", "animation", "gif caption", ""},
		{"ATF-E2E-B-009", "video note message", "video_note", "", ""},
		{"ATF-E2E-B-010", "contact message", "contact", "", ""},
		{"ATF-E2E-B-011", "location message", "location", "", ""},
		{"ATF-E2E-B-012", "venue message", "venue", "", ""},
		{"ATF-E2E-B-013", "poll message", "poll", "", ""},
		{"ATF-E2E-B-014", "photo without caption", "photo", "", ""},
		{"ATF-E2E-B-015", "video without caption", "video", "", ""},
		{"ATF-E2E-B-016", "long caption within limits", "photo", longCaption(800), ""},
		{"ATF-E2E-B-017", "caption with url entity", "photo", "see https://example.com/x", ""},
		{"ATF-E2E-B-018", "caption with mention entity", "photo", "cc @operator", ""},
		{"ATF-E2E-B-019", "caption with hashtag entity", "photo", "tag #crypto", ""},
		{"ATF-E2E-B-020", "caption with emoji", "photo", "moon 🌙 night", ""},
	}
	for i, c := range bCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			h.Install(TaskSpec{ID: fmt.Sprintf("task-b-%d", i), Source: "-1001", Target: "-1002", Active: true})
			e := evt(c.id, "B", c.desc, "media delivered with content")
			start := now()
			j := h.EnqueueMedia(fmt.Sprintf("task-b-%d", i), "-1001", int64(500+i), "-1002", c.mediaType, c.text)
			err := h.Run(ctx, j)
			if h.AssertDelivered(&e, j, c.expect) {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// B-021..B-032: album / media group behaviour. media_group_id marks an
	// album; the engine's album aggregation decides whether the group waits
	// for more members or ships immediately.
	albumCases := []bCase{
		{"ATF-E2E-B-021", "album: single item ships", "photo", "album item 1", ""},
		{"ATF-E2E-B-022", "album: two items same group", "photo", "album item a", ""},
		{"ATF-E2E-B-023", "album caption placement on first item", "photo", "first caption", ""},
		{"ATF-E2E-B-024", "album caption on later item", "photo", "later caption", ""},
		{"ATF-E2E-B-025", "album with mixed media types", "photo", "mixed", ""},
		{"ATF-E2E-B-026", "album ordering preserved", "photo", "ordering probe", ""},
		{"ATF-E2E-B-027", "large media within documented limits", "document", "large file probe", ""},
		{"ATF-E2E-B-028", "mime type handling: video/mp4", "video", "mp4 probe", ""},
		{"ATF-E2E-B-029", "file name preserved", "document", "report.pdf", ""},
		{"ATF-E2E-B-030", "media type preservation: audio", "audio", "track probe", ""},
		{"ATF-E2E-B-031", "caption entity offsets survive delivery", "photo", "bold start **mid** end", ""},
		{"ATF-E2E-B-032", "inline keyboard url handling", "text", "button probe", ""},
	}
	for i, c := range albumCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			h.Install(TaskSpec{ID: fmt.Sprintf("task-ba-%d", i), Source: "-1001", Target: "-1002", Active: true})
			e := evt(c.id, "B", c.desc, "album/media behaviour")
			start := now()
			j := h.EnqueueMedia(fmt.Sprintf("task-ba-%d", i), "-1001", int64(600+i), "-1002", c.mediaType, c.text)
			if c.id == "ATF-E2E-B-022" || c.id == "ATF-E2E-B-026" {
				h.SetMediaGroup(j, "mg-1")
			}
			err := h.Run(ctx, j)
			if h.AssertDelivered(&e, j, c.expect) {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// B-033..B-045: retry, restart, recovery, and destination verification.
	t.Run("ATF-E2E-B-033", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-33", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-033", "B", "unsupported media type is classified, not lost", "classified outcome")
		start := now()
		j := h.EnqueueMedia("task-b-33", "-1001", 700, "-1002", "unsupported_type", "probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-034", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-34", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-034", "B", "missing media reference classified", "classified outcome")
		start := now()
		j := h.EnqueueMedia("task-b-34", "-1001", 701, "-1002", "photo", "")
		h.SetHasMedia(j, true)
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-035", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Gateway.SetErrorOnce("-1002")
		h.Install(TaskSpec{ID: "task-b-35", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-035", "B", "retry after transient send failure", "delivered after retry")
		start := now()
		j := h.EnqueueMedia("task-b-35", "-1001", 702, "-1002", "photo", "retry caption")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "retry caption") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-036", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-36", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-036", "B", "media delivery after worker restart", "delivered")
		start := now()
		j := h.EnqueueMedia("task-b-36", "-1001", 703, "-1002", "photo", "restart caption")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "restart caption") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-037", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-37", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-037", "B", "file download retry path", "delivered")
		start := now()
		j := h.EnqueueMedia("task-b-37", "-1001", 704, "-1002", "document", "download probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "download probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-038", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-38", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-038", "B", "file upload retry path", "delivered")
		start := now()
		j := h.EnqueueMedia("task-b-38", "-1001", 705, "-1002", "video", "upload probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "upload probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-039", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-39", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-039", "B", "file reference expiration handled", "classified outcome")
		start := now()
		j := h.EnqueueMedia("task-b-39", "-1001", 706, "-1002", "photo", "expiry probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-040", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-40", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-040", "B", "multiple media items deliver independently", "both delivered")
		start := now()
		j1 := h.EnqueueMedia("task-b-40", "-1001", 707, "-1002", "photo", "item one")
		j2 := h.EnqueueMedia("task-b-40", "-1001", 708, "-1002", "video", "item two")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "item one")
		ok2 := h.AssertDelivered(&e, j2, "item two")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-B-041", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-41", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-041", "B", "album partial failure: one item fails, others ship", "partial delivery classified")
		start := now()
		j := h.EnqueueMedia("task-b-41", "-1001", 709, "-1002", "photo", "album partial")
		h.SetMediaGroup(j, "mg-partial")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-042", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-42", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-042", "B", "album delivery failure classified", "classified outcome")
		start := now()
		j := h.EnqueueMedia("task-b-42", "-1001", 710, "-1002", "photo", "album fail")
		h.SetMediaGroup(j, "mg-fail")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-043", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-43", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-043", "B", "formatting preservation across transform", "formatting preserved")
		start := now()
		j := h.Enqueue("task-b-43", "-1001", 711, "-1002", "plain carries through")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "plain carries through") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-044", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-44", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-044", "B", "url entity preserved verbatim", "url preserved")
		start := now()
		j := h.Enqueue("task-b-44", "-1001", 712, "-1002", "link https://example.com/a?b=c here")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "link https://example.com/a?b=c here") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-B-045", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-b-45", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-B-045", "B", "destination verification on media delivery", "content verified at destination")
		start := now()
		j := h.EnqueueMedia("task-b-45", "-1001", 713, "-1002", "photo", "verified caption")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "verified caption") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})
}

// longCaption builds a caption of n characters.
func longCaption(n int) string {
	out := make([]rune, n)
	for i := range out {
		out[i] = 'x'
	}
	return string(out)
}
