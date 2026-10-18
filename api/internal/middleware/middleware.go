package middleware

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"regexp"
	"strings"
	"time"

	"autoforward/internal/logpolicy"

	pb "autoforward/proto"
)

type statusRecorder struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	n, err := r.ResponseWriter.Write(b)
	r.bytes += n
	return n, err
}

// --------------------------------------------------------------------------- //
// Request metadata: the thread that ties a durable log entry to the actor who
// caused it. The outer middleware creates it, the auth layer fills in the
// verified identity, and the outer middleware persists one entry per request.
// --------------------------------------------------------------------------- //

type requestMetaKey struct{}

// RequestMeta is per-request attribution, filled in as the request flows
// outward-in. Anything left zero means "not known / not applicable".
type RequestMeta struct {
	CorrelationID string
	UserID        int64
	Username      string
	RuleID        string
	Action        string
}

// Meta returns the request's attribution holder, or nil when the access logger
// is not installed (tests, direct handler invocation).
func Meta(ctx context.Context) *RequestMeta {
	if v := ctx.Value(requestMetaKey{}); v != nil {
		if m, ok := v.(*RequestMeta); ok {
			return m
		}
	}
	return nil
}

var rulePathRe = regexp.MustCompile(`/rules/([^/]+)`)

// probePaths are liveness/version probes: polling traffic that would drown the
// archive without recording an operator action.
var probePaths = map[string]bool{
	"/healthz": true, "/health": true, "/api/version": true,
	"/api/v1/health/services": true, "/api/health/services": true,
}

// AccessLogger records EVERY API request into the central logger store, not just
// the failures — so the log explorer is a complete history of what the system
// did rather than only a list of what went wrong. Level tracks the outcome, and
// the entry carries the actor (user), the subject (rule), and a correlation id
// that is also returned to the caller in X-Correlation-Id for support handoffs.
func AccessLogger(sink *ErrorLogSink, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Reuse the id the CorrelationID middleware already established for this
		// request (it is also injected into outgoing gRPC metadata), so the same
		// value ties the HTTP call, the core RPC and the log entry together.
		meta := &RequestMeta{CorrelationID: CorrelationIDFromContext(r.Context())}
		if meta.CorrelationID == "" {
			// AccessLogger can run without the CorrelationID middleware (tests,
			// embedded use); never log an entry with no way to find it again.
			meta.CorrelationID = GenerateCorrelationID()
			w.Header().Set("X-Correlation-ID", meta.CorrelationID)
			w.Header().Set("X-Request-ID", meta.CorrelationID)
		}
		if m := rulePathRe.FindStringSubmatch(r.URL.Path); len(m) == 2 {
			meta.RuleID = m[1]
		}
		meta.Action = r.Method + " " + r.URL.Path

		if sink == nil || sink.Client == nil {
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), requestMetaKey{}, meta)))
			return
		}

		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r.WithContext(context.WithValue(r.Context(), requestMetaKey{}, meta)))

		if probePaths[r.URL.Path] {
			return
		}
		level := "INFO"
		switch {
		case rec.status >= 500:
			level = "ERROR"
		case rec.status >= 400:
			level = "WARN"
		}
		// The gate sits here, before the transport: a refused event must not cost
		// a goroutine, a deadline, or a retry, and must never look like a loss.
		eventType, reasonCode := classifyOutcome(r, rec.status)
		if !sink.allow(level, eventType, reasonCode) {
			return
		}
		detail, _ := json.Marshal(map[string]any{
			"method":         r.Method,
			"path":           r.URL.Path,
			"query":          r.URL.RawQuery,
			"status":         rec.status,
			"ms":             time.Since(start).Milliseconds(),
			"bytes":          rec.bytes,
			"ua":             r.UserAgent(),
			"ip":             remoteIP(r),
			"user":           meta.Username,
			"user_id":        meta.UserID,
			"correlation_id": meta.CorrelationID,
		})
		go func() {
			// Own the context inside the goroutine: cancelling the handler's
			// context (defer cancel) would abort the gRPC call before it fires.
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			_, lerr := sink.Client.Log(ctx, &pb.LogRequest{Entry: &pb.LogEntry{
				Ts:            time.Now().Unix(),
				Level:         level,
				Service:       "api",
				Category:      "http",
				Message:       http.StatusText(rec.status) + " " + meta.Action,
				Detail:        string(detail),
				RuleId:        meta.RuleID,
				UserId:        meta.UserID,
				CorrelationId: meta.CorrelationID,
			}})
			if lerr != nil {
				log.Printf("access-logger: failed to persist %d %s: %v", rec.status, r.URL.Path, lerr)
			}
		}()
	})
}

// ErrorLogSink forwards API outcomes into the central logger store. It is the
// transport used by AccessLogger.
//
// Policy is the producer-side collection policy. It is applied BEFORE anything
// is sent, because the Logger service persists whatever it receives: without a
// producer filter, the documented default ("INFO is not collected") is not true
// of this service at all — every successful request would write an INFO row.
// A nil Policy means "no filtering", which keeps this middleware usable on its
// own and in tests that do not care about the policy; main.go always sets it.
type ErrorLogSink struct {
	Client pb.LogControlServiceClient
	Policy *logpolicy.Policy
}

// allow applies the collection policy to one already-classified event.
//
// A refusal is a policy decision, not a delivery failure: no RPC is attempted, no
// timeout is spent, and no transport/queue counter moves. The refusal is counted
// by the policy itself (suppressed_level / suppressed_rate), which is what lets an
// operator tell "we chose not to collect this" apart from "we lost it".
func (s *ErrorLogSink) allow(level, eventType, reasonCode string) bool {
	if s == nil || s.Policy == nil {
		return true
	}
	return s.Policy.Allow(level, eventType, reasonCode)
}

// classifyOutcome derives an event's *structured* identity from the request and
// its outcome.
//
// This is what makes the security exemption precise, and it is deliberately not
// a match against the rendered log text: a message reworded later would silently
// change what gets audited. Route namespace and method decide what kind of event
// this is; the status code decides whether it went wrong. An ordinary successful
// read is not evidence of anything; a failed authentication, a denied
// authorization and an administrative change are.
func classifyOutcome(r *http.Request, status int) (eventType, reasonCode string) {
	switch status {
	case http.StatusUnauthorized:
		reasonCode = "UNAUTHORIZED"
	case http.StatusForbidden:
		reasonCode = "FORBIDDEN"
	}
	mutation := r.Method != http.MethodGet && r.Method != http.MethodHead && r.Method != http.MethodOptions

	switch {
	case strings.HasPrefix(r.URL.Path, "/api/admin"):
		// An administrative change is audit evidence whether or not it succeeded;
		// an administrative *read* is ordinary traffic.
		if mutation || reasonCode != "" {
			eventType = "admin.change"
		} else {
			eventType = "http.request"
		}
	case strings.HasPrefix(r.URL.Path, "/api/auth"):
		if reasonCode != "" {
			eventType = "auth.failure"
		} else {
			eventType = "http.request"
		}
	case strings.HasPrefix(r.URL.Path, "/api/tenants"):
		if reasonCode != "" {
			eventType = "tenant.access"
		} else {
			eventType = "http.request"
		}
	default:
		eventType = "http.request"
	}
	return eventType, reasonCode
}

// ErrorLogger is the failure-only logger, kept for callers that want the narrow
// behaviour. AccessLogger supersedes it and records successes too.
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
		eventType, reasonCode := classifyOutcome(r, rec.status)
		if !sink.allow(level, eventType, reasonCode) {
			return
		}
		meta := Meta(r.Context())
		ruleID, corrID := "", ""
		userID := int64(0)
		if meta != nil {
			ruleID, corrID, userID = meta.RuleID, meta.CorrelationID, meta.UserID
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
				Ts:            time.Now().Unix(),
				Level:         level,
				Service:       "api",
				Category:      "http",
				Message:       http.StatusText(rec.status) + " " + r.Method + " " + r.URL.Path,
				Detail:        string(detail),
				RuleId:        ruleID,
				UserId:        userID,
				CorrelationId: corrID,
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
			r.URL.Path == "/api/openapi.yaml" ||
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
