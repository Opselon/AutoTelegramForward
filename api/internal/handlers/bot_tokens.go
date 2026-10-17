package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"

	pb "autoforward/proto"
)

// Bot-route credentials over REST.
//
// The route itself (source -> BOT -> target) is a per-task setting; these three
// endpoints are only the credential half, so the dashboard and the mobile app
// can attach, list and remove the Telegram bots a tenant forwards through.
//
// Two properties are deliberate and must survive any refactor here:
//
//   - A secret travels one way only. The request body carries the token; every
//     response carries id + resolved @username and nothing else. The proto has
//     no field for the token, so a handler cannot leak one by accident.
//   - Ownership is checked against the caller's own list, so "not yours" and
//     "does not exist" are the same 404. A probe must not learn which ids are
//     real.
const maxBotTokenBody = 4096

// A bot's defaults are a JSON object of registry settings: bounded, but larger
// than a token, and still small enough that a bad client cannot stream megabytes
// through the decode path.
const maxBotDefaultsBody = 16384

type botTokenRequest struct {
	Label string `json:"label"`
	Token string `json:"token"`
}

func (h *Handlers) ListBotTokens(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.BotTokens.ListBotTokens(ctx, &pb.ListBotTokensRequest{
		OwnerUserId: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	tokens := make([]*pb.BotTokenInfo, 0, len(resp.Tokens))
	for _, t := range resp.Tokens {
		if t != nil {
			tokens = append(tokens, t)
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"tokens": tokens,
		"total":  len(tokens),
	})
}

func (h *Handlers) CreateBotToken(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	if id.UserID == 0 {
		// An unlinked web account owns nothing, so it may attach nothing.
		writeJSON(w, http.StatusForbidden, map[string]string{
			"error": "link a Telegram account before attaching a bot",
		})
		return
	}
	// A bot token is ~46 bytes; anything larger is not one, and the limit keeps
	// a bad client from pushing megabytes through the decode path.
	r.Body = http.MaxBytesReader(w, r.Body, maxBotTokenBody)
	var body botTokenRequest
	if !bindPB(w, r, &body) {
		return
	}
	body.Token = strings.TrimSpace(body.Token)
	if body.Token == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "token is required"})
		return
	}
	ctx, cancel := withTimeout(r)
	defer cancel()

	resp, err := h.c.BotTokens.CreateBotToken(ctx, &pb.CreateBotTokenRequest{
		OwnerUserId: id.UserID,
		Label:       strings.TrimSpace(body.Label),
		Token:       body.Token,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		// The core validated the credential with Telegram and refused it: that is
		// a bad request from the caller's point of view, not a server fault. The
		// message is core-authored and never echoes the token.
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": resp.Message,
			"code":  resp.ErrorCode,
		})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"token": resp.Token})
}

func (h *Handlers) DeleteBotToken(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	if !h.ownsBotToken(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "bot not found"})
		return
	}
	resp, err := h.c.BotTokens.DeleteBotToken(ctx, &pb.DeleteBotTokenRequest{
		Id:          r.PathValue("id"),
		OwnerUserId: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Success {
		// Refused because a task still routes through this bot: the caller has to
		// move those tasks first, so this is not a 404.
		writeJSON(w, http.StatusConflict, map[string]string{"error": resp.Message})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "deleted"})
}

// Bot defaults: the settings an attached bot carries for every task that uses
// it, so a tenant with many tasks configures them once.
//
// Two properties carried over from the credential endpoints above:
//
//   - the owner is in the WHERE clause, so a transplanted bot id is a 404 and
//     never a way to read or write another tenant's bot;
//   - every value is validated against the registry before it is stored, so a
//     setting that names a chat, a route or a schedule (which would point other
//     tasks at other chats) is refused with 400 rather than stored.
type botDefaultsRequest struct {
	Defaults   map[string]any `json:"defaults"`
	ReplaceAll bool           `json:"replace_all"`
	Clear      []string       `json:"clear"`
}

func (h *Handlers) GetBotDefaults(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	if id.UserID == 0 {
		writeJSON(w, http.StatusForbidden, map[string]string{
			"error": "link a Telegram account before configuring a bot",
		})
		return
	}
	if !h.ownsBotToken(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "bot not found"})
		return
	}
	resp, err := h.c.BotTokens.GetBotDefaults(ctx, &pb.GetBotDefaultsRequest{
		Id:          r.PathValue("id"),
		OwnerUserId: id.UserID,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeDefaultsResponse(w, http.StatusOK, resp)
}

func (h *Handlers) SetBotDefaults(w http.ResponseWriter, r *http.Request) {
	id := identity(r)
	ctx, cancel := withTimeout(r)
	defer cancel()

	if id.UserID == 0 {
		writeJSON(w, http.StatusForbidden, map[string]string{
			"error": "link a Telegram account before configuring a bot",
		})
		return
	}
	if !h.ownsBotToken(ctx, r.PathValue("id"), id.UserID) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "bot not found"})
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxBotDefaultsBody)
	var body botDefaultsRequest
	if !bindPB(w, r, &body) {
		return
	}
	encoded, err := json.Marshal(body.Defaults)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "defaults must be an object of settings",
			"code":  "invalid_body",
		})
		return
	}
	resp, err := h.c.BotTokens.SetBotDefaults(ctx, &pb.SetBotDefaultsRequest{
		Id:           r.PathValue("id"),
		OwnerUserId:  id.UserID,
		DefaultsJson: string(encoded),
		ReplaceAll:   body.ReplaceAll,
		Clear:        body.Clear,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeDefaultsResponse(w, http.StatusOK, resp)
}

// writeDefaultsResponse answers with the stored mapping even on a refusal, so a
// client can put the user's screen back to what is actually saved.
func writeDefaultsResponse(w http.ResponseWriter, status int, resp *pb.BotDefaultsResponse) {
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
	if resp.GetDefaultsJson() != "" {
		var defaults map[string]any
		if err := json.Unmarshal([]byte(resp.GetDefaultsJson()), &defaults); err == nil {
			out["defaults"] = defaults
			out["total"] = len(defaults)
		}
	}
	if resp.GetSuccess() {
		out["changed"] = resp.GetChanged()
		writeJSON(w, status, out)
		return
	}
	// A rejected write is a client error, never a 200 with a false flag.
	writeJSON(w, settingsErrorStatus(resp.GetErrorCode()), out)
}

// ownsBotToken answers from the caller's own list, so a foreign id is
// indistinguishable from a missing one.
func (h *Handlers) ownsBotToken(ctx context.Context, tokenID string, userID int64) bool {
	if userID == 0 || tokenID == "" {
		return false
	}
	resp, err := h.c.BotTokens.ListBotTokens(ctx, &pb.ListBotTokensRequest{
		OwnerUserId: userID,
	})
	if err != nil {
		return false
	}
	for _, t := range resp.Tokens {
		if t != nil && t.Id == tokenID {
			return true
		}
	}
	return false
}
