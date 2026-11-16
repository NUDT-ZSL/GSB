"""Tkinter workbench UI with a deterministic headless renderer fallback.

The Tk window shows, in stable order: ladder progress, per-stage concurrency,
measured throughput, success rate and P95, verdict reason, parameter version
and the snapshot list. When no display is available the same view model is
rendered as plain text so demos/tests stay deterministic on headless hosts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .snapshot import SnapshotStore


@dataclass
class StageRow:
    stage_index: int
    concurrency: int
    duration_s: int
    throughput_rps: int | None
    success_rate_pct: float | None
    p95_latency_ms: int | None
    verdict: str
    saturation_reason: str | None
    state: str


@dataclass
class ViewModel:
    title: str
    params_version: int
    fingerprint: str
    rows: list[StageRow]
    snapshots: list[dict]


def build_view_model(snapshot, store: SnapshotStore) -> ViewModel:
    result_by_index = {r["stage_index"]: r for r in snapshot.stage_results}
    progress_by_index = {p["stage_index"]: p for p in snapshot.progress}
    rows: list[StageRow] = []
    for stage in snapshot.scene.get("stages", []):
        index = stage["index"]
        result = result_by_index.get(index)
        progress = progress_by_index.get(index)
        state = progress["state"] if progress else "pending"
        if result:
            metrics = result["metrics"]
            rows.append(StageRow(
                stage_index=index,
                concurrency=stage["concurrency"],
                duration_s=stage["duration_s"],
                throughput_rps=metrics["throughput_rps"],
                success_rate_pct=round(metrics["success_rate_bp"] / 100.0, 2),
                p95_latency_ms=metrics["p95_latency_ms"],
                verdict=result["verdict"],
                saturation_reason=result["saturation_reason"],
                state="finished",
            ))
        else:
            rows.append(StageRow(
                stage_index=index,
                concurrency=stage["concurrency"],
                duration_s=stage["duration_s"],
                throughput_rps=None,
                success_rate_pct=None,
                p95_latency_ms=None,
                verdict="unknown" if state == "unknown" else "pending",
                saturation_reason=(progress.get("note")
                                   if progress and state == "unknown" else None),
                state=state,
            ))
    snapshots = []
    for name in store.list_names():
        snap = store.load(name)
        snapshots.append({
            "name": snap.name, "status": snap.status,
            "params_version": snap.params_version,
            "finished": ",".join(map(str, snap.result_indexes())) or "-",
        })
    return ViewModel(
        title=f"capbench - {snapshot.name} ({snapshot.status})",
        params_version=snapshot.params_version,
        fingerprint=snapshot.params_fingerprint,
        rows=rows,
        snapshots=snapshots,
    )


def render_text(view: ViewModel) -> str:
    lines = [
        view.title,
        f"params_version={view.params_version} fingerprint={view.fingerprint}",
        "",
        "idx state     conc dur  rps   sr%    p95ms verdict     reason",
    ]
    for row in view.rows:
        lines.append(
            f"{row.stage_index:>3} {row.state:<8} "
            f"{row.concurrency:>4} {row.duration_s:>3} "
            f"{(row.throughput_rps if row.throughput_rps is not None else '-'):>5} "
            f"{(row.success_rate_pct if row.success_rate_pct is not None else '-'):>5} "
            f"{(row.p95_latency_ms if row.p95_latency_ms is not None else '-'):>5} "
            f"{row.verdict:<11} {row.saturation_reason or ''}")
    lines.append("")
    lines.append("snapshots:")
    for snap in view.snapshots:
        lines.append(
            f"  - {snap['name']} status={snap['status']} "
            f"v{snap['params_version']} finished=[{snap['finished']}]")
    return "\n".join(lines)


class TkApp:
    def __init__(self, view: ViewModel) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.root = tk.Tk()
        self.root.title(view.title)
        self.root.geometry("980x520")

        header = ttk.Label(
            self.root,
            text=f"params_version={view.params_version}  "
                 f"fingerprint={view.fingerprint}",
            font=("TkDefaultFont", 10, "bold"))
        header.pack(anchor="w", padx=10, pady=(10, 4))

        columns = ("idx", "state", "conc", "dur", "rps", "sr", "p95",
                   "verdict", "reason")
        tree = ttk.Treeview(self.root, columns=columns, show="headings",
                            height=max(6, len(view.rows)))
        headings = {
            "idx": "Stage", "state": "State", "conc": "Concurrency",
            "dur": "Dur(s)", "rps": "RPS", "sr": "Success%",
            "p95": "P95(ms)", "verdict": "Verdict", "reason": "Reason",
        }
        widths = {"idx": 50, "state": 90, "conc": 100, "dur": 70,
                  "rps": 70, "sr": 90, "p95": 80, "verdict": 110,
                  "reason": 260}
        for col in columns:
            tree.heading(col, text=headings[col])
            tree.column(col, width=widths[col], anchor="w")
        for row in view.rows:
            tree.insert("", "end", values=(
                row.stage_index, row.state, row.concurrency, row.duration_s,
                row.throughput_rps if row.throughput_rps is not None else "-",
                row.success_rate_pct if row.success_rate_pct is not None else "-",
                row.p95_latency_ms if row.p95_latency_ms is not None else "-",
                row.verdict, row.saturation_reason or ""))
        tree.pack(fill="x", padx=10, pady=6)

        ttk.Label(self.root, text="Snapshots",
                  font=("TkDefaultFont", 10, "bold")).pack(
            anchor="w", padx=10, pady=(8, 2))
        snap_box = tk.Listbox(self.root, height=6)
        for snap in view.snapshots:
            snap_box.insert("end",
                            f"{snap['name']}  status={snap['status']}  "
                            f"v{snap['params_version']}  "
                            f"finished=[{snap['finished']}]")
        snap_box.pack(fill="x", padx=10)

    def run(self) -> None:
        self.root.mainloop()

    def render_once(self, output_path: str) -> str:
        """Headless-friendly: pump pending events once, then persist the view."""
        self.root.update_idletasks()
        self.root.update()
        text = render_text(_current_view_holder["view"])
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.root.destroy()
        return text


_current_view_holder: dict = {}


def launch(view: ViewModel, render_output: str | None = None,
           auto_close_ms: int | None = None) -> str:
    _current_view_holder["view"] = view
    if not os.environ.get("DISPLAY"):
        text = render_text(view)
        if render_output:
            with open(render_output, "w", encoding="utf-8") as fh:
                fh.write(text)
        return text
    app = TkApp(view)
    if render_output:
        if auto_close_ms:
            app.root.after(auto_close_ms, app.root.destroy)
        app.root.mainloop()
        text = render_text(view)
        with open(render_output, "w", encoding="utf-8") as fh:
            fh.write(text)
        return text
    app.run()
    return ""
