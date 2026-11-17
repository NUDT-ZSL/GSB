"""Report structures and atomic diagnostic/graph snapshot output."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .graph import DependencyGraph, PolicyPath, Propagation
from .util import atomic_write_json, atomic_write_bytes, cleanup_temp_files, sha256_value


def build_terminal_chain(
    graph: DependencyGraph,
    propagation: Propagation,
    path: PolicyPath | None,
    *,
    terminal_type: str,
    terminal_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy_nodes: list[dict[str, Any]] = []
    if path is not None:
        for entry_id in path.nodes:
            entry = graph.entries.get(entry_id)
            policy_nodes.append(
                {
                    "type": "policy",
                    "id": entry_id,
                    "entry_index": entry.entry_index if entry is not None else None,
                }
            )
    terminal = {"type": terminal_type, "id": terminal_id}
    if metadata:
        terminal.update(metadata)
    nodes = policy_nodes + [terminal]
    edge_kinds = list(path.edge_kinds) if path is not None else []
    edge_kinds.append(f"{terminal_type}_dependency")
    cycle = bool(
        path is not None
        and set(path.nodes).intersection(propagation.cycle_entry_ids)
    )
    chain_payload = {
        "root": path.root if path is not None else "",
        "nodes": nodes,
        "edge_kinds": edge_kinds,
        "cycle": cycle,
    }
    chain_payload["chain_id"] = "chain-" + sha256_value(chain_payload)[:16]
    return chain_payload


def chain_sort_key(chain: dict[str, Any]) -> tuple[Any, ...]:
    return (
        chain.get("root", ""),
        tuple(
            (
                node.get("type", ""),
                node.get("id", ""),
                -1 if node.get("entry_index") is None else node.get("entry_index"),
            )
            for node in chain.get("nodes", [])
        ),
        chain.get("chain_id", ""),
    )


def sort_chains(chains: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for chain in chains:
        unique[str(chain["chain_id"])] = chain
    return sorted(unique.values(), key=chain_sort_key)


def render_mermaid(
    graph: DependencyGraph,
    *,
    affected_entry_ids: Iterable[str] = (),
    changed_entry_ids: Iterable[str] = (),
) -> str:
    affected = set(affected_entry_ids)
    changed = set(changed_entry_ids)
    ids = {
        entry.id: f"n{position}"
        for position, entry in enumerate(graph.sorted_entries, start=1)
    }
    lines = ["flowchart LR"]
    for entry in graph.sorted_entries:
        marker = " [changed]" if entry.id in changed else (
            " [affected]" if entry.id in affected else ""
        )
        label = f"{entry.id}<br/>{entry.kind} v{entry.version}{marker}"
        lines.append(f'  {ids[entry.id]}["{_mermaid_escape(label)}"]')
        if entry.id in changed:
            lines.append(f"  style {ids[entry.id]} fill:#ffe8a3,stroke:#8a6d1d")
        elif entry.id in affected:
            lines.append(f"  style {ids[entry.id]} fill:#ffd8d8,stroke:#b42318")
    for edge in graph.edges:
        lines.append(
            f'  {ids[edge.source]} -->|{_mermaid_escape(edge.kind)}| {ids[edge.target]}'
        )
    for position, cycle in enumerate(graph.cycles, start=1):
        lines.append(
            "  %% E_CYCLE "
            + str(position)
            + ": "
            + " -> ".join(cycle)
        )
    lines.append("")
    return "\n".join(lines)


def write_snapshots(
    output_dir: Path,
    report: dict[str, Any],
    graph_data: dict[str, Any],
    dot_text: str,
    mermaid_text: str,
) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cleanup_temp_files(
        output_dir,
        (
            ".diagnostics.json.",
            "diagnostics.json.",
            ".dependency_graph.json.",
            "dependency_graph.json.",
            ".dependency_graph.dot.",
            "dependency_graph.dot.",
            ".dependency_graph.mmd.",
            "dependency_graph.mmd.",
        ),
    )
    graph_path = output_dir / "dependency_graph.json"
    dot_path = output_dir / "dependency_graph.dot"
    mermaid_path = output_dir / "dependency_graph.mmd"
    report_path = output_dir / "diagnostics.json"

    atomic_write_json(graph_path, graph_data)
    atomic_write_bytes(dot_path, dot_text.encode("utf-8"))
    atomic_write_bytes(mermaid_path, mermaid_text.encode("utf-8"))
    atomic_write_json(report_path, report)
    return {
        "report": str(report_path),
        "dependency_graph_json": str(graph_path),
        "dependency_graph_dot": str(dot_path),
        "dependency_graph_mermaid": str(mermaid_path),
    }


def _mermaid_escape(value: str) -> str:
    return value.replace('"', "&quot;")
