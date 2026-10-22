//! ATF Logger engine — bounded, batched gRPC log ingest/storage/query.
//!
//! The crate is split so every behaviour the service promises is testable
//! without a network, a clock or a second process:
//!
//! * [`config`]    — validated runtime configuration (flags + environment).
//! * [`redaction`] — ingest-time secret scrubbing (parity with `logger/redaction.py`).
//! * [`ingest`]    — admission/normalisation/severity mapping.
//! * [`pipeline`]  — bounded priority lanes, batching writer, backpressure.
//! * [`storage`]   — SQLite writer + read pool (the compatibility contract).
//! * [`query`]     — shared filter/pagination SQL builder.
//! * [`retention`] — age-based purging on a bounded schedule.
//! * [`transport`] — the `LogControlService` gRPC implementation.
//! * [`health`]    — liveness/readiness state.
//! * [`metrics`]   — Prometheus text-format registry + HTTP exposition.
//! * [`shutdown`]  — graceful drain coordination.

pub mod auth;
pub mod config;
pub mod health;
pub mod ingest;
pub mod metrics;
pub mod pipeline;
pub mod query;
pub mod redaction;
pub mod retention;
pub mod shutdown;
pub mod storage;
pub mod transport;

/// The protobuf/gRPC bindings generated from `proto/logger.proto`.
pub mod pb {
    #![allow(clippy::all)]
    #![allow(rustdoc::all)]
    tonic::include_proto!("autoforward");
}

/// Crate version, surfaced in the `atf_logger_info` metric and `--version`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
