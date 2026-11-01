// Command worker is the Go execution engine of AutoTelegramForward.
//
// It consumes the existing durable delivery queue (delivery_jobs) and
// executes deliveries through the Python Telegram Runtime over the versioned
// TelegramGateway gRPC contract.
//
// Activation is deliberately opt-in: with ATF_WORKER_ENABLED unset (the
// default) the Python Core keeps its DurableQueueManager loop and this
// worker consumes nothing. That is the strangler-fig guarantee — one
// authoritative execution path at a time, and a rollback is a config flip.
//
// Graceful shutdown: SIGTERM/SIGINT stop the claim loop, wait for in-flight
// deliveries to finish (bounded by the shutdown deadline), then release the
// store. A job whose delivery did not finish in time keeps its CLAIMED state
// and is reclaimed by another worker once its lease expires — the fence
// (delivery_attempts) is what guarantees that re-claim cannot double-send.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"atf/worker/internal/config"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/engine/delivery"
	"atf/worker/internal/engine/scheduler"
	"atf/worker/internal/health"
	"atf/worker/internal/queue"
	"atf/worker/internal/rulestore"
	"atf/worker/internal/telegraph"
	"atf/worker/internal/telegraph/fakegateway"
	"atf/worker/internal/telegraph/grpcclient"
)

// ContractVersion is the Telegram Gateway contract this build speaks. It
// must match the `<<contract: tfgw/v1>>` line in worker/proto.
const ContractVersion = "tfgw/v1"

// CommitSHA is baked in at build time (-ldflags -X main.CommitSHA=...) so a
// running worker can say exactly which revision it is, the same way the Go
// API does. Empty means "built without the flag"; the health surface reports
// that rather than inventing a version.
var (
	CommitSHA string
	BuildInfo string
)

func main() {
	// `atf-worker healthcheck` is the container's HEALTHCHECK command: it
	// probes the local health surface and exits with the conventional status.
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		addr := os.Getenv("ATF_WORKER_LISTEN")
		if addr == "" {
			addr = "0.0.0.0:8090"
		}
		os.Exit(probeHealth(addr))
	}
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "worker: %v\n", err)
		os.Exit(1)
	}
}

// probeHealth exits 0 only when the worker reports itself ready to execute
// Telegram work — a process that is merely alive is not enough (its delivery
// store may be unreachable, in which case it must not receive traffic).
func probeHealth(addr string) int {
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get("http://" + addr + "/healthz")
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func run() error {
	// Single, structured sink: JSON to stdout, the same format the Rust
	// Logger and the Python Core emit, so one collector handles every
	// service. Fields carry correlation/job/rule ids, never message content
	// or credentials.
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))

	cfg, err := config.FromEnv()
	if err != nil {
		return err
	}
	if err := cfg.Validate(); err != nil {
		// A disabled worker never starts its engines at all: no dial, no
		// claim loop, nothing to roll back.
		return err
	}

	log.Info("worker starting",
		"enabled", cfg.WorkerEnabled,
		"num_workers", cfg.NumWorkers,
		"batch_size", cfg.BatchSize,
		"lease_seconds", cfg.LeaseSeconds,
		"db", dbLabel(cfg),
		"telegram_runtime", cfg.TelegramRuntimeAddr,
		"contract", ContractVersion,
	)

	store, err := openStore(context.Background(), cfg)
	if err != nil {
		return err
	}
	defer store.Close()

	// The Telegram Gateway: the gRPC client in production, the deterministic
	// fake when the worker is explicitly told to run without a runtime
	// (integration tests, shadow-mode control planes). The fake never
	// produces a "successful real delivery": it is test-only plumbing.
	var gateway telegraph.Gateway
	switch {
	case os.Getenv("ATF_WORKER_FAKE_GATEWAY") != "":
		gateway = fakegateway.New(os.Getenv("ATF_WORKER_FAKE_GATEWAY"))
		log.Warn("running with the TEST-ONLY fake Telegram gateway — no real Telegram operations will happen")
	default:
		client, err := grpcclient.New(cfg.TelegramRuntimeAddr, cfg.CoreRPCToken, workerID(), 60*time.Second)
		if err != nil {
			return err
		}
		defer client.Close()
		gateway = client
	}

	rules, err := openRuleStore(context.Background(), cfg)
	if err != nil {
		return err
	}
	defer rules.Close()

	schedStore, err := openSchedulerStore(context.Background(), cfg)
	if err != nil {
		return err
	}
	defer schedStore.Close()

	engine := delivery.New(store, rules, gateway, schedStore, &slogLogger{log}, &counters{}, workerID(), cfg.MediaDir, delivery.Options{
		NumWorkers:            cfg.NumWorkers,
		BatchSize:             cfg.BatchSize,
		LeaseDuration:         time.Duration(cfg.LeaseSeconds * float64(time.Second)),
		MaxQueueSize:          cfg.MaxQueueSize,
		MaxPerMinutePerTarget: cfg.MaxPerMinutePerTarget,
		RPCTimeout:            60 * time.Second,
	})

	schedEngine := scheduler.New(schedStore, gateway, &slogLogger{log}, &schedCounters{}, workerID(), "", scheduler.Defaults())

	tracker := health.NewTracker()
	healthSrv := health.NewServer(cfg.ListenAddr, tracker, func(ctx context.Context) error {
		_, err := store.PendingCount(ctx)
		return err
	})
	go func() {
		if err := healthSrv.Start(); err != nil && !errors.Is(err, net.ErrClosed) {
			log.Warn("health server stopped", "error", err)
		}
	}()

	// SIGTERM / SIGINT: stop claiming, drain in-flight, exit cleanly. The
	// shutdown grace is bounded so a stuck delivery cannot hold a rolling
	// restart forever; the lease + fence make that cut safe.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	var schedWG sync.WaitGroup
	schedWG.Add(1)
	go func() {
		defer schedWG.Done()
		if err := schedEngine.Run(ctx); err != nil {
			log.Warn("scheduler engine stopped", "error", err)
		}
	}()

	err = runLoop(ctx, log, engine, store, tracker, cfg)
	schedWG.Wait()
	log.Info("worker stopped")
	if err != nil {
		return err
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := healthSrv.Shutdown(shutdownCtx); err != nil {
		log.Warn("health server shutdown", "error", err)
	}
	return nil
}

// runLoop is the claim loop: N bounded goroutines, one shared claimer.
//
// A single claimer rather than N competing claimers: SQLite serializes the
// claim transaction anyway, so competing claimers would only add contention.
// The claimer hands batches to a bounded channel consumed by the workers,
// which gives natural backpressure — a slow Telegram never piles up an
// unbounded in-memory queue of claimed jobs.
func runLoop(ctx context.Context, log *slog.Logger, engine *delivery.Engine, store queue.Store, tracker *health.Tracker, cfg *config.Config) error {
	batches := make(chan []*job.DeliveryJob, cfg.NumWorkers)
	var wg sync.WaitGroup
	tracker.SetState(health.StateStarting)

	// Workers: bounded goroutines, each consuming claimed batches.
	for i := 0; i < cfg.NumWorkers; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for batch := range batches {
				for _, j := range batch {
					select {
					case <-ctx.Done():
						// Shutdown mid-batch: the job stays CLAIMED and its
						// lease expires; another worker re-claims it. The
						// fence guarantees that re-claim cannot double-send.
						return
					default:
					}
					tracker.Enter()
					if err := engine.Process(ctx, j); err != nil {
						log.Error("engine could not process job",
							"job_id", j.ID, "rule_id", j.RuleID, "error", err)
						tracker.Outcome("failed")
					}
					tracker.Leave()
				}
			}
		}(i)
	}
	tracker.SetState(health.StateReady)

	// Claimer: one transactional claim per poll interval, backoff when idle.
	claimErr := make(chan error, 1)
	go func() {
		defer close(batches)
		defer close(claimErr)
		idle := 100 * time.Millisecond
		for {
			select {
			case <-ctx.Done():
				return
			default:
			}
			batch, err := store.ClaimBatch(ctx, workerID(), cfg.BatchSize,
				time.Duration(cfg.LeaseSeconds*float64(time.Second)))
			if err != nil {
				// A database outage is a *degraded* condition, not a crash:
				// the jobs stay durable, the claim retries. The health
				// surface reports it through the readiness probe.
				tracker.SetState(health.StateDegraded)
				log.Error("claim failed — retrying", "error", err)
				select {
				case claimErr <- err:
				case <-ctx.Done():
					return
				}
				time.Sleep(time.Second)
				continue
			}
			if len(batch) == 0 {
				// Idle: back off so an empty queue does not busy-spin.
				select {
				case <-time.After(idle):
				case <-ctx.Done():
					return
				}
				continue
			}
			if tracker.State() == health.StateDegraded {
				tracker.SetState(health.StateReady)
			}
			select {
			case batches <- batch:
			case <-ctx.Done():
				return
			}
		}
	}()

	// The reclaim / fence sweep: expired leases return to the claimable set,
	// and CLAIMED fence rows whose lease passed become UNKNOWN (never
	// "dispatch by default": the send may have landed). Bounded interval,
	// best-effort: a sweep failure only delays recovery, never loses work.
	var sweepWG sync.WaitGroup
	sweepWG.Add(1)
	go func() {
		defer sweepWG.Done()
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				n, err := store.FenceExpireStale(ctx, float64(time.Now().Unix()))
				if err != nil {
					log.Warn("fence sweep failed", "error", err)
					continue
				}
				if n > 0 {
					log.Warn("fence sweep moved expired claims to UNKNOWN", "count", n)
				}
			}
		}
	}()

	// Wait for shutdown: stop the claimer first (close(batches) drains the
	// workers), then wait for the in-flight deliveries.
	<-ctx.Done()
	tracker.SetState(health.StateStopping)
	log.Info("shutdown received — draining in-flight deliveries")
	sweepWG.Wait()
	select {
	case err := <-claimErr:
		log.Warn("last claim loop error", "error", err)
	default:
	}
	wg.Wait()
	return nil
}

// openStore opens the durable queue. PostgreSQL takes precedence when
// configured (the production deployment model), otherwise the same SQLite
// atf.db the Python Core writes.
func openStore(ctx context.Context, cfg *config.Config) (queue.Store, error) {
	if cfg.DatabaseURL != "" {
		s, err := queue.NewPostgres(ctx, cfg.DatabaseURL)
		if err != nil {
			return nil, fmt.Errorf("open delivery store: %w", redactDSN(err))
		}
		return s, nil
	}
	return queue.NewSQLite(ctx, cfg.DSN())
}

// openRuleStore builds the worker's read-only view of task state: the real
// forward_rules row plus the filter_rules row it references, each from the
// backend that actually owns it.
//
// The ownership split mirrors the Core composition root exactly (see
// docs/architecture/data-ownership.md):
//
//   - delivery data follows ATF_DATABASE_URL (the delivery store above);
//   - forward_rules follows ATF_POSTGRES_ALL — PostgreSQL only when the
//     deployment opted the config repositories into it, SQLite otherwise;
//   - filter_rules is ALWAYS SQLite under the current Core, so the worker
//     opens the shared atf.db for it even when everything else is PG.
//
// The consequence: with ATF_DATABASE_URL set and ATF_POSTGRES_ALL unset the
// rule store holds TWO databases (PG forward_rules + SQLite filter_rules).
// That is deliberate, not an oversight — a worker that read filter_rules
// from PostgreSQL would be reading a table that does not exist there.
func openRuleStore(ctx context.Context, cfg *config.Config) (*rulestore.Store, error) {
	rsCfg := rulestore.Config{
		// filter_rules is ALWAYS SQLite under the current Core, and the
		// Core initializes atf.db even when PostgreSQL carries the durable
		// queue — so the SQLite DSN is set unconditionally.
		SQLite:      rulestore.DSN{Raw: cfg.DSN()},
		Postgres:    rulestore.DSN{Raw: cfg.DatabaseURL},
		PostgresAll: cfg.PostgresAll,
	}
	store, err := rulestore.New(ctx, rsCfg)
	if err != nil {
		return nil, fmt.Errorf("open rule store: %w", redactDSN(err))
	}
	return store, nil
}

// redactDSN strips credentials from any DSN embedded in an error before
// it reaches a log or the process's exit status, so a startup failure never
// leaks the database password it is complaining about.
func redactDSN(err error) error {
	msg := err.Error()
	// scheme://user:pass@host/db → scheme://host/db
	if i := strings.Index(msg, "://"); i >= 0 {
		rest := msg[i+3:]
		if at := strings.Index(rest, "@"); at >= 0 {
			msg = msg[:i+3] + rest[at+1:]
		}
	}
	return errors.New(msg)
}

// workerID is this process's identity in the queue + fence. Hostname-prefixed
// so two replicas on one host still differ.
func workerID() string {
	host, err := os.Hostname()
	if err != nil || host == "" {
		host = "worker"
	}
	return fmt.Sprintf("%s-%d", host, os.Getpid())
}

// dbLabel describes the store for the startup log without leaking a DSN's
// embedded credentials.
func dbLabel(cfg *config.Config) string {
	if cfg.DatabaseURL != "" {
		return "postgres"
	}
	return "sqlite:" + cfg.DBPath
}

// --- adapters: keep the engine free of logging/metrics plumbing ------------ //

type slogLogger struct{ log *slog.Logger }

func (l *slogLogger) Info(msg string, args ...any)  { l.log.Info(msg, args...) }
func (l *slogLogger) Warn(msg string, args ...any)  { l.log.Warn(msg, args...) }
func (l *slogLogger) Error(msg string, args ...any) { l.log.Error(msg, args...) }

type counters struct {
	mu        sync.Mutex
	sent      int64
	retried   int64
	failed    int64
	unknown   int64
	rpcCount  int64
	rpcSum    float64
	waitSum   float64
	waitCount int64
}

func (c *counters) Outcome(kind string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	switch kind {
	case "sent":
		c.sent++
	case "retried":
		c.retried++
	case "failed":
		c.failed++
	case "unknown":
		c.unknown++
	}
}

func (c *counters) ObserveRPC(operation string, seconds float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.rpcCount++
	c.rpcSum += seconds
}

func (c *counters) ObserveWait(seconds float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if seconds > 0 && !math.IsNaN(seconds) {
		c.waitCount++
		c.waitSum += seconds
	}
}

// openSchedulerStore opens the durable scheduled_actions store against the
// same database the delivery queue uses (PostgreSQL when configured,
// otherwise SQLite atf.db).
func openSchedulerStore(ctx context.Context, cfg *config.Config) (scheduler.Store, error) {
	if cfg.DatabaseURL != "" {
		conn, err := sql.Open("pgx", cfg.DatabaseURL)
		if err != nil {
			return nil, fmt.Errorf("open postgres scheduler store: %w", redactDSN(err))
		}
		s, err := scheduler.NewPostgres(conn)
		if err != nil {
			conn.Close()
			return nil, fmt.Errorf("init postgres scheduler store: %w", redactDSN(err))
		}
		return s, nil
	}
	conn, err := sql.Open("sqlite3", cfg.DSN())
	if err != nil {
		return nil, fmt.Errorf("open sqlite scheduler store: %w", err)
	}
	s, err := scheduler.NewSQLite(conn)
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("init sqlite scheduler store: %w", err)
	}
	return s, nil
}

type schedCounters struct{}

func (schedCounters) Outcome(kind string)                       {}
func (schedCounters) ObserveDueLatency(seconds float64)         {}
func (schedCounters) ObserveQueueDepth(n int)                   {}
func (schedCounters) ObserveRetry(actionID string, attempt int) {}
