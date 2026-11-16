use crate::digest::{digest_serialized, digest_str};
use crate::error::{Result, StoreError};
use crate::graph::GraphDocument;
use crate::segment::{digest_segment_list, SegmentInfo};
use crate::views::{TraceStep, ViewRuntime};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub const SESSION_SNAPSHOT_VERSION: u32 = 1;
pub const SNAPSHOT_POINT_VERSION: u32 = 1;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotPoint {
    pub version: u32,
    pub segment: String,
    pub offset: u64,
    pub logical_timestamp: u64,
    pub state_digest: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionSnapshot {
    pub snapshot_version: u32,
    pub segments: Vec<SegmentInfo>,
    pub segment_set_digest: String,
    pub snapshot_point: SnapshotPoint,
    pub logical_timestamp: u64,
    pub state: BTreeMap<String, String>,
    pub state_digest: String,
    pub graph_version: u32,
    pub graph_digest: String,
    pub graph: GraphDocument,
    pub views: Vec<ViewRuntime>,
    pub invalidation_trace: Vec<TraceStep>,
    pub next_trace_step: u64,
}

impl SessionSnapshot {
    pub fn state_digest(state: &BTreeMap<String, String>) -> String {
        digest_serialized(state).unwrap_or_else(|_| digest_str("state"))
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|error| {
                    StoreError::io(format!(
                        "failed to create snapshot directory {}: {error}",
                        parent.display()
                    ))
                })?;
            }
        }
        let bytes = serde_json::to_vec_pretty(self).map_err(|error| {
            StoreError::snapshot(format!("failed to encode session snapshot: {error}"))
        })?;
        std::fs::write(path, bytes).map_err(|error| {
            StoreError::snapshot(format!(
                "failed to write session snapshot {}: {error}",
                path.display()
            ))
        })
    }

    pub fn load(
        path: &Path,
        data_dir: &Path,
        simulate_missing_segment: Option<&str>,
    ) -> Result<Self> {
        let bytes = std::fs::read(path).map_err(|error| {
            StoreError::snapshot(format!(
                "failed to read session snapshot {}: {error}",
                path.display()
            ))
        })?;
        let value: serde_json::Value = serde_json::from_slice(&bytes).map_err(|error| {
            StoreError::snapshot(format!(
                "invalid session snapshot JSON {}: {error}",
                path.display()
            ))
        })?;
        require_u64_field(&value, "snapshot_version")?;
        require_u64_field(&value, "logical_timestamp")?;
        require_u64_field(&value, "graph_version")?;
        let point = value
            .get("snapshot_point")
            .ok_or_else(|| StoreError::snapshot("session snapshot is missing snapshot_point"))?;
        if point.get("version").is_none() {
            return Err(StoreError::snapshot(
                "snapshot_point is missing its format version",
            ));
        }
        let snapshot: SessionSnapshot = serde_json::from_value(value).map_err(|error| {
            StoreError::snapshot(format!(
                "invalid session snapshot {}: {error}",
                path.display()
            ))
        })?;
        if snapshot.snapshot_version != SESSION_SNAPSHOT_VERSION {
            return Err(StoreError::snapshot(format!(
                "unsupported session snapshot version {}",
                snapshot.snapshot_version
            )));
        }
        if snapshot.snapshot_point.version != SNAPSHOT_POINT_VERSION {
            return Err(StoreError::snapshot(format!(
                "unsupported snapshot point version {}",
                snapshot.snapshot_point.version
            )));
        }
        if snapshot.snapshot_point.logical_timestamp != snapshot.logical_timestamp {
            return Err(StoreError::snapshot(
                "snapshot point timestamp disagrees with session logical timestamp",
            ));
        }
        let actual_state_digest = Self::state_digest(&snapshot.state);
        if actual_state_digest != snapshot.state_digest {
            return Err(StoreError::snapshot(format!(
                "snapshot state digest mismatch: stored={} actual={actual_state_digest}",
                snapshot.state_digest
            )));
        }
        let segment_digest = digest_segment_list(&snapshot.segments);
        if segment_digest != snapshot.segment_set_digest {
            return Err(StoreError::snapshot(format!(
                "snapshot segment set digest mismatch: stored={} actual={segment_digest}",
                snapshot.segment_set_digest
            )));
        }
        let graph_digest = snapshot.graph.digest();
        if graph_digest != snapshot.graph_digest {
            return Err(StoreError::snapshot(format!(
                "snapshot graph digest mismatch: stored={} actual={graph_digest}",
                snapshot.graph_digest
            )));
        }
        if snapshot.graph.graph_version != snapshot.graph_version {
            return Err(StoreError::snapshot(
                "snapshot graph version disagrees with graph document",
            ));
        }
        if !snapshot
            .segments
            .iter()
            .any(|segment| segment.name == snapshot.snapshot_point.segment)
        {
            return Err(StoreError::snapshot(format!(
                "snapshot point references segment {} outside the persisted segment set",
                snapshot.snapshot_point.segment
            )));
        }

        for segment in &snapshot.segments {
            if simulate_missing_segment == Some(segment.name.as_str()) {
                return Err(snapshot_missing_segment(data_dir, &segment.name));
            }
            let segment_path = data_dir.join(&segment.name);
            if !segment_path.is_file() {
                return Err(snapshot_missing_segment(data_dir, &segment.name));
            }
        }
        Ok(snapshot)
    }
}

fn require_u64_field(value: &serde_json::Value, field: &str) -> Result<()> {
    if value
        .get(field)
        .and_then(|candidate| candidate.as_u64())
        .is_none()
    {
        return Err(StoreError::snapshot(format!(
            "session snapshot is missing required version/timestamp field {field}"
        )));
    }
    Ok(())
}

fn snapshot_missing_segment(data_dir: &Path, name: &str) -> StoreError {
    StoreError::snapshot(format!(
        "snapshot references missing segment {} in {}",
        name,
        data_dir.display()
    ))
    .with_segment(PathBuf::from(name))
}
