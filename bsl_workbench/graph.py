from __future__ import annotations

import json
from typing import Any, Optional


def _dot_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _reports(payload: dict[str, Any], file_id: Optional[str] = None) -> list[dict[str, Any]]:
    if payload.get("schema_version") == "bsl-batch-report-v1":
        reports = payload.get("files", [])
        if file_id:
            reports = [
                report
                for report in reports
                if report.get("file", {}).get("file_id") == file_id
            ]
        return reports
    return [payload]


def render_dot(payload: dict[str, Any], file_id: Optional[str] = None) -> str:
    reports = _reports(payload, file_id)
    lines = ["digraph BSLDependencies {", "  rankdir=LR;", "  node [shape=box];"]
    for report_index, report in enumerate(reports):
        file_info = report.get("file", {})
        cluster_id = f"cluster_{report_index}"
        lines.append(f"  subgraph {cluster_id} {{")
        title = file_info.get("file_id") or f"report-{report_index}"
        lines.append(f"    label={_dot_quote(title)};")
        segments = {
            segment["canonical_key"]: segment for segment in report.get("segments", [])
        }
        cycle_nodes = set()
        for diagnostic in report.get("diagnostics", []):
            for key in diagnostic.get("details", {}).get("cycle_keys", [])[:-1]:
                cycle_nodes.add(key)

        for node in report.get("dependencies", {}).get("nodes", []):
            segment = segments.get(node)
            label = segment.get("name", node) if segment else node
            attributes = [f"label={_dot_quote(label)}"]
            if node in cycle_nodes:
                attributes.append('color="red"')
                attributes.append('penwidth="2"')
            lines.append(f"    {_dot_quote(node)} [{', '.join(attributes)}];")

        for edge in report.get("dependencies", {}).get("edges", []):
            style = "dashed" if edge.get("kind") == "reference" else "solid"
            color = "darkorange" if edge.get("kind") == "reference" else "black"
            lines.append(
                "    "
                + _dot_quote(edge["from"])
                + " -> "
                + _dot_quote(edge["to"])
                + f" [style={style}, color={color}, label={_dot_quote(edge.get('kind', ''))}];"
            )
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_mermaid(payload: dict[str, Any], file_id: Optional[str] = None) -> str:
    lines = ["flowchart LR"]
    for report in _reports(payload, file_id):
        for edge in report.get("dependencies", {}).get("edges", []):
            source = _mermaid_id(edge["from"])
            target = _mermaid_id(edge["to"])
            label = edge.get("kind", "dependency")
            lines.append(f'  {source}["{_mermaid_label(edge["from"])}"]')
            lines.append(f'  {target}["{_mermaid_label(edge["to"])}"]')
            arrow = "-.->" if edge.get("kind") == "reference" else "-->"
            lines.append(f"  {source} {arrow}|{label}| {target}")
    return "\n".join(lines) + "\n"


def _mermaid_id(value: str) -> str:
    return "n_" + str(_stable_hash(value))


def _stable_hash(value: str) -> int:
    import hashlib

    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def _mermaid_label(value: str) -> str:
    return value.replace('"', "'")
