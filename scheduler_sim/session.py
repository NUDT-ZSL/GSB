"""Scheduling session: manifest state, weights, run, snapshots, replay.

Snapshots persist the full state (node-set version, pod-queue version,
plugin weights, all score details, bindings). Restoring compares the
snapshot versions against the current session; on any mismatch the
current state is REPLACED WHOLESALE by the snapshot content (never
merged). After restore, replaying the recorded pods must reproduce
bit-identical scores and bindings, otherwise E_REPLAY is raised.
"""

import copy
import hashlib
import json

import yaml

from . import SESSION_VERSION, SNAPSHOT_FORMAT
from .errors import ReplayError
from .manifest import parse_nodes_doc, parse_pods_doc
from .scoring import DEFAULT_WEIGHTS, schedule_queue


def _canonical(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _digest(doc):
    return hashlib.sha256(_canonical(doc).encode("utf-8")).hexdigest()[:16]


def load_weights(path):
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    weights = doc.get("weights", doc)
    out = {}
    for key in DEFAULT_WEIGHTS:
        value = int(weights.get(key, DEFAULT_WEIGHTS[key]))
        if value < 0:
            raise ValueError(f"weight {key} must be >= 0")
        out[key] = value
    return out


class Session:
    def __init__(self, weights=None):
        self.session_version = SESSION_VERSION
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        self.nodes_doc = None
        self.pods_doc = None
        self.nodes_version = None
        self.pod_queue_version = None
        self.result = None
        self.events = []  # deterministic structured events

    # -- manifest handling ------------------------------------------------
    def load_manifest_docs(self, nodes_doc, pods_doc):
        """Validate and adopt manifests; any error aborts the whole load."""
        nodes_version, nodes = parse_nodes_doc(copy.deepcopy(nodes_doc))
        pods_version, pods = parse_pods_doc(copy.deepcopy(pods_doc))
        self.nodes_doc = copy.deepcopy(nodes_doc)
        self.pods_doc = copy.deepcopy(pods_doc)
        self.nodes_version = _digest(nodes_doc)
        self.pod_queue_version = _digest(pods_doc)
        self._manifest_version_tag = (nodes_version, pods_version)
        self._initial_nodes = nodes
        self._pods = pods
        self.result = None

    def load_manifest_files(self, nodes_path, pods_path):
        with open(nodes_path, "r", encoding="utf-8") as fh:
            nodes_doc = json.load(fh)
        with open(pods_path, "r", encoding="utf-8") as fh:
            pods_doc = json.load(fh)
        self.load_manifest_docs(nodes_doc, pods_doc)

    # -- scheduling -------------------------------------------------------
    def run(self, on_stats_missing=None):
        nodes = copy.deepcopy(self._initial_nodes)

        def stats_hook(pod, node, plugin, reason):
            event = {"pod": pod, "node": node, "plugin": plugin,
                     "reason": reason}
            self.events.append({"code": "E_STATS", **event})
            if on_stats_missing is not None:
                on_stats_missing(**event)

        self.result = schedule_queue(nodes, self._pods, self.weights,
                                     on_stats_missing=stats_hook)
        return self.result

    # -- snapshots --------------------------------------------------------
    def snapshot_dict(self):
        if self.result is None:
            raise ReplayError("no run to snapshot")
        return {
            "snapshot_format": SNAPSHOT_FORMAT,
            "session_version": self.session_version,
            "nodes_version": self.nodes_version,
            "pod_queue_version": self.pod_queue_version,
            "weights": dict(self.weights),
            "nodes_manifest": self.nodes_doc,
            "pods_manifest": self.pods_doc,
            "result": self.result,
        }

    def save_snapshot(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_canonical(self.snapshot_dict()))

    def restore(self, path):
        """Restore a snapshot with whole-state replacement + replay check.

        Returns a list of human-readable restore actions (deterministic).
        """
        with open(path, "r", encoding="utf-8") as fh:
            snap = json.load(fh)
        actions = []
        if snap.get("snapshot_format") != SNAPSHOT_FORMAT:
            raise ReplayError(
                f"unsupported snapshot format {snap.get('snapshot_format')}")
        mismatch = (
            snap.get("session_version") != self.session_version
            or snap.get("nodes_version") != self.nodes_version
            or snap.get("pod_queue_version") != self.pod_queue_version
            or snap.get("weights") != self.weights)
        if mismatch:
            actions.append("version mismatch detected -> replacing current "
                           "session state wholesale from snapshot")
        else:
            actions.append("snapshot versions match current session -> "
                           "adopting snapshot state (idempotent replace)")
        # Whole replacement in both cases; never an incremental merge.
        self.weights = dict(snap["weights"])
        self.load_manifest_docs(snap["nodes_manifest"], snap["pods_manifest"])
        recorded = snap["result"]
        replayed = self.run()
        if _canonical(replayed) != _canonical(recorded):
            raise ReplayError(
                "replayed scores/bindings differ from snapshot; "
                "session state is not reproducible",
                entry="snapshot.replay", field="result")
        actions.append("replay verified: scores and bindings are "
                       "bit-identical to the snapshot")
        return actions
