use crate::digest::digest_serialized;
use crate::error::{Result, StoreError};
use crate::graph::{Dependency, DependencyGraph, ViewDefinition, ViewKind};
use crate::record::{LogRecord, Operation};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub type ViewEntries = BTreeMap<String, Vec<String>>;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TraceStep {
    pub step: u64,
    pub trigger: String,
    pub affected_view: String,
    pub relation: String,
    pub action: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ViewRuntime {
    pub id: String,
    pub version: u64,
    pub valid: bool,
    pub content: Option<ViewEntries>,
    pub content_digest: Option<String>,
    pub last_observed_digest: Option<String>,
    pub built_version: u64,
    pub built_digest: Option<String>,
}

impl ViewRuntime {
    pub fn empty(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            version: 1,
            valid: false,
            content: None,
            content_digest: None,
            last_observed_digest: None,
            built_version: 0,
            built_digest: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ViewStatus {
    pub id: String,
    pub kind: String,
    pub version: u64,
    pub valid: bool,
    pub digest: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RebuildReport {
    pub view: String,
    pub version: u64,
    pub digest: String,
    pub entry_count: usize,
    pub previous_digest: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ViewValidation {
    pub id: String,
    pub version: u64,
    pub digest: String,
    pub previous_digest: Option<String>,
    pub status: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationReport {
    pub code: String,
    pub views: Vec<ViewValidation>,
    pub first_different_view: Option<String>,
    pub first_different_key: Option<String>,
}

pub fn initialize_runtimes(graph: &DependencyGraph) -> BTreeMap<String, ViewRuntime> {
    graph
        .views()
        .map(|view| (view.id.clone(), ViewRuntime::empty(view.id.clone())))
        .collect()
}

pub fn ensure_runtime_entries(
    graph: &DependencyGraph,
    runtimes: &mut BTreeMap<String, ViewRuntime>,
) {
    for view in graph.views() {
        runtimes
            .entry(view.id.clone())
            .or_insert_with(|| ViewRuntime::empty(view.id.clone()));
    }
    runtimes.retain(|id, _| graph.view(id).is_some());
}

pub fn view_statuses(
    graph: &DependencyGraph,
    runtimes: &BTreeMap<String, ViewRuntime>,
) -> Vec<ViewStatus> {
    graph
        .document
        .views
        .iter()
        .map(|definition| {
            let runtime = runtimes.get(&definition.id);
            ViewStatus {
                id: definition.id.clone(),
                kind: definition.kind.kind_name().to_string(),
                version: runtime.map(|value| value.version).unwrap_or(1),
                valid: runtime.map(|value| value.valid).unwrap_or(false),
                digest: runtime.and_then(|value| {
                    value
                        .content_digest
                        .clone()
                        .or_else(|| value.built_digest.clone())
                }),
            }
        })
        .collect()
}

pub fn propagate_invalidation(
    graph: &DependencyGraph,
    runtimes: &mut BTreeMap<String, ViewRuntime>,
    state: &BTreeMap<String, String>,
    record: &LogRecord,
    next_trace_step: &mut u64,
) -> Result<Vec<TraceStep>> {
    let direct_matches: BTreeMap<String, String> = graph
        .matching_direct_dependencies(&record.key)
        .into_iter()
        .collect();
    if direct_matches.is_empty() {
        return Ok(Vec::new());
    }

    let mut candidates: BTreeSet<String> = direct_matches.keys().cloned().collect();
    let mut queue: Vec<String> = direct_matches.keys().cloned().collect();
    while let Some(view_id) = queue.pop() {
        for dependent in graph.dependents(&view_id) {
            if candidates.insert(dependent.clone()) {
                queue.push(dependent.clone());
            }
        }
    }

    let trigger = format!(
        "{}:{}@{}",
        operation_name(record.operation),
        record.key,
        record.logical_timestamp
    );
    let mut changed_now: BTreeSet<String> = BTreeSet::new();
    let mut trace = Vec::new();
    ensure_runtime_entries(graph, runtimes);

    for view_id in &graph.topological_order {
        if !candidates.contains(view_id) {
            continue;
        }
        let definition = match graph.view(view_id) {
            Some(definition) => definition,
            None => continue,
        };
        let direct_relation = direct_matches.get(view_id).cloned();
        let indirect_parent = definition.dependencies.iter().find_map(|dependency| {
            if let Dependency::View { view } = dependency {
                if changed_now.contains(view) {
                    return Some(format!("view:{view}"));
                }
            }
            None
        });
        let relation = match (direct_relation, indirect_parent) {
            (Some(value), _) => value,
            (None, Some(value)) => value,
            (None, None) => continue,
        };
        let entries = compute_content(definition, state)?;
        let digest = entries_digest(&entries)?;
        let runtime = runtimes
            .get_mut(view_id)
            .expect("runtime entry was inserted above");
        if runtime.last_observed_digest.as_deref() == Some(digest.as_str()) {
            continue;
        }
        runtime.last_observed_digest = Some(digest);
        runtime.valid = false;
        runtime.version = runtime.version.saturating_add(1);
        trace.push(TraceStep {
            step: *next_trace_step,
            trigger: trigger.clone(),
            affected_view: view_id.clone(),
            relation,
            action: "invalidated".to_string(),
        });
        *next_trace_step = next_trace_step.saturating_add(1);
        changed_now.insert(view_id.clone());
    }
    Ok(trace)
}

pub fn rebuild_view(
    graph: &DependencyGraph,
    runtimes: &mut BTreeMap<String, ViewRuntime>,
    state: &BTreeMap<String, String>,
    view_id: &str,
) -> Result<RebuildReport> {
    let definition = graph
        .view(view_id)
        .ok_or_else(|| StoreError::input(format!("unknown view {view_id}")))?;
    let runtime = runtimes
        .entry(view_id.to_string())
        .or_insert_with(|| ViewRuntime::empty(view_id));
    let previous_digest = runtime.content_digest.clone();
    // Recompute strictly from the base state and definition. The stale cache is never read.
    let entries = compute_content(definition, state)?;
    let digest = entries_digest(&entries)?;
    runtime.content = Some(entries.clone());
    runtime.content_digest = Some(digest.clone());
    runtime.last_observed_digest = Some(digest.clone());
    runtime.built_digest = Some(digest.clone());
    runtime.built_version = runtime.version;
    runtime.valid = true;
    Ok(RebuildReport {
        view: view_id.to_string(),
        version: runtime.version,
        digest,
        entry_count: entries.len(),
        previous_digest,
    })
}

pub fn rebuild_all(
    graph: &DependencyGraph,
    runtimes: &mut BTreeMap<String, ViewRuntime>,
    state: &BTreeMap<String, String>,
) -> Result<Vec<RebuildReport>> {
    ensure_runtime_entries(graph, runtimes);
    let mut reports = Vec::new();
    for view_id in &graph.topological_order {
        reports.push(rebuild_view(graph, runtimes, state, view_id)?);
    }
    Ok(reports)
}

/// Validate cache metadata against a fresh, independent rebuild. No stale cache is used to
/// compute the expected value. The first mismatch is returned as E_VIEW with a stable view id
/// and the first differing content key.
pub fn validate_all(
    graph: &DependencyGraph,
    runtimes: &mut BTreeMap<String, ViewRuntime>,
    state: &BTreeMap<String, String>,
) -> Result<ValidationReport> {
    ensure_runtime_entries(graph, runtimes);
    let mut views = Vec::new();
    for view_id in &graph.topological_order {
        let definition = graph.view(view_id).unwrap();
        let runtime = runtimes.get_mut(view_id).unwrap();
        let pre_version = runtime.version;
        let pre_valid = runtime.valid;
        let pre_content = runtime.content.clone();
        let pre_digest = runtime.content_digest.clone();
        let pre_built_version = runtime.built_version;
        let pre_built_digest = runtime.built_digest.clone();
        let expected_entries = compute_content(definition, state)?;
        let expected_digest = entries_digest(&expected_entries)?;

        let version_changed = pre_version != pre_built_version;
        let digest_changed = pre_built_digest.as_deref() != Some(expected_digest.as_str());
        if version_changed != digest_changed {
            return Err(view_error(
                graph,
                view_id,
                state,
                pre_content.as_ref(),
                &expected_entries,
                format!(
                    "version/digest consistency violation: version_changed={version_changed} digest_changed={digest_changed}"
                ),
            ));
        }
        if pre_valid && pre_digest.as_deref() != Some(expected_digest.as_str()) {
            return Err(view_error(
                graph,
                view_id,
                state,
                pre_content.as_ref(),
                &expected_entries,
                "valid cache digest differs from a fresh rebuild".to_string(),
            ));
        }
        if !pre_valid && !version_changed {
            return Err(view_error(
                graph,
                view_id,
                state,
                pre_content.as_ref(),
                &expected_entries,
                "cache is invalid but its version was not advanced".to_string(),
            ));
        }

        // Rebuild without changing the version. The existing content is only used afterwards for
        // reporting the first differing key; it is never used as computation input.
        let rebuilt = rebuild_view(graph, runtimes, state, view_id)?;
        debug_assert_eq!(rebuilt.digest, expected_digest);
        views.push(ViewValidation {
            id: view_id.clone(),
            version: pre_version,
            digest: expected_digest.clone(),
            previous_digest: pre_digest,
            status: "ok".to_string(),
        });
    }
    Ok(ValidationReport {
        code: "OK".to_string(),
        views,
        first_different_view: None,
        first_different_key: None,
    })
}

fn view_error(
    graph: &DependencyGraph,
    view_id: &str,
    state: &BTreeMap<String, String>,
    previous: Option<&ViewEntries>,
    expected: &ViewEntries,
    message: String,
) -> StoreError {
    let first_key = first_different_key(previous, expected);
    let mut error = StoreError::view(format!(
        "view validation failed: view={view_id} first_different_key={} reason={message}",
        first_key.as_deref().unwrap_or("none")
    ));
    if let Some(definition) = graph.view(view_id) {
        if let ViewKind::PrefixCount { prefix } = &definition.kind {
            if state.keys().all(|key| !key.starts_with(prefix)) {
                error.message.push_str(" prefix_state=empty");
            }
        }
    }
    error
}

pub fn first_different_key(
    previous: Option<&ViewEntries>,
    expected: &ViewEntries,
) -> Option<String> {
    let keys: BTreeSet<String> = previous
        .into_iter()
        .flat_map(|value| value.keys().cloned())
        .chain(expected.keys().cloned())
        .collect();
    keys.into_iter()
        .find(|key| previous.and_then(|value| value.get(key)) != expected.get(key))
}

pub fn compute_content(
    definition: &ViewDefinition,
    state: &BTreeMap<String, String>,
) -> Result<ViewEntries> {
    let mut entries = ViewEntries::new();
    match &definition.kind {
        ViewKind::PrefixCount { prefix } => {
            let count = state.keys().filter(|key| key.starts_with(prefix)).count();
            entries.insert(prefix.clone(), vec![count.to_string()]);
        }
        ViewKind::SecondaryIndex {
            index,
            prefix,
            delimiter,
            field,
        } => {
            if delimiter.is_empty() {
                return Err(StoreError::depend(format!(
                    "secondary index {index} has an empty delimiter"
                )));
            }
            for key in state.keys().filter(|key| key.starts_with(prefix)) {
                let bucket = key
                    .split(delimiter)
                    .nth(*field)
                    .unwrap_or(key.as_str())
                    .to_string();
                entries.entry(bucket).or_default().push(key.clone());
            }
            for keys in entries.values_mut() {
                keys.sort();
            }
        }
    }
    Ok(entries)
}

pub fn entries_digest(entries: &ViewEntries) -> Result<String> {
    digest_serialized(entries)
        .map_err(|error| StoreError::view(format!("failed to serialize view content: {error}")))
}

fn operation_name(operation: Operation) -> &'static str {
    match operation {
        Operation::Put => "put",
        Operation::Delete => "delete",
        Operation::Expire => "expire",
    }
}
