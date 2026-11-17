"""Tkinter inspection UI with stable render ordering.

The module imports Tkinter lazily so the rest of the toolkit (engine, CLI,
verifier, tests) keeps working on hosts without a display or without the
Tkinter package - the Windows official Python installer ships it by default.
A background worker thread loads files so the UI never blocks on disk I/O.
"""

from __future__ import annotations

import json
import os
import threading

from .queries import load_queries
from .schema import load_tables
from .snapshot import SnapshotStore


class HeadlessUnavailable(RuntimeError):
    pass


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_view_model(tables_path: str, queries_path: str,
                     result_dir: str, snapshot_dir: str) -> dict:
    tables = load_tables(tables_path)
    plan = load_queries(queries_path, tables)
    report_path = os.path.join(result_dir, "report.json")
    report = _read_json(report_path) if os.path.exists(report_path) else {}
    snapshots = []
    if os.path.isdir(snapshot_dir):
        snapshots = sorted(
            name for name in os.listdir(snapshot_dir)
            if name.endswith(".sqlite3")
        )
    return {
        "queries": [
            {"query_id": item.query_id, "canonical": item.canonical,
             "digest": item.digest}
            for item in plan.all_plans()
        ],
        "report": report,
        "snapshots": snapshots,
    }


def _render_text(view_model: dict) -> str:
    lines = ["# Query identifiers (stable order)"]
    for query in view_model["queries"]:
        lines.append(f"{query['query_id']}\t{query['canonical']}")
        lines.append(f"  digest={query['digest']}")
    report = view_model["report"]
    lines.append("")
    lines.append("# Cursors")
    for name, cursor in sorted(report.get("cursors", {}).items()):
        lines.append(f"{name}\t{cursor}")
    lines.append("")
    lines.append("# Aggregate state by group")
    for query_id in sorted(report.get("aggregate_results", {})):
        lines.append(f"[{query_id}]")
        for row in report["aggregate_results"][query_id]:
            lines.append(json.dumps(row, sort_keys=True,
                                    ensure_ascii=False, separators=(",", ":")))
    lines.append("")
    lines.append("# Window partitions and ring content")
    for query_id in sorted(report.get("window_results", {})):
        lines.append(f"[{query_id}]")
        for row in report["window_results"][query_id]:
            lines.append(json.dumps(row, sort_keys=True,
                                    ensure_ascii=False, separators=(",", ":")))
    lines.append("")
    lines.append("# Snapshots")
    lines.extend(view_model["snapshots"])
    return "\n".join(lines)


def launch_gui(tables_path: str, queries_path: str,
               result_dir: str, snapshot_dir: str) -> None:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:
        raise HeadlessUnavailable(
            "Tkinter is not available in this environment. "
            "Use the CLI 'verify' command on headless hosts; the official "
            "Windows Python installer includes Tkinter by default."
        ) from exc

    root = tk.Tk()
    root.title("AggWin Pipeline Inspector")
    root.geometry("980x720")

    query_list = tk.Listbox(root, width=120, height=10, exportselection=False)
    query_list.pack(fill="x", padx=8, pady=(8, 4))
    detail = tk.Text(root, wrap="none")
    detail.pack(fill="both", expand=True, padx=8, pady=4)
    status = ttk.Label(root, text="loading...", anchor="w")
    status.pack(fill="x", padx=8, pady=(0, 8))

    state: dict = {"view_model": None}

    def load_worker() -> None:
        view_model = _load_view_model(
            tables_path, queries_path, result_dir, snapshot_dir)
        state["view_model"] = view_model

        def apply() -> None:
            query_list.delete(0, tk.END)
            for query in view_model["queries"]:
                query_list.insert(tk.END, query["canonical"])
            detail.insert("1.0", _render_text(view_model))
            status.config(text=(
                f"loaded {len(view_model['queries'])} queries, "
                f"{len(view_model['snapshots'])} snapshots"
            ))

        root.after(0, apply)

    threading.Thread(target=load_worker, daemon=True).start()
    root.mainloop()
