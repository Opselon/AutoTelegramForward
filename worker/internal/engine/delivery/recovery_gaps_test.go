package delivery

// Regression tests for the media-delivery recovery gaps found in the phase-2
// audit (D1 album duplication, D2 bot-route media loss, D3 staged-file leak).
// Each test pins the exact production failure sequence the audit constructed.

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph"
	"atf/worker/internal/telegraph/fakegateway"
)

// TestAlbumAmbiguousGroupCopyNeverReuploads: a CopyMediaGroup whose outcome
// is ambiguous (timeout after Telegram may have accepted the upload) used to
// fall straight into the per-item re-upload loop, delivering the album twice
// — once as the group, once as N fresh messages. The ambiguity must record
// UNKNOWN and re-upload nothing.
func TestAlbumAmbiguousGroupCopyNeverReuploads(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:21:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 21,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "album", "has_media": true, "media_type": "photo",
			"media_group_id": "grp-21", "album_mode": "album",
			"album_message_ids": []any{11, 12, 13},
			"fallback_enabled":  true,
		}),
	}
	h.enqueue(t, j)
	h.gateway.CopyMediaGroupFailsWith = &telegraph.DeliveryFailure{
		Code: "NETWORK_TIMEOUT", Message: "rpc timeout after the upload was accepted",
	}

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusUnknown))
	for _, c := range h.gateway.Recorded() {
		if c.Op == "DownloadMedia" || c.Op == "SendMedia" {
			t.Errorf("ambiguous group copy re-uploaded media (%s) — the album would be delivered twice", c.Op)
		}
	}
}

// TestAlbumFallbackMapsTheFirstItem: when the group copy is refused
// (NeedsReupload) and the fallback runs, the mapping/fence target id must be
// the FIRST delivered item — edit/delete sync and auto-delete key on it.
// The Go port used to return the LAST id and pointed those at the wrong
// message (Python returns res[0].id).
func TestAlbumFallbackMapsTheFirstItem(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:22:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 22,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "album", "has_media": true, "media_type": "photo",
			"media_group_id": "grp-22", "album_mode": "album",
			"album_message_ids": []any{11, 12, 13},
			"fallback_enabled":  true,
		}),
	}
	h.enqueue(t, j)
	h.gateway.CopyMediaGroupFailsWith = &telegraph.DeliveryFailure{
		Code: "TELEGRAM_PERMANENT_ERROR", ErrorType: "PHOTO_EXT_INVALID",
		Message: "Telegram says: [400 PHOTO_EXT_INVALID]",
	}
	// ScriptedIDs indexes the fake's GLOBAL call log: group-copy failure,
	// then 3 re-upload rounds of (GetFileExtension, DownloadMedia,
	// PrepareMediaUpload, SendMedia). The SendMedia positions carry the ids
	// 101/102/103; the first delivered item must become the mapping id.
	h.gateway.ScriptedIDs = []int64{0, 0, 0, 0, 0, 101, 0, 0, 0, 0, 102, 0, 0, 0, 0, 103}

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))

	probeKey := job.AttemptKey{RuleID: j.RuleID, SourceChatID: j.SourceChatID, SourceMessageID: j.SourceMessageID, TargetChatID: j.TargetChatID, JobKind: j.JobKind}
	outcome, row, err := h.store.FenceClaim(context.Background(), probeKey, "probe-reader", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("fence probe: %v", err)
	}
	if outcome != job.FenceAlreadySent {
		t.Fatalf("fence after album fallback = %s, want already_sent", outcome)
	}
	if row == nil || row.TargetMessageID != 101 {
		t.Fatalf("album mapping id = %+v, want 101 (the FIRST re-uploaded item)", row)
	}
}

// TestAlbumFallbackHonorsOptOut: the re-upload opt-out applies to every
// failure class on the group-copy path, not only target-side rejections.
func TestAlbumFallbackHonorsOptOut(t *testing.T) {
	h := newHarness(t)
	j := &job.DeliveryJob{
		ID: "r1:c:23:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 23,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "album", "has_media": true, "media_type": "photo",
			"media_group_id": "grp-23", "album_mode": "album",
			"album_message_ids": []any{11, 12, 13},
			"fallback_enabled":  false,
		}),
	}
	h.enqueue(t, j)
	h.gateway.CopyMediaGroupFailsWith = &telegraph.DeliveryFailure{
		Code: "TELEGRAM_TEMPORARY_ERROR", Message: "flood-ish refusal",
	}

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	for _, c := range h.gateway.Recorded() {
		if c.Op == "DownloadMedia" || c.Op == "SendMedia" {
			t.Errorf("fallback_enabled=false still re-uploaded (%s) — the operator's opt-out was ignored", c.Op)
		}
	}
}

// TestBotMediaRefusalFallsBackToAccount: a bot-route media copy refused by
// Telegram with a permanent 400 (restricted source, target-side media
// rejection — the Bot API adapter reports them as BOT_API_400) used to fail
// the job permanently even though the account transport delivers it. The
// legacy Python path rescued these jobs; the port must too.
func TestBotMediaRefusalFallsBackToAccount(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	gw.BotSendFailsWith = &telegraph.DeliveryFailure{
		Code: "TELEGRAM_PERMANENT_ERROR", ErrorType: "BOT_API_400",
		Message: "Bad Request: message can't be forwarded",
	}
	r := &fixedRules{rule: &rule.ForwardRule{
		ID: "r-bot-media", SessionID: "sess-user", IsActive: true,
		DeliveryTransport: "BOT", BotTokenID: "tok-1", OwnerUserID: 7,
	}}

	log := &testLogger{}
	engine := New(store, r, gw, nil, log, &testMetrics{}, "w1", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})
	j := &job.DeliveryJob{
		ID: "r-bot-media:c:31:t", RuleID: "r-bot-media", SourceChatID: "c", SourceMessageID: 31,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "caption", "has_media": true, "media_type": "photo",
			"delivery_transport": "BOT", "bot_token_id": "tok-1", "owner_user_id": 7,
			"session_id": "sess-user", "fallback_enabled": true,
		}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, store, j.ID, string(job.StatusSent))
	userCopy := false
	for _, c := range gw.Recorded() {
		if c.Op == "CopyMessage" && c.SessionID == "sess-user" {
			userCopy = true
		}
	}
	if !userCopy {
		t.Fatal("bot media refusal did not fall back to the account transport — the job would be lost")
	}
}

// TestBotMediaRefusalHonorsOptOut: without the fallback opt-in the bot
// refusal fails the job; no silent account-transport bypass.
func TestBotMediaRefusalHonorsOptOut(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	gw.BotSendFailsWith = &telegraph.DeliveryFailure{
		Code: "TELEGRAM_PERMANENT_ERROR", ErrorType: "BOT_API_400",
		Message: "Bad Request: message can't be forwarded",
	}
	r := &fixedRules{rule: &rule.ForwardRule{
		ID: "r-bot-nofb", SessionID: "sess-user", IsActive: true,
		DeliveryTransport: "BOT", BotTokenID: "tok-1", OwnerUserID: 7,
	}}
	log := &testLogger{}
	engine := New(store, r, gw, nil, log, &testMetrics{}, "w1", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})
	j := &job.DeliveryJob{
		ID: "r-bot-nofb:c:32:t", RuleID: "r-bot-nofb", SourceChatID: "c", SourceMessageID: 32,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "caption", "has_media": true, "media_type": "photo",
			"delivery_transport": "BOT", "bot_token_id": "tok-1", "owner_user_id": 7,
			"session_id": "sess-user", "fallback_enabled": false,
		}),
	}
	queue.InsertJob(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, store, j.ID, string(job.StatusFailed))
	for _, c := range gw.Recorded() {
		if c.Op == "CopyMessage" {
			t.Error("fallback_enabled=false still dispatched through the account transport")
		}
	}
}

// TestStagedMediaCleanedUpAfterReupload: the staged scratch file must be
// gone whichever way the re-upload ends. The Go path used to leave every
// staged file behind (the Python path deletes its temp files).
func TestStagedMediaCleanedUpAfterReupload(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	dir := t.TempDir()
	h := &harness{store: store, gateway: gw, engine: New(store, staticRules{}, gw, nil, &testLogger{}, &testMetrics{}, "w-test", dir, Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	}), metrics: &testMetrics{}, log: &testLogger{}}
	j := &job.DeliveryJob{
		ID: "r1:c:41:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 41,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "caption", "has_media": true, "media_type": "photo",
			"fallback_enabled": true,
		}),
	}
	h.enqueue(t, j)
	h.gateway.CopyMediaRejected = true
	h.gateway.DownloadWritesFile = true
	ext := ".jpg"
	h.gateway.ExtensionOverride = &ext

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
	staged := filepath.Join(dir, j.ID+ext)
	if _, err := os.Stat(staged); !os.IsNotExist(err) {
		t.Errorf("staged file %s survived a successful re-upload — the media dir grows with every retry", staged)
	}
}

// TestStagedMediaCleanedUpAfterUploadFailure pins the failure side of the
// same contract, with a Telegram upload-time rejection (a knob the fake
// never had, which is why this path was untestable before).
func TestStagedMediaCleanedUpAfterUploadFailure(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	dir := t.TempDir()
	h := &harness{store: store, gateway: gw, engine: New(store, staticRules{}, gw, nil, &testLogger{}, &testMetrics{}, "w-test", dir, Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	}), metrics: &testMetrics{}, log: &testLogger{}}
	j := &job.DeliveryJob{
		ID: "r1:c:42:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 42,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "caption", "has_media": true, "media_type": "photo",
			"fallback_enabled": true,
		}),
	}
	h.enqueue(t, j)
	h.gateway.CopyMediaRejected = true
	h.gateway.DownloadWritesFile = true
	h.gateway.SendMediaFailsWith = &telegraph.DeliveryFailure{
		Code: "FLOOD_WAIT", Message: "Telegram says: [420 FLOOD_WAIT] (60)",
	}
	ext := ".jpg"
	h.gateway.ExtensionOverride = &ext

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	// A flood-waited upload is transient: the job must be retry-waiting,
	// not permanently failed — and the staged file must still be gone.
	got := jobStatus(t, h.store, j.ID)
	if got != string(job.StatusRetryWait) && got != string(job.StatusPending) {
		t.Errorf("flood-waited re-upload status = %s, want a retry state", got)
	}
	staged := filepath.Join(dir, j.ID+ext)
	if _, err := os.Stat(staged); !os.IsNotExist(err) {
		t.Errorf("staged file %s survived a failed upload", staged)
	}
}
