use crate::error::{BusinessCode, Result, StoreError};
use crate::segment::{SegmentInfo, TailTruncation};
use crate::snapshot::SnapshotPoint;
use crate::views::{TraceStep, ValidationReport, ViewStatus};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SegmentReport {
    pub name: String,
    pub bytes: u64,
    pub digest: String,
    pub records_replayed: usize,
}

impl From<SegmentInfo> for SegmentReport {
    fn from(value: SegmentInfo) -> Self {
        Self {
            name: value.name,
            bytes: value.bytes,
            digest: value.digest,
            records_replayed: 0,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecoveryReport {
    pub business_code: String,
    pub message: String,
    pub logical_timestamp: u64,
    pub snapshot: Option<SnapshotPoint>,
    pub segment_set_digest: String,
    pub segments: Vec<SegmentReport>,
    pub replayed_records: usize,
    pub truncation: Option<TailTruncation>,
    pub views: Vec<ViewStatus>,
    pub trace: Vec<TraceStep>,
}

impl RecoveryReport {
    pub fn save_json(&self, path: &Path) -> Result<()> {
        let bytes = serde_json::to_vec_pretty(self).map_err(|error| {
            StoreError::io(format!("failed to encode recovery report: {error}"))
        })?;
        std::fs::write(path, bytes).map_err(|error| {
            StoreError::io(format!(
                "failed to write recovery report {}: {error}",
                path.display()
            ))
        })
    }
}

impl fmt::Display for RecoveryReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(f, "report=recovery")?;
        writeln!(f, "business_code={}", self.business_code)?;
        writeln!(f, "message={}", self.message)?;
        writeln!(f, "logical_timestamp={}", self.logical_timestamp)?;
        match &self.snapshot {
            Some(snapshot) => {
                writeln!(f, "snapshot_segment={}", snapshot.segment)?;
                writeln!(f, "snapshot_offset={}", snapshot.offset)?;
                writeln!(f, "snapshot_timestamp={}", snapshot.logical_timestamp)?;
            }
            None => {
                writeln!(f, "snapshot_segment=none")?;
                writeln!(f, "snapshot_offset=none")?;
                writeln!(f, "snapshot_timestamp=none")?;
            }
        }
        writeln!(f, "segment_set_digest={}", self.segment_set_digest)?;
        writeln!(f, "replayed_records={}", self.replayed_records)?;
        match &self.truncation {
            Some(truncation) => {
                writeln!(f, "truncation.segment={}", truncation.segment)?;
                writeln!(f, "truncation.offset={}", truncation.offset)?;
                writeln!(f, "truncation.original_bytes={}", truncation.original_bytes)?;
                writeln!(f, "truncation.valid_bytes={}", truncation.valid_bytes)?;
                writeln!(f, "truncation.removed_bytes={}", truncation.removed_bytes)?;
            }
            None => writeln!(f, "truncation=none")?,
        }
        for segment in &self.segments {
            writeln!(
                f,
                "segment={} bytes={} digest={} replayed={}",
                segment.name, segment.bytes, segment.digest, segment.records_replayed
            )?;
        }
        for view in &self.views {
            writeln!(
                f,
                "view={} kind={} version={} valid={} digest={}",
                view.id,
                view.kind,
                view.version,
                view.valid,
                view.digest.as_deref().unwrap_or("none")
            )?;
        }
        if self.trace.is_empty() {
            writeln!(f, "trace=none")?;
        } else {
            for step in &self.trace {
                writeln!(
                    f,
                    "trace={} trigger={} affected_view={} relation={} action={}",
                    step.step, step.trigger, step.affected_view, step.relation, step.action
                )?;
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ViewVersionDiff {
    pub id: String,
    pub snapshot_version: Option<u64>,
    pub current_version: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionOpenReport {
    pub business_code: String,
    pub replacement: String,
    pub segment_digest_match: bool,
    pub snapshot_segment_digest: String,
    pub current_segment_digest: String,
    pub added_segments: Vec<String>,
    pub removed_segments: Vec<String>,
    pub changed_segments: Vec<String>,
    pub snapshot_graph_version: u32,
    pub current_graph_version: u32,
    pub snapshot_logical_timestamp: u64,
    pub current_logical_timestamp: u64,
    pub view_differences: Vec<ViewVersionDiff>,
}

impl fmt::Display for SessionOpenReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(f, "report=session-open")?;
        writeln!(f, "business_code={}", self.business_code)?;
        writeln!(f, "replacement={}", self.replacement)?;
        writeln!(f, "segment_digest_match={}", self.segment_digest_match)?;
        writeln!(
            f,
            "snapshot_segment_set_digest={}",
            self.snapshot_segment_digest
        )?;
        writeln!(
            f,
            "current_segment_set_digest={}",
            self.current_segment_digest
        )?;
        if self.added_segments.is_empty() {
            writeln!(f, "added_segment=none")?;
        } else {
            for segment in &self.added_segments {
                writeln!(f, "added_segment={segment}")?;
            }
        }
        if self.removed_segments.is_empty() {
            writeln!(f, "removed_segment=none")?;
        } else {
            for segment in &self.removed_segments {
                writeln!(f, "removed_segment={segment}")?;
            }
        }
        if self.changed_segments.is_empty() {
            writeln!(f, "changed_segment=none")?;
        } else {
            for segment in &self.changed_segments {
                writeln!(f, "changed_segment={segment}")?;
            }
        }
        writeln!(f, "graph_version.snapshot={}", self.snapshot_graph_version)?;
        writeln!(f, "graph_version.current={}", self.current_graph_version)?;
        writeln!(
            f,
            "logical_timestamp.snapshot={}",
            self.snapshot_logical_timestamp
        )?;
        writeln!(
            f,
            "logical_timestamp.current={}",
            self.current_logical_timestamp
        )?;
        if self.view_differences.is_empty() {
            writeln!(f, "view_version_difference=none")?;
        } else {
            for difference in &self.view_differences {
                writeln!(
                    f,
                    "view_version_difference={} snapshot_version={} current_version={}",
                    difference.id,
                    optional_number(difference.snapshot_version),
                    optional_number(difference.current_version)
                )?;
            }
        }
        Ok(())
    }
}

fn optional_number(value: Option<u64>) -> String {
    value
        .map(|value| value.to_string())
        .unwrap_or_else(|| "none".to_string())
}

impl fmt::Display for ValidationReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(f, "report=view-validation")?;
        writeln!(f, "business_code={}", self.code)?;
        for view in &self.views {
            writeln!(
                f,
                "view={} version={} digest={} previous_digest={} status={}",
                view.id,
                view.version,
                view.digest,
                view.previous_digest.as_deref().unwrap_or("none"),
                view.status
            )?;
        }
        writeln!(
            f,
            "first_different_view={}",
            self.first_different_view.as_deref().unwrap_or("none")
        )?;
        writeln!(
            f,
            "first_different_key={}",
            self.first_different_key.as_deref().unwrap_or("none")
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiagnosticLine {
    pub timestamp_ms: u64,
    pub business_code: String,
    pub segment: Option<String>,
    pub offset: Option<u64>,
    pub message: String,
}

#[derive(Debug, Clone)]
pub struct DiagnosticLogger {
    path: PathBuf,
}

impl DiagnosticLogger {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn error(&self, error: &StoreError) -> std::io::Result<()> {
        self.write(DiagnosticLine {
            timestamp_ms: wall_clock_millis(),
            business_code: error.code.as_str().to_string(),
            segment: error
                .segment
                .as_ref()
                .map(|path| path.to_string_lossy().into_owned()),
            offset: error.offset,
            message: error.message.clone(),
        })
    }

    pub fn event(
        &self,
        code: BusinessCode,
        segment: Option<String>,
        offset: Option<u64>,
        message: impl Into<String>,
    ) -> std::io::Result<()> {
        self.write(DiagnosticLine {
            timestamp_ms: wall_clock_millis(),
            business_code: code.as_str().to_string(),
            segment,
            offset,
            message: message.into(),
        })
    }

    fn write(&self, line: DiagnosticLine) -> std::io::Result<()> {
        if let Some(parent) = self.path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        let json = serde_json::to_string(&line)?;
        file.write_all(json.as_bytes())?;
        file.write_all(b"\n")?;
        file.flush()
    }
}

pub fn wall_clock_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(u128::from(u64::MAX)) as u64
}
