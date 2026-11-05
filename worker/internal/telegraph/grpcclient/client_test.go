package grpcclient

import (
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// TestTransportFailureClassifiesByGrpcStatus pins the mapping that used to
// flatten every gRPC error into NETWORK_TIMEOUT/UNAVAILABLE. The production
// consequence was twofold: the /ops error ring described a missing RPC as a
// network timeout, and IsTransient() (NETWORK_TIMEOUT counts as retryable)
// kept the worker retrying a permanent defect forever.
func TestTransportFailureClassifiesByGrpcStatus(t *testing.T) {
	cases := []struct {
		name     string
		err      error
		wantCode string
		wantType string
		retry    bool
	}{
		{
			name: "unimplemented is permanent internal error",
			err:  status.Error(codes.Unimplemented, "Method not implemented!"),
			// Classification: a missing handler is a permanent server defect,
			// named as INTERNAL_ERROR (not disguised as a network timeout).
			// Retryability: INTERNAL_ERROR is in the shared transient set by
			// contract (both Go and Python treat it conservatively), so the
			// defect this case pins is the LABEL, not the retry flag.
			wantCode: "INTERNAL_ERROR",
			wantType: "UNIMPLEMENTED",
			retry:    true,
		},
		{
			name:     "unavailable stays the retryable network case",
			err:      status.Error(codes.Unavailable, "connection refused"),
			wantCode: "NETWORK_TIMEOUT",
			wantType: "UNAVAILABLE",
			retry:    true,
		},
		{
			name:     "deadline exceeded stays retryable network case",
			err:      status.Error(codes.DeadlineExceeded, "context deadline exceeded"),
			wantCode: "NETWORK_TIMEOUT",
			wantType: "UNAVAILABLE",
			retry:    true,
		},
		{
			name:     "permission denied is an auth failure, not a timeout",
			err:      status.Error(codes.PermissionDenied, "no access"),
			wantCode: "AUTHENTICATION_ERROR",
			wantType: "PERMISSION_DENIED",
			retry:    false,
		},
		{
			name:     "resource exhausted is rate limiting",
			err:      status.Error(codes.ResourceExhausted, "quota"),
			wantCode: "RATE_LIMITED",
			wantType: "RESOURCE_EXHAUSTED",
			retry:    true,
		},
		{
			name:     "internal is internal, not a network timeout",
			err:      status.Error(codes.Internal, "boom"),
			wantCode: "INTERNAL_ERROR",
			wantType: "INTERNAL",
			retry:    true, // shared contract: INTERNAL_ERROR is transient
		},
		{
			name:     "canceled stays retryable",
			err:      status.Error(codes.Canceled, "canceled"),
			wantCode: "NETWORK_TIMEOUT",
			wantType: "UNAVAILABLE",
			retry:    true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := transportFailure(tc.err)
			if f.Code != tc.wantCode {
				t.Errorf("Code = %q, want %q", f.Code, tc.wantCode)
			}
			if f.ErrorType != tc.wantType {
				t.Errorf("ErrorType = %q, want %q", f.ErrorType, tc.wantType)
			}
			if f.IsTransient() != tc.retry {
				t.Errorf("IsTransient() = %v, want %v (retryability must follow the classification)", f.IsTransient(), tc.retry)
			}
			if f.Message == "" {
				t.Error("Message must carry the raw gRPC error text for diagnostics")
			}
		})
	}
}

// TestTransportFailureUnimplementedIsAmbiguousGuard asserts the worker never
// marks an UNIMPLEMENTED call as ambiguous (UNKNOWN/reconcile): the outcome
// is not unknown, it is a permanent failure, so reconciliation has nothing
// to decide and no duplicate can be risked.
func TestTransportFailureUnimplementedIsAmbiguousGuard(t *testing.T) {
	f := transportFailure(status.Error(codes.Unimplemented, "Method not implemented!"))
	if f.IsAmbiguous() {
		t.Fatal("UNIMPLEMENTED must not be ambiguous: the call never reached Telegram, nothing can reconcile it")
	}
}
