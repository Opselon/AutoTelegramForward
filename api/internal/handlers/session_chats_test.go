package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "autoforward/proto"

	"autoforward/internal/grpcclient"
)

// fakeChatClient stands in for the core's SessionControlService so a test can
// inspect exactly what the handler put on the wire (owner, filters, paging) and
// drive the error paths without a live core.
type fakeChatClient struct {
	pb.SessionControlServiceClient
	got  []*pb.ListChatsRequest
	resp *pb.ListChatsResponse
	err  error
}

func (f *fakeChatClient) ListChats(ctx context.Context, in *pb.ListChatsRequest, opts ...grpc.CallOption) (*pb.ListChatsResponse, error) {
	f.got = append(f.got, in)
	if f.err != nil {
		return nil, f.err
	}
	return f.resp, nil
}

// chatRequest builds a GET /sessions/{id}/chats request for the dashboard mux,
// carrying uid as the verified identity. uid <= 0 means "unauthenticated" (no
// identity in the context), which is how the middleware presents a bad token.
func chatRequest(uid int64, sessionID, rawQuery string) *http.Request {
	req := httptest.NewRequest(http.MethodGet, "/sessions/"+sessionID+"/chats?"+rawQuery, nil)
	req.SetPathValue("sessionId", sessionID)
	if uid > 0 {
		req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid}))
	}
	return req
}

func decodeBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("response is not JSON: %s", rec.Body.String())
	}
	return body
}

func numField(t *testing.T, body map[string]any, key string) float64 {
	t.Helper()
	v, ok := body[key].(float64)
	if !ok {
		t.Fatalf("field %q missing or non-numeric in %v", key, body)
	}
	return v
}

// The owner that reaches the core is the caller's own id — never anything the
// request could carry — and the query/paging parameters are forwarded as given.
func TestListChats_ScopesToCallerIdentityAndForwardsFilters(t *testing.T) {
	fc := &fakeChatClient{resp: &pb.ListChatsResponse{
		SessionId: "sess-1",
		Chats:     []*pb.ChatSummary{{ChatId: "-1001", Title: "News", Username: "news", ChatType: "channel"}},
		Count:     1, Total: 1,
	}}
	h := &Handlers{c: &grpcclient.Clients{Sessions: fc}}

	rec := httptest.NewRecorder()
	h.ListChats(rec, chatRequest(111, "sess-1", "query=news&limit=25&offset=10"))

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(fc.got) != 1 {
		t.Fatalf("core called %d times, want 1", len(fc.got))
	}
	got := fc.got[0]
	if got.OwnerUserId != 111 {
		t.Fatalf("OwnerUserId = %d, want the caller's 111", got.OwnerUserId)
	}
	if got.SessionId != "sess-1" {
		t.Fatalf("SessionId = %q, want sess-1", got.SessionId)
	}
	if got.Query != "news" || got.Limit != 25 || got.Offset != 10 {
		t.Fatalf("forwarded wrong filters: query=%q limit=%d offset=%d", got.Query, got.Limit, got.Offset)
	}

	body := decodeBody(t, rec)
	if body["session_id"] != "sess-1" {
		t.Fatalf("session_id = %v, want sess-1", body["session_id"])
	}
	if numField(t, body, "count") != 1 || numField(t, body, "total") != 1 {
		t.Fatalf("count/total wrong: %v", body)
	}
	if _, ok := body["truncated"].(bool); !ok {
		t.Fatalf("truncated missing from %v", body)
	}
	chats, ok := body["chats"].([]any)
	if !ok || len(chats) != 1 {
		t.Fatalf("chats = %v, want one entry", body["chats"])
	}
	first := chats[0].(map[string]any)
	if first["chat_id"] != "-1001" || first["chat_type"] != "channel" {
		t.Fatalf("chat entry shape wrong: %v", first)
	}
}

// A foreign or unknown session is a 404 — the caller learns only that it is not
// theirs, exactly what the contract promises.
func TestListChats_ForeignSessionIsNotFound(t *testing.T) {
	fc := &fakeChatClient{err: status.Error(codes.NotFound, "session_not_found")}
	h := &Handlers{c: &grpcclient.Clients{Sessions: fc}}

	rec := httptest.NewRecorder()
	h.ListChats(rec, chatRequest(111, "someone-elses", ""))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d: %s", rec.Code, rec.Body.String())
	}
	if got := responseCode(t, rec); got != "session_not_found" {
		t.Fatalf("code = %q, want session_not_found", got)
	}
}

// A session with no live Telegram client is a 503 (the core signals
// FAILED_PRECONDITION); it must not collapse into the generic 500.
func TestListChats_NotConnectedIsServiceUnavailable(t *testing.T) {
	fc := &fakeChatClient{err: status.Error(codes.FailedPrecondition, "session_not_connected")}
	h := &Handlers{c: &grpcclient.Clients{Sessions: fc}}

	rec := httptest.NewRecorder()
	h.ListChats(rec, chatRequest(111, "sess-1", ""))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d: %s", rec.Code, rec.Body.String())
	}
	if got := responseCode(t, rec); got != "session_not_connected" {
		t.Fatalf("code = %q, want session_not_connected", got)
	}
}

// A core that is down keeps the shared 502 mapping.
func TestListChats_CoreUnreachableIsBadGateway(t *testing.T) {
	fc := &fakeChatClient{err: status.Error(codes.Unavailable, "dial tcp: refused")}
	h := &Handlers{c: &grpcclient.Clients{Sessions: fc}}

	rec := httptest.NewRecorder()
	h.ListChats(rec, chatRequest(111, "sess-1", ""))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d: %s", rec.Code, rec.Body.String())
	}
}

// A nil SessionControlService client (core never wired) is a 502, not a panic.
func TestListChats_NilSessionsClientIsBadGateway(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{}}
	rec := httptest.NewRecorder()
	h.ListChats(rec, chatRequest(111, "sess-1", ""))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d: %s", rec.Code, rec.Body.String())
	}
}

// limit/offset are clamped into the documented window before the core call, and
// an absent value takes the default page size.
func TestListChats_ClampsLimitAndOffset(t *testing.T) {
	cases := []struct {
		name       string
		query      string
		wantLimit  int32
		wantOffset int32
	}{
		{"absent uses defaults", "", 100, 0},
		{"zero limit faults to one", "limit=0", 1, 0},
		{"negative limit faults to one", "limit=-3", 1, 0},
		{"in range passes through", "limit=200", 200, 0},
		{"over max is capped", "limit=999", 200, 0},
		{"negative offset faults to zero", "offset=-4", 100, 0},
		{"offset passes through", "limit=5&offset=40", 5, 40},
		// A value too large for the 32-bit request field is a client error, not
		// a wrapped number that would silently become a different page.
		{"offset within int32 passes through", "offset=2147483647", 100, 2147483647},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fc := &fakeChatClient{resp: &pb.ListChatsResponse{SessionId: "sess-1"}}
			h := &Handlers{c: &grpcclient.Clients{Sessions: fc}}

			rec := httptest.NewRecorder()
			h.ListChats(rec, chatRequest(111, "sess-1", tc.query))
			if rec.Code != http.StatusOK {
				t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
			}
			if len(fc.got) != 1 {
				t.Fatalf("core called %d times, want 1", len(fc.got))
			}
			if got := fc.got[0]; got.Limit != tc.wantLimit || got.Offset != tc.wantOffset {
				t.Fatalf("%s: forwarded limit=%d offset=%d, want %d/%d",
					tc.query, got.Limit, got.Offset, tc.wantLimit, tc.wantOffset)
			}
		})
	}
}

// A non-numeric limit/offset is a client error, and the core is never called —
// a silent fallback would hide a broken caller.
func TestListChats_NonNumericPagingIsBadRequest(t *testing.T) {
	cases := []struct {
		query string
		code  string
	}{
		{"limit=abc", "invalid_limit"},
		{"limit=1.5", "invalid_limit"},
		{"offset=xyz", "invalid_offset"},
		{"limit=10&offset=1.5", "invalid_offset"},
		// Past int32: refused instead of wrapping to a negative page index.
		{"limit=9999999999", "invalid_limit"},
		{"offset=2147483648", "invalid_offset"},
	}
	for _, tc := range cases {
		t.Run(tc.query, func(t *testing.T) {
			fc := &fakeChatClient{resp: &pb.ListChatsResponse{}}
			h := &Handlers{c: &grpcclient.Clients{Sessions: fc}}

			rec := httptest.NewRecorder()
			h.ListChats(rec, chatRequest(111, "sess-1", tc.query))
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
			}
			if got := responseCode(t, rec); got != tc.code {
				t.Fatalf("code = %q, want %q (%s)", got, tc.code, rec.Body.String())
			}
			if len(fc.got) != 0 {
				t.Fatal("a rejected request must never reach the core")
			}
		})
	}
}

// Without a verified identity the handler refuses before calling the core.
func TestListChats_UnauthenticatedIsRefused(t *testing.T) {
	fc := &fakeChatClient{resp: &pb.ListChatsResponse{}}
	h := &Handlers{c: &grpcclient.Clients{Sessions: fc}}

	rec := httptest.NewRecorder()
	h.ListChats(rec, chatRequest(0, "sess-1", ""))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(fc.got) != 0 {
		t.Fatal("an unauthenticated request must never reach the core")
	}
}

// A missing session id in the path is a 400, not a core round-trip.
func TestListChats_MissingSessionIDIsBadRequest(t *testing.T) {
	fc := &fakeChatClient{resp: &pb.ListChatsResponse{}}
	h := &Handlers{c: &grpcclient.Clients{Sessions: fc}}

	rec := httptest.NewRecorder()
	h.ListChats(rec, chatRequest(111, "", ""))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(fc.got) != 0 {
		t.Fatal("a request without a session id must never reach the core")
	}
}

// An empty result serialises as "chats":[] (never JSON null), so the mobile list
// parser gets a valid empty array.
func TestListChats_EmptyResultIsAnEmptyArray(t *testing.T) {
	fc := &fakeChatClient{resp: &pb.ListChatsResponse{SessionId: "sess-1", Chats: nil}}
	h := &Handlers{c: &grpcclient.Clients{Sessions: fc}}

	rec := httptest.NewRecorder()
	h.ListChats(rec, chatRequest(111, "sess-1", ""))
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	chats, ok := decodeBody(t, rec)["chats"].([]any)
	if !ok {
		t.Fatalf("chats is not an array: %s", rec.Body.String())
	}
	if len(chats) != 0 {
		t.Fatalf("chats = %v, want empty", chats)
	}
}
