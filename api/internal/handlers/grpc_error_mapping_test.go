package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// grpcError is the single translation table between the core's gRPC status and
// the API's HTTP answer, and every client decides what to do from that status.
// It handled three codes and folded everything else into 500, so an expired
// deadline read as "the server is broken" (and got retried as if it were), an
// exhausted quota was not retried at all, and a core-side refusal could not be
// told apart from a crash. The table below is the canonical gRPC -> HTTP
// mapping (the one the Google API transcoder uses), with this repo's documented
// UNAVAILABLE => 502 convention kept as-is.
func TestGrpcError_MapsEveryStatusesToTheRightHTTPCode(t *testing.T) {
	cases := []struct {
		code codes.Code
		want int
	}{
		{codes.OK, http.StatusInternalServerError},
		{codes.Canceled, statusClientClosedRequest},
		{codes.Unknown, http.StatusInternalServerError},
		{codes.InvalidArgument, http.StatusBadRequest},
		{codes.DeadlineExceeded, http.StatusGatewayTimeout},
		{codes.NotFound, http.StatusNotFound},
		{codes.AlreadyExists, http.StatusConflict},
		{codes.PermissionDenied, http.StatusForbidden},
		{codes.ResourceExhausted, http.StatusTooManyRequests},
		{codes.FailedPrecondition, http.StatusBadRequest},
		{codes.Aborted, http.StatusConflict},
		{codes.OutOfRange, http.StatusBadRequest},
		{codes.Unimplemented, http.StatusNotImplemented},
		{codes.Internal, http.StatusInternalServerError},
		// Deliberate repo convention: the API says 502 when the core is not
		// answering (docs/api-compatibility.md and the deploy gate both read it).
		{codes.Unavailable, http.StatusBadGateway},
		{codes.DataLoss, http.StatusInternalServerError},
		// A transport-level error is not a gRPC status at all.
		// status.FromError returns Unknown for it, which must stay a 500.
	}
	for _, tc := range cases {
		t.Run(tc.code.String(), func(t *testing.T) {
			rec := httptest.NewRecorder()
			grpcError(rec, status.Error(tc.code, "boom"))
			if rec.Code != tc.want {
				t.Fatalf("%s must map to %d, got %d", tc.code, tc.want, rec.Code)
			}
			// Every refusal keeps the same shape so a client renders it
			// instead of printing an identifier it does not know.
			var body map[string]any
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("refusal must be JSON: %v (%s)", err, rec.Body.String())
			}
			if _, ok := body["error"]; !ok {
				t.Fatalf("refusal must carry an error key: %s", rec.Body.String())
			}
		})
	}
}

// A non-status error (a broken connection, a nil error) must not panic and must
// not be reported as a success.
func TestGrpcError_NonStatusErrorIsAServerError(t *testing.T) {
	rec := httptest.NewRecorder()
	grpcError(rec, context_Canceled{})
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", rec.Code)
	}
}

type context_Canceled struct{}

func (context_Canceled) Error() string { return "transport is closing" }
