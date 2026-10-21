package handlers

// Replace rules — the HTTP face of the unified /replace manager.
//
// One rule is one find/replace pair; it may be assigned to several forwarding
// tasks and is stored inside each of them (the field the forwarder reads). The
// API therefore speaks about rules, not about per-task entry maps: the core
// groups the copies, and a write lands on every task the rule is assigned to.
//
// Two conventions this file keeps, both for the same reason — a client must be
// able to render what it got without guessing:
//
//   - every key is emitted explicitly in snake_case. A protobuf struct
//     marshalled directly drops false/0/"" (json `omitempty`), and an editor
//     that reads "missing" as "false" cannot round-trip a rule.
//   - an update carries only the fields the caller sent. The request struct
//     uses pointers so "absent" (leave alone) is distinct from "set to false",
//     which is the difference between disabling a rule and erasing it.
//
// Scope always comes from the verified identity: owner_user_id is never read
// from a body or a query string, and a by-id route forwards the caller's id, so
// the core answers NOT_FOUND for a foreign rule (grpcError → 404) exactly as it
// does for a missing one.

import (
	"net/http"
	"strings"

	pb "autoforward/proto"
)

const (
	// defaultReplacePage matches the core's default page size. The dashboard
	// shows "page X/Y" from total+pages, so the size has to be the one the
	// core actually applied, not one this layer hoped for — that is why the
	// response echoes per_page back from the reply instead of from the request.
	defaultReplacePage = 20
	maxReplacePage     = 100
)

// replaceOwner resolves the verified account for a mutation. Reads answer an
// empty page for an unlinked account instead of a 401: "no account" means "no
// rules", and a listing that 401s would look like an outage in the dashboard.
func replaceOwner(w http.ResponseWriter, r *http.Request) (int64, bool) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusUnauthorized, map[string]string{
			"error": "authentication required", "code": "unauthenticated",
		})
		return 0, false
	}
	return owner, true
}

// replaceRuleJSON projects one rule. Field names are the ones the dashboard,
// the bot's HTTP client and the mobile app already read.
func replaceRuleJSON(rule *pb.ReplaceRule) map[string]any {
	if rule == nil {
		return map[string]any{}
	}
	return map[string]any{
		"id":         rule.GetId(),
		"name":       rule.GetName(),
		"source":     rule.GetSource(),
		"to":         rule.GetTo(),
		"mode":       rule.GetMode(),
		"flags":      rule.GetFlags(),
		"whole_word": rule.GetWholeWord(),
		"enabled":    rule.GetEnabled(),
		"priority":   rule.GetPriority(),
		"task_ids":   append([]string{}, rule.GetTaskIds()...),
		"task_names": append([]string{}, rule.GetTaskNames()...),
		"version":    rule.GetVersion(),
		"updated_at": rule.GetUpdatedAt(),
		"badge":      rule.GetBadge(),
	}
}

// replaceRuleReq is a create/update body. Every mutable field is a pointer so
// an omitted key means "leave it as it is" — a PATCH that only flips `enabled`
// must not blank the pattern it is switching off.
type replaceRuleReq struct {
	Name      *string   `json:"name"`
	Source    *string   `json:"source"`
	To        *string   `json:"to"`
	Mode      *string   `json:"mode"`
	Flags     *string   `json:"flags"`
	WholeWord *bool     `json:"whole_word"`
	Enabled   *bool     `json:"enabled"`
	Priority  *int32    `json:"priority"`
	TaskIDs   *[]string `json:"task_ids"`
}

func (req replaceRuleReq) input() *pb.ReplaceRuleInput {
	in := &pb.ReplaceRuleInput{}
	if req.Name != nil {
		in.Name = req.Name
	}
	if req.Source != nil {
		in.Source = req.Source
	}
	if req.To != nil {
		in.To = req.To
	}
	if req.Mode != nil {
		in.Mode = req.Mode
	}
	if req.Flags != nil {
		in.Flags = req.Flags
	}
	if req.WholeWord != nil {
		in.WholeWord = req.WholeWord
	}
	if req.Enabled != nil {
		in.Enabled = req.Enabled
	}
	if req.Priority != nil {
		in.Priority = req.Priority
	}
	if req.TaskIDs != nil {
		in.TaskIds = *req.TaskIDs
	}
	return in
}

// ListReplaceRules serves one page of the caller's rules.
//
// The task filter narrows the set *and* the counts, so "3 enabled" beside a
// task filter never counts a rule that task does not run.
func (h *Handlers) ListReplaceRules(w http.ResponseWriter, r *http.Request) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{
			"rules": []any{}, "total": 0, "page": 1, "pages": 1,
			"per_page": 0, "counts": map[string]int{"total": 0, "enabled": 0, "disabled": 0},
			"tasks": []any{},
		})
		return
	}
	q := r.URL.Query()
	page, _ := boundedQueryInt(strings.TrimSpace(q.Get("page")), 1, 1_000_000)
	if page < 1 {
		page = 1
	}
	perPage := defaultReplacePage
	if raw := strings.TrimSpace(q.Get("per_page")); raw != "" {
		if v, ok := boundedQueryInt(raw, 1, maxReplacePage); ok && v > 0 {
			perPage = int(v)
		}
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Replace.ListReplaceRules(ctx, &pb.ListReplaceRulesRequest{
		OwnerUserId: owner,
		Query:       q.Get("q"),
		Status:      q.Get("status"),
		TaskId:      q.Get("task_id"),
		Page:        page,
		PerPage:     int32(perPage),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	rules := make([]any, 0, len(resp.GetRules()))
	for _, rule := range resp.GetRules() {
		rules = append(rules, replaceRuleJSON(rule))
	}
	tasks := make([]any, 0, len(resp.GetTasks()))
	for _, task := range resp.GetTasks() {
		tasks = append(tasks, map[string]string{"id": task.GetId(), "name": task.GetName()})
	}
	counts := resp.GetCounts()
	writeJSON(w, http.StatusOK, map[string]any{
		"rules":    rules,
		"total":    resp.GetTotal(),
		"page":     resp.GetPage(),
		"pages":    resp.GetPages(),
		"per_page": resp.GetPerPage(),
		"counts": map[string]int32{
			"total":    counts.GetTotal(),
			"enabled":  counts.GetEnabled(),
			"disabled": counts.GetDisabled(),
		},
		"query":   resp.GetQuery(),
		"status":  resp.GetStatus(),
		"task_id": resp.GetTaskId(),
		"tasks":   tasks,
	})
}

// GetReplaceRule serves one rule, owner-scoped by the core.
func (h *Handlers) GetReplaceRule(w http.ResponseWriter, r *http.Request) {
	owner, ok := replaceOwner(w, r)
	if !ok {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	rule, err := h.c.Replace.GetReplaceRule(ctx, &pb.GetReplaceRuleRequest{
		OwnerUserId: owner, RuleId: r.PathValue("id"),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"rule": replaceRuleJSON(rule)})
}

// CreateReplaceRule adds a rule and assigns it to the named tasks.
func (h *Handlers) CreateReplaceRule(w http.ResponseWriter, r *http.Request) {
	owner, ok := replaceOwner(w, r)
	if !ok {
		return
	}
	var req replaceRuleReq
	if !bind(w, r, &req) {
		return
	}
	if req.TaskIDs == nil || len(*req.TaskIDs) == 0 {
		// A rule nobody runs is a row that will never fire; say so here rather
		// than storing it and letting the user wonder.
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "task_ids is required", "code": "task_required",
			"message_key": "ui_rpl_err_task_required",
		})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	rule, err := h.c.Replace.CreateReplaceRule(ctx, &pb.CreateReplaceRuleRequest{
		OwnerUserId: owner, Rule: req.input(),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"rule": replaceRuleJSON(rule)})
}

// UpdateReplaceRule applies a partial update: only the keys present are written.
func (h *Handlers) UpdateReplaceRule(w http.ResponseWriter, r *http.Request) {
	owner, ok := replaceOwner(w, r)
	if !ok {
		return
	}
	var req replaceRuleReq
	if !bind(w, r, &req) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	rule, err := h.c.Replace.UpdateReplaceRule(ctx, &pb.UpdateReplaceRuleRequest{
		OwnerUserId: owner, RuleId: r.PathValue("id"), Rule: req.input(),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"rule": replaceRuleJSON(rule)})
}

// DeleteReplaceRule removes every copy of a rule and reports how many.
func (h *Handlers) DeleteReplaceRule(w http.ResponseWriter, r *http.Request) {
	owner, ok := replaceOwner(w, r)
	if !ok {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Replace.DeleteReplaceRule(ctx, &pb.DeleteReplaceRuleRequest{
		OwnerUserId: owner, RuleId: r.PathValue("id"),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": resp.GetOk(), "removed": resp.GetRemoved(),
	})
}

// SetReplaceRuleEnabled sets an absolute state, not a toggle: a retried call
// (double tap, retry after a timeout) must land where the user asked.
func (h *Handlers) SetReplaceRuleEnabled(w http.ResponseWriter, r *http.Request) {
	owner, ok := replaceOwner(w, r)
	if !ok {
		return
	}
	var req struct {
		Enabled *bool `json:"enabled"`
	}
	if !bind(w, r, &req) {
		return
	}
	if req.Enabled == nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "enabled is required", "code": "invalid_request",
		})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	rule, err := h.c.Replace.SetReplaceRuleEnabled(ctx, &pb.SetReplaceRuleEnabledRequest{
		OwnerUserId: owner, RuleId: r.PathValue("id"), Enabled: *req.Enabled,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"rule": replaceRuleJSON(rule)})
}

// DuplicateReplaceRule copies a rule, kept assigned to the same tasks.
func (h *Handlers) DuplicateReplaceRule(w http.ResponseWriter, r *http.Request) {
	owner, ok := replaceOwner(w, r)
	if !ok {
		return
	}
	var req struct {
		Name string `json:"name"`
	}
	if !bindOptional(w, r, &req) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	rule, err := h.c.Replace.DuplicateReplaceRule(ctx, &pb.DuplicateReplaceRuleRequest{
		OwnerUserId: owner, RuleId: r.PathValue("id"), Name: req.Name,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"rule": replaceRuleJSON(rule)})
}

// TestReplaceRule previews a rule (stored and/or edited) against sample text,
// using the same engine that runs on a forwarded message.
func (h *Handlers) TestReplaceRule(w http.ResponseWriter, r *http.Request) {
	owner, ok := replaceOwner(w, r)
	if !ok {
		return
	}
	var req struct {
		RuleID string `json:"rule_id"`
		Sample string `json:"sample"`
		replaceRuleReq
	}
	if !bind(w, r, &req) {
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Replace.TestReplaceRule(ctx, &pb.TestReplaceRuleRequest{
		OwnerUserId: owner, RuleId: req.RuleID, Sample: req.Sample,
		Rule: req.replaceRuleReq.input(),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":      resp.GetOk(),
		"input":   resp.GetInput(),
		"output":  resp.GetOutput(),
		"matches": resp.GetMatches(),
		"changed": resp.GetChanged(),
	})
}

// ListReplaceRuleTasks serves the task picker: the caller's tasks, nothing else.
func (h *Handlers) ListReplaceRuleTasks(w http.ResponseWriter, r *http.Request) {
	owner := identity(r).UserID
	if owner <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"tasks": []any{}})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Replace.ListReplaceRuleTasks(ctx, &pb.ListReplaceRuleTasksRequest{
		OwnerUserId: owner,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	tasks := make([]any, 0, len(resp.GetTasks()))
	for _, task := range resp.GetTasks() {
		tasks = append(tasks, map[string]string{"id": task.GetId(), "name": task.GetName()})
	}
	writeJSON(w, http.StatusOK, map[string]any{"tasks": tasks})
}
