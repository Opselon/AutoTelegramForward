package middleware

import (
	"context"
	"encoding/json"
	"log"
	"time"

	"autoforward/internal/logpolicy"
	pb "autoforward/proto"
)

// AuditEmitter records an administrative action in the durable log store.
//
// It is deliberately routed *through* the collection policy rather than around
// it. An administrative change is security-relevant evidence, so the record of
// "who turned collection down" must survive the change that turned it down — if a
// future edit narrows the exemption so that this emission is refused, the
// accompanying test fails instead of the evidence quietly disappearing.
//
// Returning a function rather than a type keeps the caller (the HTTP handlers)
// free of any knowledge of the policy or the gRPC client.
func AuditEmitter(sink *ErrorLogSink) func(actor int64, action, reasonCode string, detail map[string]any) {
	return func(actor int64, action, reasonCode string, detail map[string]any) {
		if sink == nil || sink.Client == nil {
			// No transport: the action still happened, and the API's own stderr
			// trail (the independent emergency path) is what remains.
			log.Printf("audit: no log sink available for %s by %d", action, actor)
			return
		}
		if !sink.allow(logpolicy.LevelInfo, "admin.change", reasonCode) {
			// Should be unreachable: admin.change is exempt. Keep the branch so
			// the invariant is explicit rather than implied.
			log.Printf("audit: %s by %d was refused by the collection policy", action, actor)
			return
		}
		blob, _ := json.Marshal(detail)
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			_, err := sink.Client.Log(ctx, &pb.LogRequest{Entry: &pb.LogEntry{
				Ts:            time.Now().Unix(),
				Level:         logpolicy.LevelInfo,
				Service:       "api",
				Category:      "audit",
				Message:       "admin.change " + action,
				Detail:        string(blob),
				UserId:        actor,
				CorrelationId: correlationFromDetail(detail),
			}})
			if err != nil {
				log.Printf("audit: failed to persist %s: %v", action, err)
			}
		}()
	}
}

// correlationFromDetail pulls the request's correlation id out of the detail map
// so the audit record can be joined to the request that caused it.
func correlationFromDetail(detail map[string]any) string {
	if value, ok := detail["correlation_id"]; ok {
		if text, ok := value.(string); ok {
			return text
		}
	}
	return ""
}
