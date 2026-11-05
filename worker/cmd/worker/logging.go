package main

import (
	"context"
	"log/slog"
	"os"
	"strings"

	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/observability"
	"atf/worker/internal/ops"
)

// fanoutHandler keeps the JSON container stream and the shared Rust Log Monitor
// fed from the same slog records. Central logging is asynchronous and applies
// its own bounded severity policy; a Logger outage never blocks Worker work.
type fanoutHandler struct {
	stdout  slog.Handler
	central *observability.Client
	module  string
	attrs   []slog.Attr
	groups  []string
}

func (h *fanoutHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.stdout.Enabled(ctx, level) || h.central != nil
}

func (h *fanoutHandler) Handle(ctx context.Context, record slog.Record) error {
	var err error
	if h.stdout.Enabled(ctx, record.Level) {
		// The stdout stream carries the SAME redaction the central client
		// applies to its own records: secret-shaped keys and content fields
		// become [REDACTED], secret-shaped substrings inside messages and
		// error strings are masked. The container stdout is what a log
		// collector actually scrapes — one careless "error", err argument
		// must not leak a token there.
		err = h.stdout.Handle(ctx, redactedRecord(record))
	}
	if h.central == nil {
		return err
	}
	fields := make([]any, 0, (len(h.attrs)+record.NumAttrs())*2)
	prefix := strings.Join(h.groups, ".")
	for _, attr := range h.attrs {
		appendSlogAttr(&fields, prefix, attr)
	}
	record.Attrs(func(attr slog.Attr) bool {
		appendSlogAttr(&fields, prefix, attr)
		return true
	})
	h.central.Emit(slogLevelName(record.Level), "", h.module, record.Message, fields...)
	return err
}

// redactedRecord rebuilds a record with the shared redaction applied to the
// message and every attribute (groups recursed, structure preserved). The
// per-call attributes all flow through Handle, so this is the one choke
// point the stdout sink needs.
func redactedRecord(record slog.Record) slog.Record {
	out := slog.NewRecord(record.Time, record.Level, observability.RedactSlogText(record.Message), record.PC)
	record.Attrs(func(attr slog.Attr) bool {
		out.AddAttrs(redactAttr(attr))
		return true
	})
	return out
}

func redactAttr(attr slog.Attr) slog.Attr {
	value := attr.Value.Resolve()
	if value.Kind() == slog.KindGroup {
		group := slog.Group(attr.Key)
		for _, sub := range value.Group() {
			group.Value = slog.GroupValue(append(group.Value.Group(), redactAttr(sub))...)
		}
		return group
	}
	return slog.Attr{Key: attr.Key, Value: slog.AnyValue(observability.RedactSlogValue(attr.Key, value.Any()))}
}

func (h *fanoutHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	combined := make([]slog.Attr, 0, len(h.attrs)+len(attrs))
	combined = append(combined, h.attrs...)
	combined = append(combined, attrs...)
	return &fanoutHandler{
		stdout: h.stdout.WithAttrs(attrs), central: h.central,
		module: h.module, attrs: combined, groups: append([]string(nil), h.groups...),
	}
}

func (h *fanoutHandler) WithGroup(name string) slog.Handler {
	if name == "" {
		return h
	}
	groups := append([]string(nil), h.groups...)
	groups = append(groups, name)
	return &fanoutHandler{
		stdout: h.stdout.WithGroup(name), central: h.central,
		module: h.module, attrs: append([]slog.Attr(nil), h.attrs...), groups: groups,
	}
}

func appendSlogAttr(dst *[]any, prefix string, attr slog.Attr) {
	value := attr.Value.Resolve()
	if value.Kind() == slog.KindGroup {
		group := attr.Key
		if group != "" && prefix != "" {
			group = prefix + "." + group
		} else if group == "" {
			group = prefix
		}
		for _, nested := range value.Group() {
			appendSlogAttr(dst, group, nested)
		}
		return
	}
	key := attr.Key
	if prefix != "" {
		if key != "" {
			key = prefix + "." + key
		} else {
			key = prefix
		}
	}
	if key != "" {
		*dst = append(*dst, key, value.Any())
	}
}

func slogLevelName(level slog.Level) string {
	switch {
	case level >= slog.LevelError+4:
		return "CRITICAL"
	case level >= slog.LevelError:
		return "ERROR"
	case level >= slog.LevelWarn:
		return "WARN"
	case level >= slog.LevelInfo:
		return "INFO"
	case level >= slog.LevelDebug:
		return "DEBUG"
	default:
		return "TRACE"
	}
}

// slogLogger adapts delivery/scheduler's narrow interface to the shared slog
// pipeline while tagging each event with its owning Worker subsystem.
//
// ops is the operational collector. When wired, WARN/ERROR lines ALSO land
// in the /ops error ring — this is the "fed from the engine's existing
// logger.Warn/Error paths" requirement, and it costs the pipeline nothing
// new: the line was already being emitted, it is now counted too. The
// fields the ring needs (severity, component, rule_id, job_id, code,
// correlation_id) are read out of the same args the log line carries, and
// the message is sanitized inside the ring.
type slogLogger struct {
	log    *slog.Logger
	module string
	ops    *ops.Collector
}

func (l *slogLogger) Info(msg string, args ...any) {
	l.log.Info(msg, append(args, "module", l.module)...)
}
func (l *slogLogger) Warn(msg string, args ...any) {
	l.log.Warn(msg, append(args, "module", l.module)...)
	if l.ops != nil {
		l.ops.RecordLogEntry("WARN", l.module, msg, args)
	}
}
func (l *slogLogger) Error(msg string, args ...any) {
	l.log.Error(msg, append(args, "module", l.module)...)
	if l.ops != nil {
		l.ops.RecordLogEntry("ERROR", l.module, msg, args)
	}
}

func workerLogLevel() slog.Level {
	level := strings.ToLower(strings.TrimSpace(firstNonEmptyEnv("LOG_LEVEL", "ATF_WORKER_LOG_LEVEL")))
	switch level {
	case "trace", "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	case "info", "":
		return slog.LevelInfo
	default:
		return slog.LevelInfo
	}
}

func firstNonEmptyEnv(keys ...string) string {
	for _, key := range keys {
		if value := strings.TrimSpace(os.Getenv(key)); value != "" {
			return value
		}
	}
	return ""
}

// transformDecisionLogger is the bridge between the transform pipeline's
// structured decision records and the two sinks a Worker event has: the
// shared Rust Log Monitor (through the observability client) and the
// container's own JSON stdout.
//
// It implements cleaning.DecisionRecorder, so the domain package never
// learns about gRPC, protobuf or slog — the decision shape is the contract,
// and this adapter is where it becomes a log line.
type transformDecisionLogger struct {
	central *observability.Client
	log     *slog.Logger
}

// RecordDecision fans one decision out to both sinks. It never blocks the
// delivery pipeline: Emit enqueues into the central client's bounded queue,
// and slog's stdout handler is non-blocking by construction. A failure in
// either sink is swallowed on purpose — the decision is diagnostic
// information, never a delivery dependency.
func (l *transformDecisionLogger) RecordDecision(ctx context.Context, event cleaning.DecisionEvent) {
	attrs := []any{
		"rule_id", event.RuleID,
		"correlation_id", event.CorrelationID,
		"owner_user_id", event.OwnerUserID,
		"source_id", event.SourceID,
		"target_id", event.TargetID,
		"decision", event.Decision,
		"reason_code", event.ReasonCode,
		"stages", strings.Join(event.StageNames, ","),
	}
	for key, value := range event.Detail {
		// Detail is the free-form diagnostic context. The central logger
		// redacts secret-shaped keys itself; here the values are lengths and
		// counts by construction, never message content.
		attrs = append(attrs, key, value)
	}

	switch event.Level {
	case "WARN":
		l.log.Warn(event.Message, attrs...)
	case "ERROR":
		l.log.Error(event.Message, attrs...)
	default:
		l.log.Info(event.Message, attrs...)
	}

	if l.central != nil {
		l.central.Emit(event.Level, event.Category, "worker.transform", event.Message, attrs...)
	}
}
