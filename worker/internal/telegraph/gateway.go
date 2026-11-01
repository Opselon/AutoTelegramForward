// Package telegraph is the Go Worker's narrow view of the Python Telegram
// Runtime.
//
// The interface здесь is deliberately NOT the gRPC stub surface: it is the
// business-neutral capability set the delivery engines actually need. The
// gRPC client (infra/telegraph) implements it; the in-process fake
// (fakegateway) implements it for tests. Nothing else is allowed to know a
// protobuf type exists.
//
// Rules of this boundary:
//
//   - Go decides WHAT should happen; Python executes the Telegram-specific
//     operation and reports WHAT Telegram actually said.
//   - Errors crossing this boundary are always *typed* (DeliveryFailure),
//     never raw strings, so the retry policy decides from a stable
//     vocabulary instead of pattern-matching on text.
//   - Session identity is a stable opaque id. Session strings, api hashes
//     and tokens never enter the worker, and never leave the runtime.
//   - Media is staged through a shared scratch volume; nothing streams a
//     whole file through either process's memory.
package telegraph

import "context"

// DeliveryFailure is a normalized Telegram-side failure.
//
// The classification is shared with Python: the runtime maps its pyrogram /
// MTProto errors onto the same vocabulary, so "what happened" survives the
// process boundary unchanged. Go owns the retry decision from there.
type DeliveryFailure struct {
	// Code is the canonical category (FLOOD_WAIT, PERMISSION_DENIED, ...).
	Code string
	// ErrorType is Telegram's native error string when known
	// ("FLOOD_WAIT_X", "PEER_ID_INVALID"), already redacted of secrets.
	ErrorType string
	// ErrorCode is Telegram's native status code when known ("400").
	ErrorCode string
	// RetryAfterSeconds is the wait Telegram demanded (FLOOD_WAIT family).
	// 0 means "not applicable / unknown".
	RetryAfterSeconds int
	// Message is a redacted, operator-readable detail line.
	Message string
}

// IsTransient reports whether the failure is a plausible candidate for a
// retry. It is a *classification*, not the decision: the retry policy in
// internal/engine/delivery decides, and it may refuse even a transient
// error (attempt budget spent, backpressure).
func (f *DeliveryFailure) IsTransient() bool {
	switch f.Code {
	case "FLOOD_WAIT", "RATE_LIMITED", "NETWORK_TIMEOUT", "NETWORK_DISCONNECTED",
		"SESSION_UNAVAILABLE", "TELEGRAM_TEMPORARY_ERROR", "SOURCE_UNAVAILABLE",
		"TARGET_UNAVAILABLE", "INTERNAL_ERROR":
		return true
	}
	return false
}

// IsAmbiguous reports whether the outcome on Telegram's side is unknown —
// the "did it actually send?" window. Retrying blindly here is what produces
// duplicate messages, so the caller records UNKNOWN instead and lets
// reconciliation decide.
func (f *DeliveryFailure) IsAmbiguous() bool {
	switch f.Code {
	case "NETWORK_TIMEOUT", "UNKNOWN_DELIVERY_OUTCOME":
		return true
	}
	return false
}

// IsForwardingRestricted reports whether the source forbids the operation —
// the signal that triggers the copy/re-upload fallback when the task allows
// it.
func (f *DeliveryFailure) IsForwardingRestricted() bool {
	return f.Code == "FORWARDING_RESTRICTED"
}

// MediaHandle is a staged media file inside the shared scratch volume.
type MediaHandle struct {
	Path     string
	FileName string
}

// MessageSnapshot is the business-neutral view of one source message.
type MessageSnapshot struct {
	ChatID       int64
	MessageID    int64
	MediaGroupID string
	HasMedia     bool
	MediaType    string
}

// Gateway is the Telegram transport port.
//
// Every method takes a context (deadlines and cancellation propagate across
// gRPC) and returns either its result or a *DeliveryFailure — never a
// bare error, so a failure inside the runtime cannot be mistaken for a
// local transport fault.
type Gateway interface {
	// IsSessionReady reports whether the runtime can serve MTProto
	// operations for this session *right now*. A connecting session is not
	// ready; the worker defers rather than failing the job.
	IsSessionReady(ctx context.Context, sessionID string) (bool, string, error)

	// --- reads ---------------------------------------------------------- //

	// GetMessage fetches the minimal view of one source message.
	GetMessage(ctx context.Context, sessionID string, chatID, messageID int64) (*MessageSnapshot, *DeliveryFailure)
	// GetAlbumMessageIds resolves every member of an album.
	GetAlbumMessageIds(ctx context.Context, sessionID string, chatID, messageID int64) ([]int64, *DeliveryFailure)
	// GetFileExtension returns the extension of a message's file ("" if none).
	GetFileExtension(ctx context.Context, sessionID string, chatID, messageID int64) (string, *DeliveryFailure)

	// --- media staging --------------------------------------------------- //

	// DownloadMedia streams the message's file to the shared scratch path.
	DownloadMedia(ctx context.Context, sessionID string, chatID, messageID int64, dest MediaHandle) (int64, *DeliveryFailure)
	// ReloadMedia downloads raw bytes for the re-upload fallback.
	ReloadMedia(ctx context.Context, sessionID string, chatID, messageID int64, dest MediaHandle, mediaType string) (int64, *DeliveryFailure)
	// PrepareMediaUpload stages a file for upload and returns an opaque
	// token the runtime turns into a file reference. Empty token = the
	// send must fall back to plain text.
	PrepareMediaUpload(ctx context.Context, sessionID string, source MediaHandle, sourceChatID, sourceMessageID int64, mediaType string) (string, *DeliveryFailure)

	// --- sends (the Telegram side effects) -------------------------------- //

	// ForwardMessages performs a native Telegram forward (original author
	// header preserved). The returned ids align with the request ids.
	ForwardMessages(ctx context.Context, sessionID string, targetChatID, fromChatID int64, messageIDs []int64, threadID int64) ([]int64, *DeliveryFailure)
	// CopyMessage performs a clean copy of one message.
	CopyMessage(ctx context.Context, sessionID string, targetChatID, fromChatID, messageID int64, caption string, threadID int64) (int64, *DeliveryFailure)
	// CopyMessages copies several messages one-for-one.
	CopyMessages(ctx context.Context, sessionID string, targetChatID, fromChatID int64, messageIDs []int64, caption string, threadID int64) ([]int64, *DeliveryFailure)
	// CopyMediaGroup copies an album as a group.
	CopyMediaGroup(ctx context.Context, sessionID string, targetChatID, fromChatID int64, messageID int64, caption string, threadID int64) ([]int64, *DeliveryFailure)
	// SendMessage sends plain transformed text.
	SendMessage(ctx context.Context, sessionID string, targetChatID int64, text string, threadID int64) (int64, *DeliveryFailure)
	// SendMedia uploads a staged file as new media — the re-upload half of
	// the restricted-source fallback. The token is the opaque reference
	// PrepareMediaUpload returned; "" means "stage it as text instead".
	SendMedia(ctx context.Context, sessionID string, targetChatID int64, token, caption, mediaType string, threadID int64) (int64, *DeliveryFailure)

	// --- bot transport ---------------------------------------------------- //
	// Narrow typed Bot Api operations. The worker names a stored token by id
	// and the tenant it belongs to; the runtime resolves, re-checks ownership
	// of, and decrypts the token inside the trusted Python boundary. The raw
	// token never crosses this boundary in either direction.

	// BotAvailability reports whether the named bot is usable by this tenant.
	BotAvailability(ctx context.Context, target BotTarget) (ok bool, botUsername string, ferr *DeliveryFailure)
	// BotSendText sends fully transformed text through the bot, with the
	// task's raw inline-button spec (the runtime parses and degrades it).
	BotSendText(ctx context.Context, target BotTarget, targetChatID int64, text string, threadID int64, inlineButtons []string) (int64, *DeliveryFailure)
	// BotCopyMessage copies one source message through the bot.
	BotCopyMessage(ctx context.Context, target BotTarget, targetChatID, fromChatID, messageID int64, caption string, threadID int64, inlineButtons []string) (int64, *DeliveryFailure)
	// BotSendMedia sends a staged file through the bot (the restricted-source
	// fallback on the bot route). An empty token means degrade to text.
	BotSendMedia(ctx context.Context, target BotTarget, targetChatID int64, token, caption, mediaType string, threadID int64, inlineButtons []string) (int64, *DeliveryFailure)
	// BotDeleteMessages deletes messages through the bot. A message that is
	// already gone is reported deleted: deletion is idempotent cleanup.
	BotDeleteMessages(ctx context.Context, target BotTarget, chatID int64, messageIDs []int64) (bool, *DeliveryFailure)

	// --- deletion (auto-delete scheduler) --------------------------------- //

	// DeleteMessages deletes messages through the session or the bot. A
	// message that is already gone is reported deleted: deletion is an
	// idempotent cleanup operation, never a duplicate of a prior delete.
	DeleteMessages(ctx context.Context, transport DeleteTransport, sessionID, botTokenID string, ownerUserID, chatID int64, messageIDs []int64) (bool, *DeliveryFailure)

	// --- branding stage 1 ------------------------------------------------- //

	// BrandingStage1 posts the processed deliverable into channel C and
	// reports the ids that landed. It never re-runs a transform.
	BrandingStage1(ctx context.Context, post BrandingPost) ([]int64, *DeliveryFailure)
}

// BotTarget is a named, tenant-scoped reference to a stored bot token.
//
// The worker never holds the token itself: the runtime resolves the token id
// through the Core's owner-scoped repository, re-validates that this tenant
// actually owns it, and decrypts it inside the trusted Python boundary.
type BotTarget struct {
	// TokenID is the stored bot token's id (never the raw token).
	TokenID string
	// OwnerUserID is the tenant this delivery belongs to. The runtime
	// re-validates ownership on every call, cache hit included — a token id
	// travels in the job payload and anything that travels can be transplanted.
	OwnerUserID int64
}

// DeleteTransport selects which Telegram identity performs an operation.
type DeleteTransport string

const (
	// TransportUser executes through the owner's MTProto session.
	TransportUser DeleteTransport = "user"
	// TransportBot executes through the stored bot token.
	TransportBot DeleteTransport = "bot"
)

// BrandingPost is one "post the processed deliverable into C" request.
//
// The content decision (what C receives per content_mode) is Go's; the
// runtime only executes the low-level Telegram send and reports the ids.
type BrandingPost struct {
	// Transport selects who executes the post ("user" or "bot").
	Transport DeleteTransport
	// SessionID for transport="user" (stable id, never a session string).
	SessionID string
	// BotTokenID for transport="bot" (stored token id, never the token).
	BotTokenID string
	// OwnerUserID is the tenant the hop belongs to.
	OwnerUserID int64
	// TargetChatID is channel C.
	TargetChatID int64
	// ThreadID is the forum topic inside C (0 = General / none).
	ThreadID int64
	// Operation is the deliverable shape Go decided:
	// "text" | "copy" | "media_group".
	Operation string
	// SourceChatID / SourceMessageID identify the message to copy.
	SourceChatID    int64
	SourceMessageID int64
	// SourceMessageIDs lists every album member for "media_group".
	SourceMessageIDs []int64
	// Text is the fully transformed text for operation="text".
	Text string
	// Caption is the transformed caption for "copy"/"media_group".
	Caption string
	// InlineButtons is the raw per-task button spec (bot transport only).
	InlineButtons []string
}
