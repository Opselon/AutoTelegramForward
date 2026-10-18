package handlers

import (
	"context"
	"net/http"
	"strings"

	"autoforward/internal/middleware"
	pb "autoforward/proto"
)

// authMiddleware validates the Bearer credential (JWT minted by the Python core)
// and loads the caller's Identity into the request context. Every dashboard
// route is wrapped with it so data is scoped to the authenticated account.
func (h *Handlers) authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health/services" || r.URL.Path == "/api/health/services" || r.URL.Path == "/api/v1/health/services" {
			next.ServeHTTP(w, r)
			return
		}
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
		// An account-less identity must not reach a dashboard handler. Every
		// handler below scopes its work by the owner it passes to the core, and
		// owner 0 is not a tenant: for the ordinary reads it is the shared,
		// unattributed bucket, and for a backup export it means the WHOLE
		// installation. The core issues such a token only for an account that
		// has no Telegram id yet (a synthetic negative id), so refusing it here
		// is refusing an account that cannot own data - the same rule the
		// single-handler guards (ListChats, CreateBotToken, RequireAdmin)
		// already apply one by one. Onboarding is unaffected: /api/auth/* is
		// mounted OUTSIDE this middleware, so /auth/me still answers an
		// unlinked caller and can still explain that they must link.
		if resp.UserId <= 0 {
			writeJSON(w, http.StatusForbidden, map[string]string{
				"error": "this account must be linked to a Telegram id first",
				"code":  "account_not_linked",
			})
			return
		}
		r = r.WithContext(context.WithValue(r.Context(), identityKey{}, &Identity{
			UserID:   resp.UserId,
			Username: resp.Username,
			IsAdmin:  resp.IsAdmin,
		}))
		// Hand the verified actor to the access logger. It is set here, after
		// verification, so a spoofed client header can never reach the archive.
		if m := middleware.Meta(r.Context()); m != nil {
			m.UserID = resp.UserId
			m.Username = resp.Username
		}
		next.ServeHTTP(w, r)
	})
}

// IdentityOf exposes the VERIFIED identity of a request to middleware that
// cannot import this package (the rate limiter is one: importing handlers from
// it, or it from handlers, would be a cycle). It is set deliberately narrow — a
// user id, an admin flag and "was this request authenticated at all" — and no
// caller-supplied value can influence it: the fields come from the core's
// ValidateToken answer, not from a header.
func (h *Handlers) IdentityOf(r *http.Request) (int64, bool, bool) {
	id := identity(r)
	if id == nil {
		return 0, false, false
	}
	return id.UserID, id.IsAdmin, true
}

// RequireAuth mounts a dashboard sub-mux under authentication.
func (h *Handlers) RequireAuth(inner http.Handler) http.Handler {
	return h.authMiddleware(http.StripPrefix("/api", inner))
}

// StripPrefix mounts a sub-mux without authentication (used for /api/auth/*).
func (h *Handlers) StripPrefix(inner http.Handler) http.Handler {
	return http.StripPrefix("/api/auth", inner)
}

// RequireAdmin ensures that the authenticated caller has server-verified IsAdmin true.
func (h *Handlers) RequireAdmin(inner http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := identity(r)
		if id == nil || !id.IsAdmin || id.UserID <= 0 {
			writeJSON(w, http.StatusForbidden, map[string]string{
				"error": "admin access required",
			})
			return
		}
		inner.ServeHTTP(w, r)
	})
}
