package handlers

import (
	"context"
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
