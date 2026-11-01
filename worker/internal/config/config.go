// Package config resolves the worker's runtime configuration from the same
// environment variables the Python Core and the Go API already read, so one
// deployment configuration drives every service.
//
// The worker never reads config.yaml: the settings that matter to it are the
// queue knobs the Python side already exports (ATF_QUEUE_*), plus its own
// activation flag (ATF_WORKER_ENABLED) and the Telegram Runtime endpoint.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Config is the worker's resolved configuration.
type Config struct {
	// WorkerEnabled is the strangler-fig switch. Default false: the Python
	// Core keeps its DurableQueueManager loop and the Go worker consumes
	// nothing. Flipping it moves queue ownership without any code change.
	WorkerEnabled bool

	// DatabaseURL (PostgreSQL) takes precedence when set; otherwise the
	// worker opens the same SQLite atf.db the Python Core writes.
	DatabaseURL string
	DBPath      string

	// PostgresAll mirrors ATF_POSTGRES_ALL from the Core composition root:
	// it moves forward_rules (and the other config repositories) to
	// PostgreSQL too. It does NOT move filter_rules — the Core serves that
	// from SQLite unconditionally, and the worker must read it from the
	// same place or read the wrong database.
	// When DatabaseURL is empty this is meaningless: without a PG endpoint
	// there is nothing to move.
	PostgresAll bool

	// Queue knobs, shared 1:1 with the Python worker's ATF_QUEUE_* names.
	NumWorkers      int
	BatchSize       int
	MaxQueueSize    int
	LeaseSeconds    float64
	MaxPerMinutePerTarget int

	// Telegram Runtime (gRPC).
	TelegramRuntimeAddr string
	// Internal RPC token — the same shared secret the Go API already uses
	// to call the Core. Empty in production is a hard failure (see
	// Validate), never silently allowed.
	CoreRPCToken string

	// Media scratch volume, shared between the worker and the runtime.
	MediaDir       string
	MediaQuotaBytes int64

	// gRPC listen address for the worker's own health/metrics surface.
	ListenAddr string

	// Redis, for the cross-replica coordination the Python leases already
	// use. Optional: a single-replica deployment runs without it.
	RedisURL      string
	RedisPassword string
}

// FromEnv reads the environment, applying the Python side's defaults so both
// engines tuned by the same ATF_QUEUE_* values behave the same way.
func FromEnv() (*Config, error) {
	c := &Config{
		WorkerEnabled:          envBool("ATF_WORKER_ENABLED", false),
		DatabaseURL:            os.Getenv("ATF_DATABASE_URL"),
		DBPath:                 envStr("ATF_DB_PATH", "/app/data/atf.db"),
		PostgresAll:            envBool("ATF_POSTGRES_ALL", false),
		NumWorkers:             envInt("ATF_QUEUE_NUM_WORKERS", 4),
		BatchSize:              envInt("ATF_QUEUE_BATCH_SIZE", 5),
		MaxQueueSize:           envInt("ATF_QUEUE_MAX_SIZE", 2000),
		LeaseSeconds:           envFloat("ATF_QUEUE_LEASE_SECONDS", 30.0),
		MaxPerMinutePerTarget:  envInt("ATF_QUEUE_MAX_PER_MINUTE", 0),
		TelegramRuntimeAddr:    envStr("ATF_TELEGRAM_RUNTIME_ADDR", "core:50053"),
		CoreRPCToken:           os.Getenv("ATF_CORE_RPC_TOKEN"),
		MediaDir:               envStr("ATF_MEDIA_DIR", "/tmp/atf-media"),
		MediaQuotaBytes:        envInt64("ATF_MEDIA_QUOTA_BYTES", 4*1024*1024*1024),
		ListenAddr:             envStr("ATF_WORKER_LISTEN", "0.0.0.0:8090"),
		RedisURL:               os.Getenv("ATF_REDIS_URL"),
		RedisPassword:          os.Getenv("ATF_REDIS_PASSWORD"),
	}
	if v := os.Getenv("ATF_REDIS_ADDR"); v != "" && c.RedisURL == "" {
		c.RedisURL = "redis://" + v
	}
	return c, nil
}

// Validate fails closed on the production-critical secrets: an empty RPC
// token with the worker enabled is a refuse-to-start condition, not a
// degraded mode — an unauthenticated internal RPC would let any caller on
// the network drive Telegram sessions.
func (c *Config) Validate() error {
	if !c.WorkerEnabled {
		return nil // disabled workers have nothing to protect
	}
	if strings.TrimSpace(c.CoreRPCToken) == "" {
		return fmt.Errorf("ATF_CORE_RPC_TOKEN is empty: refusing to start the worker with an unauthenticated Telegram Runtime RPC")
	}
	if c.DatabaseURL == "" && strings.TrimSpace(c.DBPath) == "" {
		return fmt.Errorf("no database configured (ATF_DATABASE_URL or ATF_DB_PATH)")
	}
	if c.NumWorkers < 1 || c.NumWorkers > 64 {
		return fmt.Errorf("ATF_QUEUE_NUM_WORKERS must be in [1,64], got %d", c.NumWorkers)
	}
	if c.BatchSize < 1 || c.BatchSize > 50 {
		return fmt.Errorf("ATF_QUEUE_BATCH_SIZE must be in [1,50], got %d", c.BatchSize)
	}
	return nil
}

// DSN returns the SQLite DSN with the busy/locking pragmas the Python side
// relies on (single writer, serialised by the kernel lock, seconds of busy
// wait so a short Python write does not fail a Go claim).
func (c *Config) DSN() string {
	q := urlValues{
		{"_busy_timeout", "10000"},
		{"_journal_mode", "WAL"},
	}
	return c.DBPath + "?" + q.String()
}

func envBool(key string, def bool) bool {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	switch strings.ToLower(v) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	}
	return def
}

func envStr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envInt64(key string, def int64) int64 {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			return n
		}
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if n, err := strconv.ParseFloat(v, 10); err == nil {
			return n
		}
	}
	return def
}

// urlValues builds a query string without importing net/url's escaping
// semantics (SQLite pragmas are plain key=value pairs).
type urlValues [][2]string

func (u urlValues) String() string {
	var sb strings.Builder
	for i, kv := range u {
		if i > 0 {
			sb.WriteByte('&')
		}
		sb.WriteString(kv[0])
		sb.WriteByte('=')
		sb.WriteString(kv[1])
	}
	return sb.String()
}
