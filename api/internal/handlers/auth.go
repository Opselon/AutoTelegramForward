package handlers

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
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

// AuthTMA authenticates a user coming from a Telegram Mini App (TMA).
// It verifies the cryptographic HMAC-SHA256 signature of window.Telegram.WebApp.initData
// using the bot token, extracts the verified numeric Telegram User ID,
// and issues an authenticated JWT token.
func (h *Handlers) AuthTMA(w http.ResponseWriter, r *http.Request) {
	var body struct {
		InitData string `json:"init_data"`
	}
	if !bind(w, r, &body) {
		return
	}
	if strings.TrimSpace(body.InitData) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "init_data is required"})
		return
	}

	values, err := url.ParseQuery(body.InitData)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "malformed init_data: " + err.Error()})
		return
	}

	receivedHash := values.Get("hash")
	if receivedHash == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "missing hash in init_data"})
		return
	}

	authDateStr := values.Get("auth_date")
	if authDateStr == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "missing auth_date in init_data"})
		return
	}
	authDate, err := strconv.ParseInt(authDateStr, 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid auth_date"})
		return
	}

	now := time.Now().Unix()

	// Future-dated auth_date: Telegram never issues these. A clock-skewed or
	// malicious payload claiming a future date would otherwise create a token
	// whose replay window has not even opened yet.
	if authDate > now {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "init_data auth_date is in the future"})
		return
	}

	// Reject tokens older than 24 hours to prevent replay attacks
	if now-authDate > 86400 {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "init_data expired"})
		return
	}

	// Reject a literal duplicate query parameter. url.ParseQuery keeps only
	// the last value for a repeated key, which changes the canonical
	// data-check string and would be verified against a single hash — but a
	// client can still smuggle a second `hash` past a naive parser. Refuse
	// any init_data with a repeated key outright.
	if strings.Contains(body.InitData, "&hash=") && strings.Count(body.InitData, "hash=") > 1 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "malformed init_data: duplicate parameter"})
		return
	}

	// Build data-check-string (all keys except 'hash', sorted alphabetically)
	var keys []string
	for k := range values {
		if k != "hash" {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	var pairs []string
	for _, k := range keys {
		pairs = append(pairs, k+"="+values.Get(k))
	}
	dataCheckString := strings.Join(pairs, "\n")

	botToken := strings.TrimSpace(os.Getenv("ATF_BOT_TOKEN"))
	if botToken == "" {
		botToken = strings.TrimSpace(os.Getenv("TELEGRAM_BOT_TOKEN"))
	}
	if botToken == "" {
		botToken = strings.TrimSpace(os.Getenv("BOT_TOKEN"))
	}
	if botToken == "" {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "server bot token not configured"})
		return
	}

	// Telegram HMAC-SHA256 formula:
	// secret_key = HMAC_SHA256("WebAppData", bot_token)
	// data_hash  = HMAC_SHA256(secret_key, data_check_string)
	hSecret := hmac.New(sha256.New, []byte("WebAppData"))
	hSecret.Write([]byte(botToken))
	secretKey := hSecret.Sum(nil)

	hData := hmac.New(sha256.New, secretKey)
	hData.Write([]byte(dataCheckString))
	expectedHash := hex.EncodeToString(hData.Sum(nil))

	if !hmac.Equal([]byte(strings.ToLower(expectedHash)), []byte(strings.ToLower(receivedHash))) {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid init_data signature"})
		return
	}

	// Signature is cryptographically verified. Now extract numeric user ID.
	var userID int64
	if userJSON := values.Get("user"); userJSON != "" {
		var u struct {
			ID int64 `json:"id"`
		}
		if err := json.Unmarshal([]byte(userJSON), &u); err == nil && u.ID > 0 {
			userID = u.ID
		}
	}
	if userID <= 0 {
		if idVal := values.Get("id"); idVal != "" {
			if parsed, err := strconv.ParseInt(idVal, 10, 64); err == nil && parsed > 0 {
				userID = parsed
			}
		}
	}
	if userID <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "no valid user id in init_data"})
		return
	}

	ctx, cancel := withTimeout(r)
	defer cancel()
	resp, err := h.c.Account.IssueWebToken(ctx, &pb.IssueWebTokenRequest{UserId: userID})
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
		UserId      int64  `json:"user_id"`
		OldPassword string `json:"old_password"`
		NewPassword string `json:"new_password"`
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
