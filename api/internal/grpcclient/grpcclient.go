package grpcclient

import (
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"autoforward/internal/middleware"
	pb "autoforward/proto"
)

// Dial connects to the Python core's gRPC server with automatic retry policy.
func Dial(addr string) (*grpc.ClientConn, error) {
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
		grpc.WithChainUnaryInterceptor(middleware.CorrelationUnaryClientInterceptor()),
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
}

// NewClients bundles the core service stubs.
func NewClients(conn *grpc.ClientConn) *Clients {
	return &Clients{
		Sessions: pb.NewSessionControlServiceClient(conn),
		Rules:    pb.NewForwardRuleControlServiceClient(conn),
		Filters:  pb.NewFilterControlServiceClient(conn),
		AI:       pb.NewAIControlServiceClient(conn),
		System:   pb.NewSystemStatusControlServiceClient(conn),
		Delivery: pb.NewDeliveryControlServiceClient(conn),
		Account:  pb.NewAccountControlServiceClient(conn),
		Backup:   pb.NewBackupControlServiceClient(conn),
		Admin:    pb.NewAdminControlServiceClient(conn),
	}
}

// AttachLogger points the Logs stub at the separate Logger microservice conn.
func AttachLogger(c *Clients, conn *grpc.ClientConn) {
	c.Logs = pb.NewLogControlServiceClient(conn)
}
