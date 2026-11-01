package queue

// PostgreSQL integration tests.
//
// These tests run against a REAL PostgreSQL, never a SQLite stand-in: the
// whole point is to prove the claim/fence/mapping SQL is correct against the
// engine the production deployment actually uses — SKIP LOCKED behavior,
// JSONB payload decoding, the DOUBLE PRECISION lease arithmetic and the
// delivery_attempts UPSERT are all things a fake would happily get wrong in
// the same direction as the code under test.
//
// Activation: set ATF_TEST_POSTGRES_DSN to a reachable database the test can
// create a throwaway schema in, e.g.
//
//	ATF_TEST_POSTGRES_DSN=postgres://postgres:test@127.0.0.1:55432/postgres go test ./internal/queue/ -run TestPostgres
//
// The schema is applied per test into a fresh database (created and dropped by
// the test itself), so a run never collides with another run and never touches
// a shared state. When the DSN is unset the suite is skipped — CI runs it
// against a real postgres service (see .github/workflows/ci.yml), and a local
// develop cycle without a database is not a failure.

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/url"
	"fmt"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"atf/worker/internal/domain/job"

	_ "github.com/jackc/pgx/v5/stdlib"
)

const pgSchemaFixture = `
CREATE TABLE IF NOT EXISTS delivery_jobs (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    worker_id TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    intermediate_chat_id TEXT NOT NULL DEFAULT '',
    intermediate_message_id BIGINT,
    delivery_stage TEXT NOT NULL DEFAULT 'DIRECT',
    job_kind TEXT NOT NULL DEFAULT 'forward',
    CONSTRAINT delivery_jobs_unique UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id, job_kind)
);
CREATE INDEX IF NOT EXISTS idx_delivery_jobs_claim
    ON delivery_jobs (status, next_retry_at, lease_until, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_jobs_rule ON delivery_jobs (rule_id);
CREATE TABLE IF NOT EXISTS message_map (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    target_message_id BIGINT,
    media_group_id TEXT NOT NULL DEFAULT '',
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    CONSTRAINT message_map_unique UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id)
);
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    error_category TEXT NOT NULL DEFAULT '',
    dead_lettered_at BIGINT NOT NULL,
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dlq_job ON dead_letter_queue (job_id);
CREATE TABLE IF NOT EXISTS delivery_attempts (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    job_kind TEXT NOT NULL DEFAULT 'forward',
    status TEXT NOT NULL DEFAULT 'CLAIMED',
    worker_id TEXT NOT NULL DEFAULT '',
    target_message_id BIGINT,
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    CONSTRAINT delivery_attempts_unique
        UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id, job_kind)
);
CREATE INDEX IF NOT EXISTS idx_delivery_attempts_fence
    ON delivery_attempts (status, lease_until);
`

// pgTestEnv gates the suite. Skipping (not failing) when it is unset keeps a
// local `go test ./...` green without a database, while CI sets it.
const pgTestEnv = "ATF_TEST_POSTGRES_DSN"

// pgSuite is one throwaway database plus a store opened against it.
type pgSuite struct {
	t        *testing.T
	rootDSN string
	dbName   string
	store    Store
}

// db returns the suite's live connection, for direct row assertions the
// store itself would never make.
func (s *pgSuite) db() *sql.DB { return s.store.(*pgStore).db }

// newPGSuite creates a fresh database, applies the production queue schema,
// and opens the store against it. The database is dropped on cleanup, so a
// failed test never leaves a half-mutated schema behind for the next one.
func newPGSuite(t *testing.T) *pgSuite {
	t.Helper()
	dsn := os.Getenv(pgTestEnv)
	if dsn == "" {
		t.Skipf("%s not set; skipping real-PostgreSQL integration tests", pgTestEnv)
	}
	_, dbName := pgScratchDatabaseName(t)

	root, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatalf("open root pg: %v", err)
	}
	// A database name cannot be a parameter; the name below is generated and
	// validated to contain nothing but [a-z0-9_], so interpolation here is a
	// name, never arbitrary SQL.
	// Drop any leftover database from a previous aborted run (a crashed
	// process does not run Cleanup), then create: idempotent across runs.
	for _, stmt := range []string{
		fmt.Sprintf(`DROP DATABASE IF EXISTS %q WITH (FORCE)`, dbName),
		fmt.Sprintf(`CREATE DATABASE %q`, dbName),
	} {
		if _, err := root.ExecContext(context.Background(), stmt); err != nil {
			root.Close()
			t.Fatalf("prepare scratch db (%s): %v", dbName, err)
		}
	}
	root.Close()

	storeDSN := pgRewriteDatabase(t, dsn, dbName)

	if err := pgApplySchema(context.Background(), storeDSN, pgSchemaFixture); err != nil {
		t.Fatalf("apply schema: %v", err)
	}
	st, err := NewPostgres(context.Background(), storeDSN)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	s := &pgSuite{t: t, rootDSN: dsn, dbName: dbName, store: st}

	t.Cleanup(func() {
		_ = s.store.Close()
		drop, err := sql.Open("pgx", dsn)
		if err == nil {
			_, _ = drop.ExecContext(context.Background(),
				fmt.Sprintf(`DROP DATABASE IF EXISTS %q WITH (FORCE)`, dbName))
			drop.Close()
		}
	})
	return s
}


// pgRewriteDatabase swaps the database in a DSN by parsing it, never by
// string surgery: "postgres://postgres@h/postgres" contains the substring
// "/postgres" twice, and a Replace would corrupt the user (and with it the
// connection) before it ever reached the path.
func pgRewriteDatabase(t *testing.T, dsn, dbName string) string {
	t.Helper()
	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatalf("parse dsn: %v", err)
	}
	u.Path = "/" + dbName
	return u.String()
}

// pgScratchDatabaseName derives a unique, syntactically safe database name
// from the test name.
func pgScratchDatabaseName(t *testing.T) (string, string) {
	name := strings.ToLower(t.Name())
	name = strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			return r
		}
		return '_'
	}, name)
	// PG identifiers max out at 63 bytes; keep room for a uniqueness suffix.
	if len(name) > 40 {
		name = name[len(name)-40:]
	}
	return name, "atftest_" + name
}

// pgApplySchema runs the fixture DDL against the target database.
func pgApplySchema(ctx context.Context, dsn, fixture string) error {
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return err
	}
	defer db.Close()
	if _, err := db.ExecContext(ctx, fixture); err != nil {
		return fmt.Errorf("fixture ddl: %w", err)
	}
	return nil
}

// pgInsert writes a delivery_jobs row directly, bypassing the store, so the
// tests can set up states the store itself would never create.
func pgInsert(t *testing.T, s *pgSuite, j *job.DeliveryJob) {
	t.Helper()
	payload := "{}"
	if raw := j.Payload.Raw(); len(raw) > 0 {
		if out, err := jsonMarshal(raw); err == nil {
			payload = out
		}
	}
	kind := j.JobKind
	if kind == "" {
		kind = "forward"
	}
	_, err := s.db().ExecContext(context.Background(), `INSERT INTO delivery_jobs
		(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json,
		 status, attempts, max_attempts, next_retry_at, lease_until, worker_id,
		 error_detail, created_at, updated_at, intermediate_chat_id,
		 intermediate_message_id, delivery_stage, job_kind)
		VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
		ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status`,
		j.ID, j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID, payload,
		string(j.Status), j.Attempts, j.MaxAttempts, j.NextRetryAt, j.LeaseUntil,
		j.WorkerID, j.ErrorDetail, j.CreatedAt, j.UpdatedAt, j.IntermediateChatID,
		j.IntermediateMsgID, j.DeliveryStage, kind)
	if err != nil {
		t.Fatalf("insert job %s: %v", j.ID, err)
	}
}

func jsonMarshal(v any) (string, error) {
	out, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// pgJob is the minimal constructor the fixtures need: a forward-shaped job
// with a payload the engine can decode.
func pgJob(id string, src int64, target string) *job.DeliveryJob {
	return &job.DeliveryJob{
		ID:              id,
		RuleID:          "r1",
		SourceChatID:    "-100123",
		SourceMessageID: src,
		TargetChatID:    target,
		Status:          job.StatusPending,
		MaxAttempts:     3,
		CreatedAt:       time.Now().Unix(),
		UpdatedAt:       time.Now().Unix(),
		DeliveryStage:   "DIRECT",
		JobKind:         "forward",
	}
}

// pgFenceKey builds the AttemptKey for a job.
func pgFenceKey(j *job.DeliveryJob) job.AttemptKey {
	return job.AttemptKey{
		RuleID:          j.RuleID,
		SourceChatID:    j.SourceChatID,
		SourceMessageID: j.SourceMessageID,
		TargetChatID:    j.TargetChatID,
		JobKind:         j.JobKind,
	}
}

func TestPostgresStartupHealthPing(t *testing.T) {
	s := newPGSuite(t)
	if _, err := s.store.PendingCount(context.Background()); err != nil {
		t.Fatalf("health ping (PendingCount) failed: %v", err)
	}
}

func TestPostgresDatabaseUnavailable(t *testing.T) {
	dsn := os.Getenv(pgTestEnv)
	if dsn == "" {
		t.Skipf("%s not set", pgTestEnv)
	}
	// Point at a port where nothing listens: the driver must fail, and the
	// worker must surface that as a startup error, never as "healthy".
	bad := strings.Replace(dsn, ":5432", ":1", 1)
	if _, err := NewPostgres(context.Background(), bad); err == nil {
		t.Fatal("an unreachable PostgreSQL must fail NewPostgres, not report ready")
	}
}

func TestPostgresClaimsOneEligibleJob(t *testing.T) {
	s := newPGSuite(t)
	pgInsert(t, s, pgJob("j1", 100, "-100999"))

	claimed, err := s.store.ClaimBatch(context.Background(), "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 1 || claimed[0].ID != "j1" {
		t.Fatalf("claim returned %v, want [j1]", claimed)
	}
	if claimed[0].Status != job.StatusClaimed || claimed[0].WorkerID != "w1" {
		t.Fatalf("claim did not mark the row CLAIMED/w1: %+v", claimed[0])
	}
}

func TestPostgresTwoWorkersClaimConcurrently(t *testing.T) {
	s := newPGSuite(t)
	// Eight INDEPENDENT targets: lane isolation must not serialize
	// different destinations, only guard one job per target.
	for i := 0; i < 8; i++ {
		pgInsert(t, s, pgJob(fmt.Sprintf("j%d", i), 100+int64(i),
			fmt.Sprintf("-1002%d", i)))
	}
	// DIFFERENT targets: concurrent claims must not collide on rows.
	var (
		wg      sync.WaitGroup
		got     atomic.Int64
		errors  atomic.Int64
	)
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			for i := 0; i < 16; i++ {
				batch, err := s.store.ClaimBatch(context.Background(), id, 2, 30*time.Second)
				if err != nil {
					errors.Add(1)
					return
				}
				got.Add(int64(len(batch)))
			}
		}(fmt.Sprintf("w%d", w))
	}
	wg.Wait()
	if errors.Load() > 0 {
		t.Fatalf("a concurrent claimer errored")
	}
	if got.Load() != 8 {
		t.Fatalf("concurrent claimers claimed %d rows, want 8 (each row exactly once)", got.Load())
	}
}

func TestPostgresMultipleJobsOneTargetAreSerialized(t *testing.T) {
	s := newPGSuite(t)
	for i := 0; i < 4; i++ {
		pgInsert(t, s, pgJob(fmt.Sprintf("j%d", i), 100+int64(i), "-100300"))
	}
	// A lane holds one in-flight job: a batch claim may return at most one
	// row per target, because the NOT EXISTS predicate removes every job the
	// lane's earliest job dominates.
	batch, err := s.store.ClaimBatch(context.Background(), "w1", 10, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(batch) != 1 {
		t.Fatalf("claim on one target returned %d jobs, want 1 (lane isolation)", len(batch))
	}
	if batch[0].ID != "j0" {
		t.Fatalf("lane claimed %s, want the earliest job j0", batch[0].ID)
	}

	// The next claim still sees only the next job for that lane until the
	// claimed one is resolved.
	batch2, err := s.store.ClaimBatch(context.Background(), "w2", 10, 30*time.Second)
	if err != nil {
		t.Fatalf("second claim: %v", err)
	}
	if len(batch2) != 0 {
		t.Fatalf("second claim on a held lane returned %v, want none", batch2)
	}
}

func TestPostgresIndependentTargetsClaimInParallel(t *testing.T) {
	s := newPGSuite(t)
	for i := 0; i < 4; i++ {
		pgInsert(t, s, pgJob(fmt.Sprintf("j%d", i), 100+int64(i),
			fmt.Sprintf("-100%d", 400+i)))
	}
	batch, err := s.store.ClaimBatch(context.Background(), "w1", 10, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(batch) != 4 {
		t.Fatalf("claims across 4 independent targets returned %d, want 4", len(batch))
	}
}

func TestPostgresSourceMessageOrdering(t *testing.T) {
	// Two jobs, same target, inserted in REVERSE source order; the claim must
	// surface the lower source_message_id first, exactly as the Python claim
	// orders by (source_chat_id, source_message_id, created_at, id).
	s := newPGSuite(t)
	pgInsert(t, s, pgJob("late", 200, "-100500"))
	pgInsert(t, s, pgJob("early", 100, "-100500"))

	batch, err := s.store.ClaimBatch(context.Background(), "w1", 10, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(batch) != 1 || batch[0].ID != "early" {
		t.Fatalf("claim ordering = %v, want [early]", batch)
	}
}

func TestPostgresRetryWindowIsRespected(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-100600")
	j.Status = job.StatusRetryWait
	j.NextRetryAt = float64(time.Now().Add(time.Hour).Unix())
	pgInsert(t, s, j)

	batch, err := s.store.ClaimBatch(context.Background(), "w1", 10, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(batch) != 0 {
		t.Fatalf("a job inside its retry window was claimed: %v", batch)
	}
}

func TestPostgresExpiredLeaseIsReclaimed(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-100700")
	j.Status = job.StatusClaimed
	j.WorkerID = "dead-worker"
	j.LeaseUntil = float64(time.Now().Add(-time.Minute).Unix())
	pgInsert(t, s, j)

	batch, err := s.store.ClaimBatch(context.Background(), "w1", 10, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(batch) != 1 || batch[0].WorkerID != "w1" {
		t.Fatalf("an expired lease was not reclaimed: %v", batch)
	}
}

func TestPostgresConcurrentFenceClaimSingleWinner(t *testing.T) {
	s := newPGSuite(t)
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "-100", SourceMessageID: 5, TargetChatID: "-200"}

	var (
		wg       sync.WaitGroup
		dispatch atomic.Int64
		other    atomic.Int64
	)
	for w := 0; w < 6; w++ {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			out, _, err := s.store.FenceClaim(context.Background(), key, id,
				float64(time.Now().Add(30*time.Second).Unix()))
			if err != nil {
				other.Add(1)
				return
			}
			if out == job.FenceDispatch {
				dispatch.Add(1)
			}
		}(fmt.Sprintf("w%d", w))
	}
	wg.Wait()
	if other.Load() > 0 {
		t.Fatalf("a fence claim errored under contention")
	}
	if dispatch.Load() != 1 {
		t.Fatalf("%d workers were granted a dispatch for one fence, want exactly 1", dispatch.Load())
	}
}

func TestPostgresDuplicateFenceClaimSameWorkerRedispatches(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-100800")
	key := pgFenceKey(j)

	first, _, err := s.store.FenceClaim(context.Background(), key, "w1",
		float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("first claim: %v", err)
	}
	if first != job.FenceDispatch {
		t.Fatalf("first claim = %s, want dispatch", first)
	}
	// CLAIMED by the SAME worker follows the holder: a crash-recovery replay
	// by the same worker identity re-dispatches rather than blocking.
	again, _, err := s.store.FenceClaim(context.Background(), key, "w1",
		float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("reclaim: %v", err)
	}
	if again != job.FenceDispatch {
		t.Fatalf("same-worker reclaim = %s, want dispatch (holder semantics)", again)
	}
}

func TestPostgresFenceHeldByAnotherWorkerIsInFlight(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-100900")
	key := pgFenceKey(j)

	if out, _, err := s.store.FenceClaim(context.Background(), key, "w1",
		float64(time.Now().Add(time.Minute).Unix())); err != nil || out != job.FenceDispatch {
		t.Fatalf("w1 claim: %v (%s)", err, out)
	}
	out, _, err := s.store.FenceClaim(context.Background(), key, "w2",
		float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("w2 claim: %v", err)
	}
	if out != job.FenceInFlight {
		t.Fatalf("a live claim held by another worker = %s, want in_flight", out)
	}
}

func TestPostgresSentFenceNeverRedispatches(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-101000")
	key := pgFenceKey(j)

	if out, _, err := s.store.FenceClaim(context.Background(), key, "w1",
		float64(time.Now().Add(time.Minute).Unix())); err != nil || out != job.FenceDispatch {
		t.Fatalf("claim: %v (%s)", err, out)
	}
	if err := s.store.FenceRecordSent(context.Background(), key, "w1", 4242); err != nil {
		t.Fatalf("record sent: %v", err)
	}
	for _, worker := range []string{"w1", "w2"} {
		out, _, err := s.store.FenceClaim(context.Background(), key, worker,
			float64(time.Now().Add(time.Minute).Unix()))
		if err != nil {
			t.Fatalf("post-SENT claim by %s: %v", worker, err)
		}
		if out != job.FenceAlreadySent {
			t.Fatalf("post-SENT claim by %s = %s, want already_sent", worker, out)
		}
	}
}

func TestPostgresUnknownFenceNeverBlindlyRedispatches(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-101100")
	key := pgFenceKey(j)

	if out, _, err := s.store.FenceClaim(context.Background(), key, "w1",
		float64(time.Now().Add(time.Minute).Unix())); err != nil || out != job.FenceDispatch {
		t.Fatalf("claim: %v (%s)", err, out)
	}
	if err := s.store.FenceRecordOutcome(context.Background(), key, "w1", "UNKNOWN", "rpc timeout"); err != nil {
		t.Fatalf("record outcome: %v", err)
	}
	out, _, err := s.store.FenceClaim(context.Background(), key, "w2",
		float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if out != job.FenceUnknown {
		t.Fatalf("an UNKNOWN fence = %s, want unknown (never blind redispatch)", out)
	}
}

func TestPostgresExplicitUnknownResolutionReopens(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-101200")
	key := pgFenceKey(j)

	if out, _, _ := s.store.FenceClaim(context.Background(), key, "w1",
		float64(time.Now().Add(time.Minute).Unix())); out != job.FenceDispatch {
		t.Fatalf("claim = %s, want dispatch", out)
	}
	if err := s.store.FenceRecordOutcome(context.Background(), key, "w1", "UNKNOWN", "rpc timeout"); err != nil {
		t.Fatalf("record: %v", err)
	}
	if err := s.store.FenceResolveUnknown(context.Background(), key, "w1",
		"operator confirmed no message in target"); err != nil {
		t.Fatalf("resolve: %v", err)
	}
	// The reason is read BEFORE the re-claim: the claim UPSERT blanks
	// last_error (exactly as Python's claim_or_check does), so the audit
	// trail lives on the fence row only until the next dispatch re-arms it.
	var lastError string
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT last_error FROM delivery_attempts WHERE id=$1`, key.String()).Scan(&lastError); err != nil {
		t.Fatalf("read fence: %v", err)
	}
	if !strings.HasPrefix(lastError, "REOPENED:") {
		t.Fatalf("resolution reason = %q, want a REOPENED: prefix", lastError)
	}
	// The resolution left the row FAILED, which the claim UPSERT re-arms.
	out, _, err := s.store.FenceClaim(context.Background(), key, "w2",
		float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("claim after resolve: %v", err)
	}
	if out != job.FenceDispatch {
		t.Fatalf("after explicit resolution = %s, want dispatch", out)
	}
}

func TestPostgresFailedFenceIsReclaimable(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-101300")
	key := pgFenceKey(j)

	if out, _, _ := s.store.FenceClaim(context.Background(), key, "w1",
		float64(time.Now().Add(time.Minute).Unix())); out != job.FenceDispatch {
		t.Fatalf("claim = %s, want dispatch", out)
	}
	if err := s.store.FenceRecordOutcome(context.Background(), key, "w1", "FAILED", "flood"); err != nil {
		t.Fatalf("record: %v", err)
	}
	out, _, err := s.store.FenceClaim(context.Background(), key, "w2",
		float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("reclaim: %v", err)
	}
	if out != job.FenceDispatch {
		t.Fatalf("a FAILED fence = %s, want dispatch (a legitimate retry)", out)
	}
}

func TestPostgresStaleClaimIsExpiredToUnknown(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-101400")
	key := pgFenceKey(j)

	// A claim whose lease is ALREADY past: the holder died (or the clock
	// moved) and the row sits CLAIMED with no live worker behind it.
	if _, _, err := s.store.FenceClaim(context.Background(), key, "gone",
		float64(time.Now().Add(-time.Minute).Unix())); err != nil {
		t.Fatalf("claim: %v", err)
	}
	n, err := s.store.FenceExpireStale(context.Background(), float64(time.Now().Unix()))
	if err != nil {
		t.Fatalf("expire: %v", err)
	}
	if n != 1 {
		t.Fatalf("expired %d rows, want 1", n)
	}
	var status string
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT status FROM delivery_attempts WHERE id=$1`, key.String()).Scan(&status); err != nil {
		t.Fatalf("read: %v", err)
	}
	if status != "UNKNOWN" {
		t.Fatalf("expired claim = %s, want UNKNOWN", status)
	}
}

func TestPostgresMarkRetryDeadLettersWhenExhausted(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-101500")
	j.Attempts = 2
	j.MaxAttempts = 3
	pgInsert(t, s, j)

	// The third attempt exhausts the budget: job FAILED + dead-letter row,
	// written by ONE transaction so the two can never disagree.
	if err := s.store.MarkRetry(context.Background(), "j1", "flood wait", time.Second); err != nil {
		t.Fatalf("mark retry: %v", err)
	}
	var (
		status     string
		attempts   int
		dlqCount   int
		lastError  string
	)
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT status, attempts, error_detail FROM delivery_jobs WHERE id=$1`, "j1").
		Scan(&status, &attempts, &lastError); err != nil {
		t.Fatalf("read job: %v", err)
	}
	if status != "FAILED" || attempts != 3 {
		t.Fatalf("job after exhaustion = %s attempts=%d, want FAILED/3", status, attempts)
	}
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT count(*) FROM dead_letter_queue WHERE job_id=$1`, "j1").Scan(&dlqCount); err != nil {
		t.Fatalf("read dlq: %v", err)
	}
	if dlqCount != 1 {
		t.Fatalf("dead letters = %d, want 1", dlqCount)
	}
	// The dead-letter row carries the attempt count and the reason on the
	// columns the Python reader reads (last_error, not the legacy name).
	var dlqAttempts int
	var dlqReason string
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT attempts, last_error FROM dead_letter_queue WHERE job_id=$1`, "j1").
		Scan(&dlqAttempts, &dlqReason); err != nil {
		t.Fatalf("read dlq row: %v", err)
	}
	if dlqAttempts != 3 || dlqReason != "flood wait" {
		t.Fatalf("dlq row = attempts=%d reason=%q, want 3/flood wait", dlqAttempts, dlqReason)
	}
}

func TestPostgresMarkSentUpdatesMapping(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-101600")
	pgInsert(t, s, j)
	if _, err := s.db().ExecContext(context.Background(),
		`INSERT INTO message_map (id, rule_id, source_chat_id, source_message_id, target_chat_id, target_message_id, delivery_status, created_at, updated_at)
		 VALUES ($1,$2,$3,$4,$5,NULL,'PENDING',$6,$6)`,
		"mm1", j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID, time.Now().Unix()); err != nil {
		t.Fatalf("seed map: %v", err)
	}
	if err := s.store.MarkSent(context.Background(), "j1", 7788); err != nil {
		t.Fatalf("mark sent: %v", err)
	}
	// MarkSent is the job-row mutation (Python's mark_sent touches
	// delivery_jobs only). The mapping row is updated separately by the
	// engine through NoteMapping — pairing them here would make a job whose
	// mapping write failed look undelivered.
	var jobStatus string
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT status FROM delivery_jobs WHERE id=$1`, "j1").Scan(&jobStatus); err != nil {
		t.Fatalf("read job: %v", err)
	}
	if jobStatus != "SENT" {
		t.Fatalf("job = %s, want SENT", jobStatus)
	}
	// The NOTE side of the pair: NoteMapping is what records the target id.
	if err := s.store.NoteMapping(context.Background(), j, job.StatusSent, 7788); err != nil {
		t.Fatalf("note mapping: %v", err)
	}
	var (
		mapStatus string
		mapTarget sql.NullInt64
	)
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT delivery_status, target_message_id FROM message_map
		 WHERE rule_id=$1 AND source_chat_id=$2 AND source_message_id=$3 AND target_chat_id=$4`,
		j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID).Scan(&mapStatus, &mapTarget); err != nil {
		t.Fatalf("read map: %v", err)
	}
	if mapStatus != "SENT" || !mapTarget.Valid || mapTarget.Int64 != 7788 {
		t.Fatalf("map = %s/%v, want SENT/7788", mapStatus, mapTarget)
	}
}

func TestPostgresKeywordReplyDoesNotOverwritePrimaryMapping(t *testing.T) {
	s := newPGSuite(t)
	forward := pgJob("fwd", 100, "-101700")
	forward.JobKind = "forward"
	reply := pgJob("kw", 100, "-101700")
	reply.ID = "kw1"
	reply.JobKind = "keyword_reply"
	pgInsert(t, s, forward)
	pgInsert(t, s, reply)
	if _, err := s.db().ExecContext(context.Background(),
		`INSERT INTO message_map (id, rule_id, source_chat_id, source_message_id, target_chat_id, target_message_id, delivery_status, created_at, updated_at)
		 VALUES ($1,$2,$3,$4,$5,111,'SENT',$6,$6)`,
		"mm1", forward.RuleID, forward.SourceChatID, forward.SourceMessageID, forward.TargetChatID, time.Now().Unix()); err != nil {
		t.Fatalf("seed map: %v", err)
	}
	// A keyword reply shares the mapping's composite key. It must NOT
	// overwrite the primary forward's target id — edit/delete sync follows
	// that id, and overwriting it would point them at the reply's post.
	if err := s.store.NoteMapping(context.Background(), reply, job.StatusSent, 2222); err != nil {
		t.Fatalf("note mapping: %v", err)
	}
	var targetID int64
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT target_message_id FROM message_map
		 WHERE rule_id=$1 AND source_chat_id=$2 AND source_message_id=$3 AND target_chat_id=$4`,
		reply.RuleID, reply.SourceChatID, reply.SourceMessageID, reply.TargetChatID).Scan(&targetID); err != nil {
		t.Fatalf("read map: %v", err)
	}
	if targetID != 111 {
		t.Fatalf("keyword reply overwrote the primary mapping: target_message_id = %d, want 111", targetID)
	}
}

func TestPostgresDeferDoesNotSpendAttempt(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-101800")
	j.Attempts = 1
	pgInsert(t, s, j)

	before := time.Now()
	if err := s.store.Defer(context.Background(), "j1", "Target rate-limited", 2*time.Second); err != nil {
		t.Fatalf("defer: %v", err)
	}
	var (
		status   string
		attempts int
		next     float64
	)
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT status, attempts, next_retry_at FROM delivery_jobs WHERE id=$1`, "j1").
		Scan(&status, &attempts, &next); err != nil {
		t.Fatalf("read: %v", err)
	}
	if status != "RETRY_WAIT" {
		t.Fatalf("defer left status %s, want RETRY_WAIT", status)
	}
	if attempts != 1 {
		t.Fatalf("defer spent an attempt: attempts=%d, want 1 (a defer is not a retry)", attempts)
	}
	if next <= float64(before.Unix()) {
		t.Fatalf("defer did not push next_retry_at into the future")
	}
}

func TestPostgresSplitAlbumProgressSurvivesRestart(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-101900")
	pgInsert(t, s, j)

	// Two album items confirmed; a third still pending. A retry must resend
	// only the pending item, never the confirmed ones.
	if err := s.store.RecordAlbumProgress(context.Background(), "j1",
		map[string]int64{"a": 11, "b": 22}); err != nil {
		t.Fatalf("record progress: %v", err)
	}
	if err := s.store.RecordAlbumProgress(context.Background(), "j1",
		map[string]int64{"c": 33}); err != nil {
		t.Fatalf("record progress 2: %v", err)
	}
	var sent string
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT payload_json -> 'album_sent' FROM delivery_jobs WHERE id=$1`, "j1").Scan(&sent); err != nil {
		t.Fatalf("read payload: %v", err)
	}
	// The merged map keeps every confirmed item: album_sent is additive, and
	// a later progress write must never drop an earlier confirmation.
	for _, id := range []string{`"a"`, `"b"`, `"c"`} {
		if !strings.Contains(sent, id) {
			t.Fatalf("album progress lost item %s: payload=%s", id, sent)
		}
	}
}

func TestPostgresTransactionRollbackDoesNotCommit(t *testing.T) {
	// The claim must not leave half-committed state when the transaction
	// itself fails. Verified negatively: an artificially broken statement
	// inside a claim-free path is not the point; the point is that the atomic
	// claim SQL is a single statement, so a failure leaves the row unclaimed.
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-102000")
	pgInsert(t, s, j)

	// Take the lease and let it expire, then claim with a cancelled context:
	// the claim must fail AND the row must remain claimable afterwards.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := s.store.ClaimBatch(ctx, "w1", 5, 30*time.Second); err == nil {
		t.Fatal("a cancelled claim context must produce an error, not silent success")
	}
	batch, err := s.store.ClaimBatch(context.Background(), "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim after failed claim: %v", err)
	}
	if len(batch) != 1 {
		t.Fatalf("row was left unclaimable after a failed claim: got %v", batch)
	}
}

func TestPostgresMultipleProcessesShareOneDatabase(t *testing.T) {
	// Two independent *pgStore handles (= two worker processes) over the same
	// database: no double-claim, no lost work.
	s := newPGSuite(t)
	dsn := os.Getenv(pgTestEnv)
	if dsn == "" {
		t.Skip("no dsn")
	}
	dsn2 := pgRewriteDatabase(t, dsn, s.dbName)
	other, err := NewPostgres(context.Background(), dsn2)
	if err != nil {
		t.Fatalf("open second store: %v", err)
	}
	defer other.Close()

	for i := 0; i < 6; i++ {
		pgInsert(t, s, pgJob(fmt.Sprintf("j%d", i), 100+int64(i),
			fmt.Sprintf("-103%d", 100+i)))
	}
	b1, err := s.store.ClaimBatch(context.Background(), "procA", 10, 30*time.Second)
	if err != nil {
		t.Fatalf("procA claim: %v", err)
	}
	b2, err := other.ClaimBatch(context.Background(), "procB", 10, 30*time.Second)
	if err != nil {
		t.Fatalf("procB claim: %v", err)
	}
	seen := map[string]bool{}
	for _, j := range append(b1, b2...) {
		if seen[j.ID] {
			t.Fatalf("job %s was claimed by BOTH processes", j.ID)
		}
		seen[j.ID] = true
	}
	if len(seen) != 6 {
		t.Fatalf("two processes claimed %d distinct jobs, want 6", len(seen))
	}
}

// TestPostgresPythonEnqueueGoConsume proves the byte-level contract: a row
// written the way the Python pipeline writes it is claimed and decoded by the
// Go worker. The row is inserted with the exact column set and JSONB payload
// the Python enqueue uses, so a drift in either engine's encoding shows up as
// a failed claim or a missing payload field.
func TestPostgresPythonEnqueueGoConsume(t *testing.T) {
	s := newPGSuite(t)
	now := time.Now().Unix()
	_, err := s.db().ExecContext(context.Background(), `
		INSERT INTO delivery_jobs (
			id, rule_id, source_chat_id, source_message_id, target_chat_id,
			payload_json, status, attempts, max_attempts, next_retry_at,
			lease_until, worker_id, error_detail, created_at, updated_at,
			intermediate_chat_id, intermediate_message_id, delivery_stage, job_kind)
		VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'PENDING', 0, 3, 0, 0, '', '', $7, $7, '', NULL, 'DIRECT', 'forward')`,
		"pyjob1", "rule-py", "-100555", 9001, "-100666",
		`{"text":"hello from python","forward_mode":"COPY_MESSAGE","content_mode":"AUTO"}`, now)
	if err != nil {
		t.Fatalf("python-style insert: %v", err)
	}
	batch, err := s.store.ClaimBatch(context.Background(), "go-worker", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("go claim: %v", err)
	}
	if len(batch) != 1 || batch[0].ID != "pyjob1" {
		t.Fatalf("go worker could not consume a python-enqueued job: %v", batch)
	}
	if batch[0].Payload.Text() != "hello from python" {
		t.Fatalf("payload text = %q, the JSONB payload was not decoded", batch[0].Payload.Text())
	}
	if batch[0].RuleID != "rule-py" || batch[0].JobKind != "forward" {
		t.Fatalf("row mapping wrong: %+v", batch[0])
	}
}

// TestPostgresGoStatePythonFenceRead proves the fence row the Go worker
// writes is the row the Python reader interprets: status SENT with a target
// message id reads back as already_sent through the Go claim path (the same
// SELECT the Python claim_or_check runs) and carries the recorded target id.
func TestPostgresGoStatePythonFenceRead(t *testing.T) {
	s := newPGSuite(t)
	j := pgJob("j1", 100, "-104000")
	key := pgFenceKey(j)

	out, _, err := s.store.FenceClaim(context.Background(), key, "go-w", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil || out != job.FenceDispatch {
		t.Fatalf("claim: %v (%s)", err, out)
	}
	if err := s.store.FenceRecordSent(context.Background(), key, "go-w", 5599); err != nil {
		t.Fatalf("record sent: %v", err)
	}
	// The exact projection the Python claim_or_check reads.
	var (
		status  string
		holder  string
		target  sql.NullInt64
	)
	if err := s.db().QueryRowContext(context.Background(),
		`SELECT status, worker_id, target_message_id FROM delivery_attempts WHERE id=$1`,
		key.String()).Scan(&status, &holder, &target); err != nil {
		t.Fatalf("fence read: %v", err)
	}
	if status != "SENT" || !target.Valid || target.Int64 != 5599 {
		t.Fatalf("fence row = %s/%v, Python would not see the confirmed send", status, target)
	}
	// And re-claiming observes already_sent, the decision Python's
	// claim_or_check makes from the same row.
	if out, _, err := s.store.FenceClaim(context.Background(), key, "go-w",
		float64(time.Now().Add(time.Minute).Unix())); err != nil || out != job.FenceAlreadySent {
		t.Fatalf("post-SENT claim = %v (%s), want already_sent", err, out)
	}
}
