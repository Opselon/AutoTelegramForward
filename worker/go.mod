module atf/worker

// The last line of defence for "it builds here": the toolchain that produced
// the committed go.sum. A machine on an older patch of the same Go release
// downloads this one instead of silently building with something else.
go 1.26.0

toolchain go1.27.2

require (
	github.com/jackc/pgx/v5 v5.12.0
	github.com/mattn/go-sqlite3 v1.14.32
	golang.org/x/text v0.43.0
	google.golang.org/grpc v1.84.0
	google.golang.org/protobuf v1.36.12
)

require (
	github.com/jackc/pgpassfile v1.0.0 // indirect
	github.com/jackc/pgservicefile v0.0.0-20240606120523-5a60cdf6a761 // indirect
	github.com/jackc/puddle/v2 v2.2.3 // indirect
	golang.org/x/net v0.57.0 // indirect
	golang.org/x/sync v0.24.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260706201446-f0a921348800 // indirect
)
