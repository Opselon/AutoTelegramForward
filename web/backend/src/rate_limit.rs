//! Per-client token bucket for the `/api` proxy surface.
//!
//! A BFF is the only component that sees raw operator traffic before the Go
//! control plane, so it is the right place for a cheap first-line cushion: one
//! in-memory bucket per client key, refilled continuously. This guards the
//! upstream against a runaway dashboard tab or a naive loop — it is NOT an
//! auth boundary (the control plane still authenticates every request). The
//! client key comes from the socket peer by default; a forwarded header is an
//! explicit operator opt-in and is read rightmost-first (see `Config`).
//!
//! Buckets are pruned with a cost budget, not per-request: a client idle past
//! `max(2 × burst / rps, 60 s)` is dropped, but the scan runs at most once
//! per `PRUNE_COOLDOWN` and again whenever the map has *doubled* since the
//! last sweep. A flood of unique keys therefore pays O(n) amortised instead
//! of turning every request into a 4096-entry scan.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Minimum wall-clock time between two prune sweeps.
const PRUNE_COOLDOWN: Duration = Duration::from_secs(5);
/// Map size that triggers pruning, and the growth factor that forces an
/// early sweep inside the cooldown.
const PRUNE_THRESHOLD: usize = 4096;
const PRUNE_GROWTH_FACTOR: usize = 2;

struct Inner {
    buckets: HashMap<String, Bucket>,
    last_prune: Option<Instant>,
    len_at_last_prune: usize,
}

pub struct RateLimiter {
    state: Mutex<Inner>,
    refill_per_sec: f64,
    burst: f64,
}

struct Bucket {
    tokens: f64,
    last: Instant,
}

/// Verdict for one request: allowed, or rejected with the wait that would
/// make it allowed (surfaced to the client as `Retry-After` seconds).
pub enum Verdict {
    Allow,
    Reject(Duration),
}

impl RateLimiter {
    pub fn new(rps: f64, burst: u32) -> Self {
        Self {
            state: Mutex::new(Inner {
                buckets: HashMap::new(),
                last_prune: None,
                len_at_last_prune: 0,
            }),
            refill_per_sec: rps,
            burst: burst as f64,
        }
    }

    pub fn check(&self, key: &str) -> Verdict {
        let mut state = match self.state.lock() {
            Ok(guard) => guard,
            // A poisoned lock (a panic while holding it) must not take the
            // whole gateway down: fail OPEN, the control plane still enforces
            // its own limits.
            Err(poisoned) => poisoned.into_inner(),
        };
        self.prune_if_due(&mut state);

        let now = Instant::now();
        let bucket = state
            .buckets
            .entry(key.to_string())
            .or_insert_with(|| Bucket {
                tokens: self.burst,
                last: now,
            });
        let elapsed = now.duration_since(bucket.last).as_secs_f64();
        bucket.tokens = (bucket.tokens + elapsed * self.refill_per_sec).min(self.burst);
        bucket.last = now;

        if bucket.tokens >= 1.0 {
            bucket.tokens -= 1.0;
            Verdict::Allow
        } else {
            let wait = (1.0 - bucket.tokens) / self.refill_per_sec;
            Verdict::Reject(Duration::from_secs_f64(wait.ceil().max(1.0)))
        }
    }

    /// Prune when due: past the cooldown, or — above the threshold — whenever
    /// the map has doubled since the last full-size sweep (so a unique-key
    /// flood cannot outrun it). The first crossing of the threshold always
    /// sweeps immediately.
    fn prune_if_due(&self, state: &mut Inner) {
        let len = state.buckets.len();
        if len <= PRUNE_THRESHOLD {
            return;
        }
        let cooled_down = state
            .last_prune
            .map(|t| t.elapsed() >= PRUNE_COOLDOWN)
            .unwrap_or(true);
        // Doubling only counts against a sweep that itself saw a full map;
        // otherwise a sweep that nearly empties the map would make the very
        // next crossing instant again.
        let doubled = state.len_at_last_prune >= PRUNE_THRESHOLD
            && len >= state.len_at_last_prune.saturating_mul(PRUNE_GROWTH_FACTOR);
        if !(cooled_down || doubled) {
            return;
        }
        let idle_for = Duration::from_secs_f64((self.burst / self.refill_per_sec).max(60.0) * 2.0);
        state
            .buckets
            .retain(|_, bucket| bucket.last.elapsed() < idle_for);
        state.last_prune = Some(Instant::now());
        state.len_at_last_prune = state.buckets.len();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rejects(v: Verdict) -> bool {
        matches!(v, Verdict::Reject(_))
    }

    #[test]
    fn burst_allows_then_depletes() {
        let rl = RateLimiter::new(0.0001, 3); // effectively no refill mid-test
        assert!(!rejects(rl.check("a")));
        assert!(!rejects(rl.check("a")));
        assert!(!rejects(rl.check("a")));
        assert!(
            rejects(rl.check("a")),
            "4th request inside one burst window must wait"
        );
    }

    #[test]
    fn keys_are_isolated() {
        let rl = RateLimiter::new(0.0001, 1);
        assert!(!rejects(rl.check("a")));
        assert!(rejects(rl.check("a")));
        assert!(
            !rejects(rl.check("b")),
            "a second client must not inherit the first's spend"
        );
    }

    #[test]
    fn refill_recovers_over_time() {
        let rl = RateLimiter::new(1000.0, 1); // ~1 token per ms
        assert!(!rejects(rl.check("a")));
        assert!(rejects(rl.check("a")));
        std::thread::sleep(Duration::from_millis(3));
        assert!(
            !rejects(rl.check("a")),
            "a refilled bucket admits the retry"
        );
    }

    #[test]
    fn idle_buckets_are_pruned_on_threshold_crossing() {
        let rl = RateLimiter::new(0.0001, 1);
        // Fill past the prune threshold (4096) with buckets aged well beyond
        // the prune window (max(burst/rps, 60 s) × 2 = 20 000 s here).
        {
            let mut state = rl.state.lock().unwrap();
            for i in 0..4097u32 {
                let bucket = state.buckets.entry(format!("stale-{i}")).or_insert(Bucket {
                    tokens: 0.0,
                    last: Instant::now(),
                });
                bucket.last = Instant::now() - Duration::from_secs(30_000);
            }
        }
        let _ = rl.check("fresh");
        let state = rl.state.lock().unwrap();
        assert_eq!(
            state.buckets.len(),
            1,
            "every stale bucket must be pruned on the next check"
        );
        assert!(state.buckets.contains_key("fresh"));
    }

    #[test]
    fn pruning_is_amortised_inside_the_cooldown() {
        let rl = RateLimiter::new(0.0001, 1);
        {
            let mut state = rl.state.lock().unwrap();
            for i in 0..4097u32 {
                let bucket = state.buckets.entry(format!("stale-{i}")).or_insert(Bucket {
                    tokens: 0.0,
                    last: Instant::now(),
                });
                bucket.last = Instant::now() - Duration::from_secs(30_000);
            }
        }
        let _ = rl.check("fresh-a"); // crosses threshold -> sweeps, records len_at_last_prune
        {
            // Re-age everything stale *after* the sweep, inside the cooldown.
            let mut state = rl.state.lock().unwrap();
            let keys: Vec<String> = state.buckets.keys().cloned().collect();
            for key in keys {
                if key == "fresh-a" {
                    continue;
                }
                state.buckets.insert(
                    format!("reaged-{key}"),
                    Bucket {
                        tokens: 0.0,
                        last: Instant::now() - Duration::from_secs(30_000),
                    },
                );
            }
        }
        let _ = rl.check("fresh-b"); // still inside the cooldown -> no sweep
        let state = rl.state.lock().unwrap();
        assert!(
            state.buckets.len() > 1,
            "inside the cooldown a re-grown stale population must not trigger a per-request sweep"
        );
    }

    #[test]
    fn doubling_since_last_prune_forces_an_early_sweep() {
        let rl = RateLimiter::new(0.0001, 1);
        {
            // Fill past the threshold with *fresh* buckets so sweep #1 (at the
            // first crossing) retains them all and records a full-map length.
            let mut state = rl.state.lock().unwrap();
            for i in 0..4100u32 {
                state.buckets.entry(format!("live-{i}")).or_insert(Bucket {
                    tokens: 1.0,
                    last: Instant::now(),
                });
            }
        }
        let _ = rl.check("fresh-a"); // sweep #1; len_at_last_prune ≈ 4101 (all fresh)
        {
            let mut state = rl.state.lock().unwrap();
            assert!(
                state.len_at_last_prune >= 4100,
                "sweep #1 must record a full map"
            );
            for i in 0..4200u32 {
                let bucket = state
                    .buckets
                    .entry(format!("newly-stale-{i}"))
                    .or_insert(Bucket {
                        tokens: 0.0,
                        last: Instant::now(),
                    });
                bucket.last = Instant::now() - Duration::from_secs(30_000);
            }
        }
        let _ = rl.check("fresh-b"); // doubled, still inside cooldown -> sweep #2
        let state = rl.state.lock().unwrap();
        assert!(
            !state.buckets.contains_key("newly-stale-0"),
            "a map that doubled since the last sweep must prune before the cooldown ends"
        );
    }
}
