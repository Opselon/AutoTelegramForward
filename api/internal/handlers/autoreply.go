package handlers

// AI private-message auto-reply (schema v28).
//
// The HTTP shape is deliberately flatter than the gRPC contract: a rule's
// conditions and exclusions are JSON objects over HTTP and JSON strings on the
// wire, an export is a document rather than a document string, and every
// response is assembled key by key in snake_case.
//
// The last point is not style. Protobuf's JSON tags are `omitempty`, so
// marshalling a pb struct straight to the client drops exactly the `false`,
// `0` and `""` values a settings form needs to render — a client would have to
// read "field missing" as "field is false", and quiet hours (-1 = off) would
// be indistinguishable from unset. Explicit projection keeps every field
// present and every key spelled the way the dashboard sends it.
//
// Tenant scope comes from identity(r) on every call. owner_user_id is never
// read from a body or a query string; a by-id request forwards the caller's id
// and the core answers NOT_FOUND for a foreign row (grpcError maps that to
// 404). A caller with no linked account reads empty lists and defaults, and
// cannot write anything at all.

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"strconv"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"

	pb "autoforward/proto"
)

const (
	// defaultAutoReplyPage is the page size used when a listing is asked for
	// without one. The dashboard renders "showing 100 of N" from total, so a
	// bounded default is visible rather than silent.
	defaultAutoReplyPage = 100
	// maxAutoReplyPage caps a client-supplied page size. The core clamps again
	// (and slices the snapshot it already read), but this bound stops one
	// request from asking this process to render an unbounded page.
	maxAutoReplyPage = 500
)

// autoReplyOwner resolves the verified account id for a mutation, refusing a
// caller the auth middleware could not identify. Read handlers do not use it:
// they answer empty for owner 0 instead, because "no account" means "no rows"
// and a 401 there would be a refusal where an empty page is the honest answer.
func autoReplyOwner(w http.ResponseWriter, r *http.Request) (int64, bool) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusUnauthorized, map[string]string{
			"error": "authentication required", "code": "unauthenticated",
		})
		return 0, false
	}
	return owner, true
}

// autoReplyPageLimit clamps a listing's page size. A missing, zero or
// unparseable value takes the default rather than failing the read: the
// dashboard polls these lists, and a broken page parameter must not blank it.
func autoReplyPageLimit(raw string) int32 {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return defaultAutoReplyPage
	}
	limit, ok := boundedQueryInt(raw, 0, maxAutoReplyPage)
	if !ok || limit <= 0 {
		return defaultAutoReplyPage
	}
	return limit
}

// autoReplyPageOffset clamps an offset at the 32-bit ceiling so the narrowing
// conversion to the proto field cannot wrap negative.
func autoReplyPageOffset(raw string) int32 {
	offset, ok := boundedQueryInt(strings.TrimSpace(raw), 0, math.MaxInt32)
	if !ok {
		return 0
	}
	return offset
}

// autoReplyJSONObject validates a client-supplied JSON object and normalises it
// for the wire field the core parses. An absent field is an empty object: a
// rule with no conditions is a valid "match everything" rule, not a malformed
// one.
func autoReplyJSONObject(raw json.RawMessage) (string, bool) {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" {
		return "{}", true
	}
	var object map[string]any
	if err := json.Unmarshal([]byte(trimmed), &object); err != nil || object == nil {
		// A JSON array, scalar or malformed object: the core would read it as
		// {} via its tolerant loader, which would silently turn "keywords":[x]
		// into "match everything". Refuse it here instead.
		return "", false
	}
	encoded, err := json.Marshal(object)
	if err != nil {
		return "", false
	}
	return string(encoded), true
}

// autoReplyObject renders a stored JSON object back as an object so a client
// reads the shape it sent. A blank or corrupt column becomes {}: the rule is
// still usable, and one bad row cannot fail the whole listing.
func autoReplyObject(raw string) any {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return map[string]any{}
	}
	var object map[string]any
	if err := json.Unmarshal([]byte(trimmed), &object); err != nil || object == nil {
		return map[string]any{}
	}
	return object
}

// autoReplyDays keeps an empty schedule a JSON array rather than null; the
// weekday picker renders straight from this field.
func autoReplyDays(days []int32) []int32 {
	return append(make([]int32, 0, len(days)), days...)
}

// autoReplyStatusCounts turns the core's per-status JSON into an object. A
// malformed column must not fail the read: the counters beside it are already
// correct, and an empty chart beats a 502 on a dashboard that has the numbers.
func autoReplyStatusCounts(raw string) map[string]int32 {
	counts := map[string]int32{}
	if strings.TrimSpace(raw) == "" {
		return counts
	}
	if err := json.Unmarshal([]byte(raw), &counts); err != nil || counts == nil {
		return map[string]int32{}
	}
	return counts
}

// autoReplyRemovedCount reads the count the core puts in its status message.
//
// The proto has no count field on RemoveAutoReplyTargets, so the core reports
// `removed=N` in the message. A message that does not carry a number is
// reported as zero rather than guessed: "success" plus a wrong count is worse
// than "success" plus no count.
func autoReplyRemovedCount(message string) int {
	value, ok := strings.CutPrefix(strings.TrimSpace(message), "removed=")
	if !ok {
		return 0
	}
	n, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil || n < 0 {
		return 0
	}
	return n
}

// writeAutoReplyStatus writes the {"success": true} envelope for an action the
// core confirmed, and surfaces a refusal (which the core reports in the body
// rather than as a gRPC error) as a 400 with its code.
func writeAutoReplyStatus(w http.ResponseWriter, resp *pb.StatusResponse) {
	if !resp.GetSuccess() {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"success":    false,
			"error":      resp.GetMessage(),
			"error_code": resp.GetErrorCode(),
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true})
}

// ------------------------------------------------------------------ settings

// autoReplyDefaultSettings mirrors the core's answer to a caller with no linked
// account: the documented defaults, no ids, nothing personal.
func autoReplyDefaultSettings() *pb.AutoReplySettings {
	return &pb.AutoReplySettings{TargetMode: "EVERYONE", Tone: "friendly", ResponseLength: "short"}
}

func autoReplySettingsJSON(s *pb.AutoReplySettings) map[string]any {
	return map[string]any{
		"enabled":                 s.GetEnabled(),
		"ai_config_id":            s.GetAiConfigId(),
		"prompt_id":               s.GetPromptId(),
		"target_mode":             s.GetTargetMode(),
		"language":                s.GetLanguage(),
		"tone":                    s.GetTone(),
		"response_length":         s.GetResponseLength(),
		"temperature":             s.GetTemperature(),
		"max_tokens":              s.GetMaxTokens(),
		"context_messages":        s.GetContextMessages(),
		"memory_enabled":          s.GetMemoryEnabled(),
		"memory_max_facts":        s.GetMemoryMaxFacts(),
		"extra_instructions":      s.GetExtraInstructions(),
		"max_reply_chars":         s.GetMaxReplyChars(),
		"typing_simulation":       s.GetTypingSimulation(),
		"typing_delay_min":        s.GetTypingDelayMin(),
		"typing_delay_max":        s.GetTypingDelayMax(),
		"cooldown_seconds":        s.GetCooldownSeconds(),
		"daily_limit_per_chat":    s.GetDailyLimitPerChat(),
		"rate_limit_per_minute":   s.GetRateLimitPerMinute(),
		"max_concurrency":         s.GetMaxConcurrency(),
		"max_attempts":            s.GetMaxAttempts(),
		"retry_backoff_seconds":   s.GetRetryBackoffSeconds(),
		"quiet_hours_start":       s.GetQuietHoursStart(),
		"quiet_hours_end":         s.GetQuietHoursEnd(),
		"timezone_offset_minutes": s.GetTimezoneOffsetMinutes(),
		"active_days":             autoReplyDays(s.GetActiveDays()),
		"fallback_text":           s.GetFallbackText(),
		"ignore_bots":             s.GetIgnoreBots(),
		"version":                 s.GetVersion(),
		"created_at":              s.GetCreatedAt(),
		"updated_at":              s.GetUpdatedAt(),
	}
}

func (h *Handlers) GetAutoReplySettings(w http.ResponseWriter, r *http.Request) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"settings": autoReplySettingsJSON(autoReplyDefaultSettings())})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.GetAutoReplySettings(ctx, &pb.GetAutoReplySettingsRequest{OwnerUserId: owner})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"settings": autoReplySettingsJSON(resp)})
}

// UpdateAutoReplySettings applies a settings form. The body is the settings
// object itself plus an optional "update_mask", so the mask is lifted out
// before the settings are decoded: a typo in a settings key is then a 400
// instead of a field that silently keeps its old value.
func (h *Handlers) UpdateAutoReplySettings(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	body, ok := readBody(w, r, maxJSONBodyBytes)
	if !ok {
		return
	}
	var fields map[string]json.RawMessage
	if len(strings.TrimSpace(string(body))) > 0 {
		if err := json.Unmarshal(body, &fields); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
			return
		}
	}
	var mask []string
	if rawMask, present := fields["update_mask"]; present {
		if err := json.Unmarshal(rawMask, &mask); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "update_mask must be an array of field names", "code": "invalid_update_mask",
			})
			return
		}
		delete(fields, "update_mask")
	}
	if fields == nil {
		fields = map[string]json.RawMessage{}
	}
	settingsJSON, err := json.Marshal(fields)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid settings"})
		return
	}
	settings := &pb.AutoReplySettings{}
	// protojson accepts both the proto field names (snake_case, what the
	// dashboard sends) and the lowerCamel form; a value of the wrong type is
	// refused rather than coerced to zero.
	if err := protojson.Unmarshal(settingsJSON, settings); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid settings: " + err.Error()})
		return
	}
	settings.OwnerUserId = owner
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.UpdateAutoReplySettings(ctx, &pb.UpdateAutoReplySettingsRequest{
		OwnerUserId: owner,
		Settings:    settings,
		UpdateMask:  mask,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"settings": autoReplySettingsJSON(resp)})
}

// ------------------------------------------------------------------- prompts

func autoReplyPromptJSON(p *pb.AutoReplyPrompt) map[string]any {
	return map[string]any{
		"id":                   p.GetId(),
		"name":                 p.GetName(),
		"description":          p.GetDescription(),
		"system_prompt":        p.GetSystemPrompt(),
		"user_prompt_template": p.GetUserPromptTemplate(),
		"language":             p.GetLanguage(),
		"tone":                 p.GetTone(),
		"response_length":      p.GetResponseLength(),
		"tags":                 append(make([]string, 0, len(p.GetTags())), p.GetTags()...),
		"category":             p.GetCategory(),
		"is_default":           p.GetIsDefault(),
		"is_archived":          p.GetIsArchived(),
		"usage_count":          p.GetUsageCount(),
		"created_at":           p.GetCreatedAt(),
		"updated_at":           p.GetUpdatedAt(),
	}
}

func (h *Handlers) ListAutoReplyPrompts(w http.ResponseWriter, r *http.Request) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"prompts": []any{}, "total": 0})
		return
	}
	q := r.URL.Query()
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.ListAutoReplyPrompts(ctx, &pb.ListAutoReplyPromptsRequest{
		OwnerUserId:     owner,
		Search:          q.Get("search"),
		IncludeArchived: toBool(q.Get("include_archived")),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	prompts := make([]any, 0, len(resp.GetPrompts()))
	for _, p := range resp.GetPrompts() {
		if p != nil {
			prompts = append(prompts, autoReplyPromptJSON(p))
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"prompts": prompts, "total": len(prompts)})
}

type autoReplyPromptReq struct {
	Name               string   `json:"name"`
	Description        string   `json:"description"`
	SystemPrompt       string   `json:"system_prompt"`
	UserPromptTemplate string   `json:"user_prompt_template"`
	Language           string   `json:"language"`
	Tone               string   `json:"tone"`
	ResponseLength     string   `json:"response_length"`
	Tags               []string `json:"tags"`
	Category           string   `json:"category"`
	IsDefault          bool     `json:"is_default"`
	// Not part of the documented create form, but accepted so a client that
	// round-trips a prompt from a listing does not silently un-archive it: the
	// save is a full replacement of the row.
	IsArchived bool `json:"is_archived"`
}

func (h *Handlers) CreateAutoReplyPrompt(w http.ResponseWriter, r *http.Request) {
	h.saveAutoReplyPrompt(w, r, "")
}

func (h *Handlers) UpdateAutoReplyPrompt(w http.ResponseWriter, r *http.Request) {
	h.saveAutoReplyPrompt(w, r, r.PathValue("id"))
}

func (h *Handlers) saveAutoReplyPrompt(w http.ResponseWriter, r *http.Request, id string) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	var req autoReplyPromptReq
	if !bind(w, r, &req) {
		return
	}
	if strings.TrimSpace(req.Name) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "name is required", "code": "invalid_request",
		})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.SaveAutoReplyPrompt(ctx, &pb.SaveAutoReplyPromptRequest{
		OwnerUserId: owner,
		Prompt: &pb.AutoReplyPrompt{
			Id:                 id,
			Name:               req.Name,
			Description:        req.Description,
			SystemPrompt:       req.SystemPrompt,
			UserPromptTemplate: req.UserPromptTemplate,
			Language:           req.Language,
			Tone:               req.Tone,
			ResponseLength:     req.ResponseLength,
			Tags:               req.Tags,
			Category:           req.Category,
			IsDefault:          req.IsDefault,
			IsArchived:         req.IsArchived,
		},
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	code := http.StatusOK
	if id == "" {
		code = http.StatusCreated
	}
	writeJSON(w, code, map[string]any{"prompt": autoReplyPromptJSON(resp)})
}

func (h *Handlers) DeleteAutoReplyPrompt(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.DeleteAutoReplyPrompt(ctx, &pb.DeleteAutoReplyPromptRequest{
		OwnerUserId: owner,
		PromptId:    r.PathValue("id"),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeAutoReplyStatus(w, resp)
}

type autoReplyDuplicateReq struct {
	Name string `json:"name"`
}

func (h *Handlers) DuplicateAutoReplyPrompt(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	var req autoReplyDuplicateReq
	if !bindOptional(w, r, &req) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.DuplicateAutoReplyPrompt(ctx, &pb.DuplicateAutoReplyPromptRequest{
		OwnerUserId: owner,
		PromptId:    r.PathValue("id"),
		Name:        req.Name,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"prompt": autoReplyPromptJSON(resp)})
}

// ExportAutoReplyPrompts returns the core's portable document verbatim. The
// document is parsed only to guarantee the response body is valid JSON: a
// string field forwarded into a JSON document would arrive double-encoded and
// would not be importable.
func (h *Handlers) ExportAutoReplyPrompts(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	var ids []string
	for _, raw := range strings.Split(r.URL.Query().Get("ids"), ",") {
		if id := strings.TrimSpace(raw); id != "" {
			ids = append(ids, id)
		}
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.ExportAutoReplyPrompts(ctx, &pb.ExportAutoReplyPromptsRequest{
		OwnerUserId: owner,
		PromptIds:   ids,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	document := json.RawMessage(resp.GetDocumentJson())
	if len(document) == 0 || !json.Valid(document) {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "core returned an invalid prompt document", "code": "invalid_document",
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"document": document})
}

// ImportAutoReplyPrompts accepts the same document shape export produces, so a
// downloaded file can be posted back unchanged. The body is forwarded as the
// document string; re-encoding it here would drop keys a future schema version
// adds, which is exactly what a versioned document is for.
func (h *Handlers) ImportAutoReplyPrompts(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	body, ok := readBody(w, r, maxJSONBodyBytes)
	if !ok {
		return
	}
	var document map[string]json.RawMessage
	if err := json.Unmarshal(body, &document); err != nil || document == nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "invalid JSON: expected an object with a prompts array", "code": "import_invalid",
		})
		return
	}
	if _, present := document["prompts"]; !present {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "prompts is required", "code": "import_invalid",
		})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.ImportAutoReplyPrompts(ctx, &pb.ImportAutoReplyPromptsRequest{
		OwnerUserId:  owner,
		DocumentJson: string(body),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	prompts := make([]any, 0, len(resp.GetPrompts()))
	for _, p := range resp.GetPrompts() {
		if p != nil {
			prompts = append(prompts, autoReplyPromptJSON(p))
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"imported": resp.GetImported(),
		"skipped":  resp.GetSkipped(),
		"prompts":  prompts,
	})
}

// ------------------------------------------------------------------- targets

func autoReplyTargetJSON(t *pb.AutoReplyTarget) map[string]any {
	return map[string]any{
		"chat_id":    t.GetChatId(),
		"title":      t.GetTitle(),
		"username":   t.GetUsername(),
		"is_enabled": t.GetIsEnabled(),
		"prompt_id":  t.GetPromptId(),
		"note":       t.GetNote(),
		"priority":   t.GetPriority(),
		"added_at":   t.GetAddedAt(),
	}
}

func autoReplyTargetsJSON(targets []*pb.AutoReplyTarget) []any {
	out := make([]any, 0, len(targets))
	for _, t := range targets {
		if t != nil {
			out = append(out, autoReplyTargetJSON(t))
		}
	}
	return out
}

func (h *Handlers) ListAutoReplyTargets(w http.ResponseWriter, r *http.Request) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"targets": []any{}, "total": 0, "enabled": 0})
		return
	}
	q := r.URL.Query()
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.ListAutoReplyTargets(ctx, &pb.ListAutoReplyTargetsRequest{
		OwnerUserId: owner,
		Search:      q.Get("search"),
		Mode:        q.Get("mode"),
		Limit:       autoReplyPageLimit(q.Get("limit")),
		Offset:      autoReplyPageOffset(q.Get("offset")),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"targets": autoReplyTargetsJSON(resp.GetTargets()),
		"total":   resp.GetTotal(),
		"enabled": resp.GetEnabled(),
	})
}

type autoReplyTargetReq struct {
	ChatId    string `json:"chat_id"`
	Title     string `json:"title"`
	Username  string `json:"username"`
	IsEnabled bool   `json:"is_enabled"`
	PromptId  string `json:"prompt_id"`
	Note      string `json:"note"`
	Priority  int32  `json:"priority"`
}

type autoReplyAddTargetsReq struct {
	Targets []autoReplyTargetReq `json:"targets"`
}

func (h *Handlers) AddAutoReplyTargets(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	var req autoReplyAddTargetsReq
	if !bind(w, r, &req) {
		return
	}
	if len(req.Targets) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "targets is required", "code": "invalid_request",
		})
		return
	}
	targets := make([]*pb.AutoReplyTarget, 0, len(req.Targets))
	for i, t := range req.Targets {
		if strings.TrimSpace(t.ChatId) == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "targets[" + strconv.Itoa(i) + "].chat_id is required", "code": "invalid_request",
			})
			return
		}
		targets = append(targets, &pb.AutoReplyTarget{
			ChatId:    t.ChatId,
			Title:     t.Title,
			Username:  t.Username,
			IsEnabled: t.IsEnabled,
			PromptId:  t.PromptId,
			Note:      t.Note,
			Priority:  t.Priority,
		})
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.AddAutoReplyTargets(ctx, &pb.AddAutoReplyTargetsRequest{
		OwnerUserId: owner,
		Targets:     targets,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"targets": autoReplyTargetsJSON(resp.GetTargets()),
		"total":   resp.GetTotal(),
		"enabled": resp.GetEnabled(),
	})
}

// UpdateAutoReplyTarget replaces one target's editable fields. PUT is a full
// replacement, so an omitted flag clears it — but `username` is not part of the
// documented body because it is resolved by the core, not typed by the user,
// and the core's upsert rewrites it verbatim. It is carried forward from the
// stored row so editing a note cannot erase the handle.
func (h *Handlers) UpdateAutoReplyTarget(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	chatID := strings.TrimSpace(r.PathValue("chatId"))
	if chatID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "chat id is required", "code": "invalid_request",
		})
		return
	}
	var req autoReplyTargetReq
	if !bind(w, r, &req) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	username := req.Username
	if username == "" {
		username = h.autoReplyStoredUsername(ctx, owner, chatID)
	}
	resp, err := h.c.AutoReply.UpdateAutoReplyTarget(ctx, &pb.UpdateAutoReplyTargetRequest{
		OwnerUserId: owner,
		Target: &pb.AutoReplyTarget{
			ChatId:    chatID,
			Title:     req.Title,
			Username:  username,
			IsEnabled: req.IsEnabled,
			PromptId:  req.PromptId,
			Note:      req.Note,
			Priority:  req.Priority,
		},
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"target": autoReplyTargetJSON(resp)})
}

// autoReplyStoredUsername is a best-effort read used only to preserve a field
// the PUT body does not carry; a failed lookup leaves it empty, which is what
// the caller's own body said anyway.
func (h *Handlers) autoReplyStoredUsername(ctx context.Context, owner int64, chatID string) string {
	resp, err := h.c.AutoReply.ListAutoReplyTargets(ctx, &pb.ListAutoReplyTargetsRequest{OwnerUserId: owner})
	if err != nil {
		return ""
	}
	for _, t := range resp.GetTargets() {
		if t.GetChatId() == chatID {
			return t.GetUsername()
		}
	}
	return ""
}

type autoReplyRemoveTargetsReq struct {
	ChatIds []string `json:"chat_ids"`
}

func (h *Handlers) RemoveAutoReplyTargets(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	var req autoReplyRemoveTargetsReq
	if !bind(w, r, &req) {
		return
	}
	if len(req.ChatIds) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "chat_ids is required", "code": "invalid_request",
		})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.RemoveAutoReplyTargets(ctx, &pb.RemoveAutoReplyTargetsRequest{
		OwnerUserId: owner,
		ChatIds:     req.ChatIds,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.GetSuccess() {
		writeAutoReplyStatus(w, resp)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success": true,
		"removed": autoReplyRemovedCount(resp.GetMessage()),
	})
}

type autoReplyResolveReq struct {
	SessionId string `json:"session_id"`
	Query     string `json:"query"`
}

func (h *Handlers) ResolveAutoReplyChat(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	var req autoReplyResolveReq
	if !bind(w, r, &req) {
		return
	}
	if strings.TrimSpace(req.Query) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "query is required", "code": "invalid_request",
		})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.ResolveAutoReplyChat(ctx, &pb.ResolveAutoReplyChatRequest{
		OwnerUserId: owner,
		SessionId:   req.SessionId,
		Query:       req.Query,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	// A found=true answer with no target would be a broken response; the client
	// is told "not found" rather than handed a null it must special-case.
	var target any
	found := resp.GetFound() && resp.GetTarget() != nil
	if found {
		target = autoReplyTargetJSON(resp.GetTarget())
	}
	writeJSON(w, http.StatusOK, map[string]any{"found": found, "target": target})
}

// --------------------------------------------------------------------- rules

func autoReplyRuleJSON(rule *pb.AutoReplyRule) map[string]any {
	return map[string]any{
		"id":               rule.GetId(),
		"name":             rule.GetName(),
		"is_enabled":       rule.GetIsEnabled(),
		"priority":         rule.GetPriority(),
		"action":           rule.GetAction(),
		"conditions":       autoReplyObject(rule.GetConditionsJson()),
		"exclusions":       autoReplyObject(rule.GetExclusionsJson()),
		"prompt_id":        rule.GetPromptId(),
		"ai_config_id":     rule.GetAiConfigId(),
		"cooldown_seconds": rule.GetCooldownSeconds(),
		"language":         rule.GetLanguage(),
		"tone":             rule.GetTone(),
		"response_length":  rule.GetResponseLength(),
		"temperature":      rule.GetTemperature(),
		"max_tokens":       rule.GetMaxTokens(),
		"created_at":       rule.GetCreatedAt(),
		"updated_at":       rule.GetUpdatedAt(),
	}
}

func (h *Handlers) ListAutoReplyRules(w http.ResponseWriter, r *http.Request) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"rules": []any{}})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.ListAutoReplyRules(ctx, &pb.ListAutoReplyRulesRequest{OwnerUserId: owner})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"rules": autoReplyRulesJSON(resp.GetRules())})
}

func autoReplyRulesJSON(rules []*pb.AutoReplyRule) []any {
	out := make([]any, 0, len(rules))
	for _, rule := range rules {
		if rule != nil {
			out = append(out, autoReplyRuleJSON(rule))
		}
	}
	return out
}

type autoReplyRuleReq struct {
	Id              string          `json:"id"`
	Name            string          `json:"name"`
	IsEnabled       bool            `json:"is_enabled"`
	Priority        int32           `json:"priority"`
	Action          string          `json:"action"`
	Conditions      json.RawMessage `json:"conditions"`
	Exclusions      json.RawMessage `json:"exclusions"`
	PromptId        string          `json:"prompt_id"`
	AiConfigId      string          `json:"ai_config_id"`
	CooldownSeconds *int32          `json:"cooldown_seconds"`
	Language        string          `json:"language"`
	Tone            string          `json:"tone"`
	ResponseLength  string          `json:"response_length"`
	Temperature     float64         `json:"temperature"`
	MaxTokens       int32           `json:"max_tokens"`
}

func (h *Handlers) CreateAutoReplyRule(w http.ResponseWriter, r *http.Request) {
	h.saveAutoReplyRule(w, r, "")
}

func (h *Handlers) UpdateAutoReplyRule(w http.ResponseWriter, r *http.Request) {
	// The path id wins over a body id: it is the resource the caller addressed,
	// and letting the body redirect the write would be a small confusion attack.
	h.saveAutoReplyRule(w, r, r.PathValue("id"))
}

func (h *Handlers) saveAutoReplyRule(w http.ResponseWriter, r *http.Request, id string) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	var req autoReplyRuleReq
	if !bind(w, r, &req) {
		return
	}
	if strings.TrimSpace(req.Name) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "name is required", "code": "invalid_request",
		})
		return
	}
	conditions, ok := autoReplyJSONObject(req.Conditions)
	if !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "conditions must be a JSON object", "code": "invalid_request",
		})
		return
	}
	exclusions, ok := autoReplyJSONObject(req.Exclusions)
	if !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "exclusions must be a JSON object", "code": "invalid_request",
		})
		return
	}
	if id == "" {
		id = req.Id
	}
	cooldown := int32(-1) // -1 = inherit from the account settings
	if req.CooldownSeconds != nil {
		cooldown = *req.CooldownSeconds
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.SaveAutoReplyRule(ctx, &pb.SaveAutoReplyRuleRequest{
		OwnerUserId: owner,
		Rule: &pb.AutoReplyRule{
			Id:              id,
			Name:            req.Name,
			IsEnabled:       req.IsEnabled,
			Priority:        req.Priority,
			Action:          req.Action,
			ConditionsJson:  conditions,
			ExclusionsJson:  exclusions,
			PromptId:        req.PromptId,
			AiConfigId:      req.AiConfigId,
			CooldownSeconds: cooldown,
			Language:        req.Language,
			Tone:            req.Tone,
			ResponseLength:  req.ResponseLength,
			Temperature:     req.Temperature,
			MaxTokens:       req.MaxTokens,
		},
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	code := http.StatusOK
	if id == "" {
		code = http.StatusCreated
	}
	writeJSON(w, code, map[string]any{"rule": autoReplyRuleJSON(resp)})
}

func (h *Handlers) DeleteAutoReplyRule(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.DeleteAutoReplyRule(ctx, &pb.DeleteAutoReplyRuleRequest{
		OwnerUserId: owner,
		RuleId:      r.PathValue("id"),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeAutoReplyStatus(w, resp)
}

type autoReplyReorderReq struct {
	RuleIds []string `json:"rule_ids"`
}

func (h *Handlers) ReorderAutoReplyRules(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	var req autoReplyReorderReq
	if !bind(w, r, &req) {
		return
	}
	if len(req.RuleIds) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "rule_ids is required", "code": "invalid_request",
		})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.ReorderAutoReplyRules(ctx, &pb.ReorderAutoReplyRulesRequest{
		OwnerUserId: owner,
		RuleIds:     req.RuleIds,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"rules": autoReplyRulesJSON(resp.GetRules())})
}

// ---------------------------------------------------------------------- jobs

func autoReplyJobJSON(j *pb.AutoReplyJob) map[string]any {
	return map[string]any{
		"id":                j.GetId(),
		"chat_id":           j.GetChatId(),
		"chat_title":        j.GetChatTitle(),
		"status":            j.GetStatus(),
		"attempt":           j.GetAttempt(),
		"max_attempts":      j.GetMaxAttempts(),
		"error":             j.GetError(),
		"error_code":        j.GetErrorCode(),
		"inbound_text":      j.GetInboundText(),
		"reply_text":        j.GetReplyText(),
		"correlation_id":    j.GetCorrelationId(),
		"latency_ms":        j.GetLatencyMs(),
		"created_at":        j.GetCreatedAt(),
		"sent_at":           j.GetSentAt(),
		"source_message_id": j.GetSourceMessageId(),
	}
}

func (h *Handlers) ListAutoReplyJobs(w http.ResponseWriter, r *http.Request) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"jobs": []any{}, "total": 0})
		return
	}
	q := r.URL.Query()
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.ListAutoReplyJobs(ctx, &pb.ListAutoReplyJobsRequest{
		OwnerUserId: owner,
		Status:      q.Get("status"),
		ChatId:      q.Get("chat_id"),
		Limit:       autoReplyPageLimit(q.Get("limit")),
		Offset:      autoReplyPageOffset(q.Get("offset")),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	jobs := make([]any, 0, len(resp.GetJobs()))
	for _, j := range resp.GetJobs() {
		if j != nil {
			jobs = append(jobs, autoReplyJobJSON(j))
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"jobs": jobs, "total": resp.GetTotal()})
}

func (h *Handlers) CancelAutoReplyJob(w http.ResponseWriter, r *http.Request) {
	h.autoReplyJobAction(w, r, false)
}

func (h *Handlers) RetryAutoReplyJob(w http.ResponseWriter, r *http.Request) {
	h.autoReplyJobAction(w, r, true)
}

func (h *Handlers) autoReplyJobAction(w http.ResponseWriter, r *http.Request, retry bool) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	req := &pb.AutoReplyJobRequest{OwnerUserId: owner, JobId: r.PathValue("id")}
	var (
		resp *pb.StatusResponse
		err  error
	)
	if retry {
		resp, err = h.c.AutoReply.RetryAutoReplyJob(ctx, req)
	} else {
		resp, err = h.c.AutoReply.CancelAutoReplyJob(ctx, req)
	}
	if err != nil {
		grpcError(w, err)
		return
	}
	writeAutoReplyStatus(w, resp)
}

// --------------------------------------------------------------- stats/preview

func autoReplyStatsJSON(s *pb.AutoReplyStatsResponse) map[string]any {
	return map[string]any{
		"sent_today":         s.GetSentToday(),
		"sent_total":         s.GetSentTotal(),
		"pending":            s.GetPending(),
		"retrying":           s.GetRetrying(),
		"dead_letter":        s.GetDeadLetter(),
		"chats_enabled":      s.GetChatsEnabled(),
		"prompts":            s.GetPrompts(),
		"rules":              s.GetRules(),
		"average_latency_ms": s.GetAverageLatencyMs(),
		"last_sent_at":       s.GetLastSentAt(),
		"statuses":           autoReplyStatusCounts(s.GetStatusJson()),
	}
}

func (h *Handlers) GetAutoReplyStats(w http.ResponseWriter, r *http.Request) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"stats": autoReplyStatsJSON(&pb.AutoReplyStatsResponse{})})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.GetAutoReplyStats(ctx, &pb.GetAutoReplyStatsRequest{OwnerUserId: owner})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"stats": autoReplyStatsJSON(resp)})
}

type autoReplyPreviewReq struct {
	InboundText string `json:"inbound_text"`
	PromptId    string `json:"prompt_id"`
	AiConfigId  string `json:"ai_config_id"`
}

// PreviewAutoReply generates one reply without sending it. It uses the long AI
// budget: a preview waits on a model provider, and the default 15s request
// timeout would surface as "deadline exceeded" on exactly the slow providers a
// user is trying to diagnose.
func (h *Handlers) PreviewAutoReply(w http.ResponseWriter, r *http.Request) {
	owner, ok := autoReplyOwner(w, r)
	if !ok {
		return
	}
	var req autoReplyPreviewReq
	if !bind(w, r, &req) {
		return
	}
	if strings.TrimSpace(req.InboundText) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "inbound_text is required", "code": "invalid_request",
		})
		return
	}
	ctx, cancel := withAITimeout(r)
	defer cancel()
	resp, err := h.c.AutoReply.PreviewAutoReply(ctx, &pb.PreviewAutoReplyRequest{
		OwnerUserId: owner,
		InboundText: req.InboundText,
		PromptId:    req.PromptId,
		AiConfigId:  req.AiConfigId,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"result": map[string]any{
		"ok":         resp.GetOk(),
		"reply_text": resp.GetReplyText(),
		"error":      resp.GetError(),
		"error_code": resp.GetErrorCode(),
		"provider":   resp.GetProvider(),
		"model":      resp.GetModel(),
		"latency_ms": resp.GetLatencyMs(),
		"tokens":     resp.GetTokens(),
	}})
}
