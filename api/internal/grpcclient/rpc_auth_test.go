package grpcclient

import (
	"context"
	"errors"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// The core's gRPC surface takes the acting tenant from `owner_user_id` in each
// request. This API stamps that field from the verified identity, but the core
// cannot tell *this* caller from any other process that can reach core:50051 —
// so when a shared credential is configured, every call must carry it.
//
// These tests pin the client half of that contract: the credential is attached
// to outgoing unary AND streaming calls, it is only attached when configured,
// and the caller's own context is left alone.

func captureUnary(t *testing.T, token string) (metadata.MD, error) {
	t.Helper()
	var seen metadata.MD
	invoker := func(ctx context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
		seen, _ = metadata.FromOutgoingContext(ctx)
		return nil
	}
	err := InternalAuthUnaryClientInterceptor(token)(
		context.Background(), "/autoforward.Test/Get", &struct{}{}, &struct{}{}, nil, invoker,
	)
	return seen, err
}

func TestUnaryCallCarriesTheCredential(t *testing.T) {
	seen, err := captureUnary(t, "tok-123")
	if err != nil {
		t.Fatalf("interceptor returned an error: %v", err)
	}
	got := seen.Get("authorization")
	if len(got) != 1 || got[0] != "Bearer tok-123" {
		t.Fatalf("authorization = %v, want [Bearer tok-123]", got)
	}
}

func TestUnconfiguredCredentialSendsNothing(t *testing.T) {
	for _, token := range []string{"", "   "} {
		// "   " is a configuration mistake, not a credential: sending
		// "Bearer    " would be a credential whose secret is whitespace.
		seen, err := captureUnary(t, token)
		if err != nil {
			t.Fatalf("token %q: interceptor returned an error: %v", token, err)
		}
		if got := seen.Get("authorization"); len(got) != 0 {
			t.Fatalf("token %q: sent an authorization header anyway: %v", token, got)
		}
	}
}

func TestTheCallersContextIsNotMutated(t *testing.T) {
	parent := context.Background()
	var sent metadata.MD
	invoker := func(ctx context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
		sent, _ = metadata.FromOutgoingContext(ctx)
		return nil
	}
	_ = InternalAuthUnaryClientInterceptor("tok")(
		parent, "/autoforward.Test/Get", &struct{}{}, &struct{}{}, nil, invoker,
	)
	if sent.Get("authorization") == nil {
		t.Fatal("credential was not attached to the derived context")
	}
	if fromParent, _ := metadata.FromOutgoingContext(parent); len(fromParent.Get("authorization")) != 0 {
		t.Fatalf("the credential leaked into the caller's context: %v", fromParent)
	}
}

func TestAnExistingAuthorizationHeaderIsKept(t *testing.T) {
	// AppendToOutgoingContext appends; it must not clobber another value the
	// caller set, or a future caller-side header would vanish silently.
	ctx := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer other")
	var sent metadata.MD
	invoker := func(ctx context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
		sent, _ = metadata.FromOutgoingContext(ctx)
		return nil
	}
	_ = InternalAuthUnaryClientInterceptor("tok")(
		ctx, "/autoforward.Test/Get", &struct{}{}, &struct{}{}, nil, invoker,
	)
	if got := sent.Get("authorization"); len(got) != 2 {
		t.Fatalf("authorization = %v, want both the caller's and this one", got)
	}
}

type recordingStreamer struct {
	seen metadata.MD
}

func (r *recordingStreamer) stream(ctx context.Context, _ *grpc.StreamDesc, _ *grpc.ClientConn, _ string, _ ...grpc.CallOption) (grpc.ClientStream, error) {
	r.seen, _ = metadata.FromOutgoingContext(ctx)
	return nil, nil
}

func TestStreamingCallCarriesTheCredential(t *testing.T) {
	for _, tc := range []struct {
		token string
		want  int
	}{{"tok-123", 1}, {"", 0}} {
		st := &recordingStreamer{}
		_, err := InternalAuthStreamClientInterceptor(tc.token)(
			context.Background(), &grpc.StreamDesc{}, nil, "/autoforward.Test/Watch", st.stream,
		)
		if err != nil {
			t.Fatalf("token %q: stream interceptor returned an error: %v", tc.token, err)
		}
		got := st.seen.Get("authorization")
		if len(got) != tc.want {
			t.Fatalf("token %q: authorization = %v, want %d value(s)", tc.token, got, tc.want)
		}
		if tc.want > 0 && got[0] != "Bearer tok-123" {
			t.Fatalf("authorization = %v, want [Bearer tok-123]", got)
		}
	}
}

func TestErrorsFromTheInvokerPassThrough(t *testing.T) {
	sentinel := errors.New("core is down")
	invoker := func(context.Context, string, any, any, *grpc.ClientConn, ...grpc.CallOption) error {
		return sentinel
	}
	err := InternalAuthUnaryClientInterceptor("tok")(
		context.Background(), "/autoforward.Test/Get", &struct{}{}, &struct{}{}, nil, invoker,
	)
	if !errors.Is(err, sentinel) {
		t.Fatalf("err = %v, want the invoker's error unwrapped", err)
	}
}

// Dial must not fail because of the credential: it is read from the same
// environment variable both processes use, and a blank value is a valid
// (unauthenticated) configuration.
func TestDialAcceptsBothCredentialStates(t *testing.T) {
	for _, token := range []string{"", "tok-123"} {
		t.Setenv(CoreRPCTokenEnv, token)
		conn, err := Dial("127.0.0.1:1")
		if err != nil {
			t.Fatalf("token %q: Dial: %v", token, err)
		}
		if conn == nil {
			t.Fatalf("token %q: Dial returned no connection", token)
		}
		_ = conn.Close()
	}
}

// The Go client and the Python core must agree on the variable name; a rename
// on one side is a silent loss of the boundary (the credential simply stops
// being sent), which no other test would catch.
func TestTheEnvVarNameMatchesTheCores(t *testing.T) {
	if CoreRPCTokenEnv != "ATF_CORE_RPC_TOKEN" {
		t.Fatalf("CoreRPCTokenEnv = %q, want ATF_CORE_RPC_TOKEN (core/config.py reads the same name)", CoreRPCTokenEnv)
	}
}

// ---- logger per-capability tokens --------------------------------------------

// The logger's surface is capability-scoped: ingesting (Log), reading the
// archive (QueryLogs/LogStats) and erasing it (PurgeLogs) each have their own
// secret, and the server refuses cross-class reuse. The client must therefore
// pick the token to attach by method — a single shared header would either
// under-attach (purge sent with a read token) or over-share (a producer token
// reaching the reader).

func captureLoggerUnary(t *testing.T, interp grpc.UnaryClientInterceptor, method string) (metadata.MD, error) {
	t.Helper()
	var seen metadata.MD
	invoker := func(ctx context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
		seen, _ = metadata.FromOutgoingContext(ctx)
		return nil
	}
	err := interp(context.Background(), method, &struct{}{}, &struct{}{}, nil, invoker)
	return seen, err
}

func TestLoggerTokensAttachPerCapability(t *testing.T) {
	interp := LoggerAuthUnaryClientInterceptor("ingest-tok", "read-tok", "admin-tok")

	for _, tc := range []struct {
		method string
		want   string
	}{
		{"/autoforward.LogControlService/Log", "Bearer ingest-tok"},
		{"/autoforward.LogControlService/QueryLogs", "Bearer read-tok"},
		{"/autoforward.LogControlService/LogStats", "Bearer read-tok"},
		{"/autoforward.LogControlService/PurgeLogs", "Bearer admin-tok"},
	} {
		seen, err := captureLoggerUnary(t, interp, tc.method)
		if err != nil {
			t.Fatalf("%s: interceptor returned an error: %v", tc.method, err)
		}
		got := seen.Get("authorization")
		if len(got) != 1 || got[0] != tc.want {
			t.Fatalf("%s: authorization = %v, want [%s]", tc.method, got, tc.want)
		}
	}
}

func TestLoggerUnconfiguredClassSendsNothingForThatClass(t *testing.T) {
	// Only purge is armed: Log and QueryLogs must reach the (open) surface with
	// no header at all, not with an empty one.
	interp := LoggerAuthUnaryClientInterceptor("", "   ", "admin-tok")

	for _, tc := range []struct {
		method string
		want   string
	}{{"/autoforward.LogControlService/Log", ""}, {"/autoforward.LogControlService/QueryLogs", ""}, {"/autoforward.LogControlService/PurgeLogs", "Bearer admin-tok"}} {
		seen, err := captureLoggerUnary(t, interp, tc.method)
		if err != nil {
			t.Fatalf("%s: interceptor returned an error: %v", tc.method, err)
		}
		got := seen.Get("authorization")
		want := []string{}
		if tc.want != "" {
			want = []string{tc.want}
		}
		if len(got) != len(want) || (len(want) == 1 && got[0] != want[0]) {
			t.Fatalf("%s: authorization = %v, want %v", tc.method, got, want)
		}
	}
}
