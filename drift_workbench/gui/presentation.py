"""Pure presentation helpers used by the Tkinter workbench and the tests."""

from __future__ import annotations

from typing import Any

from ..engine.models import (
    DRIFT_ARTIFACT_INVALID,
    DRIFT_EXTRA,
    DRIFT_MISSING,
    DRIFT_VALUE_MISMATCH,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
)

HEAT_COLORS = {
    DRIFT_MISSING: "#d94841",
    DRIFT_VALUE_MISMATCH: "#e8943a",
    DRIFT_EXTRA: "#8d6e63",
    DRIFT_ARTIFACT_INVALID: "#7e57c2",
    "clean": "#4caf50",
}

STATUS_COLORS = {
    STATUS_SUCCESS: "#2e7d32",
    STATUS_FAILED: "#c62828",
    STATUS_SKIPPED: "#6a1b9a",
}


def heat_index(
    resource_key: str,
    drifts: list[Any],
    invalid_keys: set[str],
) -> tuple[int, str, str]:
    """Return (severity, drift_type, color) for one resource node.

    Severity scale 0..3 keeps the heat map stable and explainable:
    0 clean, 1 invalid/extra, 2 value mismatch, 3 missing.
    """
    resource_drifts = [
        drift for drift in drifts if drift.resource_key == resource_key
    ]
    types = {drift.drift_type for drift in resource_drifts}
    if DRIFT_MISSING in types:
        return 3, DRIFT_MISSING, HEAT_COLORS[DRIFT_MISSING]
    if DRIFT_VALUE_MISMATCH in types:
        return 2, DRIFT_VALUE_MISMATCH, HEAT_COLORS[DRIFT_VALUE_MISMATCH]
    if resource_key in invalid_keys or DRIFT_ARTIFACT_INVALID in types:
        return 1, DRIFT_ARTIFACT_INVALID, HEAT_COLORS[DRIFT_ARTIFACT_INVALID]
    if DRIFT_EXTRA in types:
        return 1, DRIFT_EXTRA, HEAT_COLORS[DRIFT_EXTRA]
    return 0, "clean", HEAT_COLORS["clean"]


def stable_node_layout(
    nodes: list[str],
    *,
    width: int = 760,
    height: int = 460,
    columns: int = 3,
) -> dict[str, tuple[int, int]]:
    """Deterministic grid layout for dependency graph rendering."""
    ordered = sorted(nodes)
    rows = max(1, (len(ordered) + columns - 1) // columns)
    cell_width = width // max(columns, 1)
    cell_height = height // max(rows, 1)
    layout: dict[str, tuple[int, int]] = {}
    for index, node in enumerate(ordered):
        row, column = divmod(index, columns)
        x = cell_width * column + cell_width // 2
        y = cell_height * row + cell_height // 2
        layout[node] = (x, y)
    return layout


def edge_segments(
    edges: list[tuple[str, str]],
    layout: dict[str, tuple[int, int]],
) -> list[tuple[int, int, int, int]]:
    segments = []
    for source, target in sorted(edges):
        if source in layout and target in layout:
            x1, y1 = layout[source]
            x2, y2 = layout[target]
            segments.append((x1, y1, x2, y2))
    return segments


def summarize_drifts(drifts: list[Any]) -> dict[str, int]:
    summary = {
        DRIFT_MISSING: 0,
        DRIFT_VALUE_MISMATCH: 0,
        DRIFT_EXTRA: 0,
        DRIFT_ARTIFACT_INVALID: 0,
    }
    for drift in drifts:
        summary[drift.drift_type] = summary.get(drift.drift_type, 0) + 1
    return summary


def render_diagnosis_lines(workspace: Any, result: Any | None = None) -> list[str]:
    """Stable, human-readable diagnosis lines for reports and the GUI panel."""
    view = workspace.current_view
    lines: list[str] = []
    if view is None:
        return ["no workspace view loaded"]
    lines.append(
        "view="
        + str(view.view_id)
        + " group_versions="
        + str(dict(view.group_versions))
        + " graph_version="
        + str(view.graph_version)
    )
    lines.append("changed=" + ",".join(view.changed_keys))
    lines.append("invalidated=" + ",".join(view.invalidated_keys))
    summary = summarize_drifts(workspace.last_drifts)
    lines.append(
        "drifts missing={missing} value_mismatch={value_mismatch} "
        "extra={extra} artifact_invalid={artifact_invalid}".format(**summary)
    )
    plan_order = [action.resource_key for action in workspace.last_plan]
    lines.append("plan_order=" + ",".join(plan_order))
    if result is not None:
        lines.append("execution_order=" + ",".join(result.order))
        lines.append(
            "succeeded=" + ",".join(result.succeeded)
            + " failed=" + ",".join(result.failed)
            + " skipped=" + ",".join(result.skipped)
            + " rolled_back=" + ",".join(result.rolled_back)
        )
        for trace in result.traces:
            lines.append(
                f"#{trace.order} {trace.resource_key} {trace.status} "
                f"{trace.code} {trace.reason}"
                + (f" by={trace.triggered_by}" if trace.triggered_by else "")
            )
    return lines


def status_color(status: str) -> str:
    return STATUS_COLORS.get(status, "#455a64")

