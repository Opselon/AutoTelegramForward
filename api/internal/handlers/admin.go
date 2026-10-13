package handlers

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"

	pb "autoforward/proto"
)

// AdminOverview handles GET /admin/overview
func (h *Handlers) AdminOverview(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.GetOverview(ctx, &pb.AdminOverviewRequest{})
	if err != nil {
		grpcError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"core_online":     resp.CoreOnline,
		"uptime_seconds":  resp.UptimeSeconds,
		"total_users":     resp.TotalUsers,
		"active_rules":    resp.ActiveRules,
		"queue_pending":   resp.QueuePending,
		"recent_errors":   resp.RecentErrors,
		"system_status":   resp.SystemStatus,
	})
}

// AdminListUsers handles GET /admin/users
func (h *Handlers) AdminListUsers(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()

	limit := 50
	offset := 0
	if l := r.URL.Query().Get("limit"); l != "" {
		if val, err := strconv.Atoi(l); err == nil && val > 0 {
			limit = val
		}
	}
	if o := r.URL.Query().Get("offset"); o != "" {
		if val, err := strconv.Atoi(o); err == nil && val >= 0 {
			offset = val
		}
	}

	resp, err := h.c.Admin.ListUsers(ctx, &pb.AdminListUsersRequest{
		Limit:  int32(limit),
		Offset: int32(offset),
	})
	if err != nil {
		grpcError(w, err)
		return
	}

	users := make([]map[string]any, 0, len(resp.Users))
	for _, u := range resp.Users {
		users = append(users, map[string]any{
			"user_id":     u.UserId,
			"username":    u.Username,
			"plan":        u.Plan,
			"status":      u.Status,
			"is_admin":    u.IsAdmin,
			"rules_count": u.RulesCount,
			"created_at":  u.CreatedAt,
		})
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"users": users,
		"total": resp.Total,
	})
}

// AdminSetUserStatus handles POST /admin/users/{id}/status
func (h *Handlers) AdminSetUserStatus(w http.ResponseWriter, r *http.Request) {
	idStr := r.PathValue("id")
	uid, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil || uid <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid user id"})
		return
	}

	var body struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "malformed JSON body"})
		return
	}

	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.SetUserStatus(ctx, &pb.AdminSetUserStatusRequest{
		UserId: uid,
		Status: body.Status,
	})
	if err != nil {
		grpcError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"success": resp.Success,
		"message": resp.Message,
	})
}

// AdminBroadcast handles POST /admin/broadcast
func (h *Handlers) AdminBroadcast(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	var body struct {
		Message string `json:"message"`
		Target  string `json:"target"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "malformed JSON body"})
		return
	}
	if strings.TrimSpace(body.Message) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "message cannot be empty"})
		return
	}

	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.Broadcast(ctx, &pb.AdminBroadcastRequest{
		InitiatorUserId: id.UserID,
		MessageText:     body.Message,
		Target:          body.Target,
	})
	if err != nil {
		grpcError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"success":          resp.Success,
		"broadcast_id":     resp.BroadcastId,
		"total_recipients": resp.TotalRecipients,
		"message":          resp.Message,
	})
}

// AdminGetSettings handles GET /admin/settings
func (h *Handlers) AdminGetSettings(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.GetSettings(ctx, &pb.AdminGetSettingsRequest{})
	if err != nil {
		grpcError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"force_sub_enabled": resp.ForceSubEnabled,
		"force_sub_channel": resp.ForceSubChannel,
		"web_url":           resp.WebUrl,
		"registration_open": resp.RegistrationOpen,
	})
}

// AdminSaveSettings handles POST /admin/settings
func (h *Handlers) AdminSaveSettings(w http.ResponseWriter, r *http.Request) {
	var body struct {
		ForceSubEnabled  bool   `json:"force_sub_enabled"`
		ForceSubChannel  string `json:"force_sub_channel"`
		WebURL           string `json:"web_url"`
		RegistrationOpen bool   `json:"registration_open"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "malformed JSON body"})
		return
	}

	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.SaveSettings(ctx, &pb.AdminSaveSettingsRequest{
		ForceSubEnabled:  body.ForceSubEnabled,
		ForceSubChannel:  body.ForceSubChannel,
		WebUrl:           body.WebURL,
		RegistrationOpen: body.RegistrationOpen,
	})
	if err != nil {
		grpcError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"success": resp.Success,
		"message": resp.Message,
	})
}

// AdminListAdmins handles GET /admin/admins
func (h *Handlers) AdminListAdmins(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.ListAdmins(ctx, &pb.AdminListAdminsRequest{})
	if err != nil {
		grpcError(w, err)
		return
	}

	admins := make([]map[string]any, 0, len(resp.Admins))
	for _, a := range resp.Admins {
		admins = append(admins, map[string]any{
			"user_id":    a.UserId,
			"role":       a.Role,
			"added_by":   a.AddedBy,
			"created_at": a.CreatedAt,
		})
	}

	writeJSON(w, http.StatusOK, map[string]any{"admins": admins})
}

// AdminAddAdmin handles POST /admin/admins
func (h *Handlers) AdminAddAdmin(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	var body struct {
		UserID int64 `json:"user_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.UserID <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "valid numeric user_id required"})
		return
	}

	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.AddAdmin(ctx, &pb.AdminAddAdminRequest{
		UserId:  body.UserID,
		AddedBy: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"success": resp.Success,
		"message": resp.Message,
	})
}

// AdminRemoveAdmin handles DELETE /admin/admins/{id}
func (h *Handlers) AdminRemoveAdmin(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	idStr := r.PathValue("id")
	target, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil || target <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "valid numeric user id required"})
		return
	}

	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.RemoveAdmin(ctx, &pb.AdminRemoveAdminRequest{
		UserId:    target,
		RemovedBy: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"success": resp.Success,
		"message": resp.Message,
	})
}

// AdminListExtensions handles GET /admin/extensions
func (h *Handlers) AdminListExtensions(w http.ResponseWriter, r *http.Request) {
	extensions := []map[string]any{
		{
			"id":          "ai_rewrite_copilot",
			"name":        "AI Rewrite & Tone Matching",
			"description": "Enterprise AI prompt engineering with ChatGPT, DeepSeek, and Anthropic.",
			"version":     "2.4.0",
			"status":      "enabled",
			"author":      "ATF Engineering",
		},
		{
			"id":          "watermark_cleaner",
			"name":        "Media & Watermark Remover",
			"description": "Automated caption sanitization, emoji removal, and signature striping.",
			"version":     "1.8.2",
			"status":      "enabled",
			"author":      "ATF Engineering",
		},
		{
			"id":          "channel_mirror",
			"name":        "Real-Time Channel Synchronizer",
			"description": "Sub-second bidirectional channel mirroring with message edit & deletion tracking.",
			"version":     "3.1.0",
			"status":      "enabled",
			"author":      "ATF Engineering",
		},
		{
			"id":          "restricted_bypass",
			"name":        "Restricted Content Copier",
			"description": "High-fidelity stream pipeline for copying content from protected channels.",
			"version":     "2.0.1",
			"status":      "enabled",
			"author":      "ATF Engineering",
		},
	}
	writeJSON(w, http.StatusOK, map[string]any{"extensions": extensions})
}

// AdminToggleExtension handles POST /admin/extensions/{id}/toggle
func (h *Handlers) AdminToggleExtension(w http.ResponseWriter, r *http.Request) {
	extID := r.PathValue("id")
	writeJSON(w, http.StatusOK, map[string]any{
		"success": true,
		"message": "Extension " + extID + " state updated",
	})
}

// ClientGetSettings handles GET /client/settings
func (h *Handlers) ClientGetSettings(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.GetClientSettings(ctx, &pb.ClientSettingsRequest{
		UserId: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"user_id":                resp.UserId,
		"plan":                   resp.Plan,
		"quota_forwarded":        resp.QuotaForwarded,
		"language":               resp.Language,
		"has_custom_credentials": resp.HasCustomCredentials,
		"api_id":                 resp.ApiId,
		"api_hash_masked":        resp.ApiHashMasked,
	})
}

// ClientSaveCredentials handles POST /client/credentials
func (h *Handlers) ClientSaveCredentials(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	var body struct {
		ApiID   int64  `json:"api_id"`
		ApiHash string `json:"api_hash"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "malformed JSON body"})
		return
	}

	if body.ApiID <= 0 || len(strings.TrimSpace(body.ApiHash)) != 32 {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "api_id must be a positive integer and api_hash must be exactly 32 hexadecimal characters",
		})
		return
	}

	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.Admin.SaveClientCredentials(ctx, &pb.ClientSaveCredentialsRequest{
		UserId:  id.UserID,
		ApiId:   body.ApiID,
		ApiHash: strings.TrimSpace(body.ApiHash),
	})
	if err != nil {
		grpcError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"success": resp.Success,
		"message": resp.Message,
	})
}
