use crate::digest::{digest_serialized, digest_str};
use crate::error::{Result, StoreError};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Dependency {
    Key { key: String },
    Prefix { prefix: String },
    View { view: String },
}

impl Dependency {
    pub fn label(&self) -> String {
        match self {
            Dependency::Key { key } => format!("key:{key}"),
            Dependency::Prefix { prefix } => format!("prefix:{prefix}"),
            Dependency::View { view } => format!("view:{view}"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ViewKind {
    PrefixCount {
        prefix: String,
    },
    SecondaryIndex {
        index: String,
        prefix: String,
        #[serde(default = "default_delimiter")]
        delimiter: String,
        #[serde(default = "default_field")]
        field: usize,
    },
}

fn default_delimiter() -> String {
    ":".to_string()
}

fn default_field() -> usize {
    1
}

impl ViewKind {
    pub fn kind_name(&self) -> &'static str {
        match self {
            ViewKind::PrefixCount { .. } => "prefix_count",
            ViewKind::SecondaryIndex { .. } => "secondary_index",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ViewDefinition {
    pub id: String,
    pub kind: ViewKind,
    #[serde(default)]
    pub dependencies: Vec<Dependency>,
}

fn default_graph_version() -> u32 {
    1
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphDocument {
    #[serde(default = "default_graph_version")]
    pub graph_version: u32,
    #[serde(default)]
    pub views: Vec<ViewDefinition>,
}

impl GraphDocument {
    pub fn canonicalized(mut self) -> Self {
        for view in &mut self.views {
            view.dependencies
                .sort_by_key(|dependency| dependency.label());
            view.dependencies.dedup();
        }
        self.views.sort_by(|left, right| left.id.cmp(&right.id));
        self
    }

    pub fn digest(&self) -> String {
        digest_serialized(&self.clone().canonicalized()).unwrap_or_else(|_| digest_str("graph"))
    }
}

#[derive(Debug, Clone)]
pub struct DependencyGraph {
    pub document: GraphDocument,
    pub topological_order: Vec<String>,
    pub reverse_edges: BTreeMap<String, Vec<String>>,
    pub digest: String,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct GraphLoadOptions {
    pub inject_dependency_cycle: bool,
}

impl DependencyGraph {
    pub fn load_file(path: &Path, options: GraphLoadOptions) -> Result<Self> {
        let bytes = std::fs::read(path).map_err(|error| {
            StoreError::depend(format!("failed to read graph {}: {error}", path.display()))
        })?;
        let document: GraphDocument = serde_json::from_slice(&bytes).map_err(|error| {
            StoreError::depend(format!("invalid graph JSON {}: {error}", path.display()))
        })?;
        Self::from_document(document, options)
    }

    pub fn from_document(mut document: GraphDocument, options: GraphLoadOptions) -> Result<Self> {
        document = document.canonicalized();
        if options.inject_dependency_cycle {
            inject_two_node_cycle(&mut document);
            document = document.canonicalized();
        }
        let by_id: BTreeMap<String, ViewDefinition> = document
            .views
            .iter()
            .cloned()
            .map(|view| (view.id.clone(), view))
            .collect();
        if by_id.len() != document.views.len() {
            return Err(StoreError::depend(
                "dependency graph contains duplicate view ids",
            ));
        }
        for view in &document.views {
            if view.id.trim().is_empty() {
                return Err(StoreError::depend("view id must not be empty"));
            }
            for dependency in &view.dependencies {
                if let Dependency::View { view: target } = dependency {
                    if target == &view.id {
                        return Err(StoreError::depend(format!(
                            "dependency cycle detected at view {}",
                            view.id
                        )));
                    }
                    if !by_id.contains_key(target) {
                        return Err(StoreError::depend(format!(
                            "view {} depends on unknown view {}",
                            view.id, target
                        )));
                    }
                }
            }
        }

        let mut indegree: BTreeMap<String, usize> =
            by_id.keys().map(|id| (id.clone(), 0usize)).collect();
        let mut reverse_edges: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for view in &document.views {
            let mut targets: BTreeSet<String> = BTreeSet::new();
            for dependency in &view.dependencies {
                if let Dependency::View { view: target } = dependency {
                    targets.insert(target.clone());
                }
            }
            for target in targets {
                *indegree.get_mut(&view.id).unwrap() += 1;
                reverse_edges
                    .entry(target)
                    .or_default()
                    .push(view.id.clone());
            }
        }
        for dependents in reverse_edges.values_mut() {
            dependents.sort();
            dependents.dedup();
        }
        let mut ready: BTreeSet<String> = indegree
            .iter()
            .filter_map(|(id, degree)| if *degree == 0 { Some(id.clone()) } else { None })
            .collect();
        let mut topological_order = Vec::with_capacity(by_id.len());
        while let Some(id) = ready.iter().next().cloned() {
            ready.remove(&id);
            topological_order.push(id.clone());
            if let Some(dependents) = reverse_edges.get(&id) {
                for dependent in dependents {
                    let degree = indegree.get_mut(dependent).unwrap();
                    *degree -= 1;
                    if *degree == 0 {
                        ready.insert(dependent.clone());
                    }
                }
            }
        }
        if topological_order.len() != by_id.len() {
            let cycle_nodes: Vec<String> = indegree
                .iter()
                .filter_map(|(id, degree)| if *degree > 0 { Some(id.clone()) } else { None })
                .collect();
            return Err(StoreError::depend(format!(
                "dependency graph contains a cycle; affected views: {}",
                cycle_nodes.join(",")
            )));
        }
        let digest = document.digest();
        Ok(Self {
            document,
            topological_order,
            reverse_edges,
            digest,
        })
    }

    pub fn empty() -> Self {
        Self::from_document(
            GraphDocument {
                graph_version: 1,
                views: Vec::new(),
            },
            GraphLoadOptions::default(),
        )
        .expect("empty graph is valid")
    }

    pub fn add_view(&self, view: ViewDefinition) -> Result<Self> {
        if self
            .document
            .views
            .iter()
            .any(|candidate| candidate.id == view.id)
        {
            return Err(StoreError::depend(format!(
                "view {} is already declared",
                view.id
            )));
        }
        let mut document = self.document.clone();
        document.views.push(view);
        document.graph_version = document.graph_version.saturating_add(1);
        Self::from_document(document, GraphLoadOptions::default())
    }

    pub fn views(&self) -> impl Iterator<Item = &ViewDefinition> {
        self.document.views.iter()
    }

    pub fn view(&self, id: &str) -> Option<&ViewDefinition> {
        self.document.views.iter().find(|view| view.id == id)
    }

    /// Return direct dependencies that match a changed key, with a deterministic label.
    pub fn matching_direct_dependencies(&self, key: &str) -> Vec<(String, String)> {
        let mut matches = Vec::new();
        for view in &self.document.views {
            for dependency in &view.dependencies {
                let matched = match dependency {
                    Dependency::Key { key: required } => required == key,
                    Dependency::Prefix { prefix } => key.starts_with(prefix),
                    Dependency::View { .. } => false,
                };
                if matched {
                    matches.push((view.id.clone(), dependency.label()));
                }
            }
        }
        matches.sort();
        matches.dedup();
        matches
    }

    pub fn dependents(&self, view_id: &str) -> &[String] {
        self.reverse_edges
            .get(view_id)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }
}

fn inject_two_node_cycle(document: &mut GraphDocument) {
    let view_ids: Vec<String> = document.views.iter().map(|view| view.id.clone()).collect();
    if view_ids.len() == 1 {
        document.views[0].dependencies.push(Dependency::View {
            view: view_ids[0].clone(),
        });
    } else if view_ids.len() >= 2 {
        let first = view_ids[0].clone();
        let last = view_ids[view_ids.len() - 1].clone();
        if let Some(view) = document.views.iter_mut().find(|view| view.id == first) {
            view.dependencies
                .push(Dependency::View { view: last.clone() });
        }
        if let Some(view) = document.views.iter_mut().find(|view| view.id == last) {
            view.dependencies.push(Dependency::View { view: first });
        }
    }
}
