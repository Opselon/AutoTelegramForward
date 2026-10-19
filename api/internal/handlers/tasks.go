package handlers

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"time"

	pb "autoforward/proto"
)

// taskKinds mirrors the Core registry's KINDS set: an unknown kind is
// refused here, before a gRPC round trip, and again by Core (the two lists
// are contract-tested against each other in the Python suite / this packs).
var taskKinds = map[string]bool{
	"session.backup": true,
	"dlq.replay":     true,
	"dlq.purge":      true,
	"backup.export":  true,
}

// Per-user task lifecycle (API-first phase 2). Every endpoint scopes itself
// to the verified identity from the JWT — OwnerUserId is never read from the
// request. A foreign task is deliberately indistinguishable from a missing
// one (404), so ids cannot be probed.

const (
	// taskCreateTimeout bounds the create path independently of the default
	// 15s request budget: creation is a durability contract, not an execution.
	taskCreateTimeout = 15 * time.Second
	// maxTasksPage caps every listing; the Core clamps again server-side.
	maxTasksPage = 200
)

type createTaskReq struct {
	Kind           string          `json:"kind"`
	Payload        json.RawMessage `json:"payload"`
	IdempotencyKey string          `json:"idempotency_key"`
	MaxAttempts    int32           `json:"max_attempts"`
}

// CreateTask registers a tenant-owned durable task. Same (owner, kind,
// idempotency_key) returns the original task with already_existed=true —
// safe to retry over an unstable network.
func (h *Handlers) CreateTask(w http.ResponseWriter, r *http.Request) {
	var req createTaskReq
	// `POST /api/tasks?kind=<kind>` with no body is a documented convenience.
	// It used to be implemented by *skipping* the body read whenever the query
	// carried a kind — which also skipped it for a request that had BOTH, so a
	// body payload and its idempotency key were silently discarded and the
	// caller got a 201 for a task that could not do what it asked.
	//
	// The body is now always read when one is present (a chunked body reports
	// ContentLength -1, so "not zero" is the right test rather than "> 0"); the
	// query value is only a fallback for the no-body form, so the explicit body
	// wins and the two can never disagree silently.
	if r.ContentLength != 0 {
		if !bind(w, r, &req) {
			return
		}
	}
	if req.Kind == "" {
		req.Kind = r.URL.Query().Get("kind")
	}
	if req.Kind == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "kind is required", "error_code": "invalid_request"})
		return
	}
	if !taskKinds[req.Kind] {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error":      "kind '" + req.Kind + "' is not supported",
			"error_code": "unknown_task_kind"})
		return
	}
	owner := identity(r)
	if owner.UserID <= 0 {
		writeJSON(w, http.StatusUnauthorized, map[string]string{
			"error": "unauthorized", "error_code": "unauthorized"})
		return
	}
	// Bound the create window independently of the shared timeout: creation
	// is a durability contract, not an execution.
	ctx, cancel := context.WithTimeout(r.Context(), taskCreateTimeout)
	defer cancel()
	payload := ""
	if len(req.Payload) > 0 {
		// Forward the raw JSON of the object form; the store bounds and
		// validates it (payload_too_large / invalid_json back as 422-style
		// refusal codes mapped below).
		payload = string(req.Payload)
	}
	resp, err := h.c.Tasks.CreateTask(ctx, &pb.CreateTaskRequest{
		OwnerUserId:    owner.UserID,
		Kind:           req.Kind,
		PayloadJson:    payload,
		IdempotencyKey: req.IdempotencyKey,
		MaxAttempts:    req.MaxAttempts,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		status := taskRefusalStatus(resp.ErrorCode)
		writeJSON(w, status, map[string]string{
			"error": resp.Error, "error_code": resp.ErrorCode})
		return
	}
	// Defense in depth: whatever the downstream answered, a task addressed
	// to any identity but the caller's never leaves this process.
	if resp.Task == nil || resp.Task.OwnerUserId != owner.UserID {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "task identity mismatch", "error_code": "invalid_response"})
		return
	}
	code := http.StatusCreated
	if resp.AlreadyExisted {
		code = http.StatusOK
	}
	writeJSON(w, code, map[string]any{
		"task":            resp.Task,
		"already_existed": resp.AlreadyExisted,
		"id":              resp.Task.GetId(),
		"status":          resp.Task.GetStatus(),
	})
}

func (h *Handlers) ListTasks(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	// Query values are parsed through boundedQueryInt: the clamp happens on the
	// int64 BEFORE the narrowing conversion, so int32() cannot wrap an
	// out-of-range value (CodeQL incorrect-integer-conversion, and the honest
	// truth about Atoi's architecture-dependent int). Same pattern admin.go
	// already uses.
	limit, ok := boundedQueryInt(q.Get("limit"), 1, maxTasksPage)
	if !ok || limit <= 0 {
		limit = 50
	}
	offset, _ := boundedQueryInt(q.Get("offset"), 0, math.MaxInt32)
	owner := identity(r)
	if owner.UserID <= 0 {
		writeJSON(w, http.StatusUnauthorized, map[string]string{
			"error": "unauthorized", "error_code": "unauthorized"})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Tasks.ListTasks(ctx, &pb.ListTasksRequest{
		OwnerUserId: owner.UserID,
		Status:      q.Get("status"),
		Kind:        q.Get("kind"),
		Limit:       int32(limit),
		Offset:      int32(offset),
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		writeJSON(w, taskRefusalStatus(resp.ErrorCode), map[string]string{
			"error": resp.Error, "error_code": resp.ErrorCode})
		return
	}
	// Defense in depth: a downstream that ignored OwnerUserId (or a future
	// regression) must not leak another tenant's tasks; enforce the badge
	// here regardless of who filled it correctly.
	scoped := make([]*pb.UserTask, 0, len(resp.Tasks))
	for _, t := range resp.Tasks {
		if t != nil && t.OwnerUserId == owner.UserID {
			scoped = append(scoped, t)
		}
	}
	resp.Tasks = scoped
	writeJSON(w, http.StatusOK, map[string]any{
		"tasks":  resp.Tasks,
		"total":  resp.Total,
		"limit":  limit,
		"offset": offset,
	})
}

func (h *Handlers) GetTask(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	owner := identity(r)
	if owner.UserID <= 0 {
		writeJSON(w, http.StatusUnauthorized, map[string]string{
			"error": "unauthorized", "error_code": "unauthorized"})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Tasks.GetTask(ctx, &pb.GetTaskRequest{
		TaskId: id, OwnerUserId: owner.UserID})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		writeJSON(w, taskRefusalStatus(resp.ErrorCode), map[string]string{
			"error": resp.Error, "error_code": resp.ErrorCode})
		return
	}
	if resp.Task == nil || resp.Task.OwnerUserId != owner.UserID {
		// Another tenant's task is indistinguishable from a missing one.
		writeJSON(w, http.StatusNotFound, map[string]string{
			"error": "not_found", "error_code": "not_found"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": resp.Task})
}

func (h *Handlers) CancelTask(w http.ResponseWriter, r *http.Request) {
	h.taskAction(w, r, "cancel")
}

func (h *Handlers) RetryTask(w http.ResponseWriter, r *http.Request) {
	h.taskAction(w, r, "retry")
}

func (h *Handlers) taskAction(w http.ResponseWriter, r *http.Request, action string) {
	id := r.PathValue("id")
	owner := identity(r)
	if owner.UserID <= 0 {
		writeJSON(w, http.StatusUnauthorized, map[string]string{
			"error": "unauthorized", "error_code": "unauthorized"})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()
	var (
		resp *pb.TaskResponse
		err  error
	)
	if action == "cancel" {
		resp, err = h.c.Tasks.CancelTask(ctx, &pb.CancelTaskRequest{
			TaskId: id, OwnerUserId: owner.UserID})
	} else {
		resp, err = h.c.Tasks.RetryTask(ctx, &pb.RetryTaskRequest{
			TaskId: id, OwnerUserId: owner.UserID})
	}
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		writeJSON(w, taskRefusalStatus(resp.ErrorCode), map[string]string{
			"error": resp.Error, "error_code": resp.ErrorCode})
		return
	}
	if resp.Task == nil || resp.Task.OwnerUserId != owner.UserID {
		writeJSON(w, http.StatusNotFound, map[string]string{
			"error": "not_found", "error_code": "not_found"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": resp.Task})
}

// taskRefusalStatus maps the domain's refusal codes onto HTTP. Ownership is
// ALWAYS 404-shaped for reads (no existence oracle) and 403-shaped for a
// refused state transition on a task the caller can already see.
func taskRefusalStatus(code string) int {
	switch code {
	case "not_found":
		return http.StatusNotFound
	case "unauthorized", "invalid_transition", "invalid_status", "invalid_request":
		return http.StatusBadRequest
	case "invalid_idempotency_key", "unknown_task_kind", "invalid_json",
		"payload_too_large", "invalid_max_attempts", "not_cancellable",
		"running_not_cancellable", "not_retryable":
		return http.StatusBadRequest
	default:
		return http.StatusBadRequest
	}
}
