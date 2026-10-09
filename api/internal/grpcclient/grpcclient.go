package grpcclient

import (
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	pb "autoforward/proto"
)

// Dial connects to the Python core's gRPC server.
func Dial(addr string) (*grpc.ClientConn, error) {
	return grpc.NewClient(addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithTimeout(5*time.Second),
	)
}

// Clients bundles all typed service stubs.
type Clients struct {
	Sessions   pb.SessionControlServiceClient
	Rules      pb.ForwardRuleControlServiceClient
	Filters    pb.FilterControlServiceClient
	AI         pb.AIControlServiceClient
	System     pb.SystemStatusControlServiceClient
	Logs       pb.LogControlServiceClient
	Delivery   pb.DeliveryControlServiceClient
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
	}
}

// AttachLogger points the Logs stub at the separate Logger microservice conn.
func AttachLogger(c *Clients, conn *grpc.ClientConn) {
	c.Logs = pb.NewLogControlServiceClient(conn)
}
