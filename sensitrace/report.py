"""Deterministic diagnostic report and flow-graph export."""

from __future__ import annotations

import os
from typing import Any, Dict

from .atomic import atomic_write_json, build_envelope
from .checkpoint import RULES_VERSION, STRUCTURE_VERSION
from .errors import sort_key
from .lineage import LineageEngine

DIAGNOSTICS_NAME = "diagnostics.json"
GRAPH_NAME = "flow_graph.json"


def build_report(engine: LineageEngine) -> Dict[str, Any]:
    """Build the standalone diagnostics report payload."""

    diagnostics = sorted(engine.diagnostics, key=sort_key)
    counts: Dict[str, int] = {}
    for diagnostic in diagnostics:
        counts[diagnostic.code] = counts.get(diagnostic.code, 0) + 1
    return {
        "summary": {
            "node_count": len(engine.nodes),
            "edge_count": len(engine.edges),
            "diagnostic_count": len(diagnostics),
            "counts_by_code": dict(sorted(counts.items())),
        },
        "diagnostics": [item.to_dict() for item in diagnostics],
    }


def build_graph(engine: LineageEngine) -> Dict[str, Any]:
    """Build the flow-graph payload (nodes, edges and transitive closure)."""

    state = engine.to_dict()
    return {
        "versions": state["versions"],
        "nodes": state["nodes"],
        "edges": state["edges"],
        "transitive_closure": state["transitive_closure"],
    }


def export_report(engine: LineageEngine, output_dir: str,
                  crash_after_tmp: bool = False) -> Dict[str, str]:
    """Atomically write both artefacts and return their paths."""

    os.makedirs(output_dir, exist_ok=True)
    diagnostics_path = os.path.join(output_dir, DIAGNOSTICS_NAME)
    graph_path = os.path.join(output_dir, GRAPH_NAME)

    atomic_write_json(
        diagnostics_path,
        build_envelope(STRUCTURE_VERSION, RULES_VERSION, build_report(engine)),
        crash_after_tmp=crash_after_tmp,
    )
    atomic_write_json(
        graph_path,
        build_envelope(STRUCTURE_VERSION, RULES_VERSION, build_graph(engine)),
    )
    return {"diagnostics": diagnostics_path, "graph": graph_path}
