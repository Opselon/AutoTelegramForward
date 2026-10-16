//! Event validation, normalisation, severity mapping and the collection policy.
//!
//! This module is the boundary the `LogControlService.Log` RPC hands a record to:
//! everything that decides whether a record is *collected* (docs/logging-contract.md
//! §2 and §3) and what shape it has by the time it reaches storage lives here.
//!
//! Three rules from the contract are enforced:
//!
//! * **Severity is canonicalised, never upgraded.** `WARNING` is the legacy alias
//!   of `WARN`; the seven canonical levels are `TRACE, DEBUG, INFO, SUCCESS, WARN,
//!   ERROR, CRITICAL`; an unknown level keeps its own spelling and is *treated as
//!   `INFO` for thresholding* — it is never silently promoted to a security level.
//! * **Collection defaults follow the contract table**: `WARN`/`ERROR`/`CRITICAL`
//!   are collected, `INFO`/`SUCCESS`/`DEBUG`/`TRACE` are not until explicitly
//!   enabled (`ATF_LOGGER_COLLECT_LEVELS`). Filtering is counted, never silent.
//! * **Protected/audit categories are exempt from threshold filtering**: raising
//!   the threshold to silence chatty `INFO` must not disarm authorization-failure
//!   auditing, so a `category=audit|security` event is collected because of what it
//!   *is*, not because of its level.
//!
//! Redaction happens here too, before anything is queued: a record that reaches
//! the pipeline has already been scrubbed, so no exporter, queue dump or storage
//! error path can expose a secret.

use crate::pb;
use crate::redaction;

/// The canonical severities, ordered as docs/logging-contract.md §2 defines them.
pub const CANONICAL_LEVELS: [&str; 7] = [
    "TRACE", "DEBUG", "INFO", "SUCCESS", "WARN", "ERROR", "CRITICAL",
];

/// Levels `PurgeLogs` accepts. The Python servicer rejected anything outside
/// `{DEBUG, INFO, WARN, ERROR}`; the canonical set is a superset, and the legacy
/// `WARNING` alias is accepted so an old client keeps working.
pub const PURGEABLE_LEVELS: [&str; 8] = [
    "TRACE", "DEBUG", "INFO", "SUCCESS", "WARN", "WARNING", "ERROR", "CRITICAL",
];

/// Severity of an event. `Success` is deliberately below `Warn`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Severity {
    Trace = 0,
    Debug = 1,
    Info = 2,
    Success = 3,
    Warn = 4,
    Error = 5,
    Critical = 6,
}

impl Severity {
    /// Parse a level string (canonical, legacy alias, any casing).
    pub fn parse(level: &str) -> Option<Severity> {
        match normalize_level(level).as_str() {
            "TRACE" => Some(Severity::Trace),
            "DEBUG" => Some(Severity::Debug),
            "INFO" => Some(Severity::Info),
            "SUCCESS" => Some(Severity::Success),
            "WARN" => Some(Severity::Warn),
            "ERROR" => Some(Severity::Error),
            "CRITICAL" => Some(Severity::Critical),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Severity::Trace => "TRACE",
            Severity::Debug => "DEBUG",
            Severity::Info => "INFO",
            Severity::Success => "SUCCESS",
            Severity::Warn => "WARN",
            Severity::Error => "ERROR",
            Severity::Critical => "CRITICAL",
        }
    }

    /// Numeric rank, for `>=` style comparisons.
    pub fn rank(self) -> u8 {
        self as u8
    }

    /// Severities that belong in the reserved high-priority lane.
    pub fn is_high(self) -> bool {
        matches!(self, Severity::Error | Severity::Critical)
    }

    pub fn index(self) -> usize {
        self as usize
    }

    pub fn from_index(index: usize) -> Option<Severity> {
        match index {
            0 => Some(Severity::Trace),
            1 => Some(Severity::Debug),
            2 => Some(Severity::Info),
            3 => Some(Severity::Success),
            4 => Some(Severity::Warn),
            5 => Some(Severity::Error),
            6 => Some(Severity::Critical),
            _ => None,
        }
    }
}

/// Normalise a level string: trimmed, upper-cased, `WARNING` mapped to `WARN`.
///
/// An unknown level is returned upper-cased and otherwise untouched — the Python
/// engine coerced it to `INFO` and lost the producer's own vocabulary; the
/// contract keeps it readable and only *thresholds* it like `INFO`.
pub fn normalize_level(raw: &str) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return "INFO".to_string();
    }
    let upper = trimmed.to_uppercase();
    if upper == "WARNING" {
        return "WARN".to_string();
    }
    upper
}

/// Severity of a level string, or `None` when the level is unknown.
pub fn severity_of(level: &str) -> Option<Severity> {
    Severity::parse(level)
}

/// Severity used for threshold decisions when the level is unknown.
pub fn threshold_severity(level: &str) -> Severity {
    severity_of(level).unwrap_or(Severity::Info)
}

/// Bounded metric label for a level: a canonical name, or `unknown`.
///
/// Metric label cardinality is a security property here — a producer that invents
/// a new level string per request must not be able to explode the scraped series.
pub fn severity_label(level: &str) -> &'static str {
    match severity_of(level) {
        Some(s) => s.as_str(),
        None => "unknown",
    }
}

/// Case-insensitive membership test for the protected-category list.
///
/// `Config::is_protected_category` upper-cases the *category* but compares against
/// the configured value verbatim, so its lower-case defaults (`audit`,
/// `security`) never match. The frozen config module is not rewritten; the
/// ingest path uses this function instead, and [`crate::ingest::tests`] pins the
/// behaviour.
pub fn is_protected_category(category: &str, protected: &[String]) -> bool {
    let wanted = category.trim().to_uppercase();
    protected
        .iter()
        .any(|candidate| candidate.trim().to_uppercase() == wanted)
}

/// Which records are collected at all.
///
/// Built from the contract's default table plus explicit enablement, never from
/// "whatever the old installation's level threshold was".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CollectPolicy {
    enabled: [bool; 7],
    collect_unknown: bool,
    protected_categories: Vec<String>,
}

impl CollectPolicy {
    /// `WARN`, `ERROR`, `CRITICAL` only — the contract's default for a new or
    /// migrated installation.
    pub fn contract_default(protected_categories: &[String]) -> Self {
        let mut enabled = [false; 7];
        enabled[Severity::Warn.index()] = true;
        enabled[Severity::Error.index()] = true;
        enabled[Severity::Critical.index()] = true;
        CollectPolicy {
            enabled,
            collect_unknown: false,
            protected_categories: protected_categories.to_vec(),
        }
    }

    /// Everything, including DEBUG/TRACE. Used by tests and by an operator who
    /// explicitly asks for it.
    pub fn permissive() -> Self {
        CollectPolicy {
            enabled: [true; 7],
            collect_unknown: true,
            protected_categories: Vec::new(),
        }
    }

    /// Build from the environment, keeping the contract's defaults for anything
    /// that is not set.
    ///
    /// Recognised keys:
    /// * `ATF_LOGGER_COLLECT_LEVELS` — comma-separated levels, or `all`.
    /// * `ATF_LOGGER_COLLECT_INFO` / `_SUCCESS` / `_DEBUG` / `_TRACE` — `1`/`true`.
    /// * `ATF_LOGGER_COLLECT_UNKNOWN` — `1`/`true` collects unknown levels too.
    pub fn from_env(protected_categories: &[String], env: &dyn Fn(&str) -> Option<String>) -> Self {
        let mut policy = CollectPolicy::contract_default(protected_categories);
        let truthy = |v: &str| v == "1" || v.eq_ignore_ascii_case("true") || v.eq_ignore_ascii_case("yes");
        if let Some(raw) = env("ATF_LOGGER_COLLECT_LEVELS") {
            let raw = raw.trim();
            if !raw.is_empty() {
                if raw.eq_ignore_ascii_case("all") || raw == "*" {
                    policy.enabled = [true; 7];
                    policy.collect_unknown = true;
                } else {
                    policy.enabled = [false; 7];
                    policy.collect_unknown = false;
                    for token in raw.split(',') {
                        let token = token.trim();
                        if token.is_empty() {
                            continue;
                        }
                        match severity_of(token) {
                            Some(level) => policy.enabled[level.index()] = true,
                            None => policy.collect_unknown = true,
                        }
                    }
                }
            }
        }
        for (key, level) in [
            ("ATF_LOGGER_COLLECT_TRACE", Severity::Trace),
            ("ATF_LOGGER_COLLECT_DEBUG", Severity::Debug),
            ("ATF_LOGGER_COLLECT_INFO", Severity::Info),
            ("ATF_LOGGER_COLLECT_SUCCESS", Severity::Success),
            ("ATF_LOGGER_COLLECT_WARN", Severity::Warn),
            ("ATF_LOGGER_COLLECT_ERROR", Severity::Error),
            ("ATF_LOGGER_COLLECT_CRITICAL", Severity::Critical),
        ] {
            if let Some(v) = env(key) {
                if !v.is_empty() {
                    policy.enabled[level.index()] = truthy(&v);
                }
            }
        }
        if let Some(v) = env("ATF_LOGGER_COLLECT_UNKNOWN") {
            if !v.is_empty() {
                policy.collect_unknown = truthy(&v);
            }
        }
        policy
    }

    /// The contract's table, as a set.
    pub fn default_enabled_levels() -> Vec<&'static str> {
        vec!["WARN", "ERROR", "CRITICAL"]
    }

    /// Levels this policy collects, for the startup diagnostic.
    pub fn enabled_levels(&self) -> Vec<&'static str> {
        let mut out: Vec<&'static str> = CANONICAL_LEVELS
            .iter()
            .copied()
            .filter(|l| {
                severity_of(l)
                    .map(|s| self.enabled[s.index()])
                    .unwrap_or(false)
            })
            .collect();
        if self.collect_unknown {
            out.push("unknown");
        }
        out
    }

    pub fn protected_categories(&self) -> &[String] {
        &self.protected_categories
    }

    /// Is a record with this level and category collected?
    pub fn admits(&self, level: &str, category: &str) -> bool {
        if is_protected_category(category, &self.protected_categories) {
            // Security-relevant auditing is not disarmed by a severity threshold.
            return true;
        }
        match severity_of(level) {
            Some(severity) => self.enabled[severity.index()],
            None => self.collect_unknown,
        }
    }

    /// One-line description for the startup diagnostic.
    pub fn describe(&self) -> String {
        format!(
            "levels={} protected_unknown={} protected_categories=[{}]",
            self.enabled_levels().join(","),
            self.collect_unknown,
            self.protected_categories.join(",")
        )
    }
}

/// Byte budgets applied at ingest. Oversized producers are truncated and counted
/// rather than allowed to grow an unbounded queue.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    pub max_message_bytes: usize,
    pub max_detail_bytes: usize,
    pub max_tag_bytes: usize,
}

impl Limits {
    pub fn from_config(cfg: &crate::config::Config) -> Self {
        Limits {
            max_message_bytes: cfg.max_message_bytes,
            max_detail_bytes: cfg.max_detail_bytes,
            max_tag_bytes: cfg.max_tag_bytes,
        }
    }
}

/// The producer-side record, decoupled from the protobuf type so ingest rules can
/// be unit-tested without a network or a prost default.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RawEntry {
    pub ts: i64,
    pub level: String,
    pub service: String,
    pub category: String,
    pub message: String,
    pub detail: String,
    pub rule_id: String,
    pub user_id: i64,
    pub correlation_id: String,
}

impl From<&pb::LogEntry> for RawEntry {
    fn from(entry: &pb::LogEntry) -> Self {
        RawEntry {
            ts: entry.ts,
            level: entry.level.clone(),
            service: entry.service.clone(),
            category: entry.category.clone(),
            message: entry.message.clone(),
            detail: entry.detail.clone(),
            rule_id: entry.rule_id.clone(),
            user_id: entry.user_id,
            correlation_id: entry.correlation_id.clone(),
        }
    }
}

/// What was changed while normalising a record.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Adjustments {
    pub message_truncated: bool,
    pub detail_truncated: bool,
    pub tag_truncated: bool,
    pub redacted: bool,
    pub structured_detail: bool,
    pub level_aliased: bool,
}

/// Why a record was not collected.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reject {
    /// The record is malformed in a way that cannot be repaired.
    Invalid(String),
    /// A collection-policy decision. Counted, reported, never silent.
    Filtered {
        level: String,
        category: String,
    },
}

impl Reject {
    pub fn code(&self) -> &'static str {
        match self {
            Reject::Invalid(_) => "INVALID",
            Reject::Filtered { .. } => "FILTERED",
        }
    }

    pub fn message(&self) -> String {
        match self {
            Reject::Invalid(why) => format!("invalid log entry: {why}"),
            Reject::Filtered { level, category } => format!(
                "filtered by collection policy: level {level} (category {category}) is not collected; \
                 enable it with ATF_LOGGER_COLLECT_LEVELS"
            ),
        }
    }
}

/// A record that passed admission: normalised, redacted and bounded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Prepared {
    pub record: crate::storage::Record,
    pub severity: Severity,
    /// True when the producer sent a level we do not know; it was kept verbatim
    /// and thresholded as `INFO`.
    pub unknown_level: bool,
    pub adjustments: Adjustments,
}

/// Normalise, redact and bound one entry, or explain why it was rejected.
///
/// `now` is the timestamp substituted for `ts == 0`, already expressed in the
/// configured unit (see `Config::retention_ts_unit`); the engine never reinterprets
/// a producer's `ts`, because the Python clients and the benchmark use different
/// units for the same column.
pub fn prepare(
    entry: &RawEntry,
    limits: Limits,
    redact_identifiers: bool,
    policy: &CollectPolicy,
    now: i64,
) -> Result<Prepared, Reject> {
    if entry.message.len() > limits.max_message_bytes.saturating_mul(1024) {
        // A hard sanity bound: anything this far over budget is a producer bug,
        // and truncating it would hide a record an operator probably wants to see.
        return Err(Reject::Invalid(format!(
            "message of {} bytes exceeds the {} byte ceiling",
            entry.message.len(),
            limits.max_message_bytes.saturating_mul(1024)
        )));
    }

    let raw_level = if entry.level.trim().is_empty() {
        "INFO".to_string()
    } else {
        entry.level.clone()
    };
    let level = normalize_level(&raw_level);
    let severity = severity_of(&level);
    let category = if entry.category.trim().is_empty() {
        "system".to_string()
    } else {
        entry.category.trim().to_string()
    };
    if !policy.admits(&level, &category) {
        return Err(Reject::Filtered { level, category });
    }

    let mut adjustments = Adjustments {
        level_aliased: normalize_level(&raw_level) != raw_level.trim().to_uppercase(),
        ..Adjustments::default()
    };

    let (message_raw, message_truncated) =
        redaction::truncate_chars(&entry.message, limits.max_message_bytes);
    adjustments.message_truncated = message_truncated;
    let message = redaction::redact_strong(message_raw);
    adjustments.redacted |= message != message_raw;

    let detail = redaction::redact_detail(&entry.detail, limits.max_detail_bytes);
    adjustments.detail_truncated = detail.truncated;
    adjustments.structured_detail = detail.structured;
    adjustments.redacted |= detail.text != entry.detail;

    let service = bound_tag(&entry.service, "core", limits.max_tag_bytes, &mut adjustments);
    let category_bounded = bound_tag(
        &category,
        "system",
        limits.max_tag_bytes,
        &mut adjustments,
    );
    let rule_id = bound_identifier(
        &entry.rule_id,
        limits.max_tag_bytes,
        redact_identifiers,
        &mut adjustments,
    );
    let correlation_id = bound_identifier(
        &entry.correlation_id,
        limits.max_tag_bytes,
        redact_identifiers,
        &mut adjustments,
    );
    let bounded_level = bound_tag(&level, "INFO", limits.max_tag_bytes, &mut adjustments);

    Ok(Prepared {
        record: crate::storage::Record {
            id: 0,
            ts: if entry.ts == 0 { now } else { entry.ts },
            level: bounded_level,
            service,
            category: category_bounded,
            message,
            detail: detail.text,
            rule_id,
            user_id: entry.user_id,
            correlation_id,
        },
        severity: severity.unwrap_or(Severity::Info),
        unknown_level: severity.is_none(),
        adjustments,
    })
}

fn bound_tag(raw: &str, fallback: &str, max_bytes: usize, adj: &mut Adjustments) -> String {
    let value = if raw.trim().is_empty() {
        fallback.to_string()
    } else {
        raw.trim().to_string()
    };
    let (cut, truncated) = redaction::truncate_chars(&value, max_bytes);
    adj.tag_truncated |= truncated;
    cut.to_string()
}

fn bound_identifier(
    raw: &str,
    max_bytes: usize,
    redact_identifiers: bool,
    adj: &mut Adjustments,
) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    let redacted = if redact_identifiers {
        let out = redaction::redact_identifier(trimmed);
        adj.redacted |= out != trimmed;
        out
    } else {
        trimmed.to_string()
    };
    let (cut, truncated) = redaction::truncate_chars(&redacted, max_bytes);
    adj.tag_truncated |= truncated;
    cut.to_string()
}

/// Stable dedupe fingerprint of a record (FNV-1a over the identifying fields).
///
/// Used by the optional bounded duplicate-suppression window. It is deliberately
/// *not* presented as at-most-once delivery: the storage schema has no unique
/// event id, and the contract says so.
pub fn dedupe_key(record: &crate::storage::Record) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    let mut feed = |bytes: &[u8]| {
        for byte in bytes {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
        hash ^= 0x1f;
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    };
    feed(record.level.as_bytes());
    feed(record.service.as_bytes());
    feed(record.category.as_bytes());
    feed(record.message.as_bytes());
    feed(record.detail.as_bytes());
    feed(&record.ts.to_be_bytes());
    feed(&record.user_id.to_be_bytes());
    feed(record.rule_id.as_bytes());
    feed(record.correlation_id.as_bytes());
    hash
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::Record;

    fn raw(level: &str, category: &str) -> RawEntry {
        RawEntry {
            ts: 1_700_000_000,
            level: level.to_string(),
            service: "core".to_string(),
            category: category.to_string(),
            message: "hello".to_string(),
            ..RawEntry::default()
        }
    }

    fn limits() -> Limits {
        Limits {
            max_message_bytes: 4096,
            max_detail_bytes: 4096,
            max_tag_bytes: 256,
        }
    }

    fn policy() -> CollectPolicy {
        CollectPolicy::contract_default(&["audit".to_string(), "security".to_string()])
    }

    #[test]
    fn canonical_levels_and_alias() {
        assert_eq!(normalize_level("warning"), "WARN");
        assert_eq!(normalize_level("WARNING"), "WARN");
        assert_eq!(normalize_level("  warn "), "WARN");
        assert_eq!(normalize_level(""), "INFO");
        assert_eq!(normalize_level("custom-thing"), "CUSTOM-THING");
        assert_eq!(severity_of("WARNING"), Some(Severity::Warn));
        assert_eq!(severity_of("CRITICAL"), Some(Severity::Critical));
        assert_eq!(severity_of("nonsense"), None);
        // Unknown levels threshold as INFO, never as a security level.
        assert_eq!(threshold_severity("nonsense"), Severity::Info);
        assert!(!Severity::Success.is_high());
        assert!(Severity::Error.is_high());
        assert_eq!(severity_label("nonsense"), "unknown");
        assert_eq!(severity_label("warning"), "WARN");
    }

    #[test]
    fn contract_default_collects_only_warn_and_above() {
        let p = policy();
        assert!(p.admits("WARN", "forward"));
        assert!(p.admits("ERROR", "forward"));
        assert!(p.admits("CRITICAL", "forward"));
        assert!(!p.admits("INFO", "forward"));
        assert!(!p.admits("SUCCESS", "forward"));
        assert!(!p.admits("DEBUG", "forward"));
        assert!(!p.admits("TRACE", "forward"));
        // Unknown levels threshold as INFO, so they are not collected either.
        assert!(!p.admits("weird", "forward"));
        assert_eq!(p.enabled_levels(), vec!["WARN", "ERROR", "CRITICAL"]);
    }

    #[test]
    fn protected_categories_are_exempt_from_thresholding() {
        let p = policy();
        assert!(p.admits("INFO", "audit"));
        assert!(p.admits("INFO", "AUDIT"));
        assert!(p.admits("DEBUG", "Security"));
        assert!(!p.admits("DEBUG", "pipeline"));
        assert!(is_protected_category("AUDIT", &["audit".to_string()]));
        assert!(!is_protected_category("audit", &["AUDIT2".to_string()]));
    }

    #[test]
    fn policy_from_env_enables_levels() {
        let env = |k: &str| match k {
            "ATF_LOGGER_COLLECT_LEVELS" => Some("WARN,ERROR,INFO".to_string()),
            _ => None,
        };
        let p = CollectPolicy::from_env(&[], &env);
        assert!(p.admits("INFO", "pipeline"));
        assert!(!p.admits("DEBUG", "pipeline"));
        let all = |k: &str| match k {
            "ATF_LOGGER_COLLECT_LEVELS" => Some("all".to_string()),
            _ => None,
        };
        assert!(CollectPolicy::from_env(&[], &all).admits("TRACE", "pipeline"));
        let flag = |k: &str| match k {
            "ATF_LOGGER_COLLECT_DEBUG" => Some("1".to_string()),
            _ => None,
        };
        let p = CollectPolicy::from_env(&[], &flag);
        assert!(p.admits("DEBUG", "pipeline"));
        assert!(!p.admits("TRACE", "pipeline"));
    }

    #[test]
    fn prepare_normalises_and_redacts() {
        let mut entry = raw("warning", "pipeline");
        entry.detail = r#"{"password": "hunter2", "rule_id": "r-1"}"#.to_string();
        entry.rule_id = "r-1".to_string();
        let prepared = prepare(&entry, limits(), true, &CollectPolicy::permissive(), 7).unwrap();
        assert_eq!(prepared.record.level, "WARN");
        assert_eq!(prepared.severity, Severity::Warn);
        assert!(!prepared.record.detail.contains("hunter2"));
        assert_eq!(prepared.record.service, "core");
        assert_eq!(prepared.record.category, "pipeline");
        assert_eq!(prepared.record.ts, 1_700_000_000);
    }

    #[test]
    fn prepare_applies_defaults_and_bounds() {
        let mut entry = RawEntry {
            ts: 0,
            level: String::new(),
            service: String::new(),
            category: String::new(),
            message: "x".repeat(10_000),
            detail: "y".repeat(10_000),
            rule_id: "z".repeat(1024),
            correlation_id: String::new(),
            user_id: 0,
        };
        let prepared = prepare(&entry, limits(), true, &CollectPolicy::permissive(), 42).unwrap();
        assert_eq!(prepared.record.level, "INFO");
        assert_eq!(prepared.record.service, "core");
        assert_eq!(prepared.record.category, "system");
        assert_eq!(prepared.record.ts, 42, "ts=0 must be substituted");
        assert_eq!(prepared.record.message.len(), 4096);
        assert!(prepared.record.detail.len() <= 8192);
        assert_eq!(prepared.record.rule_id.len(), 256);
        assert!(prepared.adjustments.message_truncated);
        assert!(prepared.adjustments.detail_truncated);
        assert!(prepared.adjustments.tag_truncated);

        entry.message = "ok".to_string();
        entry.detail = String::new();
        entry.rule_id = String::new();
        let prepared = prepare(&entry, limits(), true, &CollectPolicy::permissive(), 42).unwrap();
        assert!(prepared.record.detail.is_empty());
        assert!(prepared.record.rule_id.is_empty());
    }

    #[test]
    fn prepare_rejects_filtered_levels_with_a_reason() {
        let entry = raw("INFO", "pipeline");
        let err = prepare(&entry, limits(), true, &policy(), 0).unwrap_err();
        assert_eq!(err.code(), "FILTERED");
        assert!(err.message().contains("INFO"));
        let entry = raw("DEBUG", "audit");
        assert!(prepare(&entry, limits(), true, &policy(), 0).is_ok());
    }

    #[test]
    fn prepare_keeps_unknown_levels_verbatim() {
        let entry = raw("custom-level", "pipeline");
        let prepared = prepare(&entry, limits(), true, &CollectPolicy::permissive(), 0).unwrap();
        assert_eq!(prepared.record.level, "CUSTOM-LEVEL");
        assert!(prepared.unknown_level);
        assert_eq!(prepared.severity, Severity::Info);
    }

    #[test]
    fn identifiers_are_redacted_only_when_enabled() {
        let mut entry = raw("ERROR", "pipeline");
        entry.correlation_id = "Bearer abcdefghijklmnop".to_string();
        let redacted = prepare(&entry, limits(), true, &CollectPolicy::permissive(), 0).unwrap();
        assert!(!redacted.record.correlation_id.contains("abcdefghijklmnop"));
        let raw_ids = prepare(&entry, limits(), false, &CollectPolicy::permissive(), 0).unwrap();
        assert!(raw_ids.record.correlation_id.contains("abcdefghijklmnop"));
    }

    #[test]
    fn dedupe_key_is_stable_and_field_sensitive() {
        let record = Record {
            level: "ERROR".to_string(),
            service: "core".to_string(),
            message: "boom".to_string(),
            ts: 5,
            ..Record::default()
        };
        assert_eq!(dedupe_key(&record), dedupe_key(&record.clone()));
        let mut other = record.clone();
        other.message = "boom2".to_string();
        assert_ne!(dedupe_key(&record), dedupe_key(&other));
    }
}
