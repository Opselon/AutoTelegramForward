package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"autoforward/internal/grpcclient"
	pb "autoforward/proto"
)

type Handlers struct {
	c *grpcclient.Clients
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
	type svc struct {
		Name      string `json:"name"`
		Online    bool   `json:"online"`
		Detail    string `json:"detail"`
		LatencyMs int64  `json:"latency_ms"`
	}
	results := make([]svc, 0, 4)
	var mu sync.Mutex
	check := func(name string, fn func() (bool, string)) {
		start := time.Now()
		ok, detail := false, ""
		done := make(chan struct{})
		go func() { ok, detail = fn(); close(done) }()
		select {
		case <-done:
		case <-time.After(4 * time.Second):
			ok, detail = false, "probe timeout (4s)"
		}
		mu.Lock()
		results = append(results, svc{
			Name:      name,
			Online:    ok,
			Detail:    detail,
			LatencyMs: time.Since(start).Milliseconds(),
		})
		mu.Unlock()
	}

	// Core (Python gRPC) — GetSystemStats proves the gRPC channel AND the
	// pipeline loop are alive.
	go check("core", func() (bool, string) {
		ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer cancel()
		resp, err := h.c.System.GetSystemStats(ctx, &pb.SystemStatsRequest{})
		if err != nil {
			return false, "gRPC: " + err.Error()
		}
		return resp.CoreRunning, fmt.Sprintf("uptime=%ds sessions=%d", resp.UptimeSeconds, resp.ActiveSessionsCount)
	})

	// Logger microservice — LogStats round-trip proves the log store answers.
	go check("logger", func() (bool, string) {
		if h.c.Logs == nil {
			return false, "logger stub not wired (dial failed at startup)"
		}
		ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer cancel()
		resp, err := h.c.Logs.LogStats(ctx, &pb.LogStatsRequest{})
		if err != nil {
			return false, "gRPC: " + err.Error()
		}
		return true, fmt.Sprintf("events=%d", resp.Total)
	})

	// API (this process) — trivially online if the request is being served.
	go check("api", func() (bool, string) {
		return true, "this process"
	})

	// Web gateway — reachable proxy probe of its /healthz.
	go check("web", func() (bool, string) {
		webURL := os.Getenv("ATF_WEB_HEALTH_URL")
		if webURL == "" {
			webURL = "http://127.0.0.1:8088/healthz"
		}
		client := &http.Client{Timeout: 4 * time.Second}
		resp, err := client.Get(webURL)
		if err != nil {
			return false, err.Error()
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return false, "HTTP " + resp.Status
		}
		return true, webURL
	})

	// Give the parallel probes a moment to settle (they race the 4s timeout).
	deadline := time.After(4500 * time.Millisecond)
	for len(results) < 4 {
		select {
		case <-deadline:
			mu.Lock()
			if len(results) < 4 {
				results = append(results, svc{Name: "?", Online: false, Detail: "probe pending"})
			}
			mu.Unlock()
			deadline = time.After(0)
		case <-time.After(50 * time.Millisecond):
		}
	}
	// Deterministic order for the UI: core → logger → api → web.
	order := map[string]int{"core": 0, "logger": 1, "api": 2, "web": 3}
	sort.Slice(results, func(i, j int) bool { return order[results[i].Name] < order[results[j].Name] })

	allOK := true
	for _, s := range results {
		if !s.Online {
			allOK = false
		}
	}
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

	if ok, errCode := checkRuleEndpoints(rule.SourceChatId, rule.TargetChatId, rule.IntermediateChannelId, rule.UseIntermediate); !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "Invalid rule endpoints: " + errCode,
			"code":  errCode,
		})
		return
	}
	// Always store canonical numeric chat ids: the dispatcher calls
	// int(chat_id) before sending, so a username would break delivery.
	ctx, cancel0 := withTimeout(r)
	sid, errS := h.resolveChat(ctx, rule.SessionId, rule.SourceChatId, id.UserID)
	cancel0()
	if errS != "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": errS, "code": "source_resolve_failed"})
		return
	}
	if sid != "" {
		rule.SourceChatId = sid
		if rule.SourceChatName == "" {
			rule.SourceChatName = rule.SourceChatId
		}
	}
	ctx, cancel1 := withTimeout(r)
	tid, errT := h.resolveChat(ctx, rule.SessionId, rule.TargetChatId, id.UserID)
	cancel1()
	if errT != "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": errT, "code": "target_resolve_failed"})
		return
	}
	if tid != "" {
		rule.TargetChatId = tid
		if rule.TargetChatName == "" {
			rule.TargetChatName = rule.TargetChatId
		}
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
	for _, rl := range listResp.Rules {
		if rl.OwnerUserId == id.UserID && rl.Id == r.PathValue("id") {
			owned = true
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
	var rule pb.ForwardRule
	opts := protojson.UnmarshalOptions{DiscardUnknown: true}
	if err := opts.Unmarshal(body, &rule); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	enrichCustomMetadata(body, &rule)

	rule.Id = r.PathValue("id")
	if rule.SourceChatId != "" && rule.TargetChatId != "" {
		if ok, errCode := checkRuleEndpoints(rule.SourceChatId, rule.TargetChatId, rule.IntermediateChannelId, rule.UseIntermediate); !ok {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "Invalid rule endpoints: " + errCode,
				"code":  errCode,
			})
			return
		}
	}
	// Canonicalize any username/link endpoints to numeric ids (same contract
	// as CreateRule — see the resolveChat doc comment).
	if sid, errS := h.resolveChat(ctx, rule.SessionId, rule.SourceChatId, id.UserID); errS != "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": errS, "code": "source_resolve_failed"})
		return
	} else if sid != "" {
		rule.SourceChatId = sid
	}
	if tid, errT := h.resolveChat(ctx, rule.SessionId, rule.TargetChatId, id.UserID); errT != "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": errT, "code": "target_resolve_failed"})
		return
	} else if tid != "" {
		rule.TargetChatId = tid
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
	resp, err := h.c.Rules.UpdateRule(ctx, &pb.UpdateRuleRequest{Rule: &rule})
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
	resp, err := h.c.Rules.ListRules(ctx, &pb.ListRulesRequest{OwnerUserId: id.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	for _, rule := range resp.Rules {
		if rule.OwnerUserId == id.UserID && rule.Id == r.PathValue("id") {
			writeJSON(w, http.StatusOK, formatRuleJSON(rule))
			return
		}
	}
	writeJSON(w, http.StatusNotFound, map[string]string{"error": "Rule not found"})
}

func (h *Handlers) ToggleRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
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

func (h *Handlers) ListDeadLetter(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	limit := 50
	if n := r.URL.Query().Get("limit"); n != "" {
		if v, err := strconv.Atoi(n); err == nil && v > 0 {
			limit = v
		}
	}
	var since int64
	if n := r.URL.Query().Get("since"); n != "" {
		if v, err := strconv.ParseInt(n, 10, 64); err == nil {
			since = v
		}
	}
	resp, err := h.c.Delivery.ListDeadLetter(ctx, &pb.ListDeadLetterRequest{
		RuleId:      r.URL.Query().Get("rule_id"),
		Limit:       int32(limit),
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
func (h *Handlers) QueryLogs(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	q := r.URL.Query()
	limit := int32(100)
	if h.c.Logs == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "logger service unavailable",
		})
		return
	}
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 500 {
			limit = int32(n)
		}
	}
	since := int64(0)
	if v := q.Get("since"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n >= 0 {
			since = n
		}
	}
	resp, err := h.c.Logs.QueryLogs(ctx, &pb.QueryLogsRequest{
		Service: q.Get("service"), Level: q.Get("level"),
		Search: q.Get("search"), Limit: limit, Since: since,
	})
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
		out = append(out, map[string]any{
			"ts":         l.Ts,
			"level":      l.Level,
			"severity":   l.Level,
			"service":    l.Service,
			"category":   l.Category,
			"message":    l.Message,
			"detail":     l.Detail,
			"error_name": l.Category,
			"id":         l.Ts,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"logs":  out,
		"total": len(out),
	})
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
	resp, err := h.c.Logs.LogStats(ctx, &pb.LogStatsRequest{})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"total":       resp.Total,
		"by_level":    resp.ByLevel,
		"by_category": resp.ByCategory,
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
	resp, err := h.c.Logs.PurgeLogs(ctx, &pb.PurgeLogsRequest{
		Level:          body.Level,
		Service:        body.Service,
		Category:       body.Category,
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
		if limit, err = strconv.Atoi(v); err != nil || limit < 1 || limit > 500 {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid limit (1-500)"})
			return
		}
	}
	q.Limit = int32(limit)
	if v := r.URL.Query().Get("offset"); v != "" {
		off, err := strconv.Atoi(v)
		if err != nil || off < 0 {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid offset"})
			return
		}
		q.Offset = int32(off)
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
