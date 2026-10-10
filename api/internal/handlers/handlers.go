package handlers

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "autoforward/proto"
	"autoforward/internal/grpcclient"
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
	resp, err := h.c.Sessions.BackupSession(ctx, &pb.BackupSessionRequest{SessionId: req.SessionID})
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
		out = append(out, map[string]any{
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
			"detection_criteria":        json.Unmarshal([]byte(rl.DetectionCriteriaJson), &map[string]any{}),
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
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"rules": out,
		"total": len(out),
	})
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

func (h *Handlers) CreateRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	var rule pb.ForwardRule
	if !bindPB(w, r, &rule) {
		return
	}
	if ok, errCode := checkRuleEndpoints(rule.SourceChatId, rule.TargetChatId, rule.IntermediateChannelId, rule.UseIntermediate); !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "Invalid rule endpoints: " + errCode,
			"code":  errCode,
		})
		return
	}
	rule.OwnerUserId = id.UserID
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Rules.CreateRule(ctx, &pb.CreateRuleRequest{Rule: &rule})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, resp)
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
		if rl.Id == r.PathValue("id") {
			owned = true
			break
		}
	}
	if !owned {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}

	var rule pb.ForwardRule
	if !bindPB(w, r, &rule) {
		return
	}
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
	rule.OwnerUserId = id.UserID
	resp, err := h.c.Rules.UpdateRule(ctx, &pb.UpdateRuleRequest{Rule: &rule})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
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
		if rule.Id == r.PathValue("id") {
			owned = true
			break
		}
	}
	if !owned {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "rule not found"})
		return
	}
	resp, err := h.c.Rules.DeleteRule(ctx, &pb.DeleteRuleRequest{Id: r.PathValue("id")})
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
		if rule.Id == r.PathValue("id") {
			writeJSON(w, http.StatusOK, rule)
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
		if rule.Id == r.PathValue("id") {
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
	writeJSON(w, http.StatusOK, upResp)
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
		if rule.Id == r.PathValue("id") {
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
		RuleId:               req.RuleId,
		SampleText:           req.SampleText,
		SampleChatId:         req.SampleChatId,
		SampleSenderId:       req.SampleSenderId,
		ForwardOriginChatId:  req.ForwardOriginChatId,
		ForwardOriginUsername: req.ForwardOriginUser,
		ForwardOriginTitle:   req.ForwardOriginTitle,
		SampleHasMedia:       req.SampleHasMedia,
		SampleMediaType:      req.SampleMediaType,
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
		RuleId: r.PathValue("id"),
		Until:  body.Until,
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
	resp, err := h.c.Rules.ResumeRule(ctx, &pb.PauseRuleRequest{RuleId: r.PathValue("id")})
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
		if rule.Id == ruleID {
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
		if f.Id == filterID {
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
		if c.Id == configID {
			return true
		}
	}
	return false
}

// ------------------------------------------------------------- delivery
func (h *Handlers) DeliveryStats(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Delivery.GetDeliveryStats(ctx, &pb.DeliveryStatsRequest{})
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
			"rule_id":          st.RuleId,
			"rule_name":        st.RuleName,
			"is_active":        st.IsActive,
			"is_paused":        st.IsPaused,
			"forwarded":        st.Forwarded,
			"filtered":         st.Filtered,
			"errors":           st.Errors,
			"last_forward_ts":  st.LastForwardTs,
			"last_error":       st.LastError,
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
		RuleId: r.URL.Query().Get("rule_id"),
		Limit:  int32(limit),
		Since:  since,
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
		"jobs":  entries,
		"entries": entries,
		"total": len(entries),
	})
}

func (h *Handlers) ReplayDeadLetter(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Delivery.ReplayDeadLetter(ctx, &pb.ReplayDeadLetterRequest{Id: r.PathValue("id")})
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
		RuleId:    body.RuleId,
		OlderThan: body.OlderThan,
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
	filters := resp.Filters
	if filters == nil {
		filters = []*pb.FilterRule{}
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
	resp, err := h.c.Filters.DeleteFilter(ctx, &pb.DeleteFilterRequest{Id: r.PathValue("id")})
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
	configs := resp.Configs
	if configs == nil {
		configs = []*pb.AIConfig{}
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
	resp, err := h.c.AI.DeleteAIConfig(ctx, &pb.DeleteAIConfigRequest{Id: r.PathValue("id")})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

type testAIReq struct {
	SampleText string `json:"sample_text"`
}

func (h *Handlers) TestAIRewrite(w http.ResponseWriter, r *http.Request) {
	var req testAIReq
	if !bind(w, r, &req) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AI.TestAIRewrite(ctx, &pb.TestAIRewriteRequest{
		AiConfigId: r.PathValue("id"), SampleText: req.SampleText,
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
			"ts":        l.Ts,
			"level":     l.Level,
			"severity":  l.Level,
			"service":   l.Service,
			"category":  l.Category,
			"message":   l.Message,
			"detail":    l.Detail,
			"error_name": l.Category,
			"id":        l.Ts,
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

// ------------------------------------------------------------- internals
func identity(r *http.Request) *Identity {
	if v := r.Context().Value(identityKey{}); v != nil {
		if id, ok := v.(*Identity); ok && id != nil {
			return id
		}
	}
	return &Identity{}
}

func withTimeout(r *http.Request) (context.Context, func()) {
	return context.WithTimeout(r.Context(), 15*time.Second)
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
