package handlers

import (
	"context"
	"net/http"
	"strings"

	pb "autoforward/proto"
)

// authMiddleware validates the Bearer credential (JWT minted by the Python core)
// and loads the caller's Identity into the request context. Every dashboard
// route is wrapped with it so data is scoped to the authenticated account.
func (h *Handlers) authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		if !strings.HasPrefix(auth, "Bearer ") {
			writeJSON(w, http.StatusUnauthorized, map[string]string{
				"error": "authentication required",
			})
			return
		}
		cred := auth[len("Bearer "):]
		ctx, cancel := withTimeout(r)
		defer cancel()
		req := &pb.ValidateTokenRequest{}
		req.Token = cred
		resp, err := h.c.Account.ValidateToken(ctx, req)
		if err != nil {
			grpcError(w, err)
			return
		}
		if !resp.Valid {
			writeJSON(w, http.StatusUnauthorized, map[string]string{
				"error": "invalid or expired token",
			})
			return
		}
		r = r.WithContext(context.WithValue(r.Context(), identityKey{}, &Identity{
			UserID:   resp.UserId,
			Username: resp.Username,
			IsAdmin:  resp.IsAdmin,
		}))
		next.ServeHTTP(w, r)
	})
}

// RequireAuth mounts a dashboard sub-mux under authentication.
func (h *Handlers) RequireAuth(inner http.Handler) http.Handler {
	return h.authMiddleware(http.StripPrefix("/api", inner))
}

// StripPrefix mounts a sub-mux without authentication (used for /api/auth/*).
func (h *Handlers) StripPrefix(inner http.Handler) http.Handler {
	return http.StripPrefix("/api/auth", inner)
}
