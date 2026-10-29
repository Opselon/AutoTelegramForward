package handlers

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	grpcclient "autoforward/internal/grpcclient"
	pb "autoforward/proto"

	"google.golang.org/grpc"
)

// ---------------------------------------------------------------------------
// Log tenant isolation: the log archive is one shared store and the logger's
// filter treats user_id == 0 as "every tenant". A normal tenant must be
// PINNED to their own user id on every read path (query, stats, export);
// only admins may widen the read. The old behavior served the whole archive
// by default and honored ?user_id=<victim> from any account.
// ---------------------------------------------------------------------------

type capturingLogsClient struct {
	pb.LogControlServiceClient
	gotQuery []*pb.QueryLogsRequest
	gotStats []*pb.LogStatsRequest
}

func (f *capturingLogsClient) QueryLogs(ctx context.Context, in *pb.QueryLogsRequest, opts ...grpc.CallOption) (*pb.QueryLogsResponse, error) {
	f.gotQuery = append(f.gotQuery, in)
	return &pb.QueryLogsResponse{Logs: []*pb.LogEntry{}, Total: 0}, nil
}

func (f *capturingLogsClient) LogStats(ctx context.Context, in *pb.LogStatsRequest, opts ...grpc.CallOption) (*pb.LogStatsResponse, error) {
	f.gotStats = append(f.gotStats, in)
	return &pb.LogStatsResponse{}, nil
}

func logReq(method, target string, uid int64, admin bool) *http.Request {
	req := httptest.NewRequest(method, target, nil)
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid, IsAdmin: admin}))
}

func TestLogScope_TenantPinnedToOwnLogs(t *testing.T) {
	logs := &capturingLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{Logs: logs}}

	h.QueryLogs(httptest.NewRecorder(), logReq("GET", "/api/logs", 111, false))
	if n := len(logs.gotQuery); n != 1 {
		t.Fatalf("expected 1 query, got %d", n)
	}
	if got := logs.gotQuery[0].UserId; got != 111 {
		t.Fatalf("tenant query must be pinned to own user id: got %d, want 111", got)
	}
}

func TestLogScope_UserIDParamCannotCrossTenants(t *testing.T) {
	logs := &capturingLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{Logs: logs}}

	// A normal tenant naming another user must be ignored, not honored.
	h.QueryLogs(httptest.NewRecorder(), logReq("GET", "/api/logs?user_id=222", 111, false))
	if got := logs.gotQuery[0].UserId; got != 111 {
		t.Fatalf("?user_id=222 must not widen a tenant's read: got %d, want 111", got)
	}
}

func TestLogScope_AdminMayWidenOrName(t *testing.T) {
	logs := &capturingLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{Logs: logs}}

	// Admin default: the installation-wide view (user id 0 = every tenant).
	h.QueryLogs(httptest.NewRecorder(), logReq("GET", "/api/logs", 1, true))
	if got := logs.gotQuery[0].UserId; got != 0 {
		t.Fatalf("admin default must stay unscoped: got %d, want 0", got)
	}
	// Admin naming a user is honored.
	h.QueryLogs(httptest.NewRecorder(), logReq("GET", "/api/logs?user_id=222", 1, true))
	if got := logs.gotQuery[1].UserId; got != 222 {
		t.Fatalf("admin ?user_id= must be honored: got %d, want 222", got)
	}
}

func TestLogScope_StatsPinnedForTenants(t *testing.T) {
	logs := &capturingLogsClient{}
	h := &Handlers{c: &grpcclient.Clients{Logs: logs}}

	// fresh=1 bypasses the shared TTL cache so every call reaches the client
	// and the scope each request carries is observable.
	h.LogStats(httptest.NewRecorder(), logReq("GET", "/api/logs/stats?fresh=1", 333, false))
	if n := len(logs.gotStats); n != 1 {
		t.Fatalf("expected 1 stats call, got %d", n)
	}
	if got := logs.gotStats[0].UserId; got != 333 {
		t.Fatalf("stats must be pinned to the caller's tenant: got %d, want 333", got)
	}

	h.LogStats(httptest.NewRecorder(), logReq("GET", "/api/logs/stats?fresh=1&mine=1", 333, false))
	if got := logs.gotStats[1].UserId; got != 333 {
		t.Fatalf("mine=1 for a tenant stays their own scope: got %d, want 333", got)
	}
}
