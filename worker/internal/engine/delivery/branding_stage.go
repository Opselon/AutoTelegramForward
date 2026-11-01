package delivery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"

	"atf/worker/internal/domain/branding"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/telegraph"
)

// --- transport selection --------------------------------------------------- //

// Transport is the Telegram identity a delivery is executed through.
type Transport string

const (
	// TransportUser is the owner's own account: the default, preserved from
	// the Python rule ("USER remains the default transport; BOT is selected
	// only when the rule explicitly requests it").
	TransportUser Transport = "USER"
	// TransportBot posts through the bot token the owner attached.
	TransportBot Transport = "BOT"
)

// transportFor resolves the transport the job asks for. The rule's snapshot
// rides in the job payload; the live rule is consulted only for the label
// when the payload does not carry one (an older job).
func transportFor(j *job.DeliveryJob, r *rule.ForwardRule) Transport {
	wanted := strings.ToUpper(strings.TrimSpace(j.Payload.DeliveryTransport()))
	if wanted == "" && r != nil {
		wanted = strings.ToUpper(strings.TrimSpace(r.DeliveryTransport))
	}
	if wanted == "BOT" {
		return TransportBot
	}
	return TransportUser
}

// botTarget builds the tenant-scoped token reference for a BOT job. The raw
// token never enters the Worker: the Runtime resolves the id through the
// owner-scoped repository inside the trusted Python boundary.
func botTarget(j *job.DeliveryJob, r *rule.ForwardRule) telegraph.BotTarget {
	tokenID := j.Payload.BotTokenID()
	if tokenID == "" && r != nil {
		tokenID = r.BotTokenID
	}
	owner := j.Payload.OwnerUserID()
	if owner == 0 && r != nil {
		owner = r.OwnerUserID
	}
	return telegraph.BotTarget{TokenID: tokenID, OwnerUserID: owner}
}

// --- the unsupported-operation signal ------------------------------------- //

// ErrUnsupported is the typed signal that the Bot Api cannot express an
// operation. The delivery policy (not the Runtime) decides whether a
// USER-transport fallback is permitted: the mission's rule is that a
// genuinely unsupported operation may fall back only when the existing
// feature permits it, never silently after a permanent authorization
// failure or an ambiguous send timeout.
type ErrUnsupported struct{ Reason string }

func (e *ErrUnsupported) Error() string { return e.Reason }

// unsupportedFailure classifies a typed Telegram failure as a
// bot-transport unsupported signal, mirroring Python's
// BotTransportUnsupported. The Runtime reports the permanent
// BOT_METHOD_INVALID refusal for album-shaped operations; the engine reads
// the type and decides.
func unsupportedFailure(ferr *telegraph.DeliveryFailure) bool {
	if ferr == nil {
		return false
	}
	return ferr.Code == "TELEGRAM_PERMANENT_ERROR" &&
		(ferr.ErrorType == "BOT_METHOD_INVALID" || ferr.ErrorType == "BOT_TRANSPORT_UNAVAILABLE")
}

// --- branding stage 1: post the deliverable into C ------------------------- //

// brandingView builds the message-side input the hop decision reads from the
// job payload. The Python pipeline decided the hop at ingest time; the Go
// Worker re-decides it at claim time from the same inputs, so a task whose
// branding config changed while the job sat in the queue behaves the way the
// live rule says, not the way the enqueue-time snapshot froze.
func brandingView(j *job.DeliveryJob) branding.MessageView {
	view := branding.MessageView{Text: j.Payload.Text()}
	if origin := j.Payload.Raw()["forward_origin"]; origin != nil {
		if m, ok := origin.(map[string]any); ok {
			view.ForwardOrigin = &branding.ForwardOrigin{
				FromChatID:       stringField(m, "from_chat_id"),
				FromChatUsername: stringField(m, "from_chat_username"),
				FromChatTitle:    stringField(m, "from_chat_title"),
			}
		}
	}
	return view
}

// brandingRuleView builds the task-side input the hop decision reads.
func brandingRuleView(r *rule.ForwardRule) branding.RuleView {
	if r == nil {
		return branding.RuleView{}
	}
	view := branding.RuleView{
		IntermediateChannelID: r.IntermediateChannelID,
		UseIntermediate:       r.UseIntermediate,
		IntermediateMode:      r.IntermediateMode,
		MessageCategory:       r.MessageCategory,
		DetectionCriteria:     r.DetectionCriteria,
	}
	// The branding_hop sub-blob lives in the rule's metadata, the same place
	// the Python read_config reads it from.
	if blob, ok := r.Metadata["branding_hop"].(map[string]any); ok {
		if view.Metadata == nil {
			view.Metadata = map[string]any{}
		}
		view.Metadata[branding.BrandingKey()] = blob
	}
	return view
}

// --- hop plan & checkpoints ------------------------------------------------ //

// hopPlan is the branding intent the Python pipeline enqueued under worker
// ownership, or a legacy already-performed plan ({"stage" absent}).
type hopPlan struct {
	ChatID       string  `json:"chat_id"`
	MessageID    int64   `json:"message_id"`
	MessageIDs   []int64 `json:"message_ids"`
	ForceForward bool    `json:"force_forward"`
	ContentMode  string  `json:"content_mode"`
	Reason       string  `json:"reason"`
	DedupKey     string  `json:"dedup_key"`
	Stage        string  `json:"stage"`
}

// isIntent reports whether this plan is a Stage-3 intent the Worker must
// execute (no post in C exists yet), versus a legacy already-performed plan.
func (p *hopPlan) isIntent() bool { return p != nil && p.Stage == "INTENT" }

// readHopPlan decodes the payload's branding blob, tolerating the legacy
// shape (a plan Python produced by actually posting into C).
func readHopPlan(j *job.DeliveryJob) *hopPlan {
	raw := j.Payload.Branding()
	if raw == nil {
		return nil
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return nil
	}
	var plan hopPlan
	if err := json.Unmarshal(b, &plan); err != nil {
		return nil
	}
	return &plan
}

// hopIdentity is the deterministic stage-1 fence key: one logical "post into
// C for this source event and this content fingerprint". Two target jobs
// that share the fingerprint share ONE post in C; each target's stage-2
// delivery stays its own fence row.
func hopIdentity(j *job.DeliveryJob, plan *hopPlan) string {
	if plan != nil && plan.DedupKey != "" {
		return fmt.Sprintf("hop:%s:%s", j.RuleID, plan.DedupKey)
	}
	// A legacy plan already carries the intermediate ids; the stage-1 side
	// effect already happened, so no stage-1 fence is created at all.
	return ""
}

// stage1Post builds the contract request for the post into C, carrying the
// deliverable shape Go decided from the content mode — never a transform.
func (e *Engine) stage1Post(ctx context.Context, j *job.DeliveryJob, r *rule.ForwardRule, plan *hopPlan, transport Transport) telegraph.BrandingPost {
	p := &j.Payload
	post := telegraph.BrandingPost{
		Transport:     telegraph.TransportUser,
		SessionID:     p.SessionID(),
		OwnerUserID:   p.OwnerUserID(),
		TargetChatID:  intChat(plan.ChatID),
		ThreadID:      p.TopicID(),
		InlineButtons: payloadButtons(p),
	}
	if transport == TransportBot {
		post.Transport = telegraph.TransportBot
		post.BotTokenID = botTarget(j, r).TokenID
		if post.OwnerUserID == 0 {
			post.OwnerUserID = botTarget(j, r).OwnerUserID
		}
	}

	contentMode := strings.ToUpper(strings.TrimSpace(contentModeOf(j, r)))
	hasMedia := p.HasMedia()
	albumIDs := p.AlbumMessageIDs()
	if len(albumIDs) == 0 {
		albumIDs = []int64{j.SourceMessageID}
	}

	// The deliverable shape mirrors core/application/branding_hop.py exactly:
	//
	//   TEXT_ONLY, or a source with no media -> post the processed text
	//   MEDIA_ONLY                             -> copy the media, no caption
	//   AUTO / TEXT_AND_MEDIA                  -> copy with the processed caption
	//
	// An album is posted as a media group so the group survives the hop; the
	// Bot Api cannot express that and the typed refusal comes back for the
	// engine's own fallback decision (never a hidden one).
	if !hasMedia || contentMode == "TEXT_ONLY" {
		body := firstNonEmpty(p.Text(), "...")
		if body == "..." {
			// The Python hop returns None when there is nothing to post and
			// the caller delivers directly; the Worker treats an empty body
			// the same way (see performBrandingStage1's caller).
			body = ""
		}
		post.Operation = "text"
		post.Text = body
		return post
	}

	if len(albumIDs) > 1 && p.MediaGroupID() != "" {
		post.Operation = "media_group"
		post.SourceChatID = intChat(j.SourceChatID)
		post.SourceMessageID = albumIDs[0]
		post.SourceMessageIDs = albumIDs
		if contentMode != "MEDIA_ONLY" {
			post.Caption = p.Text()
		}
		return post
	}

	post.Operation = "copy"
	post.SourceChatID = intChat(j.SourceChatID)
	post.SourceMessageID = j.SourceMessageID
	if contentMode != "MEDIA_ONLY" {
		post.Caption = p.Text()
	}
	return post
}

// contentModeOf resolves the content mode the post is shaped by: the job's
// snapshotted mode, with the live rule as the fallback for older jobs.
func contentModeOf(j *job.DeliveryJob, r *rule.ForwardRule) string {
	mode := strings.ToUpper(strings.TrimSpace(j.Payload.ContentMode()))
	if mode == "" && r != nil {
		mode = strings.ToUpper(strings.TrimSpace(r.ContentMode))
	}
	if mode == "" {
		mode = "AUTO"
	}
	return mode
}

// payloadButtons extracts the raw inline-button spec the Python pipeline
// snapshotted at enqueue (inline_buttons_raw, serialized as JSON strings).
func payloadButtons(p *job.Payload) []string {
	raw := p.InlineButtons()
	if len(raw) == 0 {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if s, ok := item.(string); ok && s != "" {
			out = append(out, s)
		}
	}
	return out
}

// hopFingerprint is the stable normalized content digest a dedup key is
// computed over. It mirrors the Python pipeline's _normalize_for_fingerprint:
// the Persian/Arabic fold plus whitespace collapse, so a post that only
// rewraps or respells is the same post.
func hopFingerprint(text string) string {
	folded := foldFold(text)
	sum := sha256.Sum256([]byte(folded))
	return hex.EncodeToString(sum[:])
}

// stringField reads a string field from a decoded JSON map.
func stringField(m map[string]any, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// foldFold is the Persian/Arabic-tolerant comparison form used for content
// dedup: the branding domain's port of the content-engine fold, with
// whitespace collapsed the way the Python normalizer does.
func foldFold(text string) string {
	return branding.NormalizeText(text)
}
