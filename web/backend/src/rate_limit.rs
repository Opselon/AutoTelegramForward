//! Per-client token bucket for the `/api` proxy surface.
//!
//! A BFF is the only component that sees raw operator traffic before the Go
//! control plane, so it is the right place for a cheap first-line cushion: one
//! in-memory bucket per client key, refilled continuously. This guards the
//! upstream against a runaway dashboard tab or a naive loop — it is NOT an
//! auth boundary (the control plane still authenticates every request) and the
//! client key comes from a spoofable header by default (see `Config`), so the
//! worst an attacker buys by spoofing is a skewed bucket.
//!
//! Buckets are pruned lazily: a client that has been idle past
//! `max(2 × burst / rps, 60 s)` seconds is dropped so a long-running gateway
//! does not accumulate an entry per IP that ever connected.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub struct RateLimiter {
    state: Mutex<HashMap<String, Bucket>>,
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
            state: Mutex::new(HashMap::new()),
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
        // Lazy prune: cheap (amortised), and bounded by the request rate.
        if state.len() > 4096 {
            let idle_for = Duration::from_secs_f64((self.burst / self.refill_per_sec).max(60.0) * 2.0);
            state.retain(|_, bucket| bucket.last.elapsed() < idle_for);
        }

        let now = Instant::now();
        let bucket = state.entry(key.to_string()).or_insert_with(|| Bucket {
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
        assert!(rejects(rl.check("a")), "4th request inside one burst window must wait");
    }

    #[test]
    fn keys_are_isolated() {
        let rl = RateLimiter::new(0.0001, 1);
        assert!(!rejects(rl.check("a")));
        assert!(rejects(rl.check("a")));
        assert!(!rejects(rl.check("b")), "a second client must not inherit the first's spend");
    }

    #[test]
    fn refill_recovers_over_time() {
        let rl = RateLimiter::new(1000.0, 1); // ~1 token per ms
        assert!(!rejects(rl.check("a")));
        assert!(rejects(rl.check("a")));
        std::thread::sleep(Duration::from_millis(3));
        assert!(!rejects(rl.check("a")), "a refilled bucket admits the retry");
    }

    #[test]
    fn idle_buckets_are_pruned() {
        let rl = RateLimiter::new(0.0001, 1);
        // Fill past the lazy-prune threshold (4096) with buckets aged well
        // beyond the prune window (max(burst/rps, 60 s) × 2 = 20 000 s here).
        {
            let mut state = rl.state.lock().unwrap();
            for i in 0..4097u32 {
                let bucket = state.entry(format!("stale-{i}")).or_insert(Bucket {
                    tokens: 0.0,
                    last: Instant::now(),
                });
                bucket.last = Instant::now() - Duration::from_secs(30_000);
            }
        }
        let _ = rl.check("fresh");
        let state = rl.state.lock().unwrap();
        assert_eq!(state.len(), 1, "every stale bucket must be pruned on the next check");
        assert!(state.contains_key("fresh"));
    }
}
