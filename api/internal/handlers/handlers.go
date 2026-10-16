package handlers

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	"autoforward/internal/grpcclient"
	"autoforward/internal/logpolicy"
	pb "autoforward/proto"
)

type Handlers struct {
	c *grpcclient.Clients

	// Policy is THIS API process's own log collection policy: the same instance
	// the access trail and the administrative audit trail are gated by, so the
	// control plane cannot report one configuration while another is in force.
	// nil when the API runs without one (tests, embedded use).
	Policy *logpolicy.Policy

	// LogAudit records an administrative change in the durable log store. It is
	// set by main.go to the same sink the access trail uses, so a policy change
	// cannot silence the record of itself.
	LogAudit func(actor int64, action, reasonCode string, detail map[string]any)

	// diagMu guards lastDiagStart: the diagnostic window is per-process state
	// mutated from concurrent HTTP handlers.
	diagMu        sync.Mutex
	lastDiagStart time.Time
}

func New(c *grpcclient.Clients) *Handlers { return &Handlers{c: c} }

// --------------------------------------------------------------- health
func (h *Handlers) Health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok", "time": time.Now().UTC().Format(time.RFC3339),
	})
}

// ServicesHealth reports live reachability of every microservice in the ATF
// chain, probed in parallel with short timeouts. It powers the Web-UI topology
// view so outages are visible at a glance instead of being discovered when a
// forward silently fails.
func (h *Handlers) ServicesHealth(w http.ResponseWriter, r *http.Request) {
	// The probing itself lives in system.go (ProbeServices) so the same live
	// check can back both this topology endpoint and the setup report.
	results, allOK := h.ProbeServices()

	status := "degraded"
	if allOK {
		status = "healthy"
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":   status,
		"time":     time.Now().UTC().Format(time.RFC3339),
		"services": results,
	})
}

// -------------------------------------------------------------- sessions
func (h *Handlers) ListSessions(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	id := identity(r)
	resp, err := h.c.Sessions.ListSessions(ctx, &pb.ListSessionsRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	sessions := resp.Sessions
	if sessions == nil {
		sessions = []*pb.SessionInfo{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"sessions": sessions,
		"total":    len(sessions),
	})
}

type backupReq struct {
	SessionID string `json:"session_id"`
}

func (h *Handlers) BackupSession(w http.ResponseWriter, r *http.Request) {
	var req backupReq
	// The dashboard calls POST /sessions/{id}/backup (path param); the legacy
	// v1 route still posts a JSON body. Accept both.
	if sid := r.PathValue("id"); sid != "" {
		req.SessionID = sid
	} else if !bind(w, r, &req) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Sessions.BackupSession(ctx, &pb.BackupSessionRequest{
		SessionId:   req.SessionID,
		OwnerUserId: identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

type restoreReq struct {
	EncryptedData string `json:"encrypted_session_data"`
}

func (h *Handlers) RestoreSession(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	var req restoreReq
	if !bind(w, r, &req) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Sessions.RestoreSession(ctx, &pb.RestoreSessionRequest{
		EncryptedSessionData: req.EncryptedData,
		OwnerUserId:          id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) TerminateSession(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Sessions.TerminateSession(ctx, &pb.TerminateSessionRequest{
		SessionId: r.PathValue("id"), OwnerUserId: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// defaultChatLimit is the page size used when the client omits ?limit, matching
// the mobile contract so both sides agree without a round-trip to negotiate it.
const defaultChatLimit int32 = 100

// maxChatLimit is the largest page this endpoint will assemble. The core clamps
// too, but the bound here is what stops one client from asking for a reply the
// API has to buffer in full.
const maxChatLimit int32 = 200

// maxChatOffset is the largest offset the 32-bit request field can carry.
const maxChatOffset int32 = 1<<31 - 1

// boundedQueryInt reads an int32 query parameter and clamps it into [lo, hi].
//
// The parse is done with a 32-bit size, so a value that cannot fit is rejected
// instead of wrapped, and the clamp is applied on the int64 *before* the
// narrowing conversion — which is what makes the int32(v) below safe on every
// architecture. The obvious `strconv.Atoi` + `int32(...)` pair is correct only
// by accident: Atoi yields an architecture-dependent int, and a reader (or an
// analyzer) cannot see the bound without following the surrounding branches.
func boundedQueryInt(raw string, lo, hi int32) (int32, bool) {
	v, err := strconv.ParseInt(raw, 10, 32)
	if err != nil {
		return 0, false
	}
	switch {
	case v < int64(lo):
		return lo, true
	case v > int64(hi):
		return hi, true
	default:
		return int32(v), true
	}
}

// ListChats browses the dialogs (channels, groups, private chats) of one of the
// caller's own sessions — the source/target picker feed for the mobile client.
//
// The tenant scope is derived ONLY from the verified JWT identity: owner_user_id
// is copied from identity(r), never read from the query string or body, so a
// caller cannot enumerate another account's chats by editing the URL. The
// session id is the sole client input, and the core re-checks that the session
// belongs to that owner before it touches a Telegram client.
func (h *Handlers) ListChats(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()

	id := identity(r)
	if id == nil || id.UserID <= 0 {
		// Reached only when the route is mounted without the auth middleware;
		// an unlinked account (id 0) must never fall through to a core call.
		writeJSON(w, http.StatusUnauthorized, map[string]string{
			"error": "authentication required", "code": "unauthenticated",
		})
		return
	}

	sessionID := strings.TrimSpace(r.PathValue("sessionId"))
	if sessionID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "session id is required", "code": "session_id_required",
		})
		return
	}

	q := r.URL.Query()
	// Absent limit => the documented default. A supplied value is clamped into
	// 1..200 so a client cannot ask for a zero-row page or drain the whole
	// snapshot in one reply; a non-numeric value is a client error, not a
	// silent fallback that would hide a broken caller.
	limit := defaultChatLimit
	if raw := strings.TrimSpace(q.Get("limit")); raw != "" {
		v, ok := boundedQueryInt(raw, 1, maxChatLimit)
		if !ok {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "limit must be an integer", "code": "invalid_limit",
			})
			return
		}
		limit = v
	}
	offset := int32(0)
	if raw := strings.TrimSpace(q.Get("offset")); raw != "" {
		v, ok := boundedQueryInt(raw, 0, maxChatOffset)
		if !ok {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "offset must be an integer", "code": "invalid_offset",
			})
			return
		}
		offset = v
	}

	if h.c.Sessions == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "core service unavailable"})
		return
	}
	resp, err := h.c.Sessions.ListChats(ctx, &pb.ListChatsRequest{
		SessionId:   sessionID,
		OwnerUserId: id.UserID,
		Query:       q.Get("query"),
		Limit:       int32(limit),
		Offset:      int32(offset),
	})
	if err != nil {
		chatListError(w, err)
		return
	}
	chats := resp.Chats
	if chats == nil {
		// JSON null would make the mobile list parser choke; an empty array is
		// "no matching chats", which is a valid, expected answer.
		chats = []*pb.ChatSummary{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"session_id": resp.SessionId,
		"chats":      chats,
		"count":      resp.Count,
		"total":      resp.Total,
		"truncated":  resp.Truncated,
	})
}

// ----------------------------------------------------------------- rules
func (h *Handlers) ListRules(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	id := identity(r)
	sessionID := r.URL.Query().Get("session_id")
	resp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{
		SessionId: sessionID, OwnerUserId: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	rules := resp.Rules
	if rules == nil {
		rules = []*pb.ForwardRule{}
	}
	out := make([]map[string]any, 0, len(rules))
	for _, rl := range rules {
		if rl == nil || rl.OwnerUserId != id.UserID {
			continue
		}
		out = append(out, formatRuleJSON(rl))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"rules": out,
		"total": len(out),
	})
}

func formatRuleJSON(rl *pb.ForwardRule) map[string]any {
	if rl == nil {
		return map[string]any{}
	}
	metaMap := make(map[string]any)
	if rl.CustomMetadataJson != "" {
		_ = json.Unmarshal([]byte(rl.CustomMetadataJson), &metaMap)
	}
	var criteria any
	if rl.DetectionCriteriaJson != "" {
		_ = json.Unmarshal([]byte(rl.DetectionCriteriaJson), &criteria)
	}

	res := map[string]any{
		"id":                        rl.Id,
		"session_id":                rl.SessionId,
		"source_chat_id":            rl.SourceChatId,
		"source_chat_name":          rl.SourceChatName,
		"target_chat_id":            rl.TargetChatId,
		"target_chat_name":          rl.TargetChatName,
		"routing_type":              pb.RoutingType_name[int32(rl.RoutingType)],
		"forward_mode":              pb.ForwardMode_name[int32(rl.ForwardMode)],
		"is_active":                 rl.IsActive,
		"filter_rule_id":            rl.FilterRuleId,
		"ai_config_id":              rl.AiConfigId,
		"ai_fallback_policy":        rl.AiFallbackPolicy,
		"ai_prompt_version":         rl.AiPromptVersion,
		"ai_timeout_seconds":        rl.AiTimeoutSeconds,
		"remove_links":              rl.RemoveLinks,
		"custom_caption_template":   rl.CustomCaptionTemplate,
		"created_at":                rl.CreatedAt,
		"updated_at":                rl.UpdatedAt,
		"owner_user_id":             rl.OwnerUserId,
		"name":                      rl.Name,
		"description":               rl.Description,
		"target_chat_ids":           rl.TargetChatIds,
		"message_category":          rl.MessageCategory,
		"intermediate_channel_id":   rl.IntermediateChannelId,
		"intermediate_channel_name": rl.IntermediateChannelName,
		"use_intermediate":          rl.UseIntermediate,
		"fallback_mode":             pb.ForwardMode_name[int32(rl.FallbackMode)],
		"fallback_enabled":          rl.FallbackEnabled,
		"detection_criteria":        criteria,
		"detection_criteria_json":   rl.DetectionCriteriaJson,
		"priority":                  rl.Priority,
		"execution_order":           rl.ExecutionOrder,
		"multi_route":               rl.MultiRoute,
		"media_handling":            rl.MediaHandling,
		"dedupe_policy":             rl.DedupePolicy,
		"max_retries":               rl.MaxRetries,
		"rate_limit_per_minute":     rl.RateLimitPerMinute,
		"custom_header":             rl.CustomHeader,
		"custom_footer":             rl.CustomFooter,
		"header_enabled":            rl.HeaderEnabled,
		"is_paused":                 rl.IsPaused,
		"version":                   rl.Version,
		"custom_metadata_json":      rl.CustomMetadataJson,
		"metadata":                  metaMap,
		// ---- v17: complete per-task control surface --------------------
		"source_chat_ids":                  rl.SourceChatIds,
		"source_chat_names":                rl.SourceChatNames,
		"content_mode":                     rl.ContentMode,
		"delay_seconds":                    rl.DelaySeconds,
		"skip_history":                     rl.SkipHistory,
		"since_ts":                         rl.SinceTs,
		"ignore_edits":                     rl.IgnoreEdits,
		"sync_edits":                       rl.SyncEdits,
		"sync_deletes":                     rl.SyncDeletes,
		"trigger_events":                   rl.TriggerEvents,
		"topic_id":                         preferString(rl.TopicId, metaMap["topic_id"]),
		"ai_secondary_config_id":           rl.AiSecondaryConfigId,
		"link_policy":                      rl.LinkPolicy,
		"domain_allowlist":                 rl.DomainAllowlist,
		"domain_blocklist":                 rl.DomainBlocklist,
		"allowed_media_types":              rl.AllowedMediaTypes,
		"split_long_caption":               rl.SplitLongCaption,
		"album_aggregation_window_seconds": rl.AlbumAggregationWindowSeconds,
		"intermediate_target_chat_id":      rl.IntermediateTargetChatId,
		"intermediate_mode":                rl.IntermediateMode,
		"retry_policy":                     decodedJSONObject(rl.RetryPolicyJson),
		"retry_policy_json":                rl.RetryPolicyJson,
		"proxy":                            decodedJSONValue(rl.ProxyJson),
		"proxy_json":                       rl.ProxyJson,
		"prompt_id":                        preferString(rl.PromptId, metaMap["prompt_id"]),
	}
	// The full settings snapshot: every per-task setting the server knows about,
	// keyed by name, so a client can render the whole task from one response
	// instead of probing field by field.
	if rl.SettingsJson != "" {
		var settings map[string]any
		if err := json.Unmarshal([]byte(rl.SettingsJson), &settings); err == nil {
			res["settings"] = settings
		}
	}
	for k, v := range metaMap {
		if _, exists := res[k]; !exists {
			res[k] = v
		}
	}
	return res
}

func enrichCustomMetadata(rawBody []byte, rule *pb.ForwardRule) {
	if len(rawBody) == 0 || rule == nil {
		return
	}
	var raw map[string]any
	if err := json.Unmarshal(rawBody, &raw); err != nil {
		return
	}
	meta := make(map[string]any)
	if rule.CustomMetadataJson != "" {
		_ = json.Unmarshal([]byte(rule.CustomMetadataJson), &meta)
	}
	if rawMeta, ok := raw["metadata"].(map[string]any); ok {
		for k, v := range rawMeta {
			meta[k] = v
		}
	}
	parityKeys := []string{
		"topic_id", "debug_mode", "sender_type", "feature_group",
		"duplicate_filters", "crypto_mode", "auto_posting", "power_control",
		"forward_delay_seconds", "edit_time_limit_seconds", "process_raw_text", "button_to_text",
		"keyword_confirmation", "keyword_reply", "auto_delete_seconds",
		"ocr_image_ai", "smart_ai_rewrite", "news_digest", "watermark", "watermark_text",
		"ai_creativity", "ai_max_tokens", "paid_addons",
		"whitelist_keywords", "blacklist_keywords", "link_buttons", "affiliate_kit",
		// v2.0 app update: Cleaner, reply sync, language preference.
		"cleaner_enabled", "cleaner_config",
		"sync_replies", "sync_edits", "sync_deletes",
		"language", "show_forward_header",
		// Per-task text modifications (mobile/web editor parity with the bot):
		// replace rules, content trimming, blocking, routing extras.
		"replacements", "header", "footer",
		"block_voice", "block_stickers", "remove_emojis",
		"link_replacement", "album_mode", "clean_words",
		"message_category", "detection_criteria", "use_intermediate",
		"intermediate_mode", "fallback_mode", "priority", "multi_route",
		// AI Rewrite parity
		"ai_config_id", "prompt_id", "ai_fallback_policy", "ai_prompt_version", "ai_timeout_seconds",
	}
	for _, k := range parityKeys {
		if val, ok := raw[k]; ok {
			meta[k] = val
		}
	}

	// Phase 2: typed validation of the known advanced fields. An invalid value
	// is normalized to a safe default rather than silently persisted, so a
	// malformed client payload can never reach the worker as garbage.
	normalizeAdvancedMetadata(meta)

	if len(meta) > 0 {
		if b, err := json.Marshal(meta); err == nil {
			rule.CustomMetadataJson = string(b)
		}
	}
}

// metadataFieldCaps bounds the size and shape of the persisted advanced
// settings. Without this, CustomMetadataJson is an unrestricted blob a client
// could inflate without limit.
const maxMetadataBytes = 64 * 1024
const maxWatermarkLen = 200
const maxKeywordReplyLen = 4096
const maxDelaySeconds = 3600.0

// normalizeAdvancedMetadata coerces known advanced fields into their canonical
// types and rejects out-of-range or oversized values by substituting a safe
// default. It never deletes unknown keys: they survive for forward
// compatibility, but the worker only ever reads the documented ones.
func normalizeAdvancedMetadata(meta map[string]any) {
	if meta == nil {
		return
	}

	// Numeric fields: bounded, non-negative.
	for _, k := range []string{"forward_delay_seconds", "edit_time_limit_seconds", "auto_delete_seconds"} {
		if v, ok := meta[k]; ok {
			if f, err := toFloat(v); err == nil {
				if f < 0 {
					f = 0
				}
				if k == "forward_delay_seconds" && f > maxDelaySeconds {
					f = maxDelaySeconds
				}
				meta[k] = f
			} else {
				// Not a number: treat as "unset" rather than persisting garbage.
				delete(meta, k)
			}
		}
	}

	// Boolean fields.
	for _, k := range []string{
		"debug_mode", "duplicate_filters", "crypto_mode", "auto_posting",
		"power_control", "process_raw_text", "button_to_text",
		"keyword_confirmation", "ocr_image_ai", "smart_ai_rewrite", "news_digest",
		// v2.0 app update sync flags + cleaner gate.
		"sync_replies", "sync_edits", "sync_deletes", "cleaner_enabled",
	} {
		if v, ok := meta[k]; ok {
			meta[k] = toBool(v)
		}
	}

	// Bounded strings.
	if v, ok := meta["watermark_text"]; ok {
		if s, ok := v.(string); ok && len(s) > maxWatermarkLen {
			meta["watermark_text"] = s[:maxWatermarkLen]
		}
	}
	if v, ok := meta["keyword_reply"]; ok {
		if s, ok := v.(string); ok && len(s) > maxKeywordReplyLen {
			meta["keyword_reply"] = s[:maxKeywordReplyLen]
		}
	}

	// Numeric AI settings with sane bounds.
	if v, ok := meta["ai_creativity"]; ok {
		if f, err := toFloat(v); err == nil {
			if f < 0 || f > 1 {
				f = 0.7
			}
			meta["ai_creativity"] = f
		}
	}
	if v, ok := meta["ai_max_tokens"]; ok {
		if f, err := toFloat(v); err == nil {
			if f < 0 || f > 32000 {
				f = 600
			}
			meta["ai_max_tokens"] = int(f)
		}
	}

	// topic_id: a forum thread id is a positive integer.
	if v, ok := meta["topic_id"]; ok {
		if f, err := toFloat(v); err == nil {
			if f < 0 {
				f = 0
			}
			meta["topic_id"] = int(f)
		} else {
			delete(meta, "topic_id")
		}
	}

	// v2.0 app update: validate the nested cleaner_config object.
	normalizeCleanerConfig(meta)

	// Enforce an overall payload ceiling so the metadata blob cannot be used
	// to bloat the row without bound.
	if b, err := json.Marshal(meta); err == nil && len(b) > maxMetadataBytes {
		// Keep the fields the worker actually consumes and drop the rest.
		kept := map[string]any{}
		for _, k := range []string{
			"topic_id", "debug_mode", "sender_type", "duplicate_filters", "crypto_mode",
			"auto_posting", "power_control", "forward_delay_seconds",
			"edit_time_limit_seconds", "process_raw_text", "button_to_text",
			"keyword_confirmation", "keyword_reply", "auto_delete_seconds",
			"watermark_text", "ai_creativity", "ai_max_tokens", "paid_addons",
		} {
			if v, ok := meta[k]; ok {
				kept[k] = v
			}
		}
		for k := range meta {
			if _, ok := kept[k]; !ok {
				delete(meta, k)
			}
		}
	}
}

// toFloat converts a JSON-decoded number (float64 by default) or a numeric
// string to a float64, returning an error for anything else.
func toFloat(v any) (float64, error) {
	switch n := v.(type) {
	case float64:
		return n, nil
	case float32:
		return float64(n), nil
	case int:
		return float64(n), nil
	case int64:
		return float64(n), nil
	case string:
		return strconv.ParseFloat(n, 64)
	default:
		return 0, fmt.Errorf("not numeric: %T", v)
	}
}

// toBool coerces any JSON scalar into a boolean, following YAML-style truthiness
// so a client sending "true" (string) or 1 still gets a boolean.
func toBool(v any) bool {
	switch n := v.(type) {
	case bool:
		return n
	case float64:
		return n != 0
	case int:
		return n != 0
	case string:
		s := strings.ToLower(strings.TrimSpace(n))
		return s == "true" || s == "1" || s == "yes" || s == "on"
	default:
		return false
	}
}

func normalizeChatID(s string) string {
	s = strings.TrimSpace(strings.ToLower(s))
	s = strings.TrimPrefix(s, "https://t.me/")
	s = strings.TrimPrefix(s, "http://t.me/")
	s = strings.TrimPrefix(s, "t.me/")
	s = strings.TrimPrefix(s, "@")
	return strings.TrimSpace(s)
}

func checkRuleEndpoints(source, target, intermediate string, useIntermediate bool) (bool, string) {
	sn := normalizeChatID(source)
	tn := normalizeChatID(target)
	if sn == "" {
		return false, "source_empty"
	}
	if tn == "" {
		return false, "target_empty"
	}
	if sn == tn {
		return false, "loop_detected"
	}
	if useIntermediate && intermediate != "" {
		in := normalizeChatID(intermediate)
		if in != "" && (in == sn || in == tn) {
			return false, "loop_detected"
		}
	}
	return true, "ok"
}

// isNumericChatID reports whether the chat id is a numeric Telegram id
// (optionally -100-prefixed channel id, negative group id or positive user id).
// The dispatcher does `int(chat_id)` before sending, so any username in a rule
// would crash delivery at dispatch time — rules must always store numeric ids.
func isNumericChatID(id string) bool {
	_, err := strconv.ParseInt(strings.TrimSpace(id), 10, 64)
	return err == nil
}

// resolveChat turns a username/link into a numeric chat id via the core
// session. Returns ("", "") when no resolution was needed or possible without
// error; (id, "") on success; ("", reason) when resolution failed.
// ownerID scopes the session choice to the caller's own account.
func (h *Handlers) resolveChat(ctx context.Context, sessionID, handle string, ownerID int64) (string, string) {
	norm := normalizeChatID(handle)
	if norm == "" || isNumericChatID(norm) {
		return "", "" // nothing to resolve
	}
	resp, err := h.c.Sessions.ResolveChat(ctx, &pb.ResolveChatRequest{
		SessionId:   sessionID,
		Handle:      handle,
		OwnerUserId: ownerID,
	})
	if err != nil {
		return "", "resolve_failed: " + err.Error()
	}
	if !resp.Found {
		return "", "resolve_failed: " + resp.Error
	}
	return resp.ChatId, ""
}

// decodedJSONObject unmarshals a JSON object field, tolerating "" / garbage so a
// malformed column can never fail the whole response.
func decodedJSONObject(raw string) any {
	if raw == "" {
		return map[string]any{}
	}
	var out any
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return map[string]any{}
	}
	if out == nil {
		return map[string]any{}
	}
	return out
}

func decodedJSONValue(raw string) any {
	if raw == "" {
		return nil
	}
	var out any
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil
	}
	return out
}

// preferString returns the proto field when it is set, otherwise the value that
// came in through metadata. Older clients (and the bot) store topic_id/prompt_id
// in metadata, so the response must surface whichever one actually holds a value
// instead of reporting the empty proto field.
func preferString(protoValue string, fallback any) any {
	if protoValue != "" {
		return protoValue
	}
	if fallback == nil {
		return ""
	}
	return fallback
}

func containsString(list []string, want string) bool {
	for _, v := range list {
		if v == want {
			return true
		}
	}
	return false
}

// extractUpdateMask reads the field list for a true partial update.
//
// proto3 scalars cannot distinguish "unset" from "zero", so without a mask a
// caller can never set delay_seconds to 0 or is_paused to false. The mask is
// read from the body (protojson would discard an unknown key) or, failing that,
// from ?update_mask=a,b,c so it works with an ordinary HTML form or curl call.
func extractUpdateMask(r *http.Request, body []byte) []string {
	var asObject map[string]json.RawMessage
	if err := json.Unmarshal(body, &asObject); err == nil {
		for _, key := range []string{"update_mask", "updateMask"} {
			if raw, ok := asObject[key]; ok {
				var list []string
				if err := json.Unmarshal(raw, &list); err == nil {
					return cleanMask(list)
				}
				var single string
				if err := json.Unmarshal(raw, &single); err == nil {
					return cleanMask([]string{single})
				}
			}
		}
	}
	for _, key := range []string{"update_mask", "updateMask", "fields"} {
		if q := r.URL.Query().Get(key); q != "" {
			return cleanMask(splitCSV(q))
		}
	}
	return nil
}

func splitCSV(raw string) []string {
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func cleanMask(in []string) []string {
	out := make([]string, 0, len(in))
	for _, f := range in {
		if f = strings.TrimSpace(f); f != "" && !containsString(out, f) {
			out = append(out, f)
		}
	}
	return out
}

// resolveSourceChats canonicalises a task's whole source list.
//
// A task can forward from several channels into one target. `source_chat_id`
// stays authoritative (it is what the dispatcher and older clients read) and is
// always the first entry of the resolved list, so a client that only knows the
// single-source field keeps working unchanged.
func (h *Handlers) resolveSourceChats(ctx context.Context, sessionID string, rule *pb.ForwardRule, ownerID int64) ([]string, string) {
	resolved, errStr := h.resolveEndpointChats(ctx, sessionID, rule.SourceChatId, rule.SourceChatIds, ownerID, "source_empty", "duplicate_source:")
	if errStr != "" {
		return nil, errStr
	}
	rule.SourceChatIds = resolved
	rule.SourceChatId = resolved[0]
	return resolved, ""
}

// resolveTargetChats canonicalises a task's whole target list, mirroring
// resolveSourceChats.
//
// A task can forward from several channels into several targets, and
// `ForwardRule.all_targets` in the core reads `target_chat_id` first, so that
// field stays authoritative and is always the first entry of the resolved list.
// Every target is resolved, not just the primary, so a multi-target task cannot
// store a raw username — the dispatcher does `int(chat_id)` before sending.
func (h *Handlers) resolveTargetChats(ctx context.Context, sessionID string, rule *pb.ForwardRule, ownerID int64) ([]string, string) {
	resolved, errStr := h.resolveEndpointChats(ctx, sessionID, rule.TargetChatId, rule.TargetChatIds, ownerID, "target_empty", "duplicate_target:")
	if errStr != "" {
		return nil, errStr
	}
	rule.TargetChatIds = resolved
	rule.TargetChatId = resolved[0]
	return resolved, ""
}

// resolveEndpointChats canonicalises one endpoint list: every entry is resolved
// through the core, trimmed, empties are dropped, the first-seen order is kept
// and the result is deduped. `legacy` is the single-value field that mirrors
// repeated[0]; it comes first so the mirrored primary keeps its position.
//
// Returns the reason unchanged when a handle cannot be resolved
// (resolveChat's "resolve_failed: ..."), `duplicateCode`+id when an entry
// repeats inside `repeated`, and `emptyCode` when nothing is left.
//
// Duplicate detection deliberately looks only inside `repeated`: a client that
// reads a rule back and re-sends it (GET -> PATCH) sets both the list and its
// mirror to the same id, which is the documented shape and not a duplicate.
func (h *Handlers) resolveEndpointChats(ctx context.Context, sessionID, legacy string, repeated []string, ownerID int64, emptyCode, duplicateCode string) ([]string, string) {
	// Capacity is only a hint; it is deliberately not `len(repeated)+1`, whose
	// size computation is unbounded input arithmetic (and overflows for a
	// request large enough to matter). append grows the slice anyway, so the
	// extra element costs one reallocation at most.
	raw := make([]string, 0, len(repeated))
	if strings.TrimSpace(legacy) != "" {
		raw = append(raw, legacy)
	}
	raw = append(raw, repeated...)
	repeatedFrom := len(raw) - len(repeated) // first index that came from `repeated`

	resolved := make([]string, 0, len(raw))
	seen := make(map[string]struct{}, len(raw))
	seenRepeated := make(map[string]struct{}, len(repeated))
	for i, candidate := range raw {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		numeric, errStr := h.resolveChat(ctx, sessionID, candidate, ownerID)
		if errStr != "" {
			return nil, errStr
		}
		if numeric == "" {
			numeric = candidate
		}
		if i >= repeatedFrom {
			if _, dup := seenRepeated[numeric]; dup {
				return nil, duplicateCode + numeric
			}
			seenRepeated[numeric] = struct{}{}
		}
		if _, dup := seen[numeric]; dup {
			continue // already resolved (the legacy mirror repeats repeated[0])
		}
		seen[numeric] = struct{}{}
		resolved = append(resolved, numeric)
	}
	if len(resolved) == 0 {
		return nil, emptyCode
	}
	return resolved, ""
}

// validateRoute rejects a rule whose source is also one of its targets: such a
// task would forward its own output back into itself. It runs on the resolved
// lists, so an overlap is caught however the two sides were spelled (bare id,
// @username or t.me link). A partial update that touches one side only passes
// the stored other side in, so the check still covers what is not changing.
func validateRoute(sources, targets []string) string {
	if len(sources) == 0 || len(targets) == 0 {
		return ""
	}
	for _, source := range sources {
		if containsString(targets, source) {
			return "source_target_overlap:" + source
		}
	}
	return ""
}

// writeEndpointFailure reports an endpoint or route validation failure the way
// this handler family already does: HTTP 400 with the precise reason in
// "error". The reason doubles as "code" (duplicate_target:-100,
// source_target_overlap:-100, target_empty) so a client can branch on it;
// resolution failures keep the historic source_/target_resolve_failed codes
// while the core's own message stays visible in "error".
func writeEndpointFailure(w http.ResponseWriter, reason, resolveCode string) {
	code := reason
	if strings.HasPrefix(reason, "resolve_failed") {
		code = resolveCode
	}
	writeJSON(w, http.StatusBadRequest, map[string]string{"error": reason, "code": code})
}

// primaryChatID returns the effective primary endpoint of one side: the legacy
// single-value field when it is set, otherwise the first list entry. A list-only
// task (source_chat_ids/target_chat_ids and no scalar) still has a primary, and
// that is the value the endpoint checks and the mirror have to look at.
func primaryChatID(legacy string, list []string) string {
	if v := strings.TrimSpace(legacy); v != "" {
		return v
	}
	for _, v := range list {
		if v = strings.TrimSpace(v); v != "" {
			return v
		}
	}
	return ""
}

// storedChatList returns the endpoints a stored rule effectively has, legacy
// scalar first, so a one-sided update can be validated against the side it is
// not changing.
func storedChatList(legacy string, repeated []string) []string {
	out := make([]string, 0, len(repeated)+1)
	if v := strings.TrimSpace(legacy); v != "" {
		out = append(out, v)
	}
	for _, v := range repeated {
		if v = strings.TrimSpace(v); v != "" {
			out = append(out, v)
		}
	}
	return out
}

// maskNamesAny reports whether an update mask names any of the given fields.
func maskNamesAny(mask []string, names ...string) bool {
	for _, name := range names {
		if containsString(mask, name) {
			return true
		}
	}
	return false
}

// mergeChatIdTextFields folds the comma-separated convenience fields
// source_chat_ids_text / target_chat_ids_text into the repeated lists.
//
// They are accepted because both handlers unmarshal with DiscardUnknown, so an
// extra JSON key is ignored instead of failing the request — a form post or bot
// message can then carry one string instead of an array. Split on ASCII ',' and
// on the Persian '،' the Persian UI emits, trimmed and deduped in order.
func mergeChatIdTextFields(body []byte, rule *pb.ForwardRule, withSource, withTarget bool) {
	if len(body) == 0 || rule == nil || (!withSource && !withTarget) {
		return
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return
	}
	if withSource {
		rule.SourceChatIds = mergeChatIdText(rule.SourceChatIds, raw["source_chat_ids_text"])
	}
	if withTarget {
		rule.TargetChatIds = mergeChatIdText(rule.TargetChatIds, raw["target_chat_ids_text"])
	}
}

func mergeChatIdText(existing []string, raw json.RawMessage) []string {
	if len(raw) == 0 {
		return existing
	}
	var text string
	if err := json.Unmarshal(raw, &text); err != nil {
		return existing
	}
	out := existing
	for _, id := range splitChatIdText(text) {
		if !containsString(out, id) {
			out = append(out, id)
		}
	}
	return out
}

func splitChatIdText(raw string) []string {
	fields := strings.FieldsFunc(raw, func(r rune) bool { return r == ',' || r == '،' })
	out := make([]string, 0, len(fields))
	for _, f := range fields {
		if f = strings.TrimSpace(f); f != "" && !containsString(out, f) {
			out = append(out, f)
		}
	}
	return out
}

// applyMaskToRule clears every field the mask does not name, so a partial update
// can only ever touch what the caller listed.
//
// The core enforces the same mask, but relying on that alone would make the
// gateway's behaviour depend on the core's discipline: any future code path that
// forgot the mask would silently apply the whole body. Clearing here means an
// unnamed field is not even on the wire.
//
// session_id/id/owner_user_id are exempt: the handler needs them to resolve the
// endpoints, scope the rule, and forward the caller's identity.
func applyMaskToRule(rule *pb.ForwardRule, mask []string) {
	if len(mask) == 0 || rule == nil {
		return
	}
	named := make(map[string]struct{}, len(mask))
	for _, f := range mask {
		named[normalizeFieldName(f)] = struct{}{}
	}
	msg := rule.ProtoReflect()
	msg.Range(func(fd protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
		if _, ok := named[normalizeFieldName(string(fd.Name()))]; ok {
			return true
		}
		if _, ok := named[normalizeFieldName(fd.JSONName())]; ok {
			return true
		}
		switch fd.Name() {
		case "session_id", "id", "owner_user_id":
			return true
		}
		msg.Clear(fd)
		return true
	})
}

// normalizeFieldName folds snake_case and camelCase together so a mask entry
// matches whether the client wrote delay_seconds or delaySeconds.
func normalizeFieldName(name string) string {
	return strings.ToLower(strings.ReplaceAll(name, "_", ""))
}

func (h *Handlers) CreateRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "cannot read body: " + err.Error()})
		return
	}
	var rule pb.ForwardRule
	opts := protojson.UnmarshalOptions{DiscardUnknown: true}
	if err := opts.Unmarshal(body, &rule); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	enrichCustomMetadata(body, &rule)
	// The comma-separated convenience form of the endpoint lists, for clients
	// that post one string instead of an array.
	mergeChatIdTextFields(body, &rule, true, true)

	// A list-only task (source_chat_ids/target_chat_ids without the legacy
	// scalar) still has a primary endpoint: the first list entry.
	rawSource := primaryChatID(rule.SourceChatId, rule.SourceChatIds)
	rawTarget := primaryChatID(rule.TargetChatId, rule.TargetChatIds)
	if ok, errCode := checkRuleEndpoints(rawSource, rawTarget, rule.IntermediateChannelId, rule.UseIntermediate); !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "Invalid rule endpoints: " + errCode,
			"code":  errCode,
		})
		return
	}
	// Always store canonical numeric chat ids: the dispatcher calls
	// int(chat_id) before sending, so a username would break delivery. Both the
	// source and the target list are resolved and mirrored onto their
	// single-value fields (see resolveSourceChats/resolveTargetChats).
	ctx, cancel0 := withTimeout(r)
	resolvedSources, errS := h.resolveSourceChats(ctx, rule.SessionId, &rule, id.UserID)
	cancel0()
	if errS != "" {
		writeEndpointFailure(w, errS, "source_resolve_failed")
		return
	}
	if rule.SourceChatName == "" {
		rule.SourceChatName = resolvedSources[0]
	}
	ctx, cancel1 := withTimeout(r)
	resolvedTargets, errT := h.resolveTargetChats(ctx, rule.SessionId, &rule, id.UserID)
	cancel1()
	if errT != "" {
		writeEndpointFailure(w, errT, "target_resolve_failed")
		return
	}
	if rule.TargetChatName == "" && !isNumericChatID(normalizeChatID(rawTarget)) {
		// A resolved handle makes a friendlier display name than the numeric id.
		rule.TargetChatName = resolvedTargets[0]
	}
	// Fail closed on a self-loop: a source that is also a target would forward
	// the task's own output back into itself.
	if errO := validateRoute(resolvedSources, resolvedTargets); errO != "" {
		writeEndpointFailure(w, errO, "route_conflict")
		return
	}
	if rule.UseIntermediate && rule.IntermediateChannelId != "" {
		ctx, cancel2 := withTimeout(r)
		iid, errI := h.resolveChat(ctx, rule.SessionId, rule.IntermediateChannelId, id.UserID)
		cancel2()
		if errI != "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": errI, "code": "intermediate_resolve_failed"})
			return
		}
		if iid != "" {
			rule.IntermediateChannelId = iid
		}
	}
	rule.OwnerUserId = id.UserID
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Rules.CreateRule(ctx, &pb.CreateRuleRequest{Rule: &rule})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, formatRuleJSON(resp))
}

func (h *Handlers) UpdateRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	// Ownership check via scoped list — same guard as GetRule/DeleteRule,
	// otherwise a caller could mutate another tenant's rule by guessing its id.
	listResp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	owned := false
	var current *pb.ForwardRule
	for _, rl := range listResp.Rules {
		if rl.OwnerUserId == id.UserID && rl.Id == r.PathValue("id") {
			owned = true
			current = rl
			break
		}
	}
	if !owned {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "cannot read body: " + err.Error()})
		return
	}
	updateMask := extractUpdateMask(r, body)
	var rule pb.ForwardRule
	opts := protojson.UnmarshalOptions{DiscardUnknown: true}
	if err := opts.Unmarshal(body, &rule); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	enrichCustomMetadata(body, &rule)

	// Which endpoint lists is the caller touching? Without a mask every field
	// that arrived counts as touched (legacy full-body semantics).
	sourceTouched, targetTouched := true, true
	if len(updateMask) > 0 {
		sourceTouched = maskNamesAny(updateMask, "source_chat_id", "source_chat_ids", "source_chat_ids_text")
		targetTouched = maskNamesAny(updateMask, "target_chat_id", "target_chat_ids", "target_chat_ids_text")
		// Drop everything the mask does not name before anything else runs, so
		// the resolver and the core both see only the fields the caller actually
		// asked to change.
		applyMaskToRule(&rule, updateMask)
	}

	rule.Id = r.PathValue("id")
	// With a mask, an absent field means "leave alone" — so the endpoint values
	// the resolver needs are taken from the stored rule instead of the (empty)
	// request body.
	if len(updateMask) > 0 {
		if current != nil {
			if rule.SessionId == "" {
				rule.SessionId = current.SessionId
			}
		}
		if !sourceTouched {
			rule.SourceChatId, rule.SourceChatIds = "", nil
		}
		if !targetTouched {
			rule.TargetChatId, rule.TargetChatIds = "", nil
		}
		if !containsString(updateMask, "intermediate_channel_id") {
			rule.IntermediateChannelId = ""
		}
	}
	// The *_text convenience fields are not proto fields, so applyMaskToRule
	// cannot see them: merge them after the mask, and only for the sides the
	// mask actually names.
	mergeChatIdTextFields(body, &rule, sourceTouched, targetTouched)

	srcPrimary := primaryChatID(rule.SourceChatId, rule.SourceChatIds)
	tgtPrimary := primaryChatID(rule.TargetChatId, rule.TargetChatIds)
	if srcPrimary != "" && tgtPrimary != "" {
		if ok, errCode := checkRuleEndpoints(srcPrimary, tgtPrimary, rule.IntermediateChannelId, rule.UseIntermediate); !ok {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "Invalid rule endpoints: " + errCode,
				"code":  errCode,
			})
			return
		}
	}
	// Canonicalize any username/link endpoints to numeric ids (same contract
	// as CreateRule — see the resolveChat doc comment). Every source *and* every
	// target is resolved, not just the primary, so a multi-source/multi-target
	// task cannot store a raw username.
	var resolvedSources, resolvedTargets []string
	if len(rule.SourceChatIds) > 0 || rule.SourceChatId != "" {
		var errS string
		if resolvedSources, errS = h.resolveSourceChats(ctx, rule.SessionId, &rule, id.UserID); errS != "" {
			writeEndpointFailure(w, errS, "source_resolve_failed")
			return
		}
	}
	if len(rule.TargetChatIds) > 0 || rule.TargetChatId != "" {
		var errT string
		if resolvedTargets, errT = h.resolveTargetChats(ctx, rule.SessionId, &rule, id.UserID); errT != "" {
			writeEndpointFailure(w, errT, "target_resolve_failed")
			return
		}
	}
	// A partial update usually changes one side only, but the route still has to
	// be checked as a whole — otherwise a target could be pointed at its own
	// source. The untouched side comes from the stored rule.
	if len(updateMask) > 0 && current != nil {
		if resolvedSources == nil && resolvedTargets != nil {
			resolvedSources = storedChatList(current.SourceChatId, current.SourceChatIds)
		}
		if resolvedTargets == nil && resolvedSources != nil {
			resolvedTargets = storedChatList(current.TargetChatId, current.TargetChatIds)
		}
	}
	if errO := validateRoute(resolvedSources, resolvedTargets); errO != "" {
		writeEndpointFailure(w, errO, "route_conflict")
		return
	}
	// The core applies exactly the masked fields, so naming only the list would
	// leave the single-value field the dispatcher reads first (all_targets) at
	// its stale value. Name the mirror too, so both end up on the new primary.
	if len(updateMask) > 0 {
		if resolvedSources != nil && containsString(updateMask, "source_chat_ids") && !containsString(updateMask, "source_chat_id") {
			updateMask = append(updateMask, "source_chat_id")
		}
		if resolvedTargets != nil && containsString(updateMask, "target_chat_ids") && !containsString(updateMask, "target_chat_id") {
			updateMask = append(updateMask, "target_chat_id")
		}
	}
	if rule.UseIntermediate && rule.IntermediateChannelId != "" {
		if iid, errI := h.resolveChat(ctx, rule.SessionId, rule.IntermediateChannelId, id.UserID); errI != "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": errI, "code": "intermediate_resolve_failed"})
			return
		} else if iid != "" {
			rule.IntermediateChannelId = iid
		}
	}
	rule.OwnerUserId = id.UserID
	resp, err := h.c.Rules.UpdateRule(ctx, &pb.UpdateRuleRequest{Rule: &rule, UpdateMask: updateMask})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, formatRuleJSON(resp))
}

func (h *Handlers) DuplicateRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	listResp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	var target *pb.ForwardRule
	for _, rule := range listResp.Rules {
		if rule.OwnerUserId == id.UserID && rule.Id == r.PathValue("id") {
			target = rule
			break
		}
	}
	if target == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}

	// proto.Clone is required: pb.ForwardRule embeds protoimpl.MessageState
	// which contains a sync.Mutex — a shallow `*target` copy trips `go vet`
	// ("assignment copies lock value") and would share the mutex.
	cloned := proto.Clone(target).(*pb.ForwardRule)
	cloned.Id = fmt.Sprintf("%s_copy_%d", target.Id, time.Now().Unix()%100000)
	if cloned.Name != "" {
		cloned.Name = cloned.Name + " (Copy)"
	} else {
		cloned.Name = "Copy of " + target.Id
	}
	cloned.OwnerUserId = id.UserID
	cloned.CreatedAt = time.Now().Unix()
	cloned.UpdatedAt = time.Now().Unix()

	resp, err := h.c.Rules.CreateRule(ctx, &pb.CreateRuleRequest{Rule: cloned})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, formatRuleJSON(resp))
}

func (h *Handlers) StopRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	listResp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	var target *pb.ForwardRule
	for _, rule := range listResp.Rules {
		if rule.OwnerUserId == id.UserID && rule.Id == r.PathValue("id") {
			target = rule
			break
		}
	}
	if target == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}

	target.IsActive = false
	resp, err := h.c.Rules.UpdateRule(ctx, &pb.UpdateRuleRequest{Rule: target})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, formatRuleJSON(resp))
}

func (h *Handlers) RestartRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	listResp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	var target *pb.ForwardRule
	for _, rule := range listResp.Rules {
		if rule.OwnerUserId == id.UserID && rule.Id == r.PathValue("id") {
			target = rule
			break
		}
	}
	if target == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}

	target.IsActive = true
	target.IsPaused = false
	target.PausedUntil = 0
	resp, err := h.c.Rules.UpdateRule(ctx, &pb.UpdateRuleRequest{Rule: target})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, formatRuleJSON(resp))
}

// GetRuleSettings returns the effective per-task settings plus the schema that
// describes them, so a client renders controls from server metadata instead of
// hard-coding a list that drifts from the bot's own panels.
func (h *Handlers) GetRuleSettings(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	if !h.ownsRule(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}
	resp, err := h.c.Rules.GetRuleSettings(ctx, &pb.RuleSettingsRequest{
		RuleId:      r.PathValue("id"),
		OwnerUserId: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeSettingsResponse(w, http.StatusOK, resp, 0)
}

// UpdateRuleSettings applies a validated batch of per-task settings. Validation
// lives on the server: an unknown name, a wrong type or an out-of-range value is
// rejected here rather than stored and tripped over at forwarding time.
func (h *Handlers) UpdateRuleSettings(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	if !h.ownsRule(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "cannot read body: " + err.Error()})
		return
	}
	settings, replaceAll, err := parseSettingsBody(body)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error(), "code": "invalid_body"})
		return
	}
	encoded, err := json.Marshal(settings)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error(), "code": "invalid_body"})
		return
	}
	resp, err := h.c.Rules.UpdateRuleSettings(ctx, &pb.UpdateRuleSettingsRequest{
		RuleId:       r.PathValue("id"),
		OwnerUserId:  id.UserID,
		SettingsJson: string(encoded),
		ReplaceAll:   replaceAll,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeSettingsResponse(w, http.StatusOK, resp, len(settings))
}

// GetRuleSettingsSchema describes every setting without touching a rule, so a
// client can build the whole editor before the user has one.
func (h *Handlers) GetRuleSettingsSchema(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Rules.GetRuleSettingsSchema(ctx, &pb.RuleSettingsSchemaRequest{})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeSettingsResponse(w, http.StatusOK, resp, 0)
}

// parseSettingsBody accepts {"settings": {...}, "replace_all": true} and the bare
// {"setting": value, ...} shape, because the second is what a hand-written curl
// call naturally looks like.
func parseSettingsBody(body []byte) (map[string]any, bool, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, false, fmt.Errorf("invalid JSON: %w", err)
	}
	replaceAll := false
	if v, ok := raw["replace_all"]; ok {
		_ = json.Unmarshal(v, &replaceAll)
	} else if v, ok := raw["replaceAll"]; ok {
		_ = json.Unmarshal(v, &replaceAll)
	}
	if v, ok := raw["settings"]; ok {
		var settings map[string]any
		if err := json.Unmarshal(v, &settings); err != nil {
			return nil, false, fmt.Errorf("settings must be an object")
		}
		return settings, replaceAll, nil
	}
	delete(raw, "replace_all")
	delete(raw, "replaceAll")
	settings := make(map[string]any, len(raw))
	for k, v := range raw {
		var decoded any
		if err := json.Unmarshal(v, &decoded); err != nil {
			return nil, false, fmt.Errorf("invalid value for %s", k)
		}
		settings[k] = decoded
	}
	return settings, replaceAll, nil
}

func writeSettingsResponse(w http.ResponseWriter, status int, resp *pb.RuleSettingsResponse, requested int) {
	if resp == nil {
		writeJSON(w, status, map[string]any{"success": false, "error": "empty response"})
		return
	}
	out := map[string]any{
		"success": resp.GetSuccess(),
		"message": resp.GetMessage(),
	}
	if code := resp.GetErrorCode(); code != "" {
		out["error_code"] = code
	}
	if resp.GetSettingsJson() != "" {
		var settings map[string]any
		if err := json.Unmarshal([]byte(resp.GetSettingsJson()), &settings); err == nil {
			out["settings"] = settings
		}
	}
	if resp.GetSchemaJson() != "" {
		var schema map[string]any
		if err := json.Unmarshal([]byte(resp.GetSchemaJson()), &schema); err == nil {
			out["schema"] = schema
		}
	}
	if !resp.GetSuccess() {
		// A rejected write is a client error, not a 200 with a false flag, so the
		// caller cannot mistake it for success by checking the status alone.
		writeJSON(w, settingsErrorStatus(resp.GetErrorCode()), out)
		return
	}
	if requested > 0 {
		out["requested"] = requested
	}
	writeJSON(w, status, out)
}

func settingsErrorStatus(code string) int {
	switch code {
	case "not_found":
		return http.StatusNotFound
	case "unauthorized":
		return http.StatusForbidden
	default:
		return http.StatusBadRequest
	}
}

func (h *Handlers) DeleteRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	// Ownership check via scoped list first
	listResp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	owned := false
	for _, rule := range listResp.Rules {
		if rule.OwnerUserId == id.UserID && rule.Id == r.PathValue("id") {
			owned = true
			break
		}
	}
	if !owned {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}
	resp, err := h.c.Rules.DeleteRule(ctx, &pb.DeleteRuleRequest{
		Id:          r.PathValue("id"),
		OwnerUserId: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) GetRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	rule, err := h.fetchOwnedRule(ctx, id.UserID, r.PathValue("id"))
	if err != nil {
		grpcError(w, err)
		return
	}
	if rule == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "Rule not found"})
		return
	}
	writeJSON(w, http.StatusOK, formatRuleJSON(rule))
}

// fetchOwnedRule returns the caller's rule by ID via the owner-scoped
// single-row RPC (GetRule). Returns nil (not an error) when the rule
// does not exist or belongs to another tenant — fail-closed.
func (h *Handlers) fetchOwnedRule(ctx context.Context, ownerUserID int64, ruleID string) (*pb.ForwardRule, error) {
	if ownerUserID == 0 || ruleID == "" {
		return nil, nil
	}
	rule, err := h.c.Rules.GetRule(ctx, &pb.GetRuleRequest{RuleId: ruleID, OwnerUserId: ownerUserID})
	if err != nil {
		if st, ok := status.FromError(err); ok && st.Code() == codes.NotFound {
			return nil, nil
		}
		return nil, err
	}
	if rule == nil || rule.Id == "" || rule.OwnerUserId != ownerUserID {
		return nil, nil // defense-in-depth: never trust the server blindly
	}
	return rule, nil
}

func (h *Handlers) ToggleRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	target, err := h.fetchOwnedRule(ctx, id.UserID, r.PathValue("id"))
	if err != nil {
		grpcError(w, err)
		return
	}
	if target == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "Rule not found"})
		return
	}
	target.IsActive = !target.IsActive
	upResp, err := h.c.Rules.UpdateRule(ctx, &pb.UpdateRuleRequest{Rule: target})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, formatRuleJSON(upResp))
}

func (h *Handlers) RoutePathQuickSet(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	var body struct {
		ForwardMode  string `json:"forward_mode"`
		CustomHeader string `json:"custom_header"`
	}
	if !bind(w, r, &body) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	var target *pb.ForwardRule
	for _, rule := range resp.Rules {
		if rule.OwnerUserId == id.UserID && rule.Id == r.PathValue("id") {
			target = rule
			break
		}
	}
	if target == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "Rule not found"})
		return
	}
	if body.ForwardMode != "" {
		if val, ok := pb.ForwardMode_value[body.ForwardMode]; ok {
			target.ForwardMode = pb.ForwardMode(val)
		} else {
			switch body.ForwardMode {
			case "COPY", "copy", "COPY_MESSAGE":
				target.ForwardMode = pb.ForwardMode_COPY_MESSAGE
			case "FORWARD", "forward", "DIRECT", "direct", "DIRECT_FORWARD":
				target.ForwardMode = pb.ForwardMode_DIRECT_FORWARD
			case "CUSTOM_HEADER", "custom_header", "HEADER", "header", "CUSTOM_HEADER_COPY":
				target.ForwardMode = pb.ForwardMode_CUSTOM_HEADER_COPY
			}
		}
	}
	if body.CustomHeader != "" {
		target.CustomHeader = body.CustomHeader
	}
	upResp, err := h.c.Rules.UpdateRule(ctx, &pb.UpdateRuleRequest{Rule: target})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, upResp)
}

// ------------------------------------------------- smart forwarding rules
func (h *Handlers) TestRule(w http.ResponseWriter, r *http.Request) {
	var req struct {
		RuleId              string `json:"rule_id"`
		SampleText          string `json:"sample_text"`
		SampleChatId        string `json:"sample_chat_id"`
		SampleSenderId      string `json:"sample_sender_id"`
		ForwardOriginChatId string `json:"forward_origin_chat_id"`
		ForwardOriginUser   string `json:"forward_origin_username"`
		ForwardOriginTitle  string `json:"forward_origin_title"`
		SampleHasMedia      bool   `json:"sample_has_media"`
		SampleMediaType     string `json:"sample_media_type"`
	}
	if !bind(w, r, &req) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Rules.TestRule(ctx, &pb.TestRuleRequest{
		RuleId:                req.RuleId,
		SampleText:            req.SampleText,
		SampleChatId:          req.SampleChatId,
		SampleSenderId:        req.SampleSenderId,
		ForwardOriginChatId:   req.ForwardOriginChatId,
		ForwardOriginUsername: req.ForwardOriginUser,
		ForwardOriginTitle:    req.ForwardOriginTitle,
		SampleHasMedia:        req.SampleHasMedia,
		SampleMediaType:       req.SampleMediaType,
		OwnerUserId:           identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) PauseRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	if !h.ownsRule(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "Rule not found"})
		return
	}
	var body struct {
		Until int64 `json:"until"`
	}
	_ = bind(w, r, &body)
	resp, err := h.c.Rules.PauseRule(ctx, &pb.PauseRuleRequest{
		RuleId:      r.PathValue("id"),
		Until:       body.Until,
		OwnerUserId: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) ResumeRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	if !h.ownsRule(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "Rule not found"})
		return
	}
	resp, err := h.c.Rules.ResumeRule(ctx, &pb.PauseRuleRequest{
		RuleId:      r.PathValue("id"),
		OwnerUserId: identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ownsRule reports whether the given rule id belongs to owner. Keeps the
// authenticated surface tenant-isolated (no cross-user pause/resume/edit).
func (h *Handlers) ownsRule(ctx context.Context, ruleID string, owner int64) bool {
	if owner == 0 {
		return false
	}
	resp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{OwnerUserId: owner})
	if err != nil {
		return false
	}
	for _, rule := range resp.Rules {
		if rule.OwnerUserId == owner && rule.Id == ruleID {
			return true
		}
	}
	return false
}

// ownsFilter / ownsAIConfig mirror ownsRule: the caller may only touch a
// filter or AI config that their own account owns.
func (h *Handlers) ownsFilter(ctx context.Context, filterID string, owner int64) bool {
	if owner == 0 {
		return false
	}
	resp, err := h.c.Filters.ListFilters(ctx, &pb.ListFiltersRequest{OwnerUserId: owner})
	if err != nil {
		return false
	}
	for _, f := range resp.Filters {
		if f.OwnerUserId == owner && f.Id == filterID {
			return true
		}
	}
	return false
}

func (h *Handlers) ownsAIConfig(ctx context.Context, configID string, owner int64) bool {
	if owner == 0 {
		return false
	}
	resp, err := h.c.AI.ListAIConfigs(ctx, &pb.ListAIConfigsRequest{OwnerUserId: owner})
	if err != nil {
		return false
	}
	for _, c := range resp.Configs {
		if c.OwnerUserId == owner && c.Id == configID {
			return true
		}
	}
	return false
}

// ------------------------------------------------------------- delivery
func (h *Handlers) DeliveryStats(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Delivery.GetDeliveryStats(ctx, &pb.DeliveryStatsRequest{OwnerUserId: identity(r).UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	rules := resp.Rules
	if rules == nil {
		rules = []*pb.RuleLiveStat{}
	}
	rulesOut := make([]map[string]any, 0, len(rules))
	for _, st := range rules {
		rulesOut = append(rulesOut, map[string]any{
			"rule_id":         st.RuleId,
			"rule_name":       st.RuleName,
			"is_active":       st.IsActive,
			"is_paused":       st.IsPaused,
			"forwarded":       st.Forwarded,
			"filtered":        st.Filtered,
			"errors":          st.Errors,
			"last_forward_ts": st.LastForwardTs,
			"last_error":      st.LastError,
		})
	}

	errors := resp.Errors
	if errors == nil {
		errors = []*pb.RecentError{}
	}
	errsOut := make([]map[string]any, 0, len(errors))
	for _, e := range errors {
		errsOut = append(errsOut, map[string]any{
			"ts":         e.Ts,
			"rule_id":    e.RuleId,
			"error_name": e.ErrorName,
			"severity":   e.Severity,
			"detail":     e.Detail,
			"category":   e.Category,
		})
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"processed_total":     resp.ProcessedTotal,
		"forwarded_total":     resp.ForwardedTotal,
		"failed_total":        resp.FailedTotal,
		"dedup_skipped_total": resp.DedupSkippedTotal,
		"filtered_total":      resp.FilteredTotal,
		"in_queue":            resp.InQueue,
		"dead_lettered_total": resp.DeadLetteredTotal,
		"retry_total":         resp.RetryTotal,
		"rules":               rulesOut,
		"errors":              errsOut,
	})
}

// Paging bounds for the dead-letter feed. The request field is int32, so the
// query value is parsed at 32-bit width and clamped instead of being narrowed
// from an architecture-dependent int, which wraps negative when a client sends
// a value above 2^31-1.
const (
	defaultDeadLetterLimit int32 = 50
	maxDeadLetterLimit     int32 = 500
)

// queryIntOrDefault is the paging flavour of boundedQueryInt: a list endpoint
// treats an absent, malformed or non-positive value as "use the default"
// instead of erroring, and clamps anything above the cap to the cap. Like
// boundedQueryInt, the parse happens at 32-bit width with a clamp before the
// narrowing conversion, so no architecture-dependent int is ever truncated.
func queryIntOrDefault(raw string, lo, hi, def int32) int32 {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return def
	}
	v, err := strconv.ParseInt(raw, 10, 32)
	if err != nil {
		return def
	}
	switch {
	case v < int64(lo):
		return def
	case v > int64(hi):
		return hi
	default:
		return int32(v)
	}
}

func (h *Handlers) ListDeadLetter(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	limit := queryIntOrDefault(
		r.URL.Query().Get("limit"), 1, maxDeadLetterLimit, defaultDeadLetterLimit,
	)
	var since int64
	if n := r.URL.Query().Get("since"); n != "" {
		if v, err := strconv.ParseInt(n, 10, 64); err == nil {
			since = v
		}
	}
	resp, err := h.c.Delivery.ListDeadLetter(ctx, &pb.ListDeadLetterRequest{
		RuleId:      r.URL.Query().Get("rule_id"),
		Limit:       limit,
		Since:       since,
		OwnerUserId: identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	entries := resp.Entries
	if entries == nil {
		entries = []*pb.DeadLetterEntry{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"jobs":    entries,
		"entries": entries,
		"total":   len(entries),
	})
}

func (h *Handlers) ReplayDeadLetter(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Delivery.ReplayDeadLetter(ctx, &pb.ReplayDeadLetterRequest{Id: r.PathValue("id"), OwnerUserId: identity(r).UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) PurgeDeadLetter(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	var body struct {
		RuleId    string `json:"rule_id"`
		OlderThan int64  `json:"older_than"`
	}
	_ = bind(w, r, &body)
	if body.RuleId == "" {
		body.RuleId = r.URL.Query().Get("rule_id")
	}
	resp, err := h.c.Delivery.PurgeDeadLetter(ctx, &pb.PurgeDeadLetterRequest{
		RuleId:      body.RuleId,
		OlderThan:   body.OlderThan,
		OwnerUserId: identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// --------------------------------------------------------------- filters
func (h *Handlers) ListFilters(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Filters.ListFilters(ctx, &pb.ListFiltersRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	filters := make([]*pb.FilterRule, 0, len(resp.Filters))
	for _, filter := range resp.Filters {
		if filter != nil && filter.OwnerUserId == id.UserID {
			filters = append(filters, filter)
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"filters": filters,
		"total":   len(filters),
	})
}

func (h *Handlers) CreateFilter(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	var f pb.FilterRule
	if !bindPB(w, r, &f) {
		return
	}
	f.OwnerUserId = id.UserID
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Filters.CreateFilter(ctx, &pb.CreateFilterRequest{Filter: &f})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (h *Handlers) UpdateFilter(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	if !h.ownsFilter(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "filter not found"})
		return
	}
	var f pb.FilterRule
	if !bindPB(w, r, &f) {
		return
	}
	f.Id = r.PathValue("id")
	f.OwnerUserId = id.UserID
	resp, err := h.c.Filters.UpdateFilter(ctx, &pb.UpdateFilterRequest{Filter: &f})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) DeleteFilter(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	if !h.ownsFilter(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "filter not found"})
		return
	}
	resp, err := h.c.Filters.DeleteFilter(ctx, &pb.DeleteFilterRequest{
		Id:          r.PathValue("id"),
		OwnerUserId: identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// -------------------------------------------------------------------- AI
func (h *Handlers) ListAIConfigs(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AI.ListAIConfigs(ctx, &pb.ListAIConfigsRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	configs := make([]*pb.AIConfig, 0, len(resp.Configs))
	for _, config := range resp.Configs {
		if config != nil && config.OwnerUserId == id.UserID {
			configs = append(configs, config)
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"configs": configs,
		"total":   len(configs),
	})
}

func (h *Handlers) CreateAIConfig(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	var c pb.AIConfig
	if !bindPB(w, r, &c) {
		return
	}
	c.OwnerUserId = id.UserID
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AI.CreateAIConfig(ctx, &pb.CreateAIConfigRequest{Config: &c})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (h *Handlers) UpdateAIConfig(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	if !h.ownsAIConfig(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "AI config not found"})
		return
	}
	var c pb.AIConfig
	if !bindPB(w, r, &c) {
		return
	}
	c.Id = r.PathValue("id")
	c.OwnerUserId = id.UserID
	resp, err := h.c.AI.UpdateAIConfig(ctx, &pb.UpdateAIConfigRequest{Config: &c})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) DeleteAIConfig(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()
	if !h.ownsAIConfig(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "AI config not found"})
		return
	}
	resp, err := h.c.AI.DeleteAIConfig(ctx, &pb.DeleteAIConfigRequest{
		Id:          r.PathValue("id"),
		OwnerUserId: identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

type testAIReq struct {
	SampleText string `json:"sample_text"`
}

// ---------------------------------------------------------------------------
// Prompt templates (AI Rewrite) — shared by bot & dashboard
// ---------------------------------------------------------------------------

func promptToPb(t *promptTemplateDTO) *pb.PromptTemplate {
	return &pb.PromptTemplate{
		Id: t.Id, Name: t.Name, Description: t.Description,
		SystemPrompt: t.SystemPrompt, UserPromptTemplate: t.UserPromptTemplate,
		TargetLanguage: t.TargetLanguage, CurrentVersion: t.CurrentVersion,
		IsSystem: t.IsSystem, CreatedAt: t.CreatedAt, UpdatedAt: t.UpdatedAt,
	}
}

type promptTemplateDTO struct {
	Id                 string `json:"id,omitempty"`
	Name               string `json:"name"`
	Description        string `json:"description,omitempty"`
	SystemPrompt       string `json:"system_prompt"`
	UserPromptTemplate string `json:"user_prompt_template,omitempty"`
	TargetLanguage     string `json:"target_language,omitempty"`
	CurrentVersion     int32  `json:"current_version,omitempty"`
	IsSystem           bool   `json:"is_system,omitempty"`
	CreatedAt          int64  `json:"created_at,omitempty"`
	UpdatedAt          int64  `json:"updated_at,omitempty"`
}

type savePromptReq struct {
	Template        *promptTemplateDTO `json:"template"`
	ExpectedVersion int32              `json:"expected_version"`
}

func (h *Handlers) ListPromptTemplates(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	id := identity(r)
	resp, err := h.c.AI.ListPromptTemplates(ctx, &pb.ListPromptTemplatesRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	templates := resp.Templates
	if templates == nil {
		templates = []*pb.PromptTemplate{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"templates": templates, "total": len(templates)})
}

func (h *Handlers) CreatePromptTemplate(w http.ResponseWriter, r *http.Request) {
	var req savePromptReq
	if !bind(w, r, &req) || req.Template == nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "template required"})
		return
	}
	if req.Template.IsSystem {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "cannot create system prompts", "code": "system_prompt_immutable"})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AI.SavePromptTemplate(ctx, &pb.SavePromptTemplateRequest{Template: promptToPb(req.Template), OwnerUserId: identity(r).UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	if resp.Id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "prompt validation failed or duplicate name"})
		return
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (h *Handlers) UpdatePromptTemplate(w http.ResponseWriter, r *http.Request) {
	var req savePromptReq
	if !bind(w, r, &req) || req.Template == nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "template required"})
		return
	}
	req.Template.Id = r.PathValue("id")
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AI.SavePromptTemplate(ctx, &pb.SavePromptTemplateRequest{
		Template: promptToPb(req.Template), ExpectedVersion: req.ExpectedVersion, OwnerUserId: identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	if resp.Id == "" {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "update failed (system prompt, OCC conflict, or validation)", "code": "prompt_update_conflict"})
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) DeletePromptTemplate(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AI.DeletePromptTemplate(ctx, &pb.DeletePromptTemplateRequest{Id: r.PathValue("id"), OwnerUserId: identity(r).UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": resp.Message})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"})
}

type testPromptReq struct {
	PromptId   string `json:"prompt_id"`
	AiConfigId string `json:"ai_config_id"`
	SampleText string `json:"sample_text"`
	Version    int32  `json:"version"`
}

func (h *Handlers) TestPrompt(w http.ResponseWriter, r *http.Request) {
	var req testPromptReq
	if !bind(w, r, &req) {
		return
	}
	if req.PromptId == "" || req.AiConfigId == "" || req.SampleText == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "prompt_id, ai_config_id and sample_text are required"})
		return
	}
	ctx, cancel := withAITimeout(r)
	defer cancel()
	// Ownership guard: the AI config must belong to the caller.
	if !h.ownsAIConfig(ctx, req.AiConfigId, identity(r).UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "AI config not found"})
		return
	}
	resp, err := h.c.AI.TestPrompt(ctx, &pb.TestPromptRequest{
		PromptId:    req.PromptId,
		AiConfigId:  req.AiConfigId,
		SampleText:  req.SampleText,
		Version:     req.Version,
		OwnerUserId: identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) TestAIRewrite(w http.ResponseWriter, r *http.Request) {
	var req testAIReq
	if !bind(w, r, &req) {
		return
	}
	ctx, cancel := withAITimeout(r)
	defer cancel()
	resp, err := h.c.AI.TestAIRewrite(ctx, &pb.TestAIRewriteRequest{
		AiConfigId:  r.PathValue("id"),
		SampleText:  req.SampleText,
		OwnerUserId: identity(r).UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ----------------------------------------------------------------- stats
func (h *Handlers) GetStats(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	id := identity(r)
	resp, err := h.c.System.GetSystemStats(ctx, &pb.SystemStatsRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"core_running":             resp.CoreRunning,
		"atf_core_online":          resp.CoreRunning,
		"atf_logger_online":        h.c.Logs != nil,
		"uptime_seconds":           resp.UptimeSeconds,
		"active_sessions_count":    resp.ActiveSessionsCount,
		"active_sessions":          resp.ActiveSessionsCount,
		"active_rules_count":       resp.ActiveRulesCount,
		"active_rules":             resp.ActiveRulesCount,
		"total_messages_processed": resp.TotalMessagesProcessed,
		"total_messages_forwarded": resp.TotalMessagesForwarded,
		"total_forwarded_24h":      resp.TotalMessagesForwarded,
		"total_messages_filtered":  resp.TotalMessagesFiltered,
		"total_messages_rewritten": resp.TotalMessagesRewritten,
		"bot_username":             resp.BotUsername,
		"version":                  resp.Version,
	})
}

// ------------------------------------------------------------------ logs

// logFilters is the full v18 query surface. Every field is optional: an empty
// filter means "the whole archive", and offset walks backwards through all of
// it, so an operator can reach the oldest record rather than only the newest
// page.
type logFilters struct {
	Level         string
	Levels        []string
	Service       string
	Category      string
	Search        string
	RuleID        string
	CorrelationID string
	UserID        int64
	Since         int64
	Until         int64
	Limit         int32
	Offset        int32
	Ascending     bool
}

func parseLogFilters(r *http.Request) logFilters {
	q := r.URL.Query()
	f := logFilters{
		Level:         q.Get("level"),
		Service:       q.Get("service"),
		Category:      q.Get("category"),
		Search:        q.Get("search"),
		RuleID:        q.Get("rule_id"),
		CorrelationID: q.Get("correlation_id"),
		Ascending:     q.Get("order") == "asc" || q.Get("ascending") == "true" || q.Get("ascending") == "1",
		Limit:         100,
	}
	// levels=a,b,c (or repeated ?levels=) is the multi-select severity filter.
	for _, raw := range q["levels"] {
		for _, part := range strings.Split(raw, ",") {
			if part = strings.TrimSpace(part); part != "" {
				f.Levels = append(f.Levels, part)
			}
		}
	}
	if v := q.Get("limit"); v != "" {
		if n, ok := parseInt32(v, maxLogPageSize); ok && n > 0 {
			f.Limit = n
		}
	}
	if v := q.Get("offset"); v != "" {
		if n, ok := parseInt32(v, maxLogOffset); ok {
			f.Offset = n
		}
	}
	// page is the friendly 1-based form of offset (page=3&limit=50 -> offset=100).
	// `page` is bounded before the multiplication: (page-1)*limit wraps inside
	// int64 for a page large enough, which would land on a negative offset and
	// silently serve the wrong end of the archive.
	if v := q.Get("page"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n > 1 && n <= int64(maxLogOffset) {
			off := (n - 1) * int64(f.Limit)
			if off > int64(maxLogOffset) {
				off = int64(maxLogOffset)
			}
			f.Offset = int32(off)
		}
	}
	if v := q.Get("since"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n >= 0 {
			f.Since = n
		}
	}
	if v := q.Get("until"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n >= 0 {
			f.Until = n
		}
	}
	// Time window shorthand: ?hours=6 is "the last 6 hours", which is what an
	// operator actually reaches for. `since` wins when both are given.
	if f.Since == 0 {
		if v := q.Get("hours"); v != "" {
			if n, ok := parseInt32(v, 24*366); ok && n > 0 {
				f.Since = time.Now().Add(-time.Duration(n) * time.Hour).Unix()
			}
		}
	}
	// ?mine=1 scopes to the caller's own tenant; admins may name a user_id.
	if v := q.Get("user_id"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n > 0 {
			f.UserID = n
		}
	}
	if f.UserID == 0 && (q.Get("mine") == "1" || q.Get("mine") == "true") {
		f.UserID = identity(r).UserID
	}
	return f
}

// Bounds for the paging window. A caller-supplied offset or page is narrowed to
// int32, so the bound has to be explicit: strconv.Atoi returns an int whose width
// follows the architecture, and an unbounded cast to int32 wraps negative — which
// would turn "the oldest events" into "the newest ones".
const (
	maxLogPageSize int32 = 500
	maxLogOffset   int32 = 1 << 20
)

// parseInt32 parses a non-negative decimal that must fit in [0, max].
//
// Parsing at bitSize 32 does the range check: it returns an error for anything
// outside int32, so the narrowing conversion below cannot wrap. Bounding an
// int64 value by hand instead leaves that safety invisible to static analysis
// (and to the next reader), even when it is correct.
func parseInt32(s string, max int32) (int32, bool) {
	n, err := strconv.ParseInt(s, 10, 32)
	if err != nil || n < 0 || int32(n) > max {
		return 0, false
	}
	return int32(n), true
}

func (f logFilters) toRequest() *pb.QueryLogsRequest {
	return &pb.QueryLogsRequest{
		Level: f.Level, Levels: f.Levels, Service: f.Service, Category: f.Category,
		Search: f.Search, RuleId: f.RuleID, CorrelationId: f.CorrelationID,
		UserId: f.UserID, Since: f.Since, Until: f.Until,
		Limit: f.Limit, Offset: f.Offset, Ascending: f.Ascending,
	}
}

func logEntryJSON(l *pb.LogEntry) map[string]any {
	return map[string]any{
		"ts":             l.Ts,
		"level":          l.Level,
		"severity":       l.Level,
		"service":        l.Service,
		"category":       l.Category,
		"message":        l.Message,
		"detail":         l.Detail,
		"error_name":     l.Category,
		"id":             l.Ts,
		"rule_id":        l.RuleId,
		"user_id":        l.UserId,
		"correlation_id": l.CorrelationId,
	}
}

// QueryLogs returns one page of history plus the real match count, so the UI can
// show "1–50 of 12,431" and keep paging until has_more is false.
func (h *Handlers) QueryLogs(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	if h.c.Logs == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "logger service unavailable",
		})
		return
	}
	f := parseLogFilters(r)
	resp, err := h.c.Logs.QueryLogs(ctx, f.toRequest())
	if err != nil {
		grpcError(w, err)
		return
	}
	logs := resp.Logs
	if logs == nil {
		logs = []*pb.LogEntry{}
	}
	out := make([]map[string]any, 0, len(logs))
	for _, l := range logs {
		out = append(out, logEntryJSON(l))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"logs":     out,
		"total":    resp.Total,
		"count":    len(out),
		"offset":   f.Offset,
		"limit":    f.Limit,
		"has_more": resp.HasMore,
	})
}

// RuleLogs is the per-task view: ownership is checked first, and the rule id is
// forced onto the filter so a caller cannot widen the read past their own task.
func (h *Handlers) RuleLogs(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	if h.c.Logs == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "logger service unavailable",
		})
		return
	}
	ruleID := r.PathValue("id")
	if ruleID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "rule id required"})
		return
	}
	// Fail-closed: a foreign (or unknown) rule 404s rather than leaking its log
	// stream, which contains chat ids and message fragments.
	if !h.ownsRule(ctx, ruleID, identity(r).UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}
	f := parseLogFilters(r)
	f.RuleID = ruleID
	f.UserID = 0 // attribution is via the rule, not the tenant filter
	resp, err := h.c.Logs.QueryLogs(ctx, f.toRequest())
	if err != nil {
		grpcError(w, err)
		return
	}
	logs := resp.Logs
	if logs == nil {
		logs = []*pb.LogEntry{}
	}
	out := make([]map[string]any, 0, len(logs))
	for _, l := range logs {
		out = append(out, logEntryJSON(l))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"rule_id":  ruleID,
		"logs":     out,
		"total":    resp.Total,
		"count":    len(out),
		"offset":   f.Offset,
		"limit":    f.Limit,
		"has_more": resp.HasMore,
	})
}

// ExportLogs streams the filtered history as a downloadable file. It pages
// through the logger in batches rather than raising the page ceiling, so an
// export of a 100k-row archive does not need a 100k-row RPC.
func (h *Handlers) ExportLogs(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	if h.c.Logs == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "logger service unavailable",
		})
		return
	}
	f := parseLogFilters(r)
	f.Ascending = true // an archive is read oldest-first
	format := r.URL.Query().Get("format")
	if format == "" {
		format = "json"
	}
	const batch = 500
	const maxRows = 50000 // hard stop so one request cannot exhaust memory
	var rows []*pb.LogEntry
	var total int32
	for offset := int32(0); len(rows) < maxRows; offset += batch {
		page := f
		page.Limit = batch
		page.Offset = offset
		resp, err := h.c.Logs.QueryLogs(ctx, page.toRequest())
		if err != nil {
			grpcError(w, err)
			return
		}
		total = resp.Total
		rows = append(rows, resp.Logs...)
		if !resp.HasMore || len(resp.Logs) == 0 {
			break
		}
	}
	stamp := time.Now().UTC().Format("20060102-150405")
	switch format {
	case "csv":
		w.Header().Set("Content-Type", "text/csv; charset=utf-8")
		w.Header().Set("Content-Disposition",
			fmt.Sprintf("attachment; filename=\"atf-logs-%s.csv\"", stamp))
		cw := csv.NewWriter(w)
		_ = cw.Write([]string{"ts", "iso_time", "level", "service", "category",
			"rule_id", "user_id", "correlation_id", "message", "detail"})
		for _, l := range rows {
			_ = cw.Write([]string{
				strconv.FormatInt(l.Ts, 10),
				time.Unix(l.Ts, 0).UTC().Format(time.RFC3339),
				l.Level, l.Service, l.Category, l.RuleId,
				strconv.FormatInt(l.UserId, 10), l.CorrelationId,
				l.Message, l.Detail,
			})
		}
		cw.Flush()
	default:
		out := make([]map[string]any, 0, len(rows))
		for _, l := range rows {
			out = append(out, logEntryJSON(l))
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Disposition",
			fmt.Sprintf("attachment; filename=\"atf-logs-%s.json\"", stamp))
		_ = json.NewEncoder(w).Encode(map[string]any{
			"exported_at": time.Now().UTC().Format(time.RFC3339),
			"total_match": total,
			"rows":        len(out),
			"logs":        out,
		})
	}
}

func (h *Handlers) LogStats(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	if h.c.Logs == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "logger service unavailable",
		})
		return
	}
	req := &pb.LogStatsRequest{RuleId: r.URL.Query().Get("rule_id")}
	if v := r.URL.Query().Get("mine"); v == "1" || v == "true" {
		req.UserId = identity(r).UserID
	}
	resp, err := h.c.Logs.LogStats(ctx, req)
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"total":       resp.Total,
		"by_level":    resp.ByLevel,
		"by_category": resp.ByCategory,
		"by_rule":     resp.ByRule,
		"by_service":  resp.ByService,
		"oldest_ts":   resp.OldestTs,
		"newest_ts":   resp.NewestTs,
	})
}

// PurgeLogs deletes log rows from the logger microservice. Filters are all
// optional; older_than_hours (default 24) protects the newest window so a
// blanket purge can't delete the evidence you are actively debugging.
func (h *Handlers) PurgeLogs(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	if h.c.Logs == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "logger service unavailable",
		})
		return
	}
	var body struct {
		Level          string `json:"level"`
		Service        string `json:"service"`
		Category       string `json:"category"`
		RuleID         string `json:"rule_id"`
		OlderThanHours int64  `json:"older_than_hours"`
	}
	_ = bind(w, r, &body)
	q := r.URL.Query()
	if body.Level == "" {
		body.Level = q.Get("level")
	}
	if body.Service == "" {
		body.Service = q.Get("service")
	}
	if body.Category == "" {
		body.Category = q.Get("category")
	}
	if body.RuleID == "" {
		body.RuleID = q.Get("rule_id")
	}
	if body.OlderThanHours == 0 {
		if v := q.Get("older_than_hours"); v != "" {
			if n, err := strconv.ParseInt(v, 10, 64); err == nil && n >= 0 {
				body.OlderThanHours = n
			}
		}
	}
	// Destructive default: never delete the last 24h unless asked explicitly.
	if body.OlderThanHours == 0 {
		body.OlderThanHours = 24
	}
	// A purge scoped to one task must not touch a task the caller does not own.
	if body.RuleID != "" && !h.ownsRule(ctx, body.RuleID, identity(r).UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}
	resp, err := h.c.Logs.PurgeLogs(ctx, &pb.PurgeLogsRequest{
		Level:          body.Level,
		Service:        body.Service,
		Category:       body.Category,
		RuleId:         body.RuleID,
		OlderThanHours: body.OlderThanHours,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error": resp.Error,
			"code":  "purge_failed",
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":   true,
		"purged":    resp.Purged,
		"remaining": resp.Remaining,
	})
}

// ------------------------------------------------------------- internals
func identity(r *http.Request) *Identity {
	if v := r.Context().Value(identityKey{}); v != nil {
		if id, ok := v.(*Identity); ok && id != nil {
			return id
		}
	}
	return &Identity{}
}

// --------------------------------------------------------------- backup/restore

// BackupExport returns a JSON snapshot of everything the caller owns, ready to
// be imported on another server. The bundle contains LIVE CREDENTIALS (session
// strings, AI API keys, api_hash) in plaintext, so an optional passphrase
// encrypts the payload with AES-GCM before it leaves this box.
func (h *Handlers) BackupExport(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	if h.c.Backup == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "core service unavailable"})
		return
	}
	var body struct {
		OwnerUserID int64  `json:"owner_user_id"`
		Passphrase  string `json:"passphrase"`
	}
	_ = bind(w, r, &body)
	// Non-admins can only ever export their own tenant; an admin (or a
	// single-tenant box with no identity) may pass owner_user_id explicitly.
	id := identity(r)
	// v12 hardening: a non-admin caller can only ever export their own tenant.
	// owner_user_id from the body is only honoured for admins; any other value
	// (including a 0 from an unauthenticated reach) is forced to the caller's id.
	requested := id.UserID
	if id.IsAdmin {
		requested = body.OwnerUserID
	}
	resp, err := h.c.Backup.BackupExport(ctx, &pb.BackupExportRequest{
		OwnerUserId: requested,
		Passphrase:  body.Passphrase,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": resp.Error})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success": true,
		"bundle":  resp.Bundle,
		"counts":  resp.Counts,
		// surfaced in the UI so the user knows whether the bytes are protected
		"encrypted": body.Passphrase != "",
	})
}

// BackupImport applies a bundle produced by BackupExport on this or another
// box. Destructive: pass overwrite_existing to replace rows that already exist.
func (h *Handlers) BackupImport(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	if h.c.Backup == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "core service unavailable"})
		return
	}
	var body struct {
		Bundle            string `json:"bundle"`
		Passphrase        string `json:"passphrase"`
		TargetOwnerUserID int64  `json:"target_owner_user_id"`
		OverwriteExisting bool   `json:"overwrite_existing"`
	}
	if !bind(w, r, &body) {
		return
	}
	if strings.TrimSpace(body.Bundle) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bundle is required"})
		return
	}
	// v12 hardening: import lands in the caller's own tenant unless an admin
	// explicitly redirects it. Importing into another tenant would let a
	// non-admin write rules/credentials into someone else's scope.
	id := identity(r)
	target := id.UserID
	if id.IsAdmin {
		target = body.TargetOwnerUserID
	}
	resp, err := h.c.Backup.BackupImport(ctx, &pb.BackupImportRequest{
		Bundle:            body.Bundle,
		Passphrase:        body.Passphrase,
		TargetOwnerUserId: target,
		OverwriteExisting: body.OverwriteExisting,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": resp.Error})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success": true,
		"stats":   resp.Stats,
	})
}

func withTimeout(r *http.Request) (context.Context, func()) {
	return context.WithTimeout(r.Context(), 15*time.Second)
}

// withAITimeout is used by handlers that call LLM providers: model latency can
// comfortably exceed the default 15s request budget, and a client-side deadline
// races the provider's own timeout (see AITransformer), surfacing as a confusing
// "Deadline Exceeded" instead of the real result.
func withAITimeout(r *http.Request) (context.Context, func()) {
	return context.WithTimeout(r.Context(), 90*time.Second)
}

func grpcError(w http.ResponseWriter, err error) {
	st, _ := status.FromError(err)
	switch st.Code() {
	case codes.NotFound:
		writeJSON(w, http.StatusNotFound, map[string]string{"error": st.Message()})
	case codes.Unavailable:
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "core unavailable: " + st.Message()})
	case codes.InvalidArgument:
		code := "invalid_argument"
		if strings.Contains(st.Message(), "loop_detected") {
			code = "loop_detected"
		}
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": st.Message(), "code": code})
	default:
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": st.Message()})
	}
}

// chatListError maps the core's ListChats failures onto the mobile contract.
// It exists because the chat-list error vocabulary is richer than the shared
// grpcError table allows: a session that exists but has no live Telegram client
// is a 503 (the core signals FAILED_PRECONDITION), which grpcError would fold
// into a 500. Unknown/foreign sessions keep the standard 404, and everything
// else (core unreachable => 502, internal => 500) falls back to grpcError.
func chatListError(w http.ResponseWriter, err error) {
	st, _ := status.FromError(err)
	switch st.Code() {
	case codes.FailedPrecondition:
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": st.Message(), "code": "session_not_connected",
		})
	case codes.Unauthenticated:
		writeJSON(w, http.StatusUnauthorized, map[string]string{
			"error": st.Message(), "code": "unauthenticated",
		})
	case codes.NotFound:
		writeJSON(w, http.StatusNotFound, map[string]string{
			"error": st.Message(), "code": "session_not_found",
		})
	default:
		grpcError(w, err)
	}
}

func bind(w http.ResponseWriter, r *http.Request, target any) bool {
	if err := json.NewDecoder(r.Body).Decode(target); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return false
	}
	return true
}

func bindPB(w http.ResponseWriter, r *http.Request, target any) bool {
	// protojson accepts both enum names ("COPY_MESSAGE") and numbers, while
	// encoding/json rejects names entirely — the frontend sends names.
	if msg, ok := target.(proto.Message); ok {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "cannot read body: " + err.Error()})
			return false
		}
		opts := protojson.UnmarshalOptions{DiscardUnknown: true}
		if err := opts.Unmarshal(body, msg); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
			return false
		}
		return true
	}
	if err := json.NewDecoder(r.Body).Decode(target); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(body)
}

// ------------------------------------------------------------- PV Responder
type PVResponderConfig struct {
	Enabled         bool    `json:"enabled"`
	AIConfigID      string  `json:"ai_config_id,omitempty"`
	PersonaPrompt   string  `json:"persona_prompt"`
	TypingDelayMin  float64 `json:"typing_delay_min"`
	TypingDelayMax  float64 `json:"typing_delay_max"`
	CooldownSeconds int     `json:"cooldown_seconds"`
	IgnoreBots      bool    `json:"ignore_bots"`
	HistoryLimit    int     `json:"history_limit"`
}

func defaultPVResponderConfig() PVResponderConfig {
	return PVResponderConfig{
		Enabled: false,
		PersonaPrompt: "شما صاحب این اکانت شخصی تلگرام هستید و دارید مستقیماً به پیام‌های شخصی (پی‌وی) مخاطبان و دوستان خود پاسخ می‌دهید.\n\n" +
			"قوانین حیاتی و شیوه صحبت کردن:\n" +
			"۱. لحن شما باید کاملاً خودمانی، بسیار جذاب، صمیمی، محترمانه و زنده به زبان فارسی باشد؛ دقیقاً مانند یک جوان هوشمند، خوش‌برخورد و امروزی ایرانی در چت تلگرام.\n" +
			"۲. هرگز و تحت هیچ شرایطی نگو که ربات، هوش مصنوعی، مدل زبانی یا دستیار مجازی هستی! وانمود کن که خود کاربر هستی.\n" +
			"۳. پاسخ‌ها کوتاه، صمیمانه، خوش‌طبع و موجز باشند (از پاسخ‌های طولانی، رسمی یا مقاله‌ای پرهیز کن).\n" +
			"۴. از اموجی‌های متناسب و طبیعی چت (مانند 🙏، 😉، 🤝، 😂، ✌️) به‌جا و به اندازه استفاده کن.\n" +
			"۵. اگر سوال فنی یا کاری پرسیدند، هوشمندانه و کوتاه راهنمایی کن. اگر گفت‌وگوی احوال‌پرسی است، گرم و صمیمی باش.",
		TypingDelayMin:  2.0,
		TypingDelayMax:  4.5,
		CooldownSeconds: 15,
		IgnoreBots:      true,
		HistoryLimit:    4,
	}
}

const pvConfigFile = "data/pv_responder_config.json"

func (h *Handlers) GetPVResponder(w http.ResponseWriter, r *http.Request) {
	cfg := defaultPVResponderConfig()
	data, err := os.ReadFile(pvConfigFile)
	if err == nil {
		_ = json.Unmarshal(data, &cfg)
	}
	writeJSON(w, http.StatusOK, cfg)
}

func (h *Handlers) SetPVResponder(w http.ResponseWriter, r *http.Request) {
	var cfg PVResponderConfig
	if !bind(w, r, &cfg) {
		return
	}
	if cfg.TypingDelayMin <= 0 {
		cfg.TypingDelayMin = 1.5
	}
	if cfg.TypingDelayMax <= 0 {
		cfg.TypingDelayMax = 4.5
	}
	if cfg.CooldownSeconds <= 0 {
		cfg.CooldownSeconds = 15
	}
	if cfg.HistoryLimit <= 0 {
		cfg.HistoryLimit = 4
	}
	_ = os.MkdirAll("data", 0755)
	raw, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	if err := os.WriteFile(pvConfigFile, raw, 0644); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to save config: " + err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, cfg)
}

// ---------------------------------------------------------- audit event queries
// QueryAuditEvents serves GET /audit/events — tenant-scoped centralized audit
// event search. The tenant scope is resolved server-side from the authenticated
// principal; only admins may omit it (cross-tenant operational view).
func (h *Handlers) QueryAuditEvents(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	id := identity(r)
	if h.c.Audit == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": "audit backend unavailable"})
		return
	}
	q := &pb.QueryAuditEventsRequest{
		CorrelationId: r.URL.Query().Get("correlation_id"),
		OperationId:   r.URL.Query().Get("operation_id"),
		FeatureId:     r.URL.Query().Get("feature_id"),
		Service:       r.URL.Query().Get("service"),
		Severity:      strings.ToUpper(r.URL.Query().Get("severity")),
		EventCategory: r.URL.Query().Get("event_category"),
		Outcome:       strings.ToUpper(r.URL.Query().Get("outcome")),
	}
	var err error
	if v := r.URL.Query().Get("since"); v != "" {
		if q.Since, err = strconv.ParseInt(v, 10, 64); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid since"})
			return
		}
	}
	if v := r.URL.Query().Get("until"); v != "" {
		if q.Until, err = strconv.ParseInt(v, 10, 64); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid until"})
			return
		}
	}
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		parsed, parseErr := strconv.Atoi(v)
		if parseErr != nil || parsed < 1 || parsed > 500 {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid limit (1-500)"})
			return
		}
		limit = parsed
	}
	q.Limit = int32(limit) // safe: limit ∈ [1, 500]
	if v := r.URL.Query().Get("offset"); v != "" {
		off, err := strconv.Atoi(v)
		if err != nil || off < 0 || off > 1_000_000 {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid offset"})
			return
		}
		q.Offset = int32(off) //nolint:gosec // bounded to [0, 1_000_000]
	}
	// Fail-closed tenant scoping: normal tenants are pinned to their own
	// numeric user ID; only admins may query across tenants.
	if !id.IsAdmin {
		if id.UserID == 0 {
			writeJSON(w, http.StatusForbidden, map[string]any{"error": "tenant scope required"})
			return
		}
		q.OwnerUserId = id.UserID
	} else if v := r.URL.Query().Get("owner_user_id"); v != "" {
		if q.OwnerUserId, err = strconv.ParseInt(v, 10, 64); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid owner_user_id"})
			return
		}
	}
	resp, err := h.c.Audit.QueryAuditEvents(ctx, q)
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}
