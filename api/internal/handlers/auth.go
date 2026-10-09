package handlers

import (
	"net/http"
	"time"

	pb "autoforward/proto"
)

// Identity is the authenticated per-user context extracted from a JWT that
// the Python core signed (single source of truth for auth).
type Identity struct {
	UserID   int64
	Username string
	IsAdmin  bool
}

type identityKey struct{}

// --------------------------------------------------------------- auth rpcs
func (h *Handlers) AuthRegister(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	var req pb.RegisterAccountRequest
	if !bind(w, r, &req) {
		return
	}
	resp, err := h.c.Account.RegisterAccount(ctx, &req)
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (h *Handlers) AuthLogin(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	var req pb.LoginAccountRequest
	if !bind(w, r, &req) {
		return
	}
	resp, err := h.c.Account.LoginAccount(ctx, &req)
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// AuthBotExchange lets an already-authenticated Telegram bot user log into the
// web dashboard without typing credentials (the bot mints a short-lived token
// which is exchanged here for a 7-day JWT).
func (h *Handlers) AuthBotExchange(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()
	var req pb.IssueWebTokenRequest
	if !bind(w, r, &req) {
		return
	}
	resp, err := h.c.Account.IssueWebToken(ctx, &req)
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) AuthChangePassword(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()

	var body struct {
		UserId       int64  `json:"user_id"`
		OldPassword  string `json:"old_password"`
		NewPassword  string `json:"new_password"`
	}
	if !bind(w, r, &body) {
		return
	}
	if body.UserId == 0 {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "user_id required"})
		return
	}
	resp, err := h.c.Account.ChangePassword(ctx, &pb.ChangePasswordRequest{
		UserId:      body.UserId,
		OldPassword: body.OldPassword,
		NewPassword: body.NewPassword,
	})
	if err != nil {
		grpcError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) AuthMe(w http.ResponseWriter, r *http.Request) {
	auth := r.Header.Get("Authorization")
	if len(auth) <= 7 || auth[:7] != "Bearer " {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "authentication required"})
		return
	}
	token := auth[7:]
	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Account.ValidateToken(ctx, &pb.ValidateTokenRequest{Token: token})
	if err != nil {
		grpcError(w, err)
		return
	}
	if !resp.Valid {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid or expired token"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"user_id":  resp.UserId,
		"username": resp.Username,
		"is_admin": resp.IsAdmin,
		"now":      time.Now().UTC().Format(time.RFC3339),
	})
}
