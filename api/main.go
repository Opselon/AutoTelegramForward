package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"

	"autoforward/internal/handlers"
	"autoforward/internal/grpcclient"
	"autoforward/internal/middleware"
)

func main() {
	addr := getenv("ATF_API_ADDR", ":8080")
	coreAddr := getenv("ATF_CORE_GRPC", "localhost:50051")
	loggerAddr := getenv("ATF_LOGGER_ADDR", getenv("ATF_LOGGER_GRPC", "127.0.0.1:6002"))
	apiKey := getenv("ATF_API_KEY", "")

	conn, err := grpcclient.Dial(coreAddr)
	if err != nil {
		log.Printf("warning: grpc dial to core (%s) failed: %v — handlers will return 502 until core is up", coreAddr, err)
	}
	defer conn.Close()

	clients := grpcclient.NewClients(conn)
	if loggerConn, lerr := grpcclient.Dial(loggerAddr); lerr == nil {
		grpcclient.AttachLogger(clients, loggerConn)
		log.Printf("connected to logger service at %s", loggerAddr)
	} else {
		log.Printf("warning: logger service (%s) unreachable — log endpoints return 502", loggerAddr)
	}
	h := handlers.New(clients)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", h.Health)
	mux.HandleFunc("GET /health", h.Health)

	// Public authentication endpoints (no Bearer [REDACTED] required).
	authMux := http.NewServeMux()
	authMux.HandleFunc("POST /register", h.AuthRegister)
	authMux.HandleFunc("POST /login", h.AuthLogin)
	authMux.HandleFunc("POST /exchange-bot-token", h.AuthBotExchange)
	authMux.HandleFunc("PATCH /change-password", h.AuthChangePassword)
	authMux.HandleFunc("GET /me", h.AuthMe)
	mux.Handle("/api/auth/", h.StripPrefix(authMux))

	// Authenticated dashboard endpoints — every caller is scoped to their own
	// account via Identity.UserID (0 = unlinked web account => sees nothing).
	dashMux := http.NewServeMux()
	dashMux.HandleFunc("GET /stats", h.GetStats)
	dashMux.HandleFunc("GET /rules", h.ListRules)
	dashMux.HandleFunc("POST /rules", h.CreateRule)
	dashMux.HandleFunc("GET /rules/{id}", h.GetRule)
	dashMux.HandleFunc("PUT /rules/{id}", h.UpdateRule)
	dashMux.HandleFunc("DELETE /rules/{id}", h.DeleteRule)
	dashMux.HandleFunc("POST /rules/{id}/toggle", h.ToggleRule)
	dashMux.HandleFunc("POST /rules/{id}/route-path", h.RoutePathQuickSet)
	dashMux.HandleFunc("GET /sessions", h.ListSessions)
	dashMux.HandleFunc("POST /sessions/{id}/backup", h.BackupSession)
	dashMux.HandleFunc("DELETE /sessions/{id}", h.TerminateSession)
	dashMux.HandleFunc("GET /ai-configs", h.ListAIConfigs)
	dashMux.HandleFunc("POST /ai-configs", h.CreateAIConfig)
	dashMux.HandleFunc("DELETE /ai-configs/{id}", h.DeleteAIConfig)
	dashMux.HandleFunc("GET /filters", h.ListFilters)
	dashMux.HandleFunc("POST /filters", h.CreateFilter)
	dashMux.HandleFunc("DELETE /filters/{id}", h.DeleteFilter)
	dashMux.HandleFunc("GET /queue", h.DeliveryStats)
	dashMux.HandleFunc("GET /dlq", h.ListDeadLetter)
	dashMux.HandleFunc("POST /dlq/{id}/retry", h.ReplayDeadLetter)
	dashMux.HandleFunc("DELETE /dlq", h.PurgeDeadLetter)
	dashMux.HandleFunc("POST /simulate", h.TestRule)
	dashMux.HandleFunc("POST /rules/{id}/pause", h.PauseRule)
	dashMux.HandleFunc("POST /rules/{id}/resume", h.ResumeRule)
	dashMux.HandleFunc("GET /logs", h.QueryLogs)
	dashMux.HandleFunc("GET /logs/stats", h.LogStats)
	mux.Handle("/api/", h.RequireAuth(dashMux))

	// v1 Routes — internal/operator surface, kept as-is.
	mux.HandleFunc("GET /api/v1/sessions", h.ListSessions)
	mux.HandleFunc("POST /api/v1/sessions/backup", h.BackupSession)
	mux.HandleFunc("POST /api/v1/sessions/restore", h.RestoreSession)
	mux.HandleFunc("DELETE /api/v1/sessions/{id}", h.TerminateSession)
	mux.HandleFunc("GET /api/v1/rules", h.ListRules)
	mux.HandleFunc("POST /api/v1/rules", h.CreateRule)
	mux.HandleFunc("GET /api/v1/rules/{id}", h.GetRule)
	mux.HandleFunc("PUT /api/v1/rules/{id}", h.UpdateRule)
	mux.HandleFunc("DELETE /api/v1/rules/{id}", h.DeleteRule)
	mux.HandleFunc("POST /api/v1/rules/{id}/toggle", h.ToggleRule)
	mux.HandleFunc("POST /api/v1/rules/{id}/route-path", h.RoutePathQuickSet)
	mux.HandleFunc("GET /api/v1/filters", h.ListFilters)
	mux.HandleFunc("POST /api/v1/filters", h.CreateFilter)
	mux.HandleFunc("PUT /api/v1/filters/{id}", h.UpdateFilter)
	mux.HandleFunc("DELETE /api/v1/filters/{id}", h.DeleteFilter)
	mux.HandleFunc("GET /api/v1/ai", h.ListAIConfigs)
	mux.HandleFunc("POST /api/v1/ai", h.CreateAIConfig)
	mux.HandleFunc("PUT /api/v1/ai/{id}", h.UpdateAIConfig)
	mux.HandleFunc("DELETE /api/v1/ai/{id}", h.DeleteAIConfig)
	mux.HandleFunc("POST /api/v1/ai/{id}/test", h.TestAIRewrite)
	mux.HandleFunc("GET /api/v1/stats", h.GetStats)
	mux.HandleFunc("GET /api/v1/logs", h.QueryLogs)
	mux.HandleFunc("GET /api/v1/logs/stats", h.LogStats)
	mux.HandleFunc("POST /api/v1/rules/test", h.TestRule)
	mux.HandleFunc("POST /api/v1/rules/{id}/pause", h.PauseRule)
	mux.HandleFunc("POST /api/v1/rules/{id}/resume", h.ResumeRule)
	mux.HandleFunc("GET /api/v1/delivery/stats", h.DeliveryStats)
	mux.HandleFunc("GET /api/v1/delivery/dead-letter", h.ListDeadLetter)
	mux.HandleFunc("POST /api/v1/delivery/dead-letter/{id}/replay", h.ReplayDeadLetter)
	mux.HandleFunc("POST /api/v1/delivery/dead-letter/purge", h.PurgeDeadLetter)

	var handler http.Handler = mux
	handler = middleware.RequestLogger(handler)
	if apiKey != "" {
		handler = middleware.APIKeyAuth(apiKey, handler)
	}

	log.Printf("AutoTelegramForward API listening on %s (core: %s)", addr, coreAddr)
	if err := http.ListenAndServe(addr, handler); err != nil {
		log.Fatalf("server error: %v", err)
	}
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

var _ = json.Marshal // keep import when handlers change
