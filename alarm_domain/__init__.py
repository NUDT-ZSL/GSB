"""Public integration surface for the alarm domain component."""

from .changes import ChangeSet, changeset_from_dict, load_changeset
from .clustering import cluster_events, cluster_summary, cluster_summary_bytes
from .diagnostics import build_diagnostic_model, render_diagnostic
from .errors import DomainError
from .loading import load_events, load_inputs, load_rules, load_topology
from .models import SEVERITIES, AlarmEvent, Cluster, Edge, Rule, Topology
from .rootcause import analyze_clusters, derive_root_causes
from .session import (
    AlarmSession,
    apply_changeset,
    first_analysis_difference,
    rules_version,
    topology_version,
    verify_full_recompute,
)
from .snapshot import (
    SnapshotInfo,
    SnapshotStore,
    replay_snapshot,
    restore_snapshot,
    save_snapshot,
    version_diff,
)
from .tkinter_ui import UiUnavailable, launch_diagnostic_view

__all__ = [
    "AlarmEvent",
    "AlarmSession",
    "ChangeSet",
    "Cluster",
    "DomainError",
    "Edge",
    "Rule",
    "SEVERITIES",
    "SnapshotInfo",
    "SnapshotStore",
    "Topology",
    "UiUnavailable",
    "analyze_clusters",
    "apply_changeset",
    "build_diagnostic_model",
    "changeset_from_dict",
    "cluster_events",
    "cluster_summary",
    "cluster_summary_bytes",
    "derive_root_causes",
    "first_analysis_difference",
    "launch_diagnostic_view",
    "load_changeset",
    "load_events",
    "load_inputs",
    "load_rules",
    "load_topology",
    "render_diagnostic",
    "replay_snapshot",
    "restore_snapshot",
    "rules_version",
    "save_snapshot",
    "topology_version",
    "verify_full_recompute",
    "version_diff",
]
