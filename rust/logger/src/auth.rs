//! Authentication for the gRPC surface.
//!
//! The deployment keeps the listener inside the compose network today, but the
//! archive holds every tenant's data — that is one lost network boundary away
//! from modification. This module gives the surface capability-scoped bearer
//! tokens: *ingest* (writes), *read* (query/stats), *admin* (purge). One token
//! per capability, never a master key, so a leaked producer token cannot read
//! the archive and a dashboard token cannot write.
//!
//! Contract kept honest: unset tokens leave a scope open (previous behaviour),
//! because an unattended rotation must not lock producers out of logging; the
//! trade-off is documented in docker-compose.yml and the audit.

use crate::config::Config;

/// Which capability class an RPC belongs to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scope {
    /// `Log`: producers writing records.
    Ingest,
    /// `QueryLogs` / `LogStats`: reading the archive.
    Read,
    /// `PurgeLogs`: erasing data.
    Admin,
}

impl Scope {
    pub fn as_str(self) -> &'static str {
        match self {
            Scope::Ingest => "ingest",
            Scope::Read => "read",
            Scope::Admin => "admin",
        }
    }

    /// Classify a full gRPC method name (`/logger.LogService/Log`). An unknown
    /// method is admin: a new management RPC added later must be *denied* for
    /// every existing token until this mapping consciously grants it.
    pub fn of_method(full_method: &str) -> Scope {
        if full_method.ends_with("/Log") {
            Scope::Ingest
        } else if full_method.ends_with("/QueryLogs") || full_method.ends_with("/LogStats") {
            Scope::Read
        } else {
            Scope::Admin
        }
    }
}

/// Why a presented credential failed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    /// No `authorization` metadata at all.
    Missing,
    /// Present but not `Bearer <opaque>`.
    Malformed,
    /// A well-formed token that matches no configured scope secret.
    Mismatch,
    /// A token that belongs to another capability class (never a master key:
    /// the ingest secret must not open the archive).
    Forbidden,
}

impl Failure {
    pub fn as_str(self) -> &'static str {
        match self {
            Failure::Missing => "missing",
            Failure::Malformed => "malformed",
            Failure::Mismatch => "mismatch",
            Failure::Forbidden => "forbidden",
        }
    }
}

/// The configured secrets, compared in constant time.
#[derive(Debug, Default, Clone)]
pub struct Auth {
    ingest: Vec<u8>,
    read: Vec<u8>,
    admin: Vec<u8>,
}

fn bearer_secret(metadata: &tonic::metadata::MetadataMap) -> Result<Option<Vec<u8>>, Failure> {
    let Some(value) = metadata.get("authorization") else {
        return Ok(None);
    };
    // Non-ASCII / binary metadata is not a credential we could ever have
    // configured; answer Malformed rather than pretending it was absent.
    let value = value.to_str().map_err(|_| Failure::Malformed)?;
    let encoded = value
        .strip_prefix("Bearer ")
        .ok_or(Failure::Malformed)?
        .trim();
    if encoded.is_empty() {
        return Err(Failure::Malformed);
    }
    Ok(Some(encoded.as_bytes().to_vec()))
}

/// Constant-time byte equality. Length is not hidden (it is on the wire
/// anyway); the running-time guarantee covers equal-length secrets.
fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

impl Auth {
    pub fn from_config(config: &Config) -> Self {
        Auth {
            ingest: config.auth_ingest_token.as_bytes().to_vec(),
            read: config.auth_read_token.as_bytes().to_vec(),
            admin: config.auth_admin_token.as_bytes().to_vec(),
        }
    }

    /// Is this scope's gate armed? Unset secret = open (documented above).
    pub fn is_configured(&self, scope: Scope) -> bool {
        let secret = match scope {
            Scope::Ingest => &self.ingest,
            Scope::Read => &self.read,
            Scope::Admin => &self.admin,
        };
        !secret.is_empty()
    }

    /// Validate the presented credential for `scope`. `Ok` when the scope is
    /// open or the token matches it.
    pub fn check(
        &self,
        scope: Scope,
        metadata: &tonic::metadata::MetadataMap,
    ) -> Result<(), Failure> {
        if !self.is_configured(scope) {
            return Ok(());
        }
        let Some(token) = bearer_secret(metadata)? else {
            return Err(Failure::Missing);
        };
        let scope_secret = match scope {
            Scope::Ingest => &self.ingest,
            Scope::Read => &self.read,
            Scope::Admin => &self.admin,
        };
        if ct_eq(scope_secret, &token) {
            return Ok(());
        }
        if ct_eq(&self.ingest, &token) || ct_eq(&self.read, &token) || ct_eq(&self.admin, &token) {
            // Valid credential for another class: never escalate.
            return Err(Failure::Forbidden);
        }
        Err(Failure::Mismatch)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tonic::metadata::{MetadataMap, MetadataValue};

    fn meta(auth: Option<&str>) -> MetadataMap {
        let mut m = MetadataMap::new();
        if let Some(v) = auth {
            m.insert(
                "authorization",
                MetadataValue::try_from(v).expect("ascii header"),
            );
        }
        m
    }

    fn auth() -> Auth {
        Auth {
            ingest: b"ingest-secret".to_vec(),
            read: b"read-secret".to_vec(),
            admin: b"admin-secret".to_vec(),
        }
    }

    #[test]
    fn unset_scope_stays_open() {
        let a = Auth::default();
        let m = meta(None);
        assert_eq!(a.check(Scope::Ingest, &m), Ok(()));
        assert_eq!(a.check(Scope::Read, &m), Ok(()));
        assert_eq!(a.check(Scope::Admin, &m), Ok(()));
    }

    #[test]
    fn bearer_prefix_is_case_sensitive_and_required() {
        let a = auth();
        assert_eq!(
            a.check(Scope::Ingest, &meta(Some("ingest-secret"))),
            Err(Failure::Malformed),
            "no Bearer prefix — not our trigger word"
        );
        assert_eq!(
            a.check(Scope::Ingest, &meta(Some("bearer ingest-secret"))),
            Err(Failure::Malformed)
        );
        assert_eq!(
            a.check(Scope::Ingest, &meta(Some("Bearer ingest-secret"))),
            Ok(())
        );
        assert_eq!(
            a.check(Scope::Ingest, &meta(Some("Bearer ingest-secret \t"))),
            Ok(()),
            "yaml folding / dotenv padding must not rot credentials"
        );
        assert_eq!(
            a.check(Scope::Ingest, &meta(Some("Bearer  "))),
            Err(Failure::Malformed)
        );
    }

    #[test]
    fn missing_malformed_mismatch_have_distinct_outcomes() {
        let a = auth();
        assert_eq!(
            a.check(Scope::Read, &meta(None)),
            Err(Failure::Missing),
            "absence of the metadata key is its own outcome"
        );
        assert_eq!(
            a.check(Scope::Read, &meta(Some("Basic dXNlcjpwYXNz"))),
            Err(Failure::Malformed)
        );
        assert_eq!(
            a.check(Scope::Read, &meta(Some("Bearer no-such-secret"))),
            Err(Failure::Mismatch)
        );
    }

    #[test]
    fn a_token_for_one_capability_never_opens_another() {
        let a = auth();
        let roles = [
            (Scope::Ingest, "ingest-secret"),
            (Scope::Read, "read-secret"),
            (Scope::Admin, "admin-secret"),
        ];
        for (holder, secret) in roles {
            for (target, _) in roles {
                if target == holder {
                    continue;
                }
                assert_eq!(
                    a.check(target, &meta(Some(&format!("Bearer {secret}")))),
                    Err(Failure::Forbidden),
                    "{} secret must not satisfy {}",
                    holder.as_str(),
                    target.as_str()
                );
            }
        }
    }

    #[test]
    fn method_classification_is_total_and_defaults_to_deny() {
        assert_eq!(Scope::of_method("/logger.LogService/Log"), Scope::Ingest);
        assert_eq!(
            Scope::of_method("/logger.LogService/QueryLogs"),
            Scope::Read
        );
        assert_eq!(Scope::of_method("/logger.LogService/LogStats"), Scope::Read);
        assert_eq!(
            Scope::of_method("/logger.LogService/PurgeLogs"),
            Scope::Admin
        );
        // A method added to the proto later must not inherit an open scope.
        assert_eq!(
            Scope::of_method("/logger.LogService/StreamEverything"),
            Scope::Admin
        );
    }

    #[test]
    fn ct_eq_is_exact() {
        assert!(ct_eq(b"", b""));
        assert!(ct_eq(b"x", b"x"));
        assert!(!ct_eq(b"x", b"y"));
        assert!(!ct_eq(b"x", b"xy"));
    }

    #[test]
    fn timings_for_equal_length_secrets_are_within_a_coarse_band() {
        // Not a crypto proof — a guard against an accidentally early-returning
        // loop that would make matching sequences dramatically faster.
        use std::time::SystemTime;
        let a = auth();
        let m = meta(Some("Bearer read-secret"));
        let t0 = SystemTime::now();
        for _ in 0..4000 {
            let _ = a.check(Scope::Read, &m);
        }
        let matched = t0.elapsed().unwrap();
        let m2 = meta(Some("Bearer read-secrd"));
        let t1 = SystemTime::now();
        for _ in 0..4000 {
            let _ = a.check(Scope::Read, &m2);
        }
        let unmatched = t1.elapsed().unwrap();
        let ratio = matched.as_secs_f64() / unmatched.as_secs_f64().max(1e-9);
        assert!(
            (0.4..2.5).contains(&ratio),
            "matched {:?} vs unmatched {:?} ratio {:.2}",
            matched,
            unmatched,
            ratio
        );
    }
}
