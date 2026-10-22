package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"google.golang.org/grpc"

	"autoforward/internal/grpcclient"
	pb "autoforward/proto"
)

// The DevOps panel polls GET /api/gateway every ten seconds. It was never
// routed, so every poll answered 404 and the panel showed a hardcoded version.
// These tests pin the route's answer and its tenant scoping.

type gatewaySystemClient struct {
	got  *pb.SystemStatsRequest
	resp *pb.SystemStatsResponse
}

func (g *gatewaySystemClient) GetSystemStats(ctx context.Context, in *pb.SystemStatsRequest, _ ...grpc.CallOption) (*pb.SystemStatsResponse, error) {
	g.got = in
	if g.resp != nil {
		return g.resp, nil
	}
	return &pb.SystemStatsResponse{CoreRunning: true, UptimeSeconds: 42, ActiveRulesCount: 3, ActiveSessionsCount: 2, Version: "9.9.9"}, nil
}

func (g *gatewaySystemClient) GetLogPolicy(ctx context.Context, in *pb.LogPolicyRequest, _ ...grpc.CallOption) (*pb.LogPolicyResponse, error) {
	return &pb.LogPolicyResponse{}, nil
}

func (g *gatewaySystemClient) SetLogPolicy(ctx context.Context, in *pb.SetLogPolicyRequest, _ ...grpc.CallOption) (*pb.LogPolicyResponse, error) {
	return &pb.LogPolicyResponse{}, nil
}

func readGateway(t *testing.T, h *Handlers, uid int64) map[string]any {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/gateway", nil)
	if uid != 0 {
		req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid}))
	}
	rec := httptest.NewRecorder()
	h.GatewayInfo(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return body
}

func TestGatewayInfoReportsLiveCoreState(t *testing.T) {
	fake := &gatewaySystemClient{}
	h := &Handlers{c: &grpcclient.Clients{System: fake}}

	body := readGateway(t, h, 501)

	if body["atf_core_online"] != true {
		t.Fatalf("atf_core_online = %v, want true", body["atf_core_online"])
	}
	if body["version"] != "9.9.9" {
		t.Fatalf("version = %v, want the core's version", body["version"])
	}
	if body["total_rules"] != float64(3) || body["total_sessions"] != float64(2) {
		t.Fatalf("counts = %v/%v", body["total_rules"], body["total_sessions"])
	}
	if _, ok := body["grpc_port"].(float64); !ok {
		t.Fatalf("grpc_port missing or not a number: %v", body["grpc_port"])
	}
}

func TestGatewayInfoIsTenantScoped(t *testing.T) {
	fake := &gatewaySystemClient{}
	h := &Handlers{c: &grpcclient.Clients{System: fake}}

	readGateway(t, h, 777)

	if fake.got == nil || fake.got.OwnerUserId != 777 {
		t.Fatalf("core was asked for owner %v, want 777", fake.got.GetOwnerUserId())
	}
}

func TestGatewayInfoWithoutCoreStillAnswers(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{}}
	body := readGateway(t, h, 0)
	if body["atf_core_online"] != false {
		t.Fatalf("atf_core_online = %v, want false when core is unreachable", body["atf_core_online"])
	}
}
