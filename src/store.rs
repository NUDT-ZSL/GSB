use crate::error::{BusinessCode, Result, StoreError};
use crate::graph::{DependencyGraph, GraphLoadOptions, ViewDefinition};
use crate::record::{LogRecord, Operation};
use crate::reports::{
    DiagnosticLogger, RecoveryReport, SegmentReport, SessionOpenReport, ViewVersionDiff,
};
use crate::segment::{
    append_record, discover_segment_paths, ensure_data_dir, scan_segment, segment_name,
    summarize_segments, truncate_segment, SegmentInfo, SegmentSetSummary, WriteReceipt,
};
use crate::snapshot::{
    SessionSnapshot, SnapshotPoint, SESSION_SNAPSHOT_VERSION, SNAPSHOT_POINT_VERSION,
};
use crate::views::{
    ensure_runtime_entries, initialize_runtimes, propagate_invalidation, rebuild_all, rebuild_view,
    validate_all, view_statuses, RebuildReport, TraceStep, ValidationReport, ViewRuntime,
};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy)]
pub struct StoreConfig {
    pub max_segment_bytes: u64,
}

impl Default for StoreConfig {
    fn default() -> Self {
        Self {
            max_segment_bytes: 4096,
        }
    }
}

pub struct Store {
    data_dir: PathBuf,
    config: StoreConfig,
    state: BTreeMap<String, String>,
    logical_timestamp: u64,
    snapshot_point: Option<SnapshotPoint>,
    replay_position: Option<SnapshotPoint>,
    graph: DependencyGraph,
    runtimes: BTreeMap<String, ViewRuntime>,
    invalidation_trace: Vec<TraceStep>,
    next_trace_step: u64,
    logger: Option<DiagnosticLogger>,
}

impl Store {
    pub fn open(data_dir: impl Into<PathBuf>, config: StoreConfig) -> Result<Self> {
        let data_dir = data_dir.into();
        ensure_data_dir(&data_dir)?;
        let graph = DependencyGraph::empty();
        let runtimes = initialize_runtimes(&graph);
        Ok(Self {
            data_dir,
            config,
            state: BTreeMap::new(),
            logical_timestamp: 0,
            snapshot_point: None,
            replay_position: None,
            graph,
            runtimes,
            invalidation_trace: Vec::new(),
            next_trace_step: 1,
            logger: None,
        })
    }

    pub fn set_diagnostic_logger(&mut self, logger: DiagnosticLogger) {
        self.logger = Some(logger);
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    pub fn config(&self) -> StoreConfig {
        self.config
    }

    pub fn state(&self) -> &BTreeMap<String, String> {
        &self.state
    }

    pub fn logical_timestamp(&self) -> u64 {
        self.logical_timestamp
    }

    pub fn snapshot_point(&self) -> Option<&SnapshotPoint> {
        self.snapshot_point.as_ref()
    }

    pub fn graph(&self) -> &DependencyGraph {
        &self.graph
    }

    pub fn invalidation_trace(&self) -> &[TraceStep] {
        &self.invalidation_trace
    }

    pub fn view_statuses(&self) -> Vec<crate::views::ViewStatus> {
        view_statuses(&self.graph, &self.runtimes)
    }

    pub fn load_graph_file(&mut self, path: &Path, options: GraphLoadOptions) -> Result<()> {
        let graph = DependencyGraph::load_file(path, options)?;
        self.graph = graph;
        self.runtimes = initialize_runtimes(&self.graph);
        self.invalidation_trace.clear();
        self.next_trace_step = 1;
        Ok(())
    }

    pub fn install_graph_document(&mut self, document: crate::graph::GraphDocument) -> Result<()> {
        let graph = DependencyGraph::from_document(document, GraphLoadOptions::default())?;
        self.graph = graph;
        self.runtimes = initialize_runtimes(&self.graph);
        self.invalidation_trace.clear();
        self.next_trace_step = 1;
        Ok(())
    }

    pub fn declare_view(&mut self, definition: ViewDefinition) -> Result<()> {
        let graph = self.graph.add_view(definition)?;
        self.graph = graph;
        ensure_runtime_entries(&self.graph, &mut self.runtimes);
        Ok(())
    }

    pub fn put(
        &mut self,
        key: impl Into<String>,
        value: impl Into<String>,
    ) -> Result<WriteReceipt> {
        self.append_user_record(Operation::Put, key.into(), value.into())
    }

    pub fn delete(&mut self, key: impl Into<String>) -> Result<WriteReceipt> {
        self.append_user_record(Operation::Delete, key.into(), String::new())
    }

    pub fn expire(&mut self, key: impl Into<String>) -> Result<WriteReceipt> {
        self.append_user_record(Operation::Expire, key.into(), String::new())
    }

    fn append_user_record(
        &mut self,
        operation: Operation,
        key: String,
        value: String,
    ) -> Result<WriteReceipt> {
        if key.is_empty() {
            return Err(StoreError::input("key must not be empty"));
        }
        let timestamp = self.logical_timestamp.saturating_add(1);
        let record = LogRecord {
            version: crate::record::FORMAT_VERSION,
            operation,
            key,
            value,
            logical_timestamp: timestamp,
        };
        self.append_record_internal(record)
    }

    /// Append a record whose logical timestamp must be exactly the next timestamp. This is useful
    /// for deterministic recovery tests and for callers that manage their own logical clock.
    pub fn append_record(&mut self, record: LogRecord) -> Result<WriteReceipt> {
        if record.logical_timestamp != self.logical_timestamp.saturating_add(1) {
            return Err(StoreError::input(format!(
                "record timestamp {} is not the next logical timestamp {}",
                record.logical_timestamp,
                self.logical_timestamp.saturating_add(1)
            )));
        }
        self.append_record_internal(record)
    }

    fn append_record_internal(&mut self, record: LogRecord) -> Result<WriteReceipt> {
        record.validate_for_append()?;
        let mut next_state = self.state.clone();
        apply_record_to_state(&mut next_state, &record);
        let mut next_runtimes = self.runtimes.clone();
        let mut next_trace_step = self.next_trace_step;
        let new_trace = propagate_invalidation(
            &self.graph,
            &mut next_runtimes,
            &next_state,
            &record,
            &mut next_trace_step,
        )?;
        let receipt = append_record(&self.data_dir, self.config.max_segment_bytes, &record)?;
        self.state = next_state;
        self.runtimes = next_runtimes;
        self.invalidation_trace.extend(new_trace);
        self.next_trace_step = next_trace_step;
        self.logical_timestamp = record.logical_timestamp;
        self.replay_position = Some(SnapshotPoint {
            version: SNAPSHOT_POINT_VERSION,
            segment: receipt.segment.clone(),
            offset: receipt.offset.saturating_add(receipt.bytes),
            logical_timestamp: self.logical_timestamp,
            state_digest: SessionSnapshot::state_digest(&self.state),
        });
        Ok(receipt)
    }

    pub fn current_segments(&self) -> Result<SegmentSetSummary> {
        let paths = discover_segment_paths(&self.data_dir)?;
        summarize_segments(&paths)
    }

    pub fn create_snapshot_point(&mut self) -> Result<SnapshotPoint> {
        let mut point = self.replay_position.clone().ok_or_else(|| {
            StoreError::snapshot("cannot create a snapshot point without an applied log record")
        })?;
        point.logical_timestamp = self.logical_timestamp;
        point.state_digest = SessionSnapshot::state_digest(&self.state);
        self.snapshot_point = Some(point.clone());
        Ok(point)
    }

    pub fn save_session_snapshot(&self, path: &Path) -> Result<()> {
        let mut point = self.snapshot_point.clone().ok_or_else(|| {
            StoreError::snapshot("no snapshot point exists; create one before saving the session")
        })?;
        if point.logical_timestamp != self.logical_timestamp {
            return Err(StoreError::snapshot(format!(
                "snapshot point timestamp {} is stale for logical timestamp {}; create a new snapshot point",
                point.logical_timestamp, self.logical_timestamp
            )));
        }
        point.state_digest = SessionSnapshot::state_digest(&self.state);
        let summary = self.current_segments()?;
        let mut views: Vec<ViewRuntime> = self.runtimes.values().cloned().collect();
        views.sort_by(|left, right| left.id.cmp(&right.id));
        let snapshot = SessionSnapshot {
            snapshot_version: SESSION_SNAPSHOT_VERSION,
            segments: summary.segments,
            segment_set_digest: summary.digest,
            snapshot_point: point,
            logical_timestamp: self.logical_timestamp,
            state: self.state.clone(),
            state_digest: SessionSnapshot::state_digest(&self.state),
            graph_version: self.graph.document.graph_version,
            graph_digest: self.graph.digest.clone(),
            graph: self.graph.document.clone(),
            views,
            invalidation_trace: self.invalidation_trace.clone(),
            next_trace_step: self.next_trace_step,
        };
        snapshot.save(path)
    }

    pub fn open_session_snapshot(
        &mut self,
        path: &Path,
        simulate_missing_segment: Option<&str>,
    ) -> Result<SessionOpenReport> {
        let snapshot = SessionSnapshot::load(path, &self.data_dir, simulate_missing_segment)?;
        let current_summary = self.current_segments()?;
        let current_names: BTreeSet<String> = current_summary
            .segments
            .iter()
            .map(|segment| segment.name.clone())
            .collect();
        let snapshot_names: BTreeSet<String> = snapshot
            .segments
            .iter()
            .map(|segment| segment.name.clone())
            .collect();
        let added_segments: Vec<String> =
            current_names.difference(&snapshot_names).cloned().collect();
        let removed_segments: Vec<String> =
            snapshot_names.difference(&current_names).cloned().collect();
        let snapshot_by_name: BTreeMap<&str, &SegmentInfo> = snapshot
            .segments
            .iter()
            .map(|segment| (segment.name.as_str(), segment))
            .collect();
        let changed_segments: Vec<String> = current_summary
            .segments
            .iter()
            .filter_map(|current| {
                snapshot_by_name
                    .get(current.name.as_str())
                    .and_then(|snapshot| {
                        if snapshot.bytes != current.bytes || snapshot.digest != current.digest {
                            Some(current.name.clone())
                        } else {
                            None
                        }
                    })
            })
            .collect();
        let current_versions: BTreeMap<String, u64> = self
            .runtimes
            .iter()
            .map(|(id, runtime)| (id.clone(), runtime.version))
            .collect();
        let snapshot_versions: BTreeMap<String, u64> = snapshot
            .views
            .iter()
            .map(|runtime| (runtime.id.clone(), runtime.version))
            .collect();
        let all_ids: BTreeSet<String> = current_versions
            .keys()
            .chain(snapshot_versions.keys())
            .cloned()
            .collect();
        let view_differences = all_ids
            .into_iter()
            .filter_map(|id| {
                let current = current_versions.get(&id).copied();
                let snapshot = snapshot_versions.get(&id).copied();
                if current != snapshot {
                    Some(ViewVersionDiff {
                        id,
                        snapshot_version: snapshot,
                        current_version: current,
                    })
                } else {
                    None
                }
            })
            .collect();

        let graph =
            DependencyGraph::from_document(snapshot.graph.clone(), GraphLoadOptions::default())?;
        validate_snapshot_position(&self.data_dir, &snapshot.snapshot_point)?;

        let report = SessionOpenReport {
            business_code: BusinessCode::Ok.as_str().to_string(),
            replacement: "full".to_string(),
            segment_digest_match: snapshot.segment_set_digest == current_summary.digest,
            snapshot_segment_digest: snapshot.segment_set_digest.clone(),
            current_segment_digest: current_summary.digest,
            added_segments,
            removed_segments,
            changed_segments,
            snapshot_graph_version: snapshot.graph_version,
            current_graph_version: self.graph.document.graph_version,
            snapshot_logical_timestamp: snapshot.logical_timestamp,
            current_logical_timestamp: self.logical_timestamp,
            view_differences,
        };

        self.state = snapshot.state;
        self.logical_timestamp = snapshot.logical_timestamp;
        self.snapshot_point = Some(snapshot.snapshot_point.clone());
        self.replay_position = Some(snapshot.snapshot_point);
        self.graph = graph;
        self.runtimes = snapshot
            .views
            .into_iter()
            .map(|runtime| (runtime.id.clone(), runtime))
            .collect();
        ensure_runtime_entries(&self.graph, &mut self.runtimes);
        self.invalidation_trace = snapshot.invalidation_trace;
        self.next_trace_step = snapshot.next_trace_step;
        Ok(report)
    }

    /// Replay only records after the current snapshot point, or the full log when no snapshot is
    /// active. State is committed only after every record has been validated.
    pub fn recover(&mut self) -> Result<RecoveryReport> {
        self.recover_inner()
    }

    fn recover_inner(&mut self) -> Result<RecoveryReport> {
        let paths = discover_segment_paths(&self.data_dir)?;
        let mut state = if self.replay_position.is_some() {
            self.state.clone()
        } else {
            BTreeMap::new()
        };
        let mut runtimes = if self.replay_position.is_some() {
            self.runtimes.clone()
        } else {
            initialize_runtimes(&self.graph)
        };
        let mut trace = if self.replay_position.is_some() {
            self.invalidation_trace.clone()
        } else {
            Vec::new()
        };
        let mut next_trace_step = if self.replay_position.is_some() {
            self.next_trace_step
        } else {
            1
        };
        let snapshot = self.snapshot_point.clone();
        let start_position = self.replay_position.clone();
        let mut expected_timestamp = start_position
            .as_ref()
            .map(|point| point.logical_timestamp.saturating_add(1))
            .unwrap_or(1);
        let mut start_segment_index = 0usize;
        let mut start_offset = 0u64;
        if let Some(point) = &start_position {
            start_segment_index = paths
                .iter()
                .position(|path| segment_name(path) == point.segment)
                .ok_or_else(|| {
                    StoreError::snapshot(format!(
                        "snapshot point references missing segment {}",
                        point.segment
                    ))
                    .with_segment(PathBuf::from(&point.segment))
                })?;
            validate_snapshot_position(&self.data_dir, point)?;
            start_offset = point.offset;
        }

        let mut replay_counts: BTreeMap<String, usize> = paths
            .iter()
            .map(|path| (segment_name(path), 0usize))
            .collect();
        let mut truncation = None;
        let mut replayed_records = 0usize;
        for (index, path) in paths.iter().enumerate().skip(start_segment_index) {
            let is_last_segment = index + 1 == paths.len();
            let from_offset = if index == start_segment_index {
                start_offset
            } else {
                0
            };
            let scan = scan_segment(path, from_offset, is_last_segment)?;
            for positioned in scan.records {
                if positioned.record.logical_timestamp != expected_timestamp {
                    return Err(StoreError::snapshot(format!(
                        "logical timestamp discontinuity in replay: expected {expected_timestamp}, found {}",
                        positioned.record.logical_timestamp
                    ))
                    .with_segment(path)
                    .set_offset(positioned.offset));
                }
                apply_record_to_state(&mut state, &positioned.record);
                let steps = propagate_invalidation(
                    &self.graph,
                    &mut runtimes,
                    &state,
                    &positioned.record,
                    &mut next_trace_step,
                )?;
                trace.extend(steps);
                *replay_counts
                    .get_mut(&segment_name(path))
                    .expect("segment count exists") += 1;
                expected_timestamp = expected_timestamp.saturating_add(1);
                replayed_records += 1;
            }
            if let Some(found_truncation) = scan.truncation {
                truncation = Some(found_truncation);
                break;
            }
        }

        if let Some(found) = &truncation {
            let path = self.data_dir.join(&found.segment);
            truncate_segment(&path, found.valid_bytes)?;
            if let Some(logger) = &self.logger {
                let _ = logger.event(
                    BusinessCode::Log,
                    Some(found.segment.clone()),
                    Some(found.offset),
                    format!(
                        "tail truncation repaired: removed {} bytes",
                        found.removed_bytes
                    ),
                );
            }
        }

        let summary = self.current_segments()?;
        let mut segment_reports: Vec<SegmentReport> = Vec::new();
        for segment in summary.segments {
            let count = replay_counts.get(&segment.name).copied().unwrap_or(0);
            let mut report = SegmentReport::from(segment);
            report.records_replayed = count;
            segment_reports.push(report);
        }
        let business_code = if truncation.is_some() {
            BusinessCode::Log
        } else {
            BusinessCode::Ok
        };
        let message = if truncation.is_some() {
            "tail truncation detected and repaired"
        } else {
            "recovery complete"
        };
        let report = RecoveryReport {
            business_code: business_code.as_str().to_string(),
            message: message.to_string(),
            logical_timestamp: expected_timestamp.saturating_sub(1),
            snapshot,
            segment_set_digest: summary.digest,
            segments: segment_reports,
            replayed_records,
            truncation: truncation.clone(),
            views: view_statuses(&self.graph, &runtimes),
            trace,
        };

        self.state = state;
        self.runtimes = runtimes;
        self.invalidation_trace = report.trace.clone();
        self.next_trace_step = next_trace_step;
        self.logical_timestamp = report.logical_timestamp;
        let final_paths = discover_segment_paths(&self.data_dir)?;
        self.replay_position = if let Some(last) = final_paths.last() {
            let length = fs::metadata(last)
                .map_err(|error| {
                    StoreError::io(format!("failed to stat {}: {error}", last.display()))
                })?
                .len();
            Some(SnapshotPoint {
                version: SNAPSHOT_POINT_VERSION,
                segment: segment_name(last),
                offset: length,
                logical_timestamp: self.logical_timestamp,
                state_digest: SessionSnapshot::state_digest(&self.state),
            })
        } else {
            None
        };
        let _ = report.save_json(&self.data_dir.join("recovery-report.json"));
        Ok(report)
    }

    pub fn rebuild_view(&mut self, view_id: &str) -> Result<RebuildReport> {
        let mut runtimes = self.runtimes.clone();
        let report = rebuild_view(&self.graph, &mut runtimes, &self.state, view_id)?;
        self.runtimes = runtimes;
        Ok(report)
    }

    pub fn rebuild_all_views(&mut self) -> Result<Vec<RebuildReport>> {
        let mut runtimes = self.runtimes.clone();
        let reports = rebuild_all(&self.graph, &mut runtimes, &self.state)?;
        self.runtimes = runtimes;
        Ok(reports)
    }

    pub fn validate_views(&mut self) -> Result<ValidationReport> {
        let mut runtimes = self.runtimes.clone();
        match validate_all(&self.graph, &mut runtimes, &self.state) {
            Ok(report) => {
                self.runtimes = runtimes;
                Ok(report)
            }
            Err(error) => Err(error),
        }
    }

    pub fn diagnose_snapshot(&self, path: &Path) -> Result<SessionSnapshot> {
        SessionSnapshot::load(path, &self.data_dir, None)
    }

    pub fn segment_versions(&self) -> Result<Vec<SegmentInfo>> {
        Ok(discover_segment_paths(&self.data_dir)?
            .iter()
            .map(|path| {
                let bytes = fs::metadata(path)
                    .map(|metadata| metadata.len())
                    .unwrap_or(0);
                SegmentInfo {
                    name: segment_name(path),
                    bytes,
                    digest: crate::digest::digest_file(path)
                        .unwrap_or_else(|_| "error".to_string()),
                }
            })
            .collect())
    }
}

fn apply_record_to_state(state: &mut BTreeMap<String, String>, record: &LogRecord) {
    match record.operation {
        Operation::Put => {
            state.insert(record.key.clone(), record.value.clone());
        }
        Operation::Delete | Operation::Expire => {
            state.remove(&record.key);
        }
    }
}

fn validate_snapshot_position(data_dir: &Path, point: &SnapshotPoint) -> Result<()> {
    let path = data_dir.join(&point.segment);
    let bytes = fs::read(&path).map_err(|error| {
        StoreError::snapshot(format!(
            "snapshot point segment {} cannot be read: {error}",
            path.display()
        ))
        .with_segment(&point.segment)
    })?;
    if point.offset > bytes.len() as u64 {
        return Err(StoreError::snapshot(format!(
            "snapshot offset {} exceeds segment size {}",
            point.offset,
            bytes.len()
        ))
        .with_segment(&point.segment));
    }
    let mut cursor = 0usize;
    while cursor < point.offset as usize {
        if bytes.len().saturating_sub(cursor) < 4 {
            return Err(
                StoreError::snapshot("snapshot offset is not on a record boundary")
                    .with_segment(&point.segment)
                    .set_offset(cursor as u64),
            );
        }
        let body_len = u32::from_le_bytes(bytes[cursor..cursor + 4].try_into().unwrap()) as usize;
        let frame_len = 4usize.saturating_add(body_len);
        let next = cursor.saturating_add(frame_len);
        if next > point.offset as usize {
            return Err(
                StoreError::snapshot("snapshot offset falls inside a record")
                    .with_segment(&point.segment)
                    .set_offset(cursor as u64),
            );
        }
        cursor = next;
    }
    if cursor != point.offset as usize {
        return Err(
            StoreError::snapshot("snapshot offset is not a record boundary")
                .with_segment(&point.segment)
                .set_offset(cursor as u64),
        );
    }
    Ok(())
}

trait StoreErrorOffset {
    fn set_offset(self, offset: u64) -> Self;
}

impl StoreErrorOffset for StoreError {
    fn set_offset(mut self, offset: u64) -> Self {
        self.offset = Some(offset);
        self
    }
}

impl Clone for Store {
    fn clone(&self) -> Self {
        Self {
            data_dir: self.data_dir.clone(),
            config: self.config,
            state: self.state.clone(),
            logical_timestamp: self.logical_timestamp,
            snapshot_point: self.snapshot_point.clone(),
            replay_position: self.replay_position.clone(),
            graph: self.graph.clone(),
            runtimes: self.runtimes.clone(),
            invalidation_trace: self.invalidation_trace.clone(),
            next_trace_step: self.next_trace_step,
            logger: self.logger.clone(),
        }
    }
}
