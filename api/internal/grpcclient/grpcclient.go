package grpcclient

import (
	"context"
	"os"
	"strings"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	"autoforward/internal/middleware"
	pb "autoforward/proto"
)

// CoreRPCTokenEnv names the shared credential the core's gRPC surface requires
// when it is configured. Both processes read the same variable, so a deployment
// cannot end up with enforcement on one side only by accident.
//
// Why this exists: every core servicer takes the acting tenant from
// `owner_user_id` in the request. This API stamps that field from the identity
// it verified in the caller's token, but the core has no way to tell *this*
// caller from any other process that can open a socket to it. The credential
// makes impersonating a tenant require the secret as well as the network
// position.
//
// Unset = no credential is sent, and the core (if it is also unset) accepts
// unauthenticated callers exactly as it always has. Set on one side only =
// every call is refused with UNAUTHENTICATED, which is the failure we want:
// loud, at deploy time, instead of a silently absent boundary.
const CoreRPCTokenEnv = "ATF_CORE_RPC_TOKEN"

// normalizeRPCToken treats a blank or whitespace-only value as "not
// configured", exactly as the core does (core/config.py: configured_token). The
// two sides must agree on this: a whitespace-only credential on the Go side
// would otherwise be sent as `Bearer    ` while the core, reading the same
// value, installed no boundary at all — an "authentication" whose secret is
// whitespace.
func normalizeRPCToken(token string) string {
	return strings.TrimSpace(token)
}

// InternalAuthUnaryClientInterceptor attaches the shared credential to every
// unary call. A blank token yields a pass-through interceptor rather than a nil
// one so callers can chain unconditionally.
func InternalAuthUnaryClientInterceptor(token string) grpc.UnaryClientInterceptor {
	token = normalizeRPCToken(token)
	return func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		if token != "" {
			ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
		}
		return invoker(ctx, method, req, reply, cc, opts...)
	}
}

// InternalAuthStreamClientInterceptor is the streaming counterpart, for the
// same reason: a boundary that only covers unary calls is not a boundary.
func InternalAuthStreamClientInterceptor(token string) grpc.StreamClientInterceptor {
	token = normalizeRPCToken(token)
	return func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string, streamer grpc.Streamer, opts ...grpc.CallOption) (grpc.ClientStream, error) {
		if token != "" {
			ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
		}
		return streamer(ctx, desc, cc, method, opts...)
	}
}

// Dial connects to the Python core's gRPC server with automatic retry policy,
// presenting the shared internal credential when one is configured.
func Dial(addr string) (*grpc.ClientConn, error) {
	return dial(addr, normalizeRPCToken(os.Getenv(CoreRPCTokenEnv)))
}

// Logger token environment variables, one per capability class. The server
// compares in constant time and refuses cross-class reuse, so a leaked producer
// token cannot read the archive from this API either.
const (
	LoggerIngestTokenEnv = "ATF_LOGGER_INGEST_TOKEN"
	LoggerReadTokenEnv   = "ATF_LOGGER_READ_TOKEN"
	LoggerAdminTokenEnv  = "ATF_LOGGER_ADMIN_TOKEN"
)

// LoggerAuthUnaryClientInterceptor picks the capability token by gRPC method:
// `Log` (ingest), `QueryLogs`/`LogStats` (read), anything else (admin — the
// server denies new methods by default, so this mapping must consciously grant
// them). Tokens are normalised the same way the core token is: blank or
// whitespace-only means "not configured", and then nothing is attached.
func LoggerAuthUnaryClientInterceptor(ingest, read, admin string) grpc.UnaryClientInterceptor {
	ingest = normalizeRPCToken(ingest)
	read = normalizeRPCToken(read)
	admin = normalizeRPCToken(admin)
	return func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		var token string
		switch {
		case strings.HasSuffix(method, ".LogControlService/Log"):
			token = ingest
		case strings.HasSuffix(method, ".LogControlService/QueryLogs"),
			strings.HasSuffix(method, ".LogControlService/LogStats"):
			token = read
		default:
			token = admin
		}
		if token != "" {
			ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
		}
		return invoker(ctx, method, req, reply, cc, opts...)
	}
}

// LoggerAuthStreamClientInterceptor is the streaming counterpart: the logger
// has no server streams today, but a boundary that only covers unary calls is
// not a boundary (see InternalAuthStreamClientInterceptor).
func LoggerAuthStreamClientInterceptor(ingest, read, admin string) grpc.StreamClientInterceptor {
	ingest = normalizeRPCToken(ingest)
	read = normalizeRPCToken(read)
	admin = normalizeRPCToken(admin)
	return func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string, streamer grpc.Streamer, opts ...grpc.CallOption) (grpc.ClientStream, error) {
		var token string
		switch {
		case strings.HasSuffix(method, ".LogControlService/Log"):
			token = ingest
		case strings.HasSuffix(method, ".LogControlService/QueryLogs"),
			strings.HasSuffix(method, ".LogControlService/LogStats"):
			token = read
		default:
			token = admin
		}
		if token != "" {
			ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
		}
		return streamer(ctx, desc, cc, method, opts...)
	}
}

// DialLogger connects to the Rust logger service with the per-capability
// credentials it may be configured to require. Empty variables on any class
// leave that class open on an unconfigured server and harmlessly unattached
// here — send-only-what-is-armed keeps a mixed deployment from half-arming.
func DialLogger(addr string) (*grpc.ClientConn, error) {
	ingest := normalizeRPCToken(os.Getenv(LoggerIngestTokenEnv))
	read := normalizeRPCToken(os.Getenv(LoggerReadTokenEnv))
	admin := normalizeRPCToken(os.Getenv(LoggerAdminTokenEnv))
	conn, err := grpc.NewClient(addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithChainUnaryInterceptor(
			middleware.CorrelationUnaryClientInterceptor(),
			LoggerAuthUnaryClientInterceptor(ingest, read, admin),
		),
		grpc.WithChainStreamInterceptor(
			LoggerAuthStreamClientInterceptor(ingest, read, admin),
		),
	)
	if err != nil {
		return nil, err
	}
	return conn, nil
}

// DialWithoutCredential connects to an internal service that is NOT the core.
//
// The credential authenticates this API to the core; the logger is a different
// trust domain and has no use for it, so sending it there would hand a second
// service a secret it can use to impersonate this API to the core. Use this for
// every target except ATF_CORE_GRPC.
func DialWithoutCredential(addr string) (*grpc.ClientConn, error) {
	return dial(addr, "")
}

func dial(addr, token string) (*grpc.ClientConn, error) {
	retryConfig := `{
		"methodConfig": [{
			"name": [{"service": ""}],
			"retryPolicy": {
				"maxAttempts": 5,
				"initialBackoff": "0.1s",
				"maxBackoff": "1s",
				"backoffMultiplier": 2.0,
				"retryableStatusCodes": ["UNAVAILABLE"]
			}
		}]
	}`
	return grpc.NewClient(addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultServiceConfig(retryConfig),
		grpc.WithChainUnaryInterceptor(
			middleware.CorrelationUnaryClientInterceptor(),
			InternalAuthUnaryClientInterceptor(token),
		),
		grpc.WithChainStreamInterceptor(InternalAuthStreamClientInterceptor(token)),
	)
}

// Clients bundles all typed service stubs.
type Clients struct {
	Sessions pb.SessionControlServiceClient
	Rules    pb.ForwardRuleControlServiceClient
	Filters  pb.FilterControlServiceClient
	AI       pb.AIControlServiceClient
	System   pb.SystemStatusControlServiceClient
	Logs     pb.LogControlServiceClient
	Delivery pb.DeliveryControlServiceClient
	Account  pb.AccountControlServiceClient
	Backup   pb.BackupControlServiceClient
	Admin    pb.AdminControlServiceClient
	Audit    pb.AuditControlServiceClient
	// Bot route credentials: the bots a tenant forwards *as*.
	BotTokens pb.BotTokenControlServiceClient
	// Per-user task lifecycle (API-first phase 2).
	Tasks pb.TaskControlServiceClient
	// AI private-message auto-reply (schema v28).
	AutoReply pb.AutoReplyControlServiceClient
	// Replace rules: the unified find/replace manager behind /replace.
	Replace pb.ReplaceRuleControlServiceClient
}

// NewClients bundles the core service stubs.
func NewClients(conn *grpc.ClientConn) *Clients {
	return &Clients{
		Sessions:  pb.NewSessionControlServiceClient(conn),
		Rules:     pb.NewForwardRuleControlServiceClient(conn),
		Filters:   pb.NewFilterControlServiceClient(conn),
		AI:        pb.NewAIControlServiceClient(conn),
		System:    pb.NewSystemStatusControlServiceClient(conn),
		Delivery:  pb.NewDeliveryControlServiceClient(conn),
		Account:   pb.NewAccountControlServiceClient(conn),
		Backup:    pb.NewBackupControlServiceClient(conn),
		Admin:     pb.NewAdminControlServiceClient(conn),
		Audit:     pb.NewAuditControlServiceClient(conn),
		BotTokens: pb.NewBotTokenControlServiceClient(conn),
		Tasks:     pb.NewTaskControlServiceClient(conn),
		AutoReply: pb.NewAutoReplyControlServiceClient(conn),
		Replace:   pb.NewReplaceRuleControlServiceClient(conn),
	}
}

// AttachLogger points the Logs stub at the separate Logger microservice conn.
func AttachLogger(c *Clients, conn *grpc.ClientConn) {
	c.Logs = pb.NewLogControlServiceClient(conn)
}
