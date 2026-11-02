package observability

import (
	"context"
	"net"
	"strings"
	"testing"
	"time"

	pb "autoforward/proto"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

type captureServer struct {
	pb.UnimplementedLogControlServiceServer
	entries chan *pb.LogEntry
	auth chan string
}

func (s *captureServer) Log(ctx context.Context, req *pb.LogRequest) (*pb.StatusResponse, error) {
	if md, ok := metadata.FromIncomingContext(ctx); ok {
		values := md.Get("authorization")
		if len(values) > 0 { s.auth <- values[0] }
	}
	s.entries <- req.GetEntry()
	return &pb.StatusResponse{Success: true}, nil
}

func TestClientSendsCorrelatedRuleLogToCentralLogger(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil { t.Fatal(err) }
	server := grpc.NewServer()
	capture := &captureServer{entries: make(chan *pb.LogEntry, 1), auth: make(chan string, 1)}
	pb.RegisterLogControlServiceServer(server, capture)
	go func() { _ = server.Serve(listener) }()
	defer server.Stop()

	client, err := New(listener.Addr().String(), "ingest-secret", "WARN,ERROR,CRITICAL", "")
	if err != nil { t.Fatal(err) }
	defer client.Close()
	client.Emit("ERROR", "forward", "worker.delivery", "Telegram dispatch failed",
		"rule_id", "rule-123", "correlation_id", "corr-123",
		"owner_user_id", int64(42), "error", "temporary Telegram error")

	select {
	case entry := <-capture.entries:
		if entry.GetService() != "worker" || entry.GetLevel() != "ERROR" {
			t.Fatalf("unexpected log identity: service=%q level=%q", entry.GetService(), entry.GetLevel())
		}
		if entry.GetRuleId() != "rule-123" || entry.GetCorrelationId() != "corr-123" || entry.GetUserId() != 42 {
			t.Fatalf("missing attribution: %+v", entry)
		}
		if !strings.Contains(entry.GetDetail(), "temporary Telegram error") {
			t.Fatalf("error detail missing: %s", entry.GetDetail())
		}
	case <-time.After(2*time.Second):
		t.Fatal("central logger did not receive event")
	}
	select {
	case auth := <-capture.auth:
		if auth != "Bearer ingest-secret" { t.Fatalf("unexpected ingest authorization: %q", auth) }
	case <-time.After(time.Second):
		t.Fatal("central logger did not receive ingest credential")
	}
}

func TestDefaultPolicyCollectsWarningsAndErrorsOnly(t *testing.T) {
	levels := parseLevels("", "")
	for _, level := range []string{"WARN", "ERROR", "CRITICAL"} {
		if _, ok := levels[level]; !ok { t.Fatalf("default policy missing %s", level) }
	}
	for _, level := range []string{"TRACE", "DEBUG", "INFO", "SUCCESS"} {
		if _, ok := levels[level]; ok { t.Fatalf("default policy unexpectedly collects %s", level) }
	}
}

func TestLegacyLogLevelExpandsToSeverityThreshold(t *testing.T) {
	levels := parseLevels("", "debug")
	for _, level := range []string{"DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"} {
		if _, ok := levels[level]; !ok { t.Fatalf("LOG_LEVEL=debug should include %s", level) }
	}
}

func TestSecretFieldsAreRedacted(t *testing.T) {
	got := redactText("request failed token=abc123 password:unsafe")
	if strings.Contains(got, "abc123") || strings.Contains(got, "unsafe") {
		t.Fatalf("secret survived redaction: %s", got)
	}
}
