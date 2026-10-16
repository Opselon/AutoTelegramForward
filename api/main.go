package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"

	"autoforward/internal/grpcclient"
	"autoforward/internal/handlers"
	"autoforward/internal/logpolicy"
	"autoforward/internal/middleware"
)

func main() {
	addr := getenv("ATF_API_ADDR", ":8080")
	coreAddr := getenv("ATF_CORE_GRPC", "localhost:50051")
	loggerAddr := getenv("ATF_LOGGER_ADDR", getenv("ATF_LOGGER_GRPC", "127.0.0.1:50052"))
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
	mux.HandleFunc("GET /api/health/services", h.ServicesHealth)
	// Setup/readiness surface: what this deployment is configured with (booleans
	// only, never secret values) and whether every service in the chain answers.
	mux.HandleFunc("GET /api/system/setup", h.SetupReport)
	mux.HandleFunc("GET /api/v1/system/setup", h.SetupReport)
	mux.HandleFunc("GET /api/system/env", h.EnvContractHandler)
	mux.HandleFunc("GET /api/v1/system/env", h.EnvContractHandler)

	// Public authentication endpoints (no Bearer token required).
	authMux := http.NewServeMux()
	authMux.HandleFunc("POST /register", h.AuthRegister)
	authMux.HandleFunc("POST /login", h.AuthLogin)
	authMux.HandleFunc("POST /exchange-bot-token", h.AuthBotExchange)
	authMux.HandleFunc("POST /tma", h.AuthTMA)
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
	// PATCH is the partial-update form: the body carries only what changes and
	// `update_mask` (body array or ?update_mask=a,b) names it, which is the only
	// way to set a value to zero/false/empty.
	dashMux.HandleFunc("PATCH /rules/{id}", h.UpdateRule)
	dashMux.HandleFunc("DELETE /rules/{id}", h.DeleteRule)
	dashMux.HandleFunc("POST /rules/{id}/toggle", h.ToggleRule)
	dashMux.HandleFunc("POST /rules/{id}/duplicate", h.DuplicateRule)
	dashMux.HandleFunc("POST /rules/{id}/stop", h.StopRule)
	dashMux.HandleFunc("POST /rules/{id}/restart", h.RestartRule)
	dashMux.HandleFunc("POST /rules/{id}/route-path", h.RoutePathQuickSet)
	// Per-task settings: the full control surface the Telegram bot edits, plus
	// the schema that describes it. /rules/settings/schema needs no rule, so an
	// editor can be built before the user owns anything.
	dashMux.HandleFunc("GET /rules/settings/schema", h.GetRuleSettingsSchema)
	dashMux.HandleFunc("GET /rules/{id}/settings", h.GetRuleSettings)
	dashMux.HandleFunc("PUT /rules/{id}/settings", h.UpdateRuleSettings)
	dashMux.HandleFunc("PATCH /rules/{id}/settings", h.UpdateRuleSettings)
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
	// v18: a downloadable copy of the filtered history (json|csv), so evidence
	// can leave the box without an operator screen-scraping the console.
	dashMux.HandleFunc("GET /logs/export", h.ExportLogs)
	// v18: one task's complete history, ownership-checked before the read.
	dashMux.HandleFunc("GET /rules/{id}/logs", h.RuleLogs)
	// Centralized audit event queries — tenant-scoped server-side (Phase Three).
	dashMux.HandleFunc("GET /audit/events", h.QueryAuditEvents)
	dashMux.HandleFunc("GET /health/services", h.ServicesHealth)
	// AI Rewrite prompt templates — authenticated + ownership-scoped (same surface
	// as /ai-configs). NOT on /api/v1/*: that prefix bypasses RequireAuth, so
	// identity() would be empty and the ownership guard would always 404.
	dashMux.HandleFunc("GET /prompts", h.ListPromptTemplates)
	dashMux.HandleFunc("POST /prompts", h.CreatePromptTemplate)
	dashMux.HandleFunc("PUT /prompts/{id}", h.UpdatePromptTemplate)
	dashMux.HandleFunc("DELETE /prompts/{id}", h.DeletePromptTemplate)
	dashMux.HandleFunc("POST /prompts/test", h.TestPrompt)
	// Preflight diagnostics — the problem-catching algorithm. Authenticated so
	// findings are scoped to the caller's own rules/sessions/AI configs.
	dashMux.HandleFunc("GET /diagnostics", h.Diagnostics)
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
	// v18: AccessLogger mirrors EVERY request — success and failure — into the
	// central logger, carrying the verified actor, the subject rule and the
	// correlation id. Previously only 4xx/5xx were persisted, so the dashboard
	// Log Explorer could show what went wrong but never the full history of
	// what was actually done.
	// The collection policy is enforced at the producer boundary: the Logger
	// stores whatever it is handed, so "INFO is not collected by default" is only
	// true if this side refuses it. Same env contract as the Python producer.
	handler = middleware.AccessLogger(&middleware.ErrorLogSink{
		Client: clients.Logs,
		Policy: logpolicy.FromEnv(),
	}, handler)
	if apiKey != "" {
		handler = middleware.APIKeyAuth(apiKey, handler)
	}
	// Extract, validate, or generate end-to-end Correlation ID
	handler = middleware.CorrelationID(handler)

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
