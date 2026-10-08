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
	mux.HandleFunc("GET /api/v1/sessions", h.ListSessions)
	mux.HandleFunc("POST /api/v1/sessions/backup", h.BackupSession)
	mux.HandleFunc("POST /api/v1/sessions/restore", h.RestoreSession)
	mux.HandleFunc("DELETE /api/v1/sessions/{id}", h.TerminateSession)
	mux.HandleFunc("GET /api/v1/rules", h.ListRules)
	mux.HandleFunc("POST /api/v1/rules", h.CreateRule)
	mux.HandleFunc("PUT /api/v1/rules/{id}", h.UpdateRule)
	mux.HandleFunc("DELETE /api/v1/rules/{id}", h.DeleteRule)
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
