//! `log_structured_store` is a deterministic, append-only log-structured key/value engine used
//! for teaching recovery and troubleshooting on Windows and other local environments.
//!
//! The public surface is intentionally small: open a [`Store`], load a dependency graph, append
//! records, create snapshot points, recover, rebuild views and save/open complete session
//! snapshots. All persistent data is local JSON and binary segment files; no network, database or
//! external service is required.

pub mod digest;
pub mod error;
pub mod graph;
pub mod record;
pub mod reports;
pub mod segment;
pub mod snapshot;
pub mod store;
pub mod views;

pub use error::{BusinessCode, Result, StoreError};
pub use graph::{
    Dependency, DependencyGraph, GraphDocument, GraphLoadOptions, ViewDefinition, ViewKind,
};
pub use record::{decode_record, LogRecord, Operation};
pub use reports::{DiagnosticLogger, RecoveryReport, SegmentReport, SessionOpenReport};
pub use segment::{SegmentInfo, SegmentSetSummary, TailTruncation, WriteReceipt};
pub use snapshot::{SessionSnapshot, SnapshotPoint};
pub use store::{Store, StoreConfig};
pub use views::{
    RebuildReport, TraceStep, ValidationReport, ViewRuntime, ViewStatus, ViewValidation,
};
