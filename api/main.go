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
	loggerAddr := getenv("ATF_LOGGER_GRPC", "localhost:50052")
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

	// v1 Routes
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

	// Web Gateway / Frontend Parity Routes (/api/*)
	mux.HandleFunc("GET /api/stats", h.GetStats)
	mux.HandleFunc("GET /api/rules", h.ListRules)
	mux.HandleFunc("POST /api/rules", h.CreateRule)
	mux.HandleFunc("GET /api/rules/{id}", h.GetRule)
	mux.HandleFunc("PUT /api/rules/{id}", h.UpdateRule)
	mux.HandleFunc("DELETE /api/rules/{id}", h.DeleteRule)
	mux.HandleFunc("POST /api/rules/{id}/toggle", h.ToggleRule)
	mux.HandleFunc("POST /api/rules/{id}/route-path", h.RoutePathQuickSet)
	mux.HandleFunc("GET /api/sessions", h.ListSessions)
	mux.HandleFunc("GET /api/ai-configs", h.ListAIConfigs)
	mux.HandleFunc("POST /api/ai-configs", h.CreateAIConfig)
	mux.HandleFunc("DELETE /api/ai-configs/{id}", h.DeleteAIConfig)
	mux.HandleFunc("GET /api/filters", h.ListFilters)
	mux.HandleFunc("POST /api/filters", h.CreateFilter)
	mux.HandleFunc("DELETE /api/filters/{id}", h.DeleteFilter)
	mux.HandleFunc("GET /api/queue", h.DeliveryStats)
	mux.HandleFunc("GET /api/dlq", h.ListDeadLetter)
	mux.HandleFunc("POST /api/dlq/{id}/retry", h.ReplayDeadLetter)
	mux.HandleFunc("DELETE /api/dlq", h.PurgeDeadLetter)
	mux.HandleFunc("POST /api/simulate", h.TestRule)
	mux.HandleFunc("GET /api/logs", h.QueryLogs)

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
