"""Tkinter renderer. Import is deferred so headless smoke tests still work."""
from __future__ import annotations

import os
from typing import List

from .snapshot import load_snapshot
from .view_model import DashboardModel, build_dashboard_model


def list_snapshot_files(session_dir: str) -> List[dict]:
    if not os.path.isdir(session_dir):
        return []
    items = []
    for name in sorted(os.listdir(session_dir)):
        if name.endswith(".json"):
            full = os.path.join(session_dir, name)
            items.append({"path": full, "size": os.path.getsize(full)})
    return items


def render_headless(snapshot_path: str) -> DashboardModel:
    snapshot = load_snapshot(snapshot_path)
    return build_dashboard_model(snapshot, list_snapshot_files(os.path.dirname(snapshot_path)))


def launch_tk(snapshot_path: str) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:
        raise RuntimeError(
            "Tkinter is unavailable in this Python installation; "
            "use render_headless() or install the standard-library Tk component."
        ) from exc

    model = render_headless(snapshot_path)
    root = tk.Tk()
    root.title(model.title)
    root.geometry("1080x760")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    graph_tab = ttk.Frame(notebook)
    state_tab = ttk.Frame(notebook)
    timeline_tab = ttk.Frame(notebook)
    wait_tab = ttk.Frame(notebook)
    snap_tab = ttk.Frame(notebook)
    notebook.add(graph_tab, text="Dependency graph")
    notebook.add(state_tab, text="Stage states")
    notebook.add(timeline_tab, text="Resource timeline")
    notebook.add(wait_tab, text="Waiting queue")
    notebook.add(snap_tab, text="Snapshots")

    _populate_graph(graph_tab, model)
    _populate_states(state_tab, model)
    _populate_timeline(timeline_tab, model)
    _populate_waiting(wait_tab, model)
    _populate_snapshots(snap_tab, model)
    root.mainloop()
    return 0


def _table(parent, columns, rows):
    import tkinter as tk
    from tkinter import ttk

    tree = ttk.Treeview(parent, columns=columns, show="headings")
    for column in columns:
        tree.heading(column, text=column)
        tree.column(column, width=140, anchor="w")
    for row in rows:
        tree.insert("", "end", values=row)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    tree.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    return tree


def _populate_graph(parent, model: DashboardModel) -> None:
    rows = [(source, target, kind) for source, target, kind in model.edges]
    _table(parent, ("upstream", "downstream", "kind"), rows)


def _populate_states(parent, model: DashboardModel) -> None:
    rows = [
        (
            row.stage_id,
            row.state,
            row.strong_deps,
            row.weak_deps,
            row.resource_units,
            row.attempts,
            row.missing_upstreams,
            row.output_sha256[:16],
        )
        for row in model.stage_rows
    ]
    _table(
        parent,
        (
            "stage",
            "state",
            "strong",
            "weak",
            "units",
            "attempts",
            "missing",
            "sha256_16",
        ),
        rows,
    )


def _populate_timeline(parent, model: DashboardModel) -> None:
    rows = [
        (
            band.stage_id,
            band.start_mono_ms,
            band.end_mono_ms,
            band.end_mono_ms - band.start_mono_ms,
            band.resource_units,
            band.state,
        )
        for band in model.timeline
    ]
    rows += [
        (point["stage_id"], point["mono_ms"], "", "", point["used"], point["direction"])
        for point in model.resource_curve
    ]
    _table(
        parent,
        ("stage", "start_ms", "end_ms", "duration_ms", "units/used", "state/direction"),
        rows,
    )


def _populate_waiting(parent, model: DashboardModel) -> None:
    rows = [
        (
            event["stage_id"],
            event["seq"],
            event["reason"],
            event["start_mono_ms"],
            event["resource_units"],
        )
        for event in model.waiting
    ]
    _table(parent, ("stage", "seq", "reason", "wait_start_ms", "units"), rows)


def _populate_snapshots(parent, model: DashboardModel) -> None:
    rows = [(item["path"], item["size"]) for item in model.snapshots]
    _table(parent, ("snapshot_path", "size_bytes"), rows)
