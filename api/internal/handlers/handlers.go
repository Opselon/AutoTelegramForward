package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

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
	if !bind(w, r, &req) {
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
	writeJSON(w, http.StatusOK, map[string]any{
		"rules": rules,
		"total": len(rules),
	})
}

func (h *Handlers) CreateRule(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	var rule pb.ForwardRule
	if !bindPB(w, r, &rule) {
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
	var rule pb.ForwardRule
	if !bindPB(w, r, &rule) {
		return
	}
	rule.Id = r.PathValue("id")
	rule.OwnerUserId = id.UserID
	ctx, cancel := withTimeout(r)
	defer cancel()
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
	ctx, cancel := withTimeout(r)
	defer cancel()
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
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Rules.ResumeRule(ctx, &pb.PauseRuleRequest{RuleId: r.PathValue("id")})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
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
	writeJSON(w, http.StatusOK, resp)
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
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Filters.ListFilters(ctx, &pb.ListFiltersRequest{})
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
	var f pb.FilterRule
	if !bindPB(w, r, &f) {
		return
	}
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
	var f pb.FilterRule
	if !bindPB(w, r, &f) {
		return
	}
	f.Id = r.PathValue("id")
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Filters.UpdateFilter(ctx, &pb.UpdateFilterRequest{Filter: &f})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) DeleteFilter(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Filters.DeleteFilter(ctx, &pb.DeleteFilterRequest{Id: r.PathValue("id")})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// -------------------------------------------------------------------- AI
func (h *Handlers) ListAIConfigs(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AI.ListAIConfigs(ctx, &pb.ListAIConfigsRequest{})
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
	var c pb.AIConfig
	if !bindPB(w, r, &c) {
		return
	}
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
	var c pb.AIConfig
	if !bindPB(w, r, &c) {
		return
	}
	c.Id = r.PathValue("id")
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AI.UpdateAIConfig(ctx, &pb.UpdateAIConfigRequest{Config: &c})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) DeleteAIConfig(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
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
	writeJSON(w, http.StatusOK, map[string]any{
		"logs":  logs,
		"total": len(logs),
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
	writeJSON(w, http.StatusOK, resp)
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
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": st.Message()})
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
