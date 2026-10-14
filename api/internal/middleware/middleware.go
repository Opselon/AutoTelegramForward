package middleware

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"strings"
	"time"

	pb "autoforward/proto"
)

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// ErrorLogSink forwards 4xx/5xx API outcomes into the central logger store so
// every dashboard-visible failure has a durable, queryable log entry.
type ErrorLogSink struct {
	Client pb.LogControlServiceClient
}

// ErrorLogger wraps the handler chain: any response with status >= 400 is
// mirrored into the logger service (best-effort, never blocks the response).
func ErrorLogger(sink *ErrorLogSink, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		if rec.status < 400 || sink == nil || sink.Client == nil {
			return
		}
		level := "ERROR"
		if rec.status < 500 {
			level = "WARN" // 4xx: caller-side problems
		}
		detail, _ := json.Marshal(map[string]any{
			"method": r.Method,
			"path":   r.URL.Path,
			"status": rec.status,
			"ms":     time.Since(start).Milliseconds(),
			"ua":     r.UserAgent(),
			"ip":     remoteIP(r),
		})
		go func() {
			// Own the context inside the goroutine: cancelling the handler's
			// context (defer cancel) would abort the gRPC call before it fires.
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			_, lerr := sink.Client.Log(ctx, &pb.LogRequest{Entry: &pb.LogEntry{
				Ts:       time.Now().Unix(),
				Level:    level,
				Service:  "api",
				Category: "http",
				Message:  http.StatusText(rec.status) + " " + r.Method + " " + r.URL.Path,
				Detail:   string(detail),
			}})
			if lerr != nil {
				log.Printf("error-logger: failed to persist %d %s: %v", rec.status, r.URL.Path, lerr)
			}
		}()
	})
}

func remoteIP(r *http.Request) string {
	if fwd := r.Header.Get("X-Forwarded-For"); fwd != "" {
		return fwd
	}
	return r.RemoteAddr
}

func RequestLogger(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.Path, rec.status, time.Since(start))
	})
}

func APIKeyAuth(key string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" || r.URL.Path == "/health" ||
			r.URL.Path == "/api/version" || r.URL.Path == "/api/v1/health/services" ||
			r.URL.Path == "/api/health/services" ||
			strings.HasPrefix(r.URL.Path, "/api/auth/") {
			next.ServeHTTP(w, r)
			return
		}
		if strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") {
			next.ServeHTTP(w, r)
			return
		}
		provided := r.Header.Get("X-API-Key")
		if provided == "" {
			auth := r.Header.Get("Authorization")
			if len(auth) > 7 && auth[:7] == "Bearer " {
				provided = auth[7:]
			}
		}
		if provided != key {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
			return
		}
		next.ServeHTTP(w, r)
	})
}

// CORS is a permissive dev-mode CORS wrapper (tighten for production).
func CORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
