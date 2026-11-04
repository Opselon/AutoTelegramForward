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
	"atf/worker/internal/domain/aiconfig"
	"atf/worker/internal/domain/dedup"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/prompt"
	"atf/worker/internal/engine/autoreply"
	"atf/worker/internal/engine/counters"
	"atf/worker/internal/engine/delivery"
	"atf/worker/internal/engine/scheduler"
	"atf/worker/internal/health"
	"atf/worker/internal/infra/aiprovider"
	"atf/worker/internal/infra/aiprovider/circuit"
	"atf/worker/internal/infra/aiprovider/validator"
	autoreplystore "atf/worker/internal/infra/autoreply/store"
	"atf/worker/internal/observability"
	"atf/worker/internal/ops"
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

func run() (runErr error) {
	// Single, structured sink: JSON to stdout, the same format the Rust
	// Logger and the Python Core emit, so one collector handles every
	// service. Fields carry correlation/job/rule ids, never message content
	// or credentials.
	stdoutHandler := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: workerLogLevel(),
	})

	centralLogs, logErr := observability.NewFromEnv()
	log := slog.New(&fanoutHandler{stdout: stdoutHandler, central: centralLogs, module: "worker.main"})
	if logErr != nil {
		log.Warn("central Logger client could not be initialized; stdout logging remains active", "error", logErr)
	} else if centralLogs != nil {
		log.Info("central Logger client initialized", "address", os.Getenv("ATF_LOGGER_ADDR"))
		defer func() {
			if runErr != nil {
				centralLogs.Emit("ERROR", "system", "worker.main", "worker startup or runtime failed", "error", runErr)
			}
			centralLogs.Close()
		}()
	}

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
		"version", version(),
		"commit_sha", CommitSHA,
		"build_info", BuildInfo,
	)

	store, err := openStore(context.Background(), cfg)
	if err != nil {
		return err
	}
	defer store.Close()

	// Stage 4.5 (W3): the event-level replay marker + content fingerprint
	// store. Wired here so the delivery engine's W3 preflight gate can
	// close a replayed event as SENT-without-send; a worker without it
	// keeps the pre-stage behaviour (the gate is simply absent).
	dedupStore, err := openDedupStore(context.Background(), cfg)
	if err != nil {
		return err
	}
	defer dedupStore.Close()

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

	// Stage 4.5 (W2): the forward-every-N cadence store. Same backend
	// selection as every other durable store — the queue's database owns
	// rule_counters on both backends, so the worker reads and advances the
	// SAME rows the Python Core advances. The store is optional only in the
	// engine (a missing port forwards everything); here a startup failure
	// is a startup failure, because a worker that cannot persist a cadence
	// would silently drift from the Python sequence it shares.
	cadenceStore, err := openCountersStore(context.Background(), cfg)
	if err != nil {
		return err
	}
	defer cadenceStore.Close()

	// THE ONE METRICS OWNER (the defect this closes: the engine used to own
	// a private metricsCounters and /healthz read a health.Tracker that
	// counted nothing, so /healthz could report delivered=0 on a worker
	// that had delivered 41 messages). The collector IS delivery.Metrics —
	// the engine reports through it — and the same object feeds /healthz,
	// /livez, /metrics and /ops. No second chain exists anywhere.
	collector := ops.NewCollector(ops.Config{
		WorkerID:       workerID(),
		CommitSHA:      CommitSHA,
		Version:        version(),
		NumWorkers:     cfg.NumWorkers,
		MaxQueueSize:   cfg.MaxQueueSize,
		QueueBackend:   dbLabel(cfg),
		ProcessingMode: processingMode(cfg),
		Switches: ops.Switches{
			WorkerEnabled: cfg.WorkerEnabled,
			Transforms:    cfg.WorkerTransforms,
			Autoreply:     autoreplyEnabled(),
		},
	})
	collector.SetState(ops.StateStarting)

	// Pipeline stage switches: the stages the process owns are enabled, the
	// ones Python owns are disabled. A stage that never runs reports
	// "waiting", a stage whose switch is off reports "disabled" — the two
	// are different operator questions and the panel distinguishes them.
	for _, stage := range ops.StageNames {
		collector.StageEnabled(stage, true)
	}
	if !cfg.WorkerEnabled {
		// An unactivated worker owns no stage of the delivery pipeline:
		// the Python Core executes everything.
		for _, stage := range ops.StageNames {
			collector.StageEnabled(stage, false)
		}
	}

	deliveryLog := &slogLogger{log: log, module: "worker.delivery", ops: collector}
	engine := delivery.New(store, rules, gateway, schedStore, deliveryLog, collector, workerID(), cfg.MediaDir, delivery.Options{
		NumWorkers:            cfg.NumWorkers,
		BatchSize:             cfg.BatchSize,
		LeaseDuration:         time.Duration(cfg.LeaseSeconds * float64(time.Second)),
		MaxQueueSize:          cfg.MaxQueueSize,
		MaxPerMinutePerTarget: cfg.MaxPerMinutePerTarget,
		RPCTimeout:            60 * time.Second,
	})
	engine.SetDedupStore(dedupStore)
	engine.SetCounters(cadenceStore)

	// Stage 5 (W4): the AI rewrite stage. The transformer resolves prompts
	// through the SAME owner-scoped ai_configs store the channel-rewrite and
	// PVA paths use (same ATF_MASTER_KEY-backed AES-GCM), the circuit
	// breaker provides primary->secondary failover, and the validator
	// enforces the length/injection-escape contract on provider output.
	// Without this wiring the stage exists but never runs — the AI toggle
	// on a task silently means nothing (a real defect this closes).
	aiCfgCtx, aiCfgCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer aiCfgCancel()
	aiConfigs, err := openAIConfigStore(aiCfgCtx, cfg)
	if err != nil {
		log.Warn("AI config store unavailable — the AI stage is disabled (provider credentials unreadable)",
			"error", err)
	} else {
		factory := aiprovider.NewFactory()
		transformer := aiprovider.NewTransformer(
			aiConfigs, circuit.NewRegistry(), validator.New(), factory)
		engine.SetAITransformer(transformer, aiConfigs, &aiMetricsCounters{})
		if ps, perr := prompt.NewSQLiteStore(aiCfgCtx, cfg.DSN()); perr == nil {
			engine.SetPromptStore(ps)
		} else {
			log.Warn("prompt template store unavailable — per-target prompt_id resolution falls back to the rule default", "error", perr)
		}
		log.Info("AI rewrite stage wired",
			"prompt_store", "sqlite:shared-atf.db",
			"provider_configs", "owner-scoped")
	}

	// Stage 4.2: the transform-stage strangler switch. The Worker only
	// re-runs the text-transform pipeline when the deployment moved that
	// boundary; a job the Python pipeline already transformed carries its
	// own "do not re-transform" marker, so this is belt-and-braces on top of
	// a per-job contract.
	delivery.SetWorkerOwnsTransforms(cfg.WorkerTransforms)
	log.Info("transform ownership configured", "worker_owns_transforms", cfg.WorkerTransforms)

	// The transform pipeline's decisions ride the same central Logger the
	// delivery events use: one sink, one correlation vocabulary, and the
	// Logger's own bounded-queue backpressure instead of a second client.
	if centralLogs != nil {
		engine.SetDecisionRecorder(&transformDecisionLogger{central: centralLogs, log: log})
	}

	schedulerLog := &slogLogger{log: log, module: "worker.scheduler"}
	schedEngine := scheduler.New(schedStore, gateway, schedulerLog, &schedCounters{}, workerID(), "", scheduler.Defaults())

	// The health surface reads the SAME collector the delivery engine
	// reports through (health.Server takes a Tracker-shaped adapter — see
	// collector_bridge.go — so /healthz's existing contract is preserved
	// while its counters become the real ones). One owner, one listener:
	// /ops attaches to this server's mux.
	tracker := ops.NewTrackerBridge(collector)
	healthSrv := health.NewServer(cfg.ListenAddr, tracker, func(ctx context.Context) error {
		_, err := store.PendingCount(ctx)
		// The probe's outcome feeds the connectivity flag /ops reports, so
		// "db_connected" is answered by the same call that answers
		// readiness — never a second, divergent probe.
		collector.RecordDB(err == nil, time.Now())
		if err != nil {
			return err
		}
		if n, err := store.PendingCount(ctx); err == nil {
			collector.RecordPendingDepth(int64(n))
		}
		return nil
	})

	// /ops: the operational snapshot, served from the same :8090 listener
	// as /healthz. The queue and task readers are the store's own read-only
	// aggregations (queue.StoreStats / queue.TaskRows); the rules join uses
	// the rule store's handle, never a second connection.
	// The ops surface reads the queue through the store's own read-only
	// aggregations: a type assertion keeps the queue.Store interface (the
	// engine's write path) untouched while /ops gets the reader it needs.
	// A store that does not implement it (a test double) yields nil and
	// /ops reports the fields it cannot read — never fake values.
	var queueReader ops.QueueStoreReader
	var taskReader ops.TaskStoreReader
	if reader, ok := store.(ops.QueueStoreReader); ok {
		queueReader = reader
	}
	if reader, ok := store.(ops.TaskStoreReader); ok {
		taskReader = reader
	}
	opsSrv := ops.NewServer(collector, queueReader, taskReader, rulesDBFor(rules), rulesBackendFor(rules), workerID())
	if mux := healthSrv.Mux(); mux != nil {
		opsSrv.Attach(mux)
	}
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

	// Stage 5 (W5): the PVA / auto-reply durable consumer. Same lifecycle
	// as the scheduler: claim loop + bounded pool + maintenance sweep, run
	// until SIGTERM drains in-flight replies. It is only started when the
	// deployment actually moved the PVA boundary (ATF_WORKER_AUTOREPLY),
	// because a Python Core that still owns auto-reply would otherwise see
	// two consumers on one queue — the same single-owner contract the
	// delivery queue has.
	if autoreplyEnabled() {
		pvaWG := &sync.WaitGroup{}
		if err := startAutoreplyConsumer(ctx, log, gateway, cfg, pvaWG); err != nil {
			log.Warn("auto-reply consumer failed to start — Python Core keeps the queue", "error", err)
		}
		defer pvaWG.Wait()
	} else {
		log.Info("auto-reply consumer not started — the Python Core owns the PVA queue (ATF_WORKER_AUTOREPLY)")
	}

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
// Activation is gated on cfg.WorkerEnabled — the same strangler-fig switch the
// Python queue loop consults (durable_queue.py:221). When the flag is unset the
// Python Core owns delivery execution and this worker must NOT claim a single
// job: an ungated claimer would run both engines on one queue at once, which is
// the double-execution hazard the switch exists to prevent. The health surface
// stays up (a disabled worker is "alive, not executing"), and the process exits
// cleanly on SIGTERM — a flip of the flag is a restart away, no migration.
//
// A single claimer rather than N competing claimers: SQLite serializes the
// claim transaction anyway, so competing claimers would only add contention.
// The claimer hands batches to a bounded channel consumed by the workers,
// which gives natural backpressure — a slow Telegram never piles up an
// unbounded in-memory queue of claimed jobs.
func runLoop(ctx context.Context, log *slog.Logger, engine *delivery.Engine, store queue.Store, tracker health.Tracker, cfg *config.Config) error {
	if !config.ClaimsEnabled(cfg) {
		// The disabled worker reports ready (its health surface is up) but
		// deliberately never claims. Logged once at startup and on shutdown so
		// the "why is nothing being delivered" question is answerable from the
		// log alone.
		log.Info("worker is not enabled — the Python Core owns delivery execution (ATF_WORKER_ENABLED)",
			"enabled", false)
		tracker.SetState(health.StateReady)
		<-ctx.Done()
		log.Info("worker stopping (disabled)", "enabled", false)
		return nil
	}
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

// openDedupStore opens the event/content idempotency store against the
// backend that owns the processed_messages and content_dedup tables: the
// primary database (PostgreSQL when configured, otherwise the SQLite atf.db
// the Python Core writes).
//
// A failure here is fatal, matching openStore and openRuleStore: a worker
// that silently ran without its replay-protection gate would look healthy
// while re-delivering every replayed event.
//
// Note on the SQLite path: the processed_messages table is created by the
// Python Core (database.py), not by this worker, so a store that opens
// against a database the Core has not initialized reports "not processed"
// for everything — the same posture the Python pipeline takes when the
// repository is not configured.
func openDedupStore(ctx context.Context, cfg *config.Config) (dedup.Store, error) {
	if cfg.DatabaseURL != "" {
		s, err := dedup.NewPostgres(ctx, cfg.DatabaseURL)
		if err != nil {
			return nil, fmt.Errorf("open dedup store: %w", redactDSN(err))
		}
		return s, nil
	}
	return dedup.NewSQLite(ctx, cfg.DSN())
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

// processingMode reports whether THIS process owns delivery execution
// ("worker") or the Python Core does ("legacy") — the /ops field of the
// same name. It is the strangler-fig switch re-read, nothing more.
func processingMode(cfg *config.Config) string {
	if cfg.WorkerEnabled {
		return "worker"
	}
	return "legacy"
}

// rulesDBFor returns the rule store's forward_rules reader for the /ops
// per-task join (nil when none is configured).
func rulesDBFor(rules *rulestore.Store) *sql.DB {
	if rules == nil {
		return nil
	}
	return rules.OpsDB()
}

// rulesBackendFor labels the rule store's backend so the /ops join picks
// the right placeholder convention.
func rulesBackendFor(rules *rulestore.Store) string {
	if rules == nil {
		return "sqlite"
	}
	return rules.OpsBackend()
}

// versionString reports the product version the worker belongs to: the repo's
// VERSION file, baked in at build time the same way CommitSHA is. Empty means
// "built without the flag"; the startup log says so rather than inventing a
// number, matching the health surface's policy on CommitSHA.
var VersionString string

func version() string {
	if VersionString != "" {
		return VersionString
	}
	return "unset"
}

// --- adapters: keep the engine free of logging/metrics plumbing ------------ //

type metricsCounters struct {
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

func (c *metricsCounters) Outcome(kind string) {
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

func (c *metricsCounters) ObserveRPC(operation string, seconds float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.rpcCount++
	c.rpcSum += seconds
}

func (c *metricsCounters) ObserveWait(seconds float64) {
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

// openCountersStore opens the forward-every-N cadence store (rule_counters)
// on the backend the queue uses — the same table the Python Core advances,
// so the two engines share one sequence per task. The table comes from the
// Core's bootstrap (SQLite migration v27 / initialize_postgres_schema);
// the worker only verifies it exists, never creates production schema.
func openCountersStore(ctx context.Context, cfg *config.Config) (counters.Store, error) {
	if cfg.DatabaseURL != "" {
		conn, err := sql.Open("pgx", cfg.DatabaseURL)
		if err != nil {
			return nil, fmt.Errorf("open postgres counters store: %w", redactDSN(err))
		}
		s, err := counters.NewPostgres(conn)
		if err != nil {
			conn.Close()
			return nil, fmt.Errorf("init postgres counters store: %w", redactDSN(err))
		}
		return s, nil
	}
	conn, err := sql.Open("sqlite3", cfg.DSN())
	if err != nil {
		return nil, fmt.Errorf("open sqlite counters store: %w", err)
	}
	s, err := counters.NewSQLite(conn)
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("init sqlite counters store: %w", err)
	}
	return s, nil
}

// aiMetricsCounters satisfies delivery.AIMetrics: AI stage outcomes are
// observable without coupling the engine to a metrics backend.
type aiMetricsCounters struct{}

func (aiMetricsCounters) AITransform(outcome string) {}
func (aiMetricsCounters) AIFallback(policy string)   {}

type schedCounters struct{}

func (schedCounters) Outcome(kind string)                       {}
func (schedCounters) ObserveDueLatency(seconds float64)         {}
func (schedCounters) ObserveQueueDepth(n int)                   {}
func (schedCounters) ObserveRetry(actionID string, attempt int) {}

// autoreplyEnabled reports whether this worker process owns the PVA /
// auto-reply queue. The Python Core runs its own AutoReplyWorker when this is
// unset, so exactly one consumer owns the queue at a time — the same
// single-owner contract ATF_WORKER_ENABLED applies to delivery jobs.
func autoreplyEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("ATF_WORKER_AUTOREPLY"))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// startAutoreplyConsumer wires the Go PVA consumer to the same lifecycle as
// the delivery engine: bounded pool, per-chat ordering, lease recovery, and a
// drain on SIGTERM. The WaitGroup is handed to the caller so main can wait
// for in-flight replies before exiting (a pending reply is a promise).
func startAutoreplyConsumer(ctx context.Context, log *slog.Logger, gateway telegraph.Gateway, cfg *config.Config, wg *sync.WaitGroup) error {
	// The auto-reply tables are SQLite-only (the Python Core is authoritative
	// for that schema: core/infrastructure/persistence/database.py:677), so
	// the consumer always reads the shared atf.db, never PostgreSQL.
	pvaStore, err := autoreplystore.NewSQLiteStore(ctx, cfg.DSN())
	if err != nil {
		return fmt.Errorf("open autoreply store: %w", err)
	}

	// The AI configs the PVA prompts resolve against are the SAME owner-scoped
	// table the channel-rewrite path uses, with the same at-rest encryption.
	aiStore, err := openAIConfigStore(ctx, cfg)
	if err != nil {
		pvaStore.Close()
		return fmt.Errorf("open ai config store for autoreply: %w", err)
	}

	factory := aiprovider.NewFactory()

	pvaLog := &slogLogger{log: log, module: "worker.autoreply"}
	w := autoreply.NewWorker(pvaStore, aiStore, factory, gateway, workerID(), autoreply.Options{
		Concurrency:  cfg.NumWorkers,
		PollInterval: 1500 * time.Millisecond,
		LeaseSeconds: 45,
	})
	// Structured PVA decisions ride the same sink every other business
	// decision uses: stdout JSON + the central Rust Logger fan-out.
	w.SetLogger(pvaLog)

	wg.Add(1)
	go func() {
		defer wg.Done()
		defer pvaStore.Close()
		log.Info("auto-reply consumer started",
			"module", "worker.autoreply",
			"concurrency", cfg.NumWorkers,
			"db", "sqlite:shared-atf.db")
		if err := w.Run(ctx, 0); err != nil && !errors.Is(err, context.Canceled) {
			log.Warn("auto-reply consumer stopped", "error", err)
		}
		log.Info("auto-reply consumer stopped — in-flight replies drained")
	}()
	return nil
}

// openAIConfigStore opens the owner-scoped AI configuration store the autoreply
// consumer resolves prompts against. It is the SAME table (ai_configs) the
// channel-rewrite path uses, with the SAME at-rest AES-GCM encryption, so a
// user's PVA and their channel rewriting share one credential per provider.
//
// The master key is the deployment's ATF_MASTER_KEY — the same secret the
// Python Core's CryptoService derives from (core/infrastructure/security/
// crypto.py). A worker without the key refuses to decrypt anything rather
// than silently running with a wrong key.
func openAIConfigStore(ctx context.Context, cfg *config.Config) (aiconfig.Store, error) {
	masterKey := os.Getenv("ATF_MASTER_KEY")
	if masterKey == "" {
		return nil, fmt.Errorf("ATF_MASTER_KEY is unset: the AI config store cannot decrypt provider credentials")
	}
	crypto, err := aiconfig.NewCrypto(masterKey)
	if err != nil {
		return nil, fmt.Errorf("init ai config crypto: %w", err)
	}
	// ai_configs is SQLite-only in the Python Core (the repository reads it
	// from the shared atf.db), so the Go side mirrors that placement.
	return aiconfig.NewSQLiteStore(ctx, cfg.DSN(), crypto)
}
