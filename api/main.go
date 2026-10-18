package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"autoforward/internal/grpcclient"
	"autoforward/internal/handlers"
	"autoforward/internal/logpolicy"
	"autoforward/internal/middleware"
	"autoforward/internal/ratelimit"
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

	// The API boundary's rate limiter. It is built from the environment like
	// every other boundary service, it never blocks startup when Redis is
	// unreachable (each route class applies its own outage policy), and it is
	// nil-free: RateLimit is only installed when it actually enforces
	// something, so "no limiter" is always visible as such rather than as an
	// accidental unlimited path.
	rlCfg := ratelimit.ConfigFromEnv(os.Getenv)
	rl, rlErr := ratelimit.NewRuntime(context.Background(), rlCfg, log.Printf)
	if rlErr != nil {
		log.Fatalf("rate limiter configuration error: %v", rlErr)
	}
	defer rl.Close()
	h.RateLimit = rl
	// One policy instance per process. The access trail, the administrative audit
	// trail and the control-plane routes must all observe the same effective
	// configuration, or an operator could "turn collection off" and still have it
	// collected (or read a policy the process is not applying).
	logPolicy := logpolicy.FromEnv()
	logSink := &middleware.ErrorLogSink{Client: clients.Logs, Policy: logPolicy}
	h.Policy = logPolicy
	h.LogAudit = middleware.AuditEmitter(logSink)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", h.Health)
	mux.HandleFunc("GET /health", h.Health)
	mux.HandleFunc("GET /api/version", h.Version)
	// The API's own machine-readable contract (ships in the image).
	mux.HandleFunc("GET /api/openapi.yaml", h.OpenAPIDoc)
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
	// Unauthenticated surface: the brute-force budget belongs here, keyed on the
	// peer address (and, because these routes each carry their own key group, on
	// the endpoint as well).
	mux.Handle("/api/auth/", rl.WrapClass(ratelimit.ClassAuth, nil, h.StripPrefix(authMux)))

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
	// Session chat list for the mobile source/target pickers. Scoped entirely
	// from the JWT identity, so it sits with the other /sessions routes.
	dashMux.HandleFunc("GET /sessions/{sessionId}/chats", h.ListChats)
	dashMux.HandleFunc("POST /sessions/{id}/backup", h.BackupSession)
	dashMux.HandleFunc("DELETE /sessions/{id}", h.TerminateSession)
	// Bot-route credentials. The route itself is a per-task setting below; this is
	// how a dashboard or mobile client attaches the bot a task forwards through.
	dashMux.HandleFunc("GET /bot-tokens", h.ListBotTokens)
	dashMux.HandleFunc("POST /bot-tokens", h.CreateBotToken)
	dashMux.HandleFunc("DELETE /bot-tokens/{id}", h.DeleteBotToken)
	// Per-bot defaults: what every task attached to this bot inherits.
	dashMux.HandleFunc("GET /bot-tokens/{id}/defaults", h.GetBotDefaults)
	dashMux.HandleFunc("PUT /bot-tokens/{id}/defaults", h.SetBotDefaults)
	dashMux.HandleFunc("GET /ai-configs", h.ListAIConfigs)
	dashMux.HandleFunc("POST /ai-configs", h.CreateAIConfig)
	dashMux.HandleFunc("PUT /ai-configs/{id}", h.UpdateAIConfig)
	dashMux.HandleFunc("DELETE /ai-configs/{id}", h.DeleteAIConfig)
	// The tenant's own endpoint + key: what the catalogue offers, and whether
	// the key they typed is accepted before it is stored.
	dashMux.HandleFunc("GET /ai-configs/providers", h.ListAIProviders)
	dashMux.HandleFunc("POST /ai-configs/check-key", h.CheckProviderKey)
	dashMux.HandleFunc("GET /filters", h.ListFilters)
	dashMux.HandleFunc("POST /filters", h.CreateFilter)
	dashMux.HandleFunc("PUT /filters/{id}", h.UpdateFilter)
	dashMux.HandleFunc("DELETE /filters/{id}", h.DeleteFilter)
	// Per-user task lifecycle (API-first phase 2): creation, progress,
	// cancellation and retry. Every route scopes to the verified identity.
	dashMux.HandleFunc("GET /tasks", h.ListTasks)
	dashMux.HandleFunc("POST /tasks", h.CreateTask)
	dashMux.HandleFunc("GET /tasks/{id}", h.GetTask)
	dashMux.HandleFunc("POST /tasks/{id}/cancel", h.CancelTask)
	dashMux.HandleFunc("POST /tasks/{id}/retry", h.RetryTask)

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
	// Control plane for the collection policy. Reads are authenticated; mutations
	// are admin-only and bounded (see logging_policy.go).
	dashMux.HandleFunc("GET /logs/policy", h.LogPolicy)
	dashMux.HandleFunc("GET /logs/health", h.LoggingHealth)
	dashMux.Handle("POST /logs/diagnostics/start", h.RequireAdmin(http.HandlerFunc(h.LogDiagnosticsStart)))
	dashMux.Handle("POST /logs/diagnostics/stop", h.RequireAdmin(http.HandlerFunc(h.LogDiagnosticsStop)))
	// Changing what atf-core collects, through the process that enforces it.
	// Admin-only and audited: this is the one control that can decide what the
	// evidence trail keeps.
	dashMux.Handle("PUT /logs/policy", h.RequireAdmin(http.HandlerFunc(h.LogPolicyUpdate)))
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
	// The limiter's own control surface (same RequireAdmin gate as the rest of
	// the administrator plane).
	adminMux.HandleFunc("GET /ratelimit", h.RateLimitStatus)
	dashMux.Handle("/admin/", http.StripPrefix("/admin", h.RequireAdmin(adminMux)))

	// Authenticated surface: the key comes from the identity the core signed
	// into the token (handlers.IdentityOf), never from a header or a body.
	mux.Handle("/api/", h.RequireAuth(rl.WrapByRoute(h.IdentityOf, dashMux)))

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
	// The API-key gate sits INSIDE AccessLogger on purpose. Wrapped on the
	// outside it makes every refused key invisible twice over: no access-trail
	// entry in the central logger, and no stdout line either, because
	// RequestLogger is inside it too. An unauthenticated attempt is security
	// evidence an operator must be able to see, so it is recorded (as a
	// security-relevant WARN, which the collection policy must not suppress).
	if apiKey != "" {
		handler = middleware.APIKeyAuth(apiKey, handler)
	}
	// The outer, per-address shield sits INSIDE the access logger so a refusal
	// is recorded like any other request, and outside authentication so a flood
	// of bogus tokens is refused before this process dials the core to validate
	// them.
	handler = rl.WrapShield(handler)
	handler = middleware.AccessLogger(logSink, handler)
	// Extract, validate, or generate end-to-end Correlation ID
	handler = middleware.CorrelationID(handler)

	srv := &http.Server{
		Addr:    addr,
		Handler: handler,
		// Bounded so a wedged handler cannot hold a connection (and a pool
		// checkout) forever.
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       60 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	// Graceful shutdown: a SIGTERM from the orchestrator stops accepting, lets
	// in-flight requests finish, and only then releases the Redis pool. Dropping
	// connections on a redeploy is what produces the "one 502 during a deploy"
	// that operators learn to ignore.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	log.Printf("AutoTelegramForward API listening on %s (core: %s)", addr, coreAddr)
	errCh := make(chan error, 1)
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	select {
	case err := <-errCh:
		log.Fatalf("server error: %v", err)
	case <-ctx.Done():
		log.Printf("shutdown signal received — draining (10s)")
		drainCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(drainCtx); err != nil {
			log.Printf("shutdown: %v", err)
		}
		if err := rl.Close(); err != nil {
			log.Printf("ratelimit close: %v", err)
		}
		log.Printf("shutdown complete")
	}
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

var _ = json.Marshal // keep import when handlers change
