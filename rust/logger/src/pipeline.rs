//! Bounded admission, batch formation, backpressure, drain and the ack path.
//!
//! This is where "bounded memory" and "controlled concurrency" (priority 2 in the
//! mission order) are actually enforced, and where the acknowledgement contract is
//! kept honest:
//!
//! * **Bounded by construction.** Each lane owns a fixed-capacity `VecDeque` inside
//!   one mutex. There is no unbounded channel and no growth path: when a lane is
//!   full the record is *refused and counted* (`events_rejected{reason="overload"}`,
//!   `queue_full_total{lane}`), never queued "just this once". A refused record is
//!   never silently lost — the producer's RPC answers `success=false` saying why.
//! * **One writer path, batched.** Records are grouped into batches of at most
//!   `batch_size` and committed in a single transaction. `batch_delay_ms` bounds how
//!   long the first record of a batch waits for companions, so a lonely record is
//!   not held hostage by batching.
//! * **Acks that match reality.** In [`AckMode::Queued`] `success=true` means
//!   "admitted to the bounded queue"; in [`AckMode::Durable`] it means the batch
//!   containing the record **committed**, and the sender is only answered after the
//!   storage call returned `Ok`. A dropped ack channel is a failure, not a success.
//! * **Explicit loss accounting.** `received` → `accepted`/`rejected` →
//!   `persisted`/`dropped`. A record that is admitted and then fails in storage is
//!   counted in `events_dropped{reason="storage"}` and its producer is told.
//! * **Graceful drain.** `begin_drain` refuses new work, the writers keep flushing
//!   until both lanes are empty, and `wait_drained` reports how much was left
//!   (counted as `drain_state` and, if the deadline expired, honestly as dropped).

use crate::config::{normal_lane_capacity, AckMode, Config};
use crate::ingest::{dedupe_key, Prepared, Severity};
use crate::metrics::Metrics;
use crate::storage::{insert_batch_with_retry, Record, Storage, StorageError};
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tokio::sync::oneshot;

/// Which lane a record is admitted to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lane {
    /// Everything that is not a failure.
    Normal,
    /// `ERROR`/`CRITICAL`: reserved capacity so an escalation cannot be crowded out
    /// by a burst of chatty records.
    Priority,
}

impl Lane {
    pub fn as_str(self) -> &'static str {
        match self {
            Lane::Normal => "normal",
            Lane::Priority => "priority",
        }
    }
}

/// Why a record was not admitted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AdmitError {
    /// The lane is at capacity. Backpressure, and it is counted.
    Full {
        lane: Lane,
        depth: usize,
        capacity: usize,
    },
    /// The pipeline is draining or stopped and refuses new work.
    Shutdown,
    /// A duplicate inside the configured dedupe window. Counted in
    /// `events_duplicate`; reported to the producer rather than silently accepted,
    /// because `success=true` must mean "this record is stored".
    Duplicate { window_secs: u64 },
}

impl AdmitError {
    pub fn code(&self) -> &'static str {
        match self {
            AdmitError::Full { .. } => "OVERLOAD",
            AdmitError::Shutdown => "SHUTTING_DOWN",
            AdmitError::Duplicate { .. } => "DUPLICATE",
        }
    }

    pub fn message(&self) -> String {
        match self {
            AdmitError::Full {
                lane,
                depth,
                capacity,
            } => format!(
                "logger is at capacity ({} lane: {depth}/{capacity} queued); \
                 record refused rather than buffered without bound",
                lane.as_str()
            ),
            AdmitError::Shutdown => {
                "logger is shutting down and not accepting new records".to_string()
            }
            AdmitError::Duplicate { window_secs } => format!(
                "duplicate record suppressed (dedupe window {window_secs}s); set \
                 ATF_LOGGER_DEDUPE_WINDOW_SECS=0 to store every record"
            ),
        }
    }
}

/// What the caller must wait for before it may answer `success=true`.
#[derive(Debug)]
pub enum Ack {
    /// Queued mode: the record is in the bounded queue, nothing to wait for.
    Immediate,
    /// Durable mode: the writer will report the storage outcome of the batch.
    Pending(oneshot::Receiver<Result<(), StorageError>>),
}

/// One admitted record, with the channel that answers its producer.
#[derive(Debug)]
struct Item {
    record: Record,
    admitted: Instant,
    ack: Option<oneshot::Sender<Result<(), StorageError>>>,
}

impl Item {
    fn wait_seconds(&self) -> f64 {
        self.admitted.elapsed().as_secs_f64()
    }
}

#[derive(Debug, Default)]
struct QueueState {
    normal: VecDeque<Item>,
    priority: VecDeque<Item>,
    /// Set when the pipeline stops accepting; writers keep draining.
    closed: bool,
}

impl QueueState {
    fn pop_next(&mut self) -> Option<Item> {
        // Priority first, always: a reserved lane that could be overtaken by the
        // flood it exists to escape would be pointless.
        self.priority.pop_front().or_else(|| self.normal.pop_front())
    }

    fn is_empty(&self) -> bool {
        self.normal.is_empty() && self.priority.is_empty()
    }

    fn depth(&self, lane: Lane) -> usize {
        match lane {
            Lane::Normal => self.normal.len(),
            Lane::Priority => self.priority.len(),
        }
    }
}

#[derive(Debug)]
struct Queues {
    state: Mutex<QueueState>,
    ready: Condvar,
    normal_capacity: usize,
    priority_capacity: usize,
}

impl Queues {
    fn new(normal_capacity: usize, priority_capacity: usize) -> Self {
        Queues {
            state: Mutex::new(QueueState::default()),
            ready: Condvar::new(),
            normal_capacity,
            priority_capacity,
        }
    }

    fn capacity(&self, lane: Lane) -> usize {
        match lane {
            Lane::Normal => self.normal_capacity,
            Lane::Priority => self.priority_capacity,
        }
    }

    fn try_push(&self, lane: Lane, item: Item) -> Result<(), AdmitError> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.closed {
            return Err(AdmitError::Shutdown);
        }
        let capacity = self.capacity(lane);
        let queue = match lane {
            Lane::Normal => &mut state.normal,
            Lane::Priority => &mut state.priority,
        };
        if queue.len() >= capacity {
            return Err(AdmitError::Full {
                lane,
                depth: queue.len(),
                capacity,
            });
        }
        queue.push_back(item);
        drop(state);
        self.ready.notify_one();
        Ok(())
    }

    /// Take up to `max` records, blocking for at most `idle_wait` when both lanes
    /// are empty and at most `linger` while waiting for companions of the first
    /// record.
    fn take_batch(&self, max: usize, linger: Duration, idle_wait: Duration) -> Option<Vec<Item>> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.is_empty() {
            let (guard, _) = self
                .ready
                .wait_timeout(state, idle_wait)
                .unwrap_or_else(|e| e.into_inner());
            state = guard;
        }
        let mut batch = Vec::with_capacity(max.min(1024));
        let first = state.pop_next()?;
        batch.push(first);
        let deadline = Instant::now() + linger;
        while batch.len() < max {
            if state.is_empty() {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    break;
                }
                let (guard, _) = self
                    .ready
                    .wait_timeout(state, remaining)
                    .unwrap_or_else(|e| e.into_inner());
                state = guard;
                if state.is_empty() && deadline.saturating_duration_since(Instant::now()).is_zero() {
                    break;
                }
                continue;
            }
            match state.pop_next() {
                Some(item) => batch.push(item),
                None => break,
            }
        }
        if state.is_empty() {
            self.ready.notify_one();
        }
        Some(batch)
    }

    fn close(&self) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.closed = true;
        drop(state);
        self.ready.notify_all();
    }

    fn depth(&self) -> (usize, usize) {
        let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        (state.normal.len(), state.priority.len())
    }

    fn is_empty(&self) -> bool {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .is_empty()
    }
}

/// Optional bounded duplicate suppression.
///
/// Off by default (`ATF_LOGGER_DEDUPE_WINDOW_SECS=0`). It cannot be presented as
/// at-most-once delivery — the store has no unique event id — so a suppressed
/// duplicate is reported to its producer as `success=false` with
/// `error_code=DUPLICATE` rather than being quietly accepted. Counted in
/// `events_duplicate`.
#[derive(Debug)]
struct Dedupe {
    window: Duration,
    capacity: usize,
    seen: HashMap<u64, Instant>,
    order: VecDeque<(u64, Instant)>,
}

/// Upper bound on remembered fingerprints (~100k × 16 bytes). A window longer than
/// this many records degrades to weaker dedupe, never to unbounded memory.
pub const MAX_DEDUPE_ENTRIES: usize = 100_000;

impl Dedupe {
    fn new(window: Duration) -> Self {
        Dedupe {
            window,
            capacity: MAX_DEDUPE_ENTRIES,
            seen: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    fn enabled(&self) -> bool {
        !self.window.is_zero()
    }

    /// Claim a fingerprint: `Err(())` when this record is a duplicate inside the
    /// window, `Ok(key)` when it is new and now remembered.
    ///
    /// The key has to come back because a claim can be undone: a record that fails
    /// admission (a full lane) is retried by the transport, and a retry of a refused
    /// record must not be mistaken for a duplicate of itself.
    fn claim(&mut self, record: &Record, now: Instant) -> Result<u64, ()> {
        if !self.enabled() {
            return Ok(0);
        }
        let key = dedupe_key(record);
        self.evict(now);
        match self.seen.get(&key) {
            Some(first_seen) if now.duration_since(*first_seen) <= self.window => Err(()),
            _ => {
                self.seen.insert(key, now);
                self.order.push_back((key, now));
                while self.order.len() > self.capacity {
                    if let Some((old_key, _)) = self.order.pop_front() {
                        self.seen.remove(&old_key);
                    }
                }
                Ok(key)
            }
        }
    }

    /// Undo a claim for a record that was refused admission after all.
    fn release(&mut self, key: u64) {
        self.seen.remove(&key);
    }

    fn evict(&mut self, now: Instant) {
        while let Some((key, seen_at)) = self.order.front().copied() {
            if now.duration_since(seen_at) <= self.window {
                break;
            }
            self.order.pop_front();
            if self.seen.get(&key) == Some(&seen_at) {
                self.seen.remove(&key);
            }
        }
    }

    fn len(&self) -> usize {
        self.seen.len()
    }
}

/// Pipeline tuning, derived from the validated [`Config`].
#[derive(Debug, Clone)]
pub struct PipelineConfig {
    pub normal_capacity: usize,
    pub priority_capacity: usize,
    pub batch_size: usize,
    pub batch_delay: Duration,
    pub ack_mode: AckMode,
    pub dedupe_window: Duration,
    pub max_retries: u32,
    pub retry_base_ms: u64,
    pub writers: usize,
    pub drain_timeout: Duration,
}

impl PipelineConfig {
    pub fn from_config(config: &Config) -> Self {
        PipelineConfig {
            normal_capacity: normal_lane_capacity(config.queue_capacity, config.priority_capacity),
            priority_capacity: config.priority_capacity.max(1),
            batch_size: config.batch_size.max(1),
            batch_delay: Duration::from_millis(config.batch_delay_ms),
            ack_mode: config.ack_mode,
            dedupe_window: Duration::from_secs(config.dedupe_window_secs),
            max_retries: config.max_retries,
            retry_base_ms: config.retry_base_ms,
            writers: 1,
            drain_timeout: Duration::from_millis(config.drain_timeout_ms),
        }
    }

    /// One writer is the right answer for SQLite: batching, not parallel writers,
    /// raises throughput there (docs/logging-contract.md §4). PostgreSQL accepts
    /// bounded ingestion parallelism because its storage engine is built for it.
    pub fn with_writers(mut self, writers: usize) -> Self {
        self.writers = writers.clamp(1, 8);
        self
    }

    pub fn describe(&self) -> String {
        format!(
            "normal_capacity={} priority_capacity={} batch_size={} batch_delay_ms={} \
             ack_mode={} writers={} dedupe_window_s={} max_retries={}",
            self.normal_capacity,
            self.priority_capacity,
            self.batch_size,
            self.batch_delay.as_millis(),
            self.ack_mode.as_str(),
            self.writers,
            self.dedupe_window.as_secs(),
            self.max_retries
        )
    }
}

/// Outcome of a graceful drain.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DrainOutcome {
    /// True when every admitted record was flushed before the deadline.
    pub drained: bool,
    pub remaining: usize,
    pub drained_records: u64,
    pub elapsed_millis: u64,
}

/// Shared between the public handle and every writer thread.
struct Shared {
    queues: Queues,
    storage: Arc<dyn Storage>,
    metrics: Arc<Metrics>,
    config: PipelineConfig,
    dedupe: Mutex<Dedupe>,
    /// Records admitted but not yet committed.
    pending: AtomicI64,
    /// Batches currently inside storage.
    in_flight_batches: AtomicI64,
    draining: AtomicBool,
    stopped: AtomicBool,
    drained_records: AtomicI64,
}

/// The public pipeline handle. Cheap to clone (`Arc` inside), safe to share.
pub struct Pipeline {
    shared: Arc<Shared>,
    handles: Mutex<Vec<JoinHandle<()>>>,
}

impl Pipeline {
    /// Build the pipeline and start `writers` writer threads.
    pub fn start(
        config: PipelineConfig,
        storage: Arc<dyn Storage>,
        metrics: Arc<Metrics>,
    ) -> Result<Arc<Self>, StorageError> {
        let shared = Arc::new(Shared {
            queues: Queues::new(config.normal_capacity, config.priority_capacity),
            storage: Arc::clone(&storage),
            metrics: Arc::clone(&metrics),
            dedupe: Mutex::new(Dedupe::new(config.dedupe_window)),
            pending: AtomicI64::new(0),
            in_flight_batches: AtomicI64::new(0),
            draining: AtomicBool::new(false),
            stopped: AtomicBool::new(false),
            drained_records: AtomicI64::new(0),
            config: config.clone(),
        });
        // Capacity gauges are set once; they are configuration, not measurements.
        metrics
            .queue_capacity
            .with(Lane::Normal.as_str())
            .add(config.normal_capacity as u64);
        metrics
            .queue_capacity
            .with(Lane::Priority.as_str())
            .add(config.priority_capacity as u64);
        metrics
            .queue_capacity_current
            .set((config.normal_capacity + config.priority_capacity) as i64);

        let mut handles = Vec::with_capacity(config.writers);
        for index in 0..config.writers.max(1) {
            let shared = Arc::clone(&shared);
            let handle = thread::Builder::new()
                .name(format!("atf-logger-writer-{index}"))
                .spawn(move || writer_loop(shared))
                .map_err(|err| StorageError::Config(format!("cannot start writer thread: {err}")))?;
            handles.push(handle);
        }
        metrics.pipeline_running.set(1);
        Ok(Arc::new(Pipeline {
            shared,
            handles: Mutex::new(handles),
        }))
    }

    /// Non-blocking admission. `Ack::Immediate` in queued mode, otherwise the
    /// caller must await the returned receiver before answering `success=true`.
    pub fn admit(&self, prepared: Prepared) -> Result<Ack, AdmitError> {
        let lane = classify(&prepared);
        let now = Instant::now();
        let dedupe_window_secs = self.shared.config.dedupe_window.as_secs();
        let claimed = {
            let mut dedupe = self.shared.dedupe.lock().unwrap_or_else(|e| e.into_inner());
            match dedupe.claim(&prepared.record, now) {
                Ok(key) => key,
                Err(()) => {
                    self.shared.metrics.events_duplicate.inc();
                    return Err(AdmitError::Duplicate {
                        window_secs: dedupe_window_secs,
                    });
                }
            }
        };
        let item = match self.shared.config.ack_mode {
            AckMode::Queued => Item {
                record: prepared.record,
                admitted: now,
                ack: None,
            },
            AckMode::Durable => {
                let (tx, rx) = oneshot::channel();
                let item = Item {
                    record: prepared.record,
                    admitted: now,
                    ack: Some(tx),
                };
                return match self.shared.queues.try_push(lane.lane, item) {
                    Ok(()) => {
                        self.count_admission(&lane);
                        Ok(Ack::Pending(rx))
                    }
                    Err(err) => Err(self.count_refusal(claimed, err)),
                };
            }
        };
        match self.shared.queues.try_push(lane.lane, item) {
            Ok(()) => {
                self.count_admission(&lane);
                Ok(Ack::Immediate)
            }
            Err(err) => Err(self.count_refusal(claimed, err)),
        }
    }

    fn count_admission(&self, lane: &SeverityLane) {
        let metrics = &self.shared.metrics;
        self.shared.pending.fetch_add(1, Ordering::Relaxed);
        metrics.queue_depth.inc(lane.lane.as_str());
        metrics.events_accepted.inc(lane.level_label);
        if lane.unknown_level {
            metrics.events_unknown_level.inc();
        }
        if lane.message_truncated {
            metrics.events_truncated.inc("message");
        }
        if lane.detail_truncated {
            metrics.events_truncated.inc("detail");
        }
        if lane.tag_truncated {
            metrics.events_truncated.inc("tag");
        }
        if lane.redacted {
            metrics.redaction_hits.inc();
        }
        self.refresh_depth();
    }

    fn count_refusal(&self, claimed: u64, err: AdmitError) -> AdmitError {
        // The record never entered a lane, so its dedupe claim is rolled back: a
        // producer retry of a refused record is not a duplicate of the refusal.
        if claimed != 0 {
            let mut dedupe = self.shared.dedupe.lock().unwrap_or_else(|e| e.into_inner());
            dedupe.release(claimed);
        }
        if let AdmitError::Full { lane, .. } = err {
            self.shared.metrics.queue_rejected_full.inc(lane.as_str());
        }
        err
    }

    fn refresh_depth(&self) {
        let (normal, priority) = self.shared.queues.depth();
        self.shared
            .metrics
            .queue_depth_current
            .set((normal + priority) as i64);
    }

    /// Current queue depths `(normal, priority)`.
    pub fn depth(&self) -> (usize, usize) {
        self.shared.queues.depth()
    }

    /// Records admitted but not yet committed.
    pub fn pending(&self) -> i64 {
        self.shared.pending.load(Ordering::Relaxed)
    }

    pub fn is_draining(&self) -> bool {
        self.shared.draining.load(Ordering::Relaxed)
    }

    pub fn is_stopped(&self) -> bool {
        self.shared.stopped.load(Ordering::Relaxed)
    }

    pub fn dedupe_entries(&self) -> usize {
        self.shared
            .dedupe
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .len()
    }

    pub fn config(&self) -> &PipelineConfig {
        &self.shared.config
    }

    /// Refuse new admissions; writers keep flushing what is already queued.
    pub fn begin_drain(&self) {
        self.shared.draining.store(true, Ordering::SeqCst);
        self.shared.metrics.drain_state.set(1);
        self.shared.queues.close();
    }

    /// Wait until every admitted record has been committed, or `timeout` expires.
    ///
    /// Polling rather than a second condvar: the wait happens once, at shutdown, and
    /// a 2 ms poll keeps the drain deadline honest without another synchronisation
    /// primitive that could deadlock the shutdown path.
    pub fn wait_drained(&self, timeout: Duration) -> DrainOutcome {
        let started = Instant::now();
        while started.elapsed() < timeout {
            if self.shared.pending.load(Ordering::Relaxed) <= 0
                && self.shared.in_flight_batches.load(Ordering::Relaxed) <= 0
                && self.shared.queues.is_empty()
            {
                self.shared.metrics.drain_state.set(2);
                return DrainOutcome {
                    drained: true,
                    remaining: 0,
                    drained_records: self.shared.drained_records.load(Ordering::Relaxed).max(0) as u64,
                    elapsed_millis: started.elapsed().as_millis() as u64,
                };
            }
            thread::sleep(Duration::from_millis(2));
        }
        self.shared.metrics.drain_state.set(2);
        let (normal, priority) = self.shared.queues.depth();
        DrainOutcome {
            drained: false,
            remaining: normal + priority + self.shared.pending.load(Ordering::Relaxed).max(0) as usize,
            drained_records: self.shared.drained_records.load(Ordering::Relaxed).max(0) as u64,
            elapsed_millis: started.elapsed().as_millis() as u64,
        }
    }

    /// Drain, then join every writer thread.
    ///
    /// Anything still queued when the deadline expires is counted as
    /// `events_dropped{reason="shutdown"}` (and its producer's ack channel is
    /// dropped, which the transport reports as a failure). Nothing is silently lost.
    pub fn stop(&self, timeout: Duration) -> DrainOutcome {
        self.begin_drain();
        let outcome = self.wait_drained(timeout);
        self.shared.stopped.store(true, Ordering::SeqCst);
        self.shared.queues.close();
        // Wake idle writers so they observe the stop flag instead of waiting out
        // their idle poll interval.
        self.shared.queues.ready.notify_all();
        let handles = {
            let mut handles = self.handles.lock().unwrap_or_else(|e| e.into_inner());
            std::mem::take(&mut *handles)
        };
        for handle in handles {
            let _ = handle.join();
        }
        if !outcome.drained {
            let mut state = self.shared.queues.state.lock().unwrap_or_else(|e| e.into_inner());
            // The queues are VecDeque-backed, so the undelivered remainder is too;
            // only emptiness and length are used below.
            let mut leftovers: VecDeque<Item> = std::mem::take(&mut state.normal);
            leftovers.append(&mut state.priority);
            drop(state);
            if !leftovers.is_empty() {
                self.shared
                    .metrics
                    .events_dropped
                    .with("shutdown")
                    .add(leftovers.len() as u64);
                self.shared
                    .pending
                    .fetch_sub(leftovers.len() as i64, Ordering::Relaxed);
            }
            self.refresh_depth();
        }
        self.shared.metrics.pipeline_running.set(0);
        self.shared.metrics.drain_state.set(2);
        outcome
    }
}

/// The writer thread body: take batches, commit them, answer the producers.
fn writer_loop(shared: Arc<Shared>) {
    // Idle wait: long enough to be free when the queue is empty, short enough that
    // a stop request is noticed promptly. A push notifies the condvar, so this is
    // not an ack-latency floor.
    let idle_wait = Duration::from_millis(50).min(
        shared
            .config
            .drain_timeout
            .checked_div(4)
            .unwrap_or(Duration::from_millis(50))
            .max(Duration::from_millis(5)),
    );
    loop {
        let batch = shared
            .queues
            .take_batch(shared.config.batch_size, shared.config.batch_delay, idle_wait);
        match batch {
            Some(items) if !items.is_empty() => commit_batch(&shared, items),
            _ => {
                // Nothing to do: exit only when draining and everything is flushed.
                if shared.draining.load(Ordering::SeqCst)
                    && shared.queues.is_empty()
                    && shared.pending.load(Ordering::Relaxed) <= 0
                {
                    return;
                }
                if shared.stopped.load(Ordering::SeqCst) {
                    return;
                }
            }
        }
    }
}

fn commit_batch(shared: &Arc<Shared>, items: Vec<Item>) {
    let metrics = &shared.metrics;
    let wait = items
        .iter()
        .map(Item::wait_seconds)
        .fold(0.0f64, f64::max);
    metrics.queue_wait_seconds.observe_seconds(wait);
    metrics.batch_size.observe(items.len() as f64);
    metrics.batches_in_flight.inc();
    shared.in_flight_batches.fetch_add(1, Ordering::Relaxed);

    let records: Vec<Record> = items.iter().map(|item| item.record.clone()).collect();
    let started = Instant::now();
    let outcome = insert_batch_with_retry(
        shared.storage.as_ref(),
        &records,
        shared.config.max_retries,
        shared.config.retry_base_ms,
        metrics,
    );
    let elapsed = started.elapsed();
    metrics.batch_flush_seconds.observe_seconds(elapsed.as_secs_f64());
    metrics.batch_flushes.inc();

    match outcome {
        Ok(written) => {
            metrics.events_persisted.add(written as u64);
            metrics.storage_up.set(1);
            if shared.draining.load(Ordering::SeqCst) {
                shared
                    .drained_records
                    .fetch_add(written as i64, Ordering::Relaxed);
                metrics.drained_records.add(written as u64);
            }
            for item in items {
                if let Some(ack) = item.ack {
                    let _ = ack.send(Ok(()));
                }
            }
        }
        Err(err) => {
            metrics.batches_failed.inc();
            metrics
                .events_dropped
                .with("storage")
                .add(records.len() as u64);
            metrics.storage_transaction_failures.inc();
            // A failed batch must not look healthy: readiness is lowered with the
            // storage error as the reason, and it is restored on the next success.
            metrics.storage_up.set(0);
            for item in items {
                if let Some(ack) = item.ack {
                    // The producer learns the truth: its record is not stored.
                    let _ = ack.send(Err(err.clone()));
                }
            }
        }
    }
    shared.in_flight_batches.fetch_sub(1, Ordering::Relaxed);
    shared
        .pending
        .fetch_sub(records.len() as i64, Ordering::Relaxed);
    metrics
        .batches_in_flight
        .set(shared.in_flight_batches.load(Ordering::Relaxed).max(0));
    metrics
        .pending_writes
        .set(shared.pending.load(Ordering::Relaxed).max(0));
    let (normal, priority) = shared.queues.depth();
    metrics
        .queue_depth_current
        .set((normal + priority) as i64);
}

/// Severity → lane, exposed for the transport's own diagnostics.
pub fn lane_for(severity: Severity) -> Lane {
    if severity.is_high() {
        Lane::Priority
    } else {
        Lane::Normal
    }
}

/// Everything admission needs from a prepared record, extracted before the record
/// itself is moved into the queue.
struct SeverityLane {
    lane: Lane,
    level_label: &'static str,
    unknown_level: bool,
    redacted: bool,
    message_truncated: bool,
    detail_truncated: bool,
    tag_truncated: bool,
}

fn classify(prepared: &Prepared) -> SeverityLane {
    SeverityLane {
        lane: lane_for(prepared.severity),
        level_label: crate::metrics::level_label(&prepared.record.level),
        unknown_level: prepared.unknown_level,
        redacted: prepared.adjustments.redacted,
        message_truncated: prepared.adjustments.message_truncated,
        detail_truncated: prepared.adjustments.detail_truncated,
        tag_truncated: prepared.adjustments.tag_truncated,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ingest::{CollectPolicy, Limits, RawEntry};
    use crate::storage::sqlite::SqliteStorage;
    use crate::storage::BackendConfig;

    struct Harness {
        _dir: tempfile::TempDir,
        path: String,
        metrics: Arc<Metrics>,
    }

    impl Harness {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("logs.db").to_string_lossy().to_string();
            Harness {
                _dir: dir,
                path,
                metrics: Arc::new(Metrics::new()),
            }
        }

        fn storage(&self) -> Arc<dyn Storage> {
            let backend = BackendConfig {
                backend: crate::storage::Backend::Sqlite,
                sqlite_path: self.path.clone(),
                postgres_dsn: None,
                busy_timeout_ms: 5_000,
                read_pool_size: 2,
                pool_timeout_ms: 5_000,
                statement_timeout_ms: 0,
                transaction_timeout_ms: 0,
                max_rows_per_statement: 500,
            };
            Arc::new(
                SqliteStorage::open(&Config::default(), &backend, Arc::clone(&self.metrics)).unwrap(),
            )
        }

        fn pipeline(&self, config: PipelineConfig) -> Arc<Pipeline> {
            Pipeline::start(config, self.storage(), Arc::clone(&self.metrics)).unwrap()
        }

        fn rows(&self) -> i64 {
            let storage = self.storage();
            storage.table_rows().unwrap()
        }
    }

    fn prepared(level: &str, message: &str) -> Prepared {
        prepared_with(level, "core", "system", message, 0)
    }

    fn prepared_with(level: &str, service: &str, category: &str, message: &str, user_id: i64) -> Prepared {
        let entry = RawEntry {
            ts: 1_700_000_000,
            level: level.to_string(),
            service: service.to_string(),
            category: category.to_string(),
            message: message.to_string(),
            detail: "{}".to_string(),
            rule_id: String::new(),
            user_id,
            correlation_id: String::new(),
        };
        crate::ingest::prepare(
            &entry,
            Limits {
                max_message_bytes: 1 << 16,
                max_detail_bytes: 1 << 16,
                max_tag_bytes: 1024,
            },
            true,
            &CollectPolicy::permissive(),
            1_700_000_000,
        )
        .expect("permissive policy admits this record")
    }

    #[test]
    fn batches_commit_and_persist() {
        let harness = Harness::new();
        let pipeline = harness.pipeline(PipelineConfig {
            batch_size: 8,
            ..PipelineConfig::from_config(&Config::default())
        });
        for index in 0..40 {
            assert!(matches!(
                pipeline.admit(prepared("INFO", &format!("row {index}"))),
                Ok(Ack::Immediate)
            ));
        }
        let outcome = pipeline.stop(Duration::from_secs(10));
        assert!(outcome.drained, "{outcome:?}");
        assert_eq!(harness.rows(), 40);
        assert_eq!(harness.metrics.events_accepted.with("INFO").get(), 40);
        assert_eq!(harness.metrics.events_persisted.get(), 40);
        assert_eq!(harness.metrics.events_dropped.with("overload").get(), 0);
        // 40 records in batches of at most 8 → at least 5 flushes.
        assert!(harness.metrics.batch_flushes.get() >= 5);
        assert_eq!(harness.metrics.pipeline_running.get(), 0);
    }

    #[test]
    fn durable_ack_means_committed() {
        let harness = Harness::new();
        let pipeline = harness.pipeline(PipelineConfig {
            ack_mode: AckMode::Durable,
            batch_size: 4,
            ..PipelineConfig::from_config(&Config::default())
        });
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let mut acks = Vec::new();
        for index in 0..12 {
            match pipeline.admit(prepared("ERROR", &format!("durable {index}"))).unwrap() {
                Ack::Pending(rx) => acks.push(rx),
                Ack::Immediate => panic!("durable mode must not answer immediately"),
            }
        }
        for ack in acks {
            let result = runtime.block_on(ack).expect("writer answered");
            assert!(result.is_ok(), "{result:?}");
        }
        // Every record that was acked is in the database already.
        assert_eq!(harness.rows(), 12);
        pipeline.stop(Duration::from_secs(5));
    }

    #[test]
    fn overflow_is_refused_and_counted_not_buffered() {
        let harness = Harness::new();
        let config = PipelineConfig {
            normal_capacity: 4,
            priority_capacity: 1,
            batch_size: 1024,
            // A long linger keeps the writer from draining while we fill the lane.
            batch_delay: Duration::from_millis(500),
            ..PipelineConfig::from_config(&Config::default())
        };
        let pipeline = harness.pipeline(config);
        let mut refused = 0;
        for index in 0..50 {
            match pipeline.admit(prepared("INFO", &format!("burst {index}"))) {
                Ok(_) => {}
                Err(AdmitError::Full { .. }) => refused += 1,
                Err(other) => panic!("unexpected {other:?}"),
            }
        }
        assert!(refused > 0, "a 4-slot lane must refuse a 50-record burst");
        assert_eq!(
            harness.metrics.queue_rejected_full.with("normal").get(),
            refused as u64
        );
        assert_eq!(pipeline.depth().0 + pipeline.depth().1 + refused, 50);
        let outcome = pipeline.stop(Duration::from_secs(10));
        assert!(outcome.drained);
        // The admitted records are the persisted ones; the refused ones were never
        // buffered anywhere.
        assert_eq!(harness.rows(), (50 - refused) as i64);
    }

    #[test]
    fn priority_lane_is_drained_first() {
        let harness = Harness::new();
        let pipeline = harness.pipeline(PipelineConfig {
            normal_capacity: 64,
            priority_capacity: 8,
            batch_size: 4096,
            batch_delay: Duration::from_millis(50),
            ..PipelineConfig::from_config(&Config::default())
        });
        for index in 0..5 {
            pipeline.admit(prepared("INFO", &format!("normal {index}"))).unwrap();
        }
        for index in 0..3 {
            pipeline.admit(prepared("CRITICAL", &format!("critical {index}"))).unwrap();
        }
        // Force a single batch by taking it directly through the queue, then verify
        // the priority records came out first.
        let batch = pipeline
            .shared
            .queues
            .take_batch(4096, Duration::from_millis(1), Duration::from_millis(1))
            .unwrap();
        assert_eq!(batch.len(), 8);
        let levels: Vec<String> = batch.iter().map(|item| item.record.level.clone()).collect();
        assert_eq!(&levels[..3], &["CRITICAL", "CRITICAL", "CRITICAL"]);
        assert_eq!(&levels[3..], &["INFO", "INFO", "INFO", "INFO", "INFO"]);
        pipeline.stop(Duration::from_secs(5));
    }

    #[test]
    fn drain_flushes_everything_and_reports_the_leftovers() {
        let harness = Harness::new();
        let pipeline = harness.pipeline(PipelineConfig {
            ack_mode: AckMode::Durable,
            batch_size: 16,
            drain_timeout: Duration::from_millis(2_000),
            ..PipelineConfig::from_config(&Config::default())
        });
        let mut acks = Vec::new();
        for index in 0..100 {
            if let Ok(Ack::Pending(rx)) = pipeline.admit(prepared("WARN", &format!("drain {index}"))) {
                acks.push(rx);
            }
        }
        let outcome = pipeline.stop(Duration::from_secs(10));
        assert!(outcome.drained, "{outcome:?}");
        assert!(outcome.drained_records > 0);
        assert_eq!(harness.rows(), 100);
        // Every ack was answered — no producer is left hanging by a drain.
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        for ack in acks {
            assert!(runtime.block_on(ack).is_ok());
        }
        assert!(pipeline.is_stopped());
        // Admission after the stop is refused, and it says so.
        assert_eq!(
            pipeline.admit(prepared("INFO", "after stop")).unwrap_err().code(),
            "SHUTTING_DOWN"
        );
    }

    #[test]
    fn dedupe_window_suppresses_and_counts_duplicates() {
        let harness = Harness::new();
        let pipeline = harness.pipeline(PipelineConfig {
            dedupe_window: Duration::from_secs(60),
            ..PipelineConfig::from_config(&Config::default())
        });
        let first = pipeline.admit(prepared("ERROR", "same message")).unwrap();
        assert!(matches!(first, Ack::Immediate));
        let second = pipeline.admit(prepared("ERROR", "same message"));
        assert!(second.is_err(), "an identical record inside the window is suppressed");
        assert_eq!(
            second.unwrap_err().code(),
            "DUPLICATE",
            "a suppressed duplicate is reported, not silently accepted"
        );
        assert_eq!(harness.metrics.events_duplicate.get(), 1);
        let different = pipeline.admit(prepared("ERROR", "another message"));
        assert!(different.is_ok());
        assert_eq!(pipeline.dedupe_entries(), 2);
        pipeline.stop(Duration::from_secs(5));
        assert_eq!(harness.rows(), 2);
    }

    #[test]
    fn storage_failure_is_reported_to_the_producer_and_counted() {
        // A storage that always fails: the ack must say so and the loss must be
        // counted, never silently swallowed.
        struct Broken;
        impl Storage for Broken {
            fn backend(&self) -> crate::storage::Backend {
                crate::storage::Backend::Sqlite
            }
            fn insert_batch(&self, _records: &[Record]) -> Result<usize, StorageError> {
                Err(StorageError::Backend("disk on fire".to_string()))
            }
            fn query(
                &self,
                _f: &QueryFilter,
                _p: &Page,
            ) -> Result<(Vec<Record>, i64), StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn count(&self, _f: &QueryFilter, _c: Option<i64>) -> Result<i64, StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn table_rows(&self) -> Result<i64, StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn stats(&self, _s: &StatsScope) -> Result<Stats, StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn top_groups(
                &self,
                _f: &QueryFilter,
                _by: GroupBy,
                _l: usize,
            ) -> Result<Vec<(String, i64)>, StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn error_aggregates(
                &self,
                _f: &QueryFilter,
                _l: usize,
            ) -> Result<Vec<ErrorAgg>, StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn time_buckets(
                &self,
                _f: &QueryFilter,
                _b: i64,
                _m: usize,
            ) -> Result<Vec<Bucket>, StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn purge(
                &self,
                _f: &PurgeFilter,
                _c: usize,
                _m: u64,
            ) -> Result<crate::storage::PurgeReport, StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn retention_purge(
                &self,
                _s: &crate::query::RetentionSweep,
                _c: usize,
                _m: u64,
            ) -> Result<crate::storage::PurgeReport, StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn export_chunk(
                &self,
                _f: &QueryFilter,
                _p: &Page,
            ) -> Result<Vec<Record>, StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn probe(&self) -> Result<(), StorageError> {
                Err(StorageError::Backend("down".to_string()))
            }
            fn checkpoint(&self) -> Result<(), StorageError> {
                Ok(())
            }
            fn describe(&self) -> String {
                "broken".to_string()
            }
        }
        use crate::query::{Bucket, ErrorAgg, GroupBy, Page, PurgeFilter, QueryFilter, Stats, StatsScope};

        let metrics = Arc::new(Metrics::new());
        let pipeline = Pipeline::start(
            PipelineConfig {
                ack_mode: AckMode::Durable,
                max_retries: 0,
                batch_size: 4,
                ..PipelineConfig::from_config(&Config::default())
            },
            Arc::new(Broken),
            Arc::clone(&metrics),
        )
        .unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let ack = match pipeline.admit(prepared("ERROR", "will fail")).unwrap() {
            Ack::Pending(rx) => rx,
            Ack::Immediate => panic!("durable mode must wait"),
        };
        let result = runtime.block_on(ack).expect("writer answered");
        assert!(result.is_err());
        assert_eq!(result.unwrap_err().code(), "STORAGE_ERROR");
        assert_eq!(metrics.batches_failed.get(), 1);
        assert_eq!(metrics.events_dropped.with("storage").get(), 1);
        assert_eq!(metrics.storage_up.get(), 0);
        pipeline.stop(Duration::from_secs(2));
    }

    #[test]
    fn ack_modes_and_config_describe_are_stable() {
        let config = Config::default();
        let pipeline_config = PipelineConfig::from_config(&config);
        assert_eq!(pipeline_config.ack_mode, AckMode::Durable);
        assert_eq!(pipeline_config.normal_capacity, config.queue_capacity - config.priority_capacity);
        assert_eq!(pipeline_config.priority_capacity, config.priority_capacity);
        let described = pipeline_config.describe();
        assert!(described.contains("ack_mode=durable"));
        assert!(described.contains("writers=1"));
        assert_eq!(PipelineConfig::from_config(&config).with_writers(1).writers, 1);
        assert_eq!(PipelineConfig::from_config(&config).with_writers(99).writers, 8);
        assert_eq!(lane_for(Severity::Critical), Lane::Priority);
        assert_eq!(lane_for(Severity::Warn), Lane::Normal);
    }
}
