// Package observability sends Worker events to the shared Rust Log Monitor.
// Delivery never waits for the Logger: writes enter a bounded queue and are
// retried asynchronously. WARN/ERROR/CRITICAL are retained by default.
package observability

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	pb "autoforward/proto"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

const (
	defaultQueueSize = 4096
	rpcTimeout       = 3 * time.Second
	closeGrace       = 2 * time.Second
)

var (
	secretField = regexp.MustCompile("(?i)(token|password|secret|api[_-]?hash|api[_-]?key|authorization|session[_-]?string|cookie)")
	secretValue = regexp.MustCompile("(?i)(token|password|secret|api[_-]?hash|api[_-]?key|authorization)\\s*[:=]\\s*[^,\\s;]+")
)

type Client struct {
	conn    *grpc.ClientConn
	stub    pb.LogControlServiceClient
	token   string
	levels  map[string]struct{}
	queue   chan *pb.LogEntry
	stop    chan struct{}
	done    chan struct{}
	ctx     context.Context
	cancel  context.CancelFunc
	closed  atomic.Bool
	accepted atomic.Int64
	sent     atomic.Int64
	failed   atomic.Int64
	dropped  atomic.Int64
	disabled atomic.Int64
	closeOnce sync.Once
}

// New connects to the shared Logger and starts the non-blocking delivery pump.
// A missing Logger address disables central delivery without affecting Worker.
func New(address, token, configuredLevels, legacyLevel string) (*Client, error) {
	address = strings.TrimSpace(address)
	if address == "" {
		return nil, nil
	}
	if _, _, err := net.SplitHostPort(address); err != nil {
		return nil, fmt.Errorf("invalid ATF_LOGGER_ADDR: %w", err)
	}
	token = strings.TrimSpace(token)
	token = strings.TrimPrefix(token, "Bearer ")
	conn, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, fmt.Errorf("create Logger gRPC client: %w", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	c := &Client{
		conn: conn, stub: pb.NewLogControlServiceClient(conn), token: token,
		levels: parseLevels(configuredLevels, legacyLevel),
		queue: make(chan *pb.LogEntry, defaultQueueSize),
		stop: make(chan struct{}), done: make(chan struct{}),
		ctx: ctx, cancel: cancel,
	}
	go c.run()
	return c, nil
}

// NewFromEnv uses the same ingest credential and severity configuration used
// by the Core. LOG_LEVEL remains supported for simple operator overrides.
func NewFromEnv() (*Client, error) {
	return New(os.Getenv("ATF_LOGGER_ADDR"), os.Getenv("ATF_LOGGER_INGEST_TOKEN"),
		os.Getenv("ATF_LOG_LEVELS"), os.Getenv("LOG_LEVEL"))
}

func parseLevels(configured, legacy string) map[string]struct{} {
	levels := make(map[string]struct{})
	add := func(s string) {
		name := strings.ToUpper(strings.TrimSpace(s))
		if name == "WARNING" { name = "WARN" }
		switch name {
		case "TRACE", "DEBUG", "INFO", "SUCCESS", "WARN", "ERROR", "CRITICAL":
			levels[name] = struct{}{}
		}
	}
	if strings.TrimSpace(configured) != "" {
		for _, item := range strings.FieldsFunc(configured, func(r rune) bool { return r == ',' || r == ';' }) { add(item) }
	} else if strings.TrimSpace(legacy) != "" {
		minimum := strings.ToUpper(strings.TrimSpace(legacy))
		if minimum == "WARNING" { minimum = "WARN" }
		rank := map[string]int{"TRACE":0, "DEBUG":1, "INFO":2, "SUCCESS":3, "WARN":4, "ERROR":5, "CRITICAL":6}
		start, ok := rank[minimum]
		if !ok { start = rank["WARN"] }
		for name, value := range rank { if value >= start { levels[name] = struct{}{} } }
	} else {
		levels["WARN"] = struct{}{}
		levels["ERROR"] = struct{}{}
		levels["CRITICAL"] = struct{}{}
	}
	if len(levels) == 0 {
		levels["WARN"] = struct{}{}
		levels["ERROR"] = struct{}{}
		levels["CRITICAL"] = struct{}{}
	}
	return levels
}

// Emit enqueues a structured event. It is safe for concurrent callers and
// intentionally has no error return: Logger availability must not affect
// forwarding. Queue pressure drops the oldest record and counts that loss.
func (c *Client) Emit(level, category, module, message string, attrs ...any) {
	if c == nil { return }
	if c.closed.Load() { c.disabled.Add(1); return }
	level = normalizeLevel(level)
	fields := make(map[string]any, len(attrs)/2)
	var ruleID, correlationID string
	var userID int64
	var eventType, reasonCode string
	for i := 0; i+1 < len(attrs); i += 2 {
		key, ok := attrs[i].(string)
		if !ok || key == "" { continue }
		value := attrs[i+1]
		switch strings.ToLower(key) {
		case "rule_id": ruleID = fmt.Sprint(value)
		case "correlation_id": correlationID = fmt.Sprint(value)
		case "owner_user_id", "user_id": userID = toInt64(value)
		case "event_type": eventType = fmt.Sprint(value)
		case "reason_code", "event_code": reasonCode = fmt.Sprint(value)
		}
		if secretField.MatchString(key) || isContentField(key) {
			fields[key] = "[REDACTED]"
		} else {
			fields[key] = sanitizeValue(value)
		}
	}
	if !c.allowed(level, eventType, reasonCode) { return }
	if category == "" { category = categoryFor(message, module) }
	detail, err := json.Marshal(map[string]any{
		"module": module, "event_type": eventType, "reason_code": reasonCode, "fields": fields,
	})
	if err != nil { detail = []byte("{\"detail\":\"serialization_failed\"}") }
	entry := &pb.LogEntry{
		Ts: time.Now().Unix(), Level: level, Service: "worker", Category: category,
		Message: redactText(message), Detail: string(detail), RuleId: ruleID,
		UserId: userID, CorrelationId: correlationID, Module: module,
	}
	select {
	case c.queue <- entry:
		c.accepted.Add(1)
		return
	default:
	}
	select {
	case <-c.queue: c.dropped.Add(1)
	default:
	}
	select {
	case c.queue <- entry: c.accepted.Add(1)
	default: c.dropped.Add(1)
	}
}

func (c *Client) allowed(level, eventType, reasonCode string) bool {
	event := strings.ToLower(eventType)
	if strings.HasPrefix(event, "security.") || strings.HasPrefix(event, "admin.") ||
		reasonCode == "UNAUTHORIZED" || reasonCode == "CROSS_TENANT" ||
		reasonCode == "RULE_OWNER_MISMATCH" { return true }
	_, ok := c.levels[level]
	return ok
}

func normalizeLevel(level string) string {
	switch strings.ToUpper(strings.TrimSpace(level)) {
	case "WARNING", "WARN": return "WARN"
	case "ERROR", "ERR": return "ERROR"
	case "CRITICAL", "FATAL": return "CRITICAL"
	case "DEBUG": return "DEBUG"
	case "TRACE": return "TRACE"
	case "SUCCESS": return "SUCCESS"
	default: return "INFO"
	}
}

func categoryFor(message, module string) string {
	v := strings.ToLower(message + " " + module)
	switch {
	case strings.Contains(v, "filter"), strings.Contains(v, "policy"),
		strings.Contains(v, "cleaner"), strings.Contains(v, "replace"): return "filter"
	case strings.Contains(v, "scheduler"), strings.Contains(v, "auto-delete"): return "system"
	default: return "forward"
	}
}

func isContentField(key string) bool {
	switch strings.ToLower(key) {
	case "text", "caption", "message_text", "message_body", "raw_update", "payload": return true
	default: return false
	}
}

func sanitizeValue(value any) any {
	switch v := value.(type) {
	case error: return redactText(v.Error())
	case string: return redactText(v)
	case fmt.Stringer: return redactText(v.String())
	default: return value
	}
}

func redactText(value string) string { return secretValue.ReplaceAllString(value, "$1=[REDACTED]") }

func toInt64(value any) int64 {
	switch n := value.(type) {
	case int: return int64(n)
	case int32: return int64(n)
	case int64: return n
	case uint64: return int64(n)
	case float64: return int64(n)
	case string:
		parsed, _ := strconv.ParseInt(n, 10, 64)
		return parsed
	default: return 0
	}
}

func (c *Client) run() {
	defer close(c.done)
	stopping := false
	for {
		var entry *pb.LogEntry
		if stopping {
			select {
			case entry = <-c.queue:
			default: return
			}
		} else {
			select {
			case entry = <-c.queue:
			case <-c.stop: stopping = true; continue
			}
		}
		if !c.deliver(entry) { c.dropped.Add(1); return }
	}
}

func (c *Client) deliver(entry *pb.LogEntry) bool {
	backoff := 250 * time.Millisecond
	consecutive := 0
	for {
		ctx, cancel := context.WithTimeout(c.ctx, rpcTimeout)
		if c.token != "" { ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+c.token) }
		response, err := c.stub.Log(ctx, &pb.LogRequest{Entry: entry})
		cancel()
		if err == nil && response != nil && response.GetSuccess() { c.sent.Add(1); return true }
		consecutive++
		c.failed.Add(1)
		if consecutive == 1 || consecutive == 5 || consecutive == 20 || consecutive%100 == 0 {
			reason := "Logger returned unsuccessful acknowledgement"
			if err != nil { reason = redactText(err.Error()) }
			fmt.Fprintf(os.Stderr, "worker central logger delivery failed: attempts=%d reason=%s pending=%d\n",
				consecutive, reason, len(c.queue)+1)
		}
		timer := time.NewTimer(backoff)
		select {
		case <-timer.C:
		case <-c.ctx.Done():
			timer.Stop()
			return false
		}
		if backoff < 5*time.Second {
			backoff *= 2
			if backoff > 5*time.Second { backoff = 5*time.Second }
		}
	}
}

// Snapshot returns process-local accounting for health and shutdown logs.
func (c *Client) Snapshot() map[string]int64 {
	if c == nil { return map[string]int64{} }
	return map[string]int64{
		"accepted": c.accepted.Load(), "sent": c.sent.Load(), "failed": c.failed.Load(),
		"dropped": c.dropped.Load(), "disabled": c.disabled.Load(), "pending": int64(len(c.queue)),
	}
}

// Close drains for a bounded window. A Logger outage never holds process
// shutdown indefinitely; any records left after the deadline are counted.
func (c *Client) Close() {
	if c == nil { return }
	c.closeOnce.Do(func() {
		c.closed.Store(true)
		close(c.stop)
		timer := time.NewTimer(closeGrace)
		select {
		case <-c.done:
			timer.Stop()
		case <-timer.C:
			c.cancel()
			<-c.done
		}
		c.cancel()
		if c.conn != nil { _ = c.conn.Close() }
	})
}
