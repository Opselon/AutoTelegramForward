package handlers

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"google.golang.org/grpc"

	pb "autoforward/proto"

	"autoforward/internal/grpcclient"
)

// The two admin/delivery list endpoints used to narrow a query parameter with
// `strconv.Atoi` + `int32(...)`. Atoi yields an architecture-dependent int, so
// a value above 2^31-1 wrapped negative on the wire — "the first page" became
// "a negative page" in the core. These tests pin the replacement: parse at
// 32-bit width, clamp into the documented range, and never emit a negative
// count.

type fakeAdminClient struct {
	pb.AdminControlServiceClient
	got []*pb.AdminListUsersRequest
}

func (f *fakeAdminClient) ListUsers(_ context.Context, in *pb.AdminListUsersRequest, _ ...grpc.CallOption) (*pb.AdminListUsersResponse, error) {
	f.got = append(f.got, in)
	return &pb.AdminListUsersResponse{Users: []*pb.AdminUser{}}, nil
}

type fakeDeliveryClient struct {
	pb.DeliveryControlServiceClient
	got       []*pb.ListDeadLetterRequest
	lastPurge *pb.PurgeDeadLetterRequest
}

// PurgeDeadLetter completes the fake so the DLQ purge path can be driven too:
// its body is optional, and an empty one used to corrupt the response.
func (f *fakeDeliveryClient) PurgeDeadLetter(_ context.Context, in *pb.PurgeDeadLetterRequest, _ ...grpc.CallOption) (*pb.RuleActionResponse, error) {
	f.lastPurge = in
	return &pb.RuleActionResponse{Success: true, Message: "purged"}, nil
}

func (f *fakeDeliveryClient) ListDeadLetter(_ context.Context, in *pb.ListDeadLetterRequest, _ ...grpc.CallOption) (*pb.ListDeadLetterResponse, error) {
	f.got = append(f.got, in)
	return &pb.ListDeadLetterResponse{}, nil
}

func adminUsersRequest(rawQuery string) *http.Request {
	req := httptest.NewRequest(http.MethodGet, "/admin/users?"+rawQuery, nil)
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{
		UserID: 5094837833, Username: "admin", IsAdmin: true,
	}))
}

func TestAdminListUsers_ClampsPagingInsteadOfWrappingIt(t *testing.T) {
	cases := []struct {
		name       string
		query      string
		wantLimit  int32
		wantOffset int32
	}{
		{"defaults", "", 50, 0},
		{"passthrough", "limit=25&offset=10", 25, 10},
		{"above int32 wraps without a bound check", "limit=4294967296", 50, 0},
		{"one past the page cap clamps to it", "limit=9999", 500, 0},
		{"zero limit falls back to the default", "limit=0", 50, 0},
		{"negative offset falls back to zero", "offset=-1", 50, 0},
		{"non-numeric is ignored, not fatal", "limit=abc&offset=x", 50, 0},
		{"offset at the 32-bit ceiling is kept", "offset=2147483647", 50, 2147483647},
		{"offset past the 32-bit range falls back to the default", "offset=2147483648", 50, 0},
		{"surrounding whitespace is trimmed", "limit=%20%2025%20", 25, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fc := &fakeAdminClient{}
			h := &Handlers{c: &grpcclient.Clients{Admin: fc}}
			rec := httptest.NewRecorder()
			h.AdminListUsers(rec, adminUsersRequest(tc.query))

			if rec.Code != http.StatusOK {
				t.Fatalf("expected 200, got %d (%s)", rec.Code, rec.Body.String())
			}
			if len(fc.got) != 1 {
				t.Fatalf("expected exactly one core call, got %d", len(fc.got))
			}
			got := fc.got[0]
			if got.Limit != tc.wantLimit || got.Offset != tc.wantOffset {
				t.Fatalf("limit/offset = %d/%d, want %d/%d", got.Limit, got.Offset, tc.wantLimit, tc.wantOffset)
			}
			if got.Limit < 0 || got.Offset < 0 {
				t.Fatalf("a negative page reached the core: %d/%d", got.Limit, got.Offset)
			}
		})
	}
}

func TestListDeadLetter_ClampsThePageSize(t *testing.T) {
	cases := []struct {
		name  string
		query string
		want  int32
	}{
		{"defaults", "", 50},
		{"passthrough", "limit=20", 20},
		{"above int32 wraps without a bound check", "limit=4294967296", 50},
		{"oversized clamps to the cap", "limit=100000", 500},
		{"zero falls back to the default", "limit=0", 50},
		{"non-numeric is ignored", "limit=abc", 50},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fc := &fakeDeliveryClient{}
			h := &Handlers{c: &grpcclient.Clients{Delivery: fc}}
			req := httptest.NewRequest(http.MethodGet, "/delivery/dead-letter?"+tc.query, nil)
			req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 7}))
			rec := httptest.NewRecorder()
			h.ListDeadLetter(rec, req)

			if rec.Code != http.StatusOK {
				t.Fatalf("expected 200, got %d (%s)", rec.Code, rec.Body.String())
			}
			if len(fc.got) != 1 {
				t.Fatalf("expected exactly one core call, got %d", len(fc.got))
			}
			if fc.got[0].Limit != tc.want {
				t.Fatalf("limit = %d, want %d", fc.got[0].Limit, tc.want)
			}
			if fc.got[0].Limit <= 0 {
				t.Fatalf("a non-positive page size reached the core: %d", fc.got[0].Limit)
			}
		})
	}
}

// boundedQueryInt is the one place the narrowing happens; it is also what makes
// the conversions above analysable, so it gets its own table.
func TestBoundedQueryInt_RejectsWhatDoesNotFitAndClampsTheRest(t *testing.T) {
	cases := []struct {
		raw    string
		lo, hi int32
		want   int32
		wantOK bool
	}{
		{"10", 1, 200, 10, true},
		{"0", 1, 200, 1, true},
		{"999", 1, 200, 200, true},
		{"-5", 0, 100, 0, true},
		{"2147483647", 0, 2147483647, 2147483647, true},
		{"2147483648", 0, 2147483647, 0, false},
		{"4294967296", 0, 2147483647, 0, false},
		{"-2147483649", 0, 2147483647, 0, false},
		{"", 0, 100, 0, false},
		{"abc", 0, 100, 0, false},
		{"1e3", 0, 100, 0, false},
	}
	for _, tc := range cases {
		got, ok := boundedQueryInt(tc.raw, tc.lo, tc.hi)
		if ok != tc.wantOK || (ok && got != tc.want) {
			t.Fatalf("boundedQueryInt(%q, %d, %d) = (%d, %v), want (%d, %v)",
				tc.raw, tc.lo, tc.hi, got, ok, tc.want, tc.wantOK)
		}
	}
}
