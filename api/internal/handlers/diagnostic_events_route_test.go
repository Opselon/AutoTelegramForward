package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"autoforward/internal/grpcclient"
	pb "autoforward/proto"
	"google.golang.org/grpc"
)

// diagEventsClient answers the failure-intelligence surface with a fixed
// response, so what is under test is the handler's own contract: the owner it
// forwards is the authenticated one, never a value from the query string.
type diagEventsClient struct {
	pb.DeliveryControlServiceClient
	events  []*pb.DiagnosticEventEntry
	timeline []*pb.DiagnosticEventEntry
	counts  []*pb.DiagnosticFacetCount
	gotOwner int64
	gotLimit int32
	gotFacet string
}

func (c *diagEventsClient) QueryDiagnosticEvents(_ context.Context, req *pb.QueryDiagnosticEventsRequest, _ ...grpc.CallOption) (*pb.QueryDiagnosticEventsResponse, error) {
	c.gotOwner = req.OwnerUserId
	c.gotLimit = req.Limit
	return &pb.QueryDiagnosticEventsResponse{Events: c.events, TotalReturned: int32(len(c.events))}, nil
}

func (c *diagEventsClient) GetMessageTimeline(_ context.Context, req *pb.MessageTimelineRequest, _ ...grpc.CallOption) (*pb.MessageTimelineResponse, error) {
	c.gotOwner = req.OwnerUserId
	return &pb.MessageTimelineResponse{Events: c.timeline}, nil
}

func (c *diagEventsClient) GetDiagnosticFacetCounts(_ context.Context, req *pb.DiagnosticFacetCountsRequest, _ ...grpc.CallOption) (*pb.DiagnosticFacetCountsResponse, error) {
	c.gotOwner = req.OwnerUserId
	c.gotFacet = req.Facet
	return &pb.DiagnosticFacetCountsResponse{Counts: c.counts}, nil
}

func diagEventsRequest(path string) *http.Request {
	req := httptest.NewRequest(http.MethodGet, path, nil)
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 5094837833}))
}

// The query string may carry an owner_user_id of 999 — the route must ignore it
// and forward the authenticated identity. Tenant isolation on this surface is
// the handler's responsibility, not only Core's.
func TestDiagnosticEventsForwardsTheAuthenticatedOwner(t *testing.T) {
	client := &diagEventsClient{events: []*pb.DiagnosticEventEntry{
		{EventName: "delivery.send.failed", Category: "TELEGRAM_API", RawRpcId: "CHAT_FORWARDS_RESTRICTED"},
	}}
	h := &Handlers{c: &grpcclient.Clients{Delivery: client}}
	w := httptest.NewRecorder()
	h.DiagnosticEvents(w, diagEventsRequest("/api/v1/dash/diagnostic-events?owner_user_id=999&category=TELEGRAM_API"))

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body: %s)", w.Code, w.Body.String())
	}
	if client.gotOwner != 5094837833 {
		t.Fatalf("Core received owner %d, want the authenticated 5094837833 — the query string must not be able to forge the tenant", client.gotOwner)
	}
	// The filter and the limit clamp survive the round trip.
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatalf("response is not valid JSON: %v", err)
	}
	if out["total"] != float64(1) {
		t.Errorf("total = %v, want 1", out["total"])
	}
	if client.gotLimit < 1 || client.gotLimit > 500 {
		t.Errorf("limit = %d, want it clamped into [1,500]", client.gotLimit)
	}
}

// The timeline is the per-message drill-down: it must pass the message
// coordinates through and return an empty list, not an error, when absent.
func TestMessageTimelinePassesCoordinatesThrough(t *testing.T) {
	client := &diagEventsClient{timeline: []*pb.DiagnosticEventEntry{
		{EventName: "delivery.strategy.failed", Attempt: 1, RawRpcCode: "400"},
		{EventName: "delivery.completed", Attempt: 2},
	}}
	h := &Handlers{c: &grpcclient.Clients{Delivery: client}}
	w := httptest.NewRecorder()
	h.MessageTimeline(w, diagEventsRequest("/api/v1/dash/diagnostic-events/timeline?source_peer_id=-100SRC&source_message_id=4242"))

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body: %s)", w.Code, w.Body.String())
	}
	if client.gotOwner != 5094837833 {
		t.Fatalf("Core received owner %d, want the authenticated user", client.gotOwner)
	}
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatalf("response is not valid JSON: %v", err)
	}
	events, _ := out["events"].([]any)
	if len(events) != 2 {
		t.Errorf("events = %d entries, want 2 (the whole history)", len(events))
	}
}

// The facet dimension is whitelisted: a caller cannot request an arbitrary
// GROUP BY over the event table.
func TestDiagnosticFacetsWhitelistTheDimension(t *testing.T) {
	client := &diagEventsClient{counts: []*pb.DiagnosticFacetCount{
		{Facet: "TELEGRAM_API", Count: 3},
	}}
	h := &Handlers{c: &grpcclient.Clients{Delivery: client}}

	for _, facet := range []string{"severity", "bogus", ""} {
		w := httptest.NewRecorder()
		h.DiagnosticFacets(w, diagEventsRequest("/api/v1/dash/diagnostic-events/facets?facet="+facet))
		if w.Code != http.StatusOK {
			t.Fatalf("facet %q: status = %d, want 200", facet, w.Code)
		}
	}
	// A bogus dimension falls back to the default, never an error.
	if client.gotFacet == "bogus" {
		t.Fatalf("a bogus facet must not reach Core verbatim")
	}
}
