// Package fakegateway is a deterministic, TEST-ONLY telegraph.Gateway.
//
// It lets the delivery engines be exercised end to end without a Python
// Runtime — exactly the "deterministic fake Telegram Gateway for
// business-engine tests" the migration's testing requirements demand.
//
// The fake is *never* a substitute for a real delivery:
//
//   - Every "sent" result is produced by a counter, not by Telegram. Any
//     test that wants to claim a delivery succeeded must assert against the
//     fake's recorded calls, never against a production log.
//   - It does not try to reproduce Telegram's semantics (album grouping,
//     restricted sources, flood waits) beyond the scripted behaviours a
//     test asks for. Callers configure the behaviours they want to exercise.
//
// Production code must never construct this. cmd/worker only builds it under
// the explicit ATF_WORKER_FAKE_GATEWAY flag, which a real deployment has no
// reason to set.
package fakegateway

import (
	"context"
	"fmt"
	"sync"

	"atf/worker/internal/telegraph"
)

// Fake is a scripted, concurrency-safe telegraph.Gateway.
type Fake struct {
	mu sync.Mutex

	nextID    int64
	copyCalls int

	// Recorded calls, in arrival order. Tests assert against these.
	Calls []Call

	// Scripted behaviours. nil means "no scripted failure for this op".
	ForwardFailsWith        *telegraph.DeliveryFailure
	CopyFailsWith           *telegraph.DeliveryFailure
	SendFailsWith           *telegraph.DeliveryFailure
	CopyMediaGroupFailsWith *telegraph.DeliveryFailure
	DownloadFailsWith       *telegraph.DeliveryFailure
	SessionReady            bool
	SessionState            string

	// RestrictedSource makes ForwardMessages fail with a typed
	// FORWARDING_RESTRICTED, so the fallback path can be exercised without
	// touching a real restricted channel.
	RestrictedSource bool

	// CopyFailOnNth makes the n-th CopyMessage call (1-based) fail with a
	// transient error — the "split album died on item 3" shape a resume test
	// needs. 0 disables it.
	CopyFailOnNth int

	// IDs returned for sends when nothing is scripted to fail. The n-th send
	// returns ScriptedIDs[n] when set, otherwise a monotonically rising id.
	ScriptedIDs []int64

	// fakeBotOps carries the bot / delete / branding-stage-1 scripts and
	// their recorded calls. See its own doc comment for the split.
	fakeBotOps
	// deleteScript answers DeleteMessages; nil = deleted. Kept as a pointer
	// so a test can express "deleted with no failure" explicitly.
	deleteScript *DeleteResult
}

// Call is one recorded gateway invocation.
type Call struct {
	Op         string
	SessionID  string
	ChatID     int64
	MessageID  int64
	TargetID   int64
	Caption    string
	Text       string
	ThreadID   int64
	MessageIDs []int64
	// MediaPath is the staged scratch path of a download/upload staging.
	MediaPath string
}

// New returns a Fake with a ready session and an empty script.
func New(_ string) *Fake {
	return &Fake{SessionReady: true, SessionState: "fake-ready"}
}

var _ telegraph.Gateway = (*Fake)(nil)

// record appends a call under the lock and returns the copy the test reads.
func (f *Fake) record(c Call) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Calls = append(f.Calls, c)
}

// nextTargetID produces the id a "successful" send returns.
func (f *Fake) nextTargetID() int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := len(f.Calls)
	if n < len(f.ScriptedIDs) {
		return f.ScriptedIDs[n]
	}
	f.nextID++
	return f.nextID
}

// IsSessionReady reports the scripted session state.
func (f *Fake) IsSessionReady(ctx context.Context, sessionID string) (bool, string, error) {
	f.record(Call{Op: "IsSessionReady", SessionID: sessionID})
	return f.SessionReady, f.SessionState, nil
}

// GetMessage reports a minimal snapshot with the requested ids.
func (f *Fake) GetMessage(ctx context.Context, sessionID string, chatID, messageID int64) (*telegraph.MessageSnapshot, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "GetMessage", SessionID: sessionID, ChatID: chatID, MessageID: messageID})
	return &telegraph.MessageSnapshot{ChatID: chatID, MessageID: messageID}, nil
}

// GetAlbumMessageIds echoes the single requested id when nothing is scripted.
func (f *Fake) GetAlbumMessageIds(ctx context.Context, sessionID string, chatID, messageID int64) ([]int64, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "GetAlbumMessageIds", SessionID: sessionID, ChatID: chatID, MessageID: messageID})
	return []int64{messageID}, nil
}

// GetFileExtension returns a plausible extension for the media kind.
func (f *Fake) GetFileExtension(ctx context.Context, sessionID string, chatID, messageID int64) (string, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "GetFileExtension", SessionID: sessionID, ChatID: chatID, MessageID: messageID})
	return ".bin", nil
}

// DownloadMedia reports a scripted size (or the scripted failure).
func (f *Fake) DownloadMedia(ctx context.Context, sessionID string, chatID, messageID int64, dest telegraph.MediaHandle) (int64, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "DownloadMedia", SessionID: sessionID, ChatID: chatID, MessageID: messageID, MediaPath: dest.Path})
	if f.DownloadFailsWith != nil {
		return 0, f.DownloadFailsWith
	}
	return 1024, nil
}

// ReloadMedia behaves as DownloadMedia.
func (f *Fake) ReloadMedia(ctx context.Context, sessionID string, chatID, messageID int64, dest telegraph.MediaHandle, mediaType string) (int64, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "ReloadMedia", SessionID: sessionID, ChatID: chatID, MessageID: messageID, MediaPath: dest.Path})
	if f.DownloadFailsWith != nil {
		return 0, f.DownloadFailsWith
	}
	return 1024, nil
}

// PrepareMediaUpload returns a non-empty token so the upload path proceeds.
func (f *Fake) PrepareMediaUpload(ctx context.Context, sessionID string, source telegraph.MediaHandle, sourceChatID, sourceMessageID int64, mediaType string) (string, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "PrepareMediaUpload", SessionID: sessionID, ChatID: sourceChatID, MessageID: sourceMessageID, MediaPath: source.Path})
	return fmt.Sprintf("fake-file-ref-%d-%d", sourceChatID, sourceMessageID), nil
}

// ForwardMessages either fails the scripted way or returns fresh ids.
func (f *Fake) ForwardMessages(ctx context.Context, sessionID string, targetChatID, fromChatID int64, messageIDs []int64, threadID int64) ([]int64, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "ForwardMessages", SessionID: sessionID, TargetID: targetChatID, ChatID: fromChatID, MessageIDs: append([]int64{}, messageIDs...), ThreadID: threadID})
	if f.RestrictedSource {
		return nil, &telegraph.DeliveryFailure{
			Code:      "FORWARDING_RESTRICTED",
			ErrorType: "RESTRICTED",
			Message:   "fake: source forbids forwarding",
		}
	}
	if f.ForwardFailsWith != nil {
		return nil, f.ForwardFailsWith
	}
	out := make([]int64, 0, len(messageIDs))
	for range messageIDs {
		out = append(out, f.nextTargetID())
	}
	return out, nil
}

// CopyMessage copies one message.
func (f *Fake) CopyMessage(ctx context.Context, sessionID string, targetChatID, fromChatID, messageID int64, caption string, threadID int64) (int64, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "CopyMessage", SessionID: sessionID, TargetID: targetChatID, ChatID: fromChatID, MessageID: messageID, Caption: caption, ThreadID: threadID})
	f.copyCalls++
	if f.CopyFailOnNth > 0 && f.copyCalls == f.CopyFailOnNth {
		return 0, &telegraph.DeliveryFailure{
			Code: "TELEGRAM_TEMPORARY_ERROR", ErrorType: "TRANSIENT",
			Message: "fake: scripted transient failure",
		}
	}
	if f.RestrictedSource {
		return 0, &telegraph.DeliveryFailure{
			Code:      "FORWARDING_RESTRICTED",
			ErrorType: "RESTRICTED",
			Message:   "fake: source forbids copying protected content",
		}
	}
	if f.CopyFailsWith != nil {
		return 0, f.CopyFailsWith
	}
	return f.nextTargetID(), nil
}

// CopyMessages copies several messages one-for-one.
func (f *Fake) CopyMessages(ctx context.Context, sessionID string, targetChatID, fromChatID int64, messageIDs []int64, caption string, threadID int64) ([]int64, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "CopyMessages", SessionID: sessionID, TargetID: targetChatID, ChatID: fromChatID, MessageIDs: append([]int64{}, messageIDs...), Caption: caption, ThreadID: threadID})
	if f.CopyFailsWith != nil {
		return nil, f.CopyFailsWith
	}
	out := make([]int64, 0, len(messageIDs))
	for range messageIDs {
		out = append(out, f.nextTargetID())
	}
	return out, nil
}

// CopyMediaGroup copies an album as a group.
func (f *Fake) CopyMediaGroup(ctx context.Context, sessionID string, targetChatID, fromChatID, messageID int64, caption string, threadID int64) ([]int64, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "CopyMediaGroup", SessionID: sessionID, TargetID: targetChatID, ChatID: fromChatID, MessageID: messageID, Caption: caption, ThreadID: threadID})
	if f.CopyMediaGroupFailsWith != nil {
		return nil, f.CopyMediaGroupFailsWith
	}
	return []int64{f.nextTargetID()}, nil
}

// SendMedia uploads a staged file as new media.
func (f *Fake) SendMedia(ctx context.Context, sessionID string, targetChatID int64, token, caption, mediaType string, threadID int64) (int64, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "SendMedia", SessionID: sessionID, TargetID: targetChatID, Caption: caption, MediaPath: token, ThreadID: threadID})
	if f.SendFailsWith != nil {
		return 0, f.SendFailsWith
	}
	return f.nextTargetID(), nil
}

// SendMessage sends plain text.
func (f *Fake) SendMessage(ctx context.Context, sessionID string, targetChatID int64, text string, threadID int64) (int64, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "SendMessage", SessionID: sessionID, TargetID: targetChatID, Text: text, ThreadID: threadID})
	if f.SendFailsWith != nil {
		return 0, f.SendFailsWith
	}
	return f.nextTargetID(), nil
}

// Reset clears the recorded calls (scripted behaviours stay).
func (f *Fake) Reset() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Calls = nil
	f.copyCalls = 0
	f.nextID = 0
}

// Recorded returns a snapshot of the calls (for test assertions).
func (f *Fake) Recorded() []Call {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]Call, len(f.Calls))
	copy(out, f.Calls)
	return out
}

// --- bot transport -------------------------------------------------------- //

// BotCall is one recorded bot operation, for test assertions.
type BotCall struct {
	Op            string
	TokenID       string
	OwnerUserID   int64
	TargetChatID  int64
	FromChatID    int64
	MessageID     int64
	MessageIDs    []int64
	Text          string
	Caption       string
	ThreadID      int64
	InlineButtons []string
	Token         string
	MediaType     string
}

// BotDeleteResult is what the scripted BotDeleteMessages returns. When nil
// the operation succeeds and reports deleted=true.
type BotDeleteResult struct {
	Deleted bool
	Failure *telegraph.DeliveryFailure
}

// BrandingStage1Result is what the scripted BrandingStage1 returns.
type BrandingStage1Result struct {
	IDs     []int64
	Failure *telegraph.DeliveryFailure
}

// BotAvailabilityResult is what the scripted BotAvailability returns.
type BotAvailabilityResult struct {
	OK          bool
	BotUsername string
	Failure     *telegraph.DeliveryFailure
}

// fakeBotOps is the scripted-behaviour extension embedded in Fake.
//
// The fake deliberately keeps bot behaviour separate from the USER-transport
// script: a test for the bot route asserts on BotCalls, never on Calls, so a
// test that leaks a bot call into the USER path fails loudly.
type fakeBotOps struct {
	// BotAvailabilityScript answers availability probes; nil = ok with a
	// generic username.
	BotAvailabilityScript *BotAvailabilityResult
	// BotSendFailsWith fails every bot send with this typed failure.
	BotSendFailsWith *telegraph.DeliveryFailure
	// BotSendUnsupported makes every bot send raise the typed
	// BOT-unsupported signal the delivery engine falls back from.
	BotSendUnsupported bool
	// BotDeleteScript answers deletion; nil = deleted.
	BotDeleteScript *BotDeleteResult
	// BrandingStage1Script answers the stage-1 post; nil = one fresh id.
	BrandingStage1Script *BrandingStage1Result
	// BrandingStage1IDs are returned in order for successive stage-1 posts
	// when BrandingStage1Script is nil. Exhausted → rising counter.
	BrandingStage1IDs []int64

	botCalls    []BotCall
	brandingSeq int
}

// BotAvailability reports the scripted bot availability.
func (f *Fake) BotAvailability(ctx context.Context, target telegraph.BotTarget) (bool, string, *telegraph.DeliveryFailure) {
	f.mu.Lock()
	f.botCalls = append(f.botCalls, BotCall{Op: "BotAvailability", TokenID: target.TokenID, OwnerUserID: target.OwnerUserID})
	script := f.BotAvailabilityScript
	f.mu.Unlock()
	if script != nil {
		return script.OK, script.BotUsername, script.Failure
	}
	return true, "fake-bot", nil
}

// BotSendText sends text through the scripted fake bot.
func (f *Fake) BotSendText(ctx context.Context, target telegraph.BotTarget, targetChatID int64, text string, threadID int64, inlineButtons []string) (int64, *telegraph.DeliveryFailure) {
	f.mu.Lock()
	f.botCalls = append(f.botCalls, BotCall{Op: "BotSendText", TokenID: target.TokenID, OwnerUserID: target.OwnerUserID, TargetChatID: targetChatID, Text: text, ThreadID: threadID, InlineButtons: append([]string{}, inlineButtons...)})
	if f.BotSendUnsupported {
		f.mu.Unlock()
		return 0, &telegraph.DeliveryFailure{Code: "TELEGRAM_PERMANENT_ERROR", ErrorType: "BOT_METHOD_INVALID", Message: "media group delivery is not available on the bot route"}
	}
	fail := f.BotSendFailsWith
	f.mu.Unlock()
	if fail != nil {
		return 0, fail
	}
	return f.nextTargetID(), nil
}

// BotCopyMessage copies one message through the scripted fake bot.
func (f *Fake) BotCopyMessage(ctx context.Context, target telegraph.BotTarget, targetChatID, fromChatID, messageID int64, caption string, threadID int64, inlineButtons []string) (int64, *telegraph.DeliveryFailure) {
	f.mu.Lock()
	f.botCalls = append(f.botCalls, BotCall{Op: "BotCopyMessage", TokenID: target.TokenID, OwnerUserID: target.OwnerUserID, TargetChatID: targetChatID, FromChatID: fromChatID, MessageID: messageID, Caption: caption, ThreadID: threadID, InlineButtons: append([]string{}, inlineButtons...)})
	if f.BotSendUnsupported {
		f.mu.Unlock()
		return 0, &telegraph.DeliveryFailure{Code: "TELEGRAM_PERMANENT_ERROR", ErrorType: "BOT_METHOD_INVALID", Message: "media group delivery is not available on the bot route"}
	}
	fail := f.BotSendFailsWith
	f.mu.Unlock()
	if fail != nil {
		return 0, fail
	}
	return f.nextTargetID(), nil
}

// BotSendMedia sends a staged file through the scripted fake bot.
func (f *Fake) BotSendMedia(ctx context.Context, target telegraph.BotTarget, targetChatID int64, token, caption, mediaType string, threadID int64, inlineButtons []string) (int64, *telegraph.DeliveryFailure) {
	f.mu.Lock()
	f.botCalls = append(f.botCalls, BotCall{Op: "BotSendMedia", TokenID: target.TokenID, OwnerUserID: target.OwnerUserID, TargetChatID: targetChatID, Token: token, Caption: caption, MediaType: mediaType, ThreadID: threadID, InlineButtons: append([]string{}, inlineButtons...)})
	if f.BotSendUnsupported {
		f.mu.Unlock()
		return 0, &telegraph.DeliveryFailure{Code: "TELEGRAM_PERMANENT_ERROR", ErrorType: "BOT_METHOD_INVALID", Message: "media group delivery is not available on the bot route"}
	}
	fail := f.BotSendFailsWith
	f.mu.Unlock()
	if fail != nil {
		return 0, fail
	}
	return f.nextTargetID(), nil
}

// BotDeleteMessages deletes through the scripted fake bot.
func (f *Fake) BotDeleteMessages(ctx context.Context, target telegraph.BotTarget, chatID int64, messageIDs []int64) (bool, *telegraph.DeliveryFailure) {
	f.mu.Lock()
	f.botCalls = append(f.botCalls, BotCall{Op: "BotDeleteMessages", TokenID: target.TokenID, OwnerUserID: target.OwnerUserID, TargetChatID: chatID, MessageIDs: append([]int64{}, messageIDs...)})
	script := f.BotDeleteScript
	f.mu.Unlock()
	if script != nil {
		return script.Deleted, script.Failure
	}
	return true, nil
}

// --- deletion (both transports) ------------------------------------------- //

// DeleteResult is what the scripted DeleteMessages returns.
type DeleteResult struct {
	Deleted bool
	Failure *telegraph.DeliveryFailure
}

// DeleteScript answers every DeleteMessages call; nil = deleted.
type DeleteScript *DeleteResult

// DeleteFailsWith makes every DeleteMessages fail with this typed failure.
// A nil-failure script with Deleted=true reports a successful cleanup.
func (f *Fake) DeleteMessages(ctx context.Context, transport telegraph.DeleteTransport, sessionID, botTokenID string, ownerUserID, chatID int64, messageIDs []int64) (bool, *telegraph.DeliveryFailure) {
	f.record(Call{Op: "DeleteMessages:" + string(transport), SessionID: sessionID, ChatID: chatID, MessageIDs: append([]int64{}, messageIDs...)})
	f.mu.Lock()
	script := f.deleteScript
	f.mu.Unlock()
	if script != nil {
		return (*script).Deleted, (*script).Failure
	}
	return true, nil
}

// --- branding stage 1 ------------------------------------------------------ //

// BrandingStage1 posts into C according to the script.
func (f *Fake) BrandingStage1(ctx context.Context, post telegraph.BrandingPost) ([]int64, *telegraph.DeliveryFailure) {
	f.mu.Lock()
	f.botCalls = append(f.botCalls, BotCall{
		Op: "BrandingStage1", TokenID: post.BotTokenID, OwnerUserID: post.OwnerUserID,
		TargetChatID: post.TargetChatID, FromChatID: post.SourceChatID, MessageID: post.SourceMessageID,
		MessageIDs: append([]int64{}, post.SourceMessageIDs...), Text: post.Text, Caption: post.Caption,
		ThreadID: post.ThreadID, InlineButtons: append([]string{}, post.InlineButtons...),
	})
	script := f.BrandingStage1Script
	f.brandingSeq++
	seq := f.brandingSeq
	ids := f.BrandingStage1IDs
	f.mu.Unlock()
	if script != nil {
		return script.IDs, script.Failure
	}
	if len(ids) > 0 {
		if seq-1 < len(ids) {
			return []int64{ids[seq-1]}, nil
		}
		return []int64{ids[len(ids)-1]}, nil
	}
	return []int64{f.nextTargetID()}, nil
}

// BotRecorded returns the recorded bot operations (for test assertions).
func (f *Fake) BotRecorded() []BotCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]BotCall, len(f.botCalls))
	copy(out, f.botCalls)
	return out
}

// SetDeleteScript scripts DeleteMessages (nil restores "deleted").
func (f *Fake) SetDeleteScript(script *DeleteResult) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deleteScript = script
}
