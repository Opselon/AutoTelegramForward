package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"

	"autoforward/internal/grpcclient"
	"autoforward/internal/handlers"
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
	mux.HandleFunc("GET /api/version", h.Version)
	mux.HandleFunc("GET /api/v1/health/services", h.ServicesHealth)

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
	dashMux.HandleFunc("PUT /ai-configs/{id}", h.UpdateAIConfig)
	dashMux.HandleFunc("DELETE /ai-configs/{id}", h.DeleteAIConfig)
	dashMux.HandleFunc("GET /filters", h.ListFilters)
	dashMux.HandleFunc("POST /filters", h.CreateFilter)
	dashMux.HandleFunc("PUT /filters/{id}", h.UpdateFilter)
	dashMux.HandleFunc("DELETE /filters/{id}", h.DeleteFilter)
	dashMux.HandleFunc("GET /queue", h.DeliveryStats)
	dashMux.HandleFunc("GET /dlq", h.ListDeadLetter)
	dashMux.HandleFunc("POST /dlq/{id}/retry", h.ReplayDeadLetter)
	dashMux.HandleFunc("DELETE /dlq", h.PurgeDeadLetter)
	dashMux.HandleFunc("POST /simulate", h.TestRule)
	dashMux.HandleFunc("POST /rules/{id}/pause", h.PauseRule)
	dashMux.HandleFunc("GET /pv-responder", h.GetPVResponder)
	dashMux.HandleFunc("POST /pv-responder", h.SetPVResponder)
	dashMux.HandleFunc("PUT /pv-responder", h.SetPVResponder)
	dashMux.HandleFunc("POST /rules/{id}/resume", h.ResumeRule)
	dashMux.HandleFunc("GET /logs", h.QueryLogs)
	dashMux.HandleFunc("GET /logs/stats", h.LogStats)
	dashMux.HandleFunc("POST /logs/purge", h.PurgeLogs)
	dashMux.HandleFunc("GET /health/services", h.ServicesHealth)
	// AI Rewrite prompt templates — authenticated + ownership-scoped (same surface
	// as /ai-configs). NOT on /api/v1/*: that prefix bypasses RequireAuth, so
	// identity() would be empty and the ownership guard would always 404.
	dashMux.HandleFunc("GET /prompts", h.ListPromptTemplates)
	dashMux.HandleFunc("POST /prompts", h.CreatePromptTemplate)
	dashMux.HandleFunc("PUT /prompts/{id}", h.UpdatePromptTemplate)
	dashMux.HandleFunc("DELETE /prompts/{id}", h.DeletePromptTemplate)
	dashMux.HandleFunc("POST /prompts/test", h.TestPrompt)
	// Backup & Restore — whole-installation migration between servers.
	dashMux.HandleFunc("POST /backup/export", h.BackupExport)
	dashMux.HandleFunc("POST /backup/import", h.BackupImport)

	// Client self-management endpoints (personal credentials, settings)
	dashMux.HandleFunc("GET /client/settings", h.ClientGetSettings)
	dashMux.HandleFunc("POST /client/credentials", h.ClientSaveCredentials)

	// Administrator Control Plane endpoints (strictly gated by RequireAdmin)
	adminMux := http.NewServeMux()
	adminMux.HandleFunc("GET /overview", h.AdminOverview)
	adminMux.HandleFunc("GET /users", h.AdminListUsers)
	adminMux.HandleFunc("POST /users/{id}/status", h.AdminSetUserStatus)
	adminMux.HandleFunc("POST /broadcast", h.AdminBroadcast)
	adminMux.HandleFunc("GET /settings", h.AdminGetSettings)
	adminMux.HandleFunc("POST /settings", h.AdminSaveSettings)
	adminMux.HandleFunc("GET /admins", h.AdminListAdmins)
	adminMux.HandleFunc("POST /admins", h.AdminAddAdmin)
	adminMux.HandleFunc("DELETE /admins/{id}", h.AdminRemoveAdmin)
	adminMux.HandleFunc("GET /extensions", h.AdminListExtensions)
	adminMux.HandleFunc("POST /extensions/{id}/toggle", h.AdminToggleExtension)
	dashMux.Handle("/admin/", http.StripPrefix("/admin", h.RequireAdmin(adminMux)))

	mux.Handle("/api/", h.RequireAuth(dashMux))

	var handler http.Handler = mux
	handler = middleware.RequestLogger(handler)
	// Mirror every 4xx/5xx into the central logger so the dashboard Log
	// Explorer shows API failures without ssh/journalctl.
	handler = middleware.ErrorLogger(&middleware.ErrorLogSink{Client: clients.Logs}, handler)
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
