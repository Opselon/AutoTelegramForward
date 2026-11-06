// Package unconfigured is the health-only telegraph.Gateway of a worker
// that cannot reach the Telegram runtime.
//
// The worker container always runs — even before the operator opts into Go
// execution — so it can report health. A worker in that state has no usable
// Telegram transport: ATF_CORE_RPC_TOKEN is empty, so the real gRPC client
// refuses to construct ("refusing an unauthenticated client"). Rather than
// refusing to START — which would leave the ops endpoint dark, the API's
// health payload degraded, and the container restart-looping (the crash
// loop the v2.5.0 clean-install gate caught) — the process boots with this
// gateway: every operation fails closed with a failure that names the
// missing configuration and the operation that was never attempted.
//
// The delivery engine never reaches it while ATF_WORKER_ENABLED is off.
// The moment the operator enables the worker, an unconfigured token is
// fatal at startup again — fail closed, on purpose.
//
// Like the fake gateway, this is never a substitute for a real delivery:
// nothing sent through it happened.
package unconfigured

import (
	"context"

	"atf/worker/internal/telegraph"
)

// Code is the canonical failure category for an operation that was refused
// because the worker has no Telegram transport configured. Not in
// DeliveryFailure.IsTransient: a missing configuration does not heal by
// retrying.
const Code = "TRANSPORT_UNCONFIGURED"

// Gateway is the telegraph.Gateway of a health-only worker.
type Gateway struct {
	// reason is the startup error the real client refused with, surfaced
	// verbatim so the operator reads the actual cause, not a paraphrase.
	reason string
}

// New returns a Gateway whose every operation fails with reason.
func New(reason string) *Gateway {
	return &Gateway{reason: reason}
}

// refusal builds the failure every operation returns. The operation name is
// spelled out so a log line or Log Center row answers "what did NOT happen"
// without reading the stack.
func (g *Gateway) refusal(op string) *telegraph.DeliveryFailure {
	return &telegraph.DeliveryFailure{
		Code: Code,
		Message: "worker runs health-only: Telegram transport unconfigured (" +
			g.reason + ") — " + op + " never attempted",
	}
}

// IsSessionReady reports the transport as never ready: a connecting session
// is not ready either, so callers defer rather than fail the job.
func (g *Gateway) IsSessionReady(_ context.Context, _ string) (bool, string, error) {
	return false, "transport unconfigured (" + g.reason + ")", nil
}

// --- reads ------------------------------------------------------------- //

// GetMessage is refused: see the package comment.
func (g *Gateway) GetMessage(_ context.Context, _ string, _, _ int64) (*telegraph.MessageSnapshot, *telegraph.DeliveryFailure) {
	return nil, g.refusal("GetMessage")
}

// GetAlbumMessageIds is refused: see the package comment.
func (g *Gateway) GetAlbumMessageIds(_ context.Context, _ string, _, _ int64) ([]int64, *telegraph.DeliveryFailure) {
	return nil, g.refusal("GetAlbumMessageIds")
}

// GetFileExtension is refused: see the package comment.
func (g *Gateway) GetFileExtension(_ context.Context, _ string, _, _ int64) (string, *telegraph.DeliveryFailure) {
	return "", g.refusal("GetFileExtension")
}

// --- media staging ------------------------------------------------------ //

// DownloadMedia is refused: see the package comment.
func (g *Gateway) DownloadMedia(_ context.Context, _ string, _, _ int64, _ telegraph.MediaHandle) (int64, *telegraph.DeliveryFailure) {
	return 0, g.refusal("DownloadMedia")
}

// ReloadMedia is refused: see the package comment.
func (g *Gateway) ReloadMedia(_ context.Context, _ string, _, _ int64, _ telegraph.MediaHandle, _ string) (int64, *telegraph.DeliveryFailure) {
	return 0, g.refusal("ReloadMedia")
}

// PrepareMediaUpload is refused: see the package comment.
func (g *Gateway) PrepareMediaUpload(_ context.Context, _ string, _ telegraph.MediaHandle, _, _ int64, _ string) (string, *telegraph.DeliveryFailure) {
	return "", g.refusal("PrepareMediaUpload")
}

// --- sends (the Telegram side effects) ---------------------------------- //

// ForwardMessages is refused: see the package comment.
func (g *Gateway) ForwardMessages(_ context.Context, _ string, _, _ int64, _ []int64, _ int64) ([]int64, *telegraph.DeliveryFailure) {
	return nil, g.refusal("ForwardMessages")
}

// CopyMessage is refused: see the package comment.
func (g *Gateway) CopyMessage(_ context.Context, _ string, _, _, _ int64, _ string, _ int64, _ bool) (int64, *telegraph.DeliveryFailure) {
	return 0, g.refusal("CopyMessage")
}

// CopyMessages is refused: see the package comment.
func (g *Gateway) CopyMessages(_ context.Context, _ string, _, _ int64, _ []int64, _ string, _ int64, _ bool) ([]int64, *telegraph.DeliveryFailure) {
	return nil, g.refusal("CopyMessages")
}

// CopyMediaGroup is refused: see the package comment.
func (g *Gateway) CopyMediaGroup(_ context.Context, _ string, _, _ int64, _ int64, _ string, _ int64) ([]int64, *telegraph.DeliveryFailure) {
	return nil, g.refusal("CopyMediaGroup")
}

// SendMessage is refused: see the package comment.
func (g *Gateway) SendMessage(_ context.Context, _ string, _ int64, _ string, _ int64) (int64, *telegraph.DeliveryFailure) {
	return 0, g.refusal("SendMessage")
}

// SendMedia is refused: see the package comment.
func (g *Gateway) SendMedia(_ context.Context, _ string, _ int64, _, _, _ string, _ int64) (int64, *telegraph.DeliveryFailure) {
	return 0, g.refusal("SendMedia")
}

// --- bot transport ------------------------------------------------------ //

// BotAvailability is refused: see the package comment.
func (g *Gateway) BotAvailability(_ context.Context, _ telegraph.BotTarget) (bool, string, *telegraph.DeliveryFailure) {
	return false, "", g.refusal("BotAvailability")
}

// BotSendText is refused: see the package comment.
func (g *Gateway) BotSendText(_ context.Context, _ telegraph.BotTarget, _ int64, _ string, _ int64, _ []string) (int64, *telegraph.DeliveryFailure) {
	return 0, g.refusal("BotSendText")
}

// BotCopyMessage is refused: see the package comment.
func (g *Gateway) BotCopyMessage(_ context.Context, _ telegraph.BotTarget, _, _, _ int64, _ string, _ int64, _ []string) (int64, *telegraph.DeliveryFailure) {
	return 0, g.refusal("BotCopyMessage")
}

// BotSendMedia is refused: see the package comment.
func (g *Gateway) BotSendMedia(_ context.Context, _ telegraph.BotTarget, _ int64, _, _, _ string, _ int64, _ []string) (int64, *telegraph.DeliveryFailure) {
	return 0, g.refusal("BotSendMedia")
}

// BotDeleteMessages is refused: see the package comment.
func (g *Gateway) BotDeleteMessages(_ context.Context, _ telegraph.BotTarget, _ int64, _ []int64) (bool, *telegraph.DeliveryFailure) {
	return false, g.refusal("BotDeleteMessages")
}

// --- deletion (auto-delete scheduler) ----------------------------------- //

// DeleteMessages is refused: see the package comment.
func (g *Gateway) DeleteMessages(_ context.Context, _ telegraph.DeleteTransport, _, _ string, _, _ int64, _ []int64) (bool, *telegraph.DeliveryFailure) {
	return false, g.refusal("DeleteMessages")
}

// --- branding stage 1 ---------------------------------------------------- //

// BrandingStage1 is refused: see the package comment.
func (g *Gateway) BrandingStage1(_ context.Context, _ telegraph.BrandingPost) ([]int64, *telegraph.DeliveryFailure) {
	return nil, g.refusal("BrandingStage1")
}
