module autoforward

// The last line of defence for "it builds here": the toolchain that produced
// the committed go.sum. A machine on an older patch of the same Go release
// downloads this one instead of silently building with something else.
go 1.27

toolchain go1.27.2

require (
	github.com/redis/go-redis/v9 v9.23.0
	google.golang.org/grpc v1.84.0
	google.golang.org/protobuf v1.36.12
)

require (
	github.com/alicebob/miniredis/v2 v2.40.0 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/yuin/gopher-lua v1.1.1 // indirect
	go.uber.org/atomic v1.12.0 // indirect
	golang.org/x/net v0.61.0 // indirect
	golang.org/x/sys v0.49.0 // indirect
	golang.org/x/text v0.43.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20261012220534-4341edf3f941 // indirect
)
