package unconfigured

import (
	"context"
	"strings"
	"testing"

	"atf/worker/internal/telegraph"
)

// A compile-time shape check: the health-only gateway is a telegraph.Gateway,
// so it can drop into cmd/worker's disabled path.
var _ telegraph.Gateway = (*Gateway)(nil)

// The health-only gateway must fail EVERY operation with the canonical
// non-transient code and a message that names the missing configuration —
// never a nil failure (which reads as "the operation happened") and never a
// panic (which would take the ops endpoint down with it).
func TestEveryOperationFailsClosed(t *testing.T) {
	g := New("internal RPC token is empty: refusing an unauthenticated client")
	ctx := context.Background()
	target := telegraph.BotTarget{TokenID: "t1", OwnerUserID: 7}

	if ok, reason, err := g.IsSessionReady(ctx, "s1"); ok || err != nil || !strings.Contains(reason, "unconfigured") {
		t.Fatalf("IsSessionReady = (%v, %q, %v), want (false, unconfigured reason, nil)", ok, reason, err)
	}

	_, f := g.GetMessage(ctx, "s1", 1, 2)
	check(t, "GetMessage", f, g.reason)
	_, f = g.GetAlbumMessageIds(ctx, "s1", 1, 2)
	check(t, "GetAlbumMessageIds", f, g.reason)
	_, f = g.GetFileExtension(ctx, "s1", 1, 2)
	check(t, "GetFileExtension", f, g.reason)
	_, f = g.DownloadMedia(ctx, "s1", 1, 2, telegraph.MediaHandle{})
	check(t, "DownloadMedia", f, g.reason)
	_, f = g.ReloadMedia(ctx, "s1", 1, 2, telegraph.MediaHandle{}, "photo")
	check(t, "ReloadMedia", f, g.reason)
	_, f = g.PrepareMediaUpload(ctx, "s1", telegraph.MediaHandle{}, 1, 2, "photo")
	check(t, "PrepareMediaUpload", f, g.reason)
	_, f = g.ForwardMessages(ctx, "s1", 1, 2, []int64{3}, 0)
	check(t, "ForwardMessages", f, g.reason)
	_, f = g.CopyMessage(ctx, "s1", 1, 2, 3, "", 0, false)
	check(t, "CopyMessage", f, g.reason)
	_, f = g.CopyMessages(ctx, "s1", 1, 2, []int64{3}, "", 0, false)
	check(t, "CopyMessages", f, g.reason)
	_, f = g.CopyMediaGroup(ctx, "s1", 1, 2, 3, "", 0)
	check(t, "CopyMediaGroup", f, g.reason)
	_, f = g.SendMessage(ctx, "s1", 1, "text", 0)
	check(t, "SendMessage", f, g.reason)
	_, f = g.SendMedia(ctx, "s1", 1, "tok", "cap", "photo", 0)
	check(t, "SendMedia", f, g.reason)
	_, f = g.BotSendText(ctx, target, 1, "text", 0, nil)
	check(t, "BotSendText", f, g.reason)
	_, f = g.BotCopyMessage(ctx, target, 1, 2, 3, "", 0, nil)
	check(t, "BotCopyMessage", f, g.reason)
	_, f = g.BotSendMedia(ctx, target, 1, "tok", "cap", "photo", 0, nil)
	check(t, "BotSendMedia", f, g.reason)
	_, f = g.BrandingStage1(ctx, telegraph.BrandingPost{})
	check(t, "BrandingStage1", f, g.reason)

	ok, username, f := g.BotAvailability(ctx, target)
	if ok || username != "" {
		t.Fatalf("BotAvailability = (%v, %q), want (false, \"\")", ok, username)
	}
	check(t, "BotAvailability", f, g.reason)

	ok, f = g.BotDeleteMessages(ctx, target, 1, []int64{2})
	if ok {
		t.Fatalf("BotDeleteMessages = %v, want false", ok)
	}
	check(t, "BotDeleteMessages", f, g.reason)

	ok, f = g.DeleteMessages(ctx, telegraph.TransportUser, "s1", "b1", 7, 1, []int64{2})
	if ok {
		t.Fatalf("DeleteMessages = %v, want false", ok)
	}
	check(t, "DeleteMessages", f, g.reason)
}

// check asserts one refusal.
func check(t *testing.T, op string, f *telegraph.DeliveryFailure, reason string) {
	t.Helper()
	if f == nil {
		t.Fatalf("%s returned nil failure — a nil reads as \"the operation happened\"", op)
	}
	if f.Code != Code {
		t.Errorf("%s failure code = %q, want %q (message: %s)", op, f.Code, Code, f.Message)
	}
	if f.IsTransient() {
		t.Errorf("%s: %q must not be transient — a missing configuration does not heal by retrying", op, f.Code)
	}
	if !strings.Contains(f.Message, "health-only") {
		t.Errorf("%s refusal must name the health-only state, got: %s", op, f.Message)
	}
	if !strings.Contains(f.Message, reason) {
		t.Errorf("%s refusal must carry the startup reason verbatim, got: %s", op, f.Message)
	}
	if !strings.Contains(f.Message, op) {
		t.Errorf("%s refusal must name the operation that was never attempted, got: %s", op, f.Message)
	}
}
