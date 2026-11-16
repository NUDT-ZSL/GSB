"""Tkinter diagnostic workbench.

Tkinter is imported lazily inside :func:`launch` so the engine, tests and demo
run on headless machines (or minimal Python installs) without a display. On a
standard Windows Python 3.11+ installation ``tkinter`` is included.
"""

from __future__ import annotations

from pathlib import Path

from ..engine.drift import build_plan, compute_drifts
from ..engine.engine import run_convergence
from ..engine.errors import WorkbenchError
from ..engine.snapshot import restore_snapshot, save_snapshot
from ..engine.workspace import Workspace
from .presentation import (
    HEAT_COLORS,
    heat_index,
    render_diagnosis_lines,
    stable_node_layout,
    status_color,
)


class DriftWorkbenchApp:
    def __init__(self, root, workspace, tk_module, ttk_module):
        self.tk = tk_module
        self.ttk = ttk_module
        self.root = root
        self.workspace = workspace
        self.last_result = None
        root.title("声明式配置漂移收敛诊断工作台")
        root.geometry("1180x760")

        self.status_var = self.tk.StringVar(value="ready")
        self._build_toolbar()
        self._build_body()
        self.refresh_all()

    def _build_toolbar(self):
        toolbar = self.ttk.Frame(self.root, padding=8)
        toolbar.pack(fill="x")
        buttons = [
            ("重新计算漂移", self.action_recompute),
            ("执行收敛", self.action_converge),
            ("保存快照", self.action_save),
            ("恢复快照", self.action_restore),
            ("视图对比", self.action_compare),
            ("刷新", self.refresh_all),
        ]
        for text, command in buttons:
            self.ttk.Button(toolbar, text=text, command=command).pack(
                side="left", padx=4
            )
        self.ttk.Label(toolbar, textvariable=self.status_var).pack(
            side="right", padx=8
        )

    def _build_body(self):
        body = self.ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        self.canvas = _ScrolledCanvas(body, self.tk, self.ttk)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        side = self.ttk.Frame(body)
        side.grid(row=0, column=1, sticky="nsew")
        side.rowconfigure(0, weight=1)
        side.rowconfigure(1, weight=1)
        side.columnconfigure(0, weight=1)

        self.resource_tree = self.ttk.Treeview(
            side,
            columns=("heat", "artifact", "invalid"),
            show="headings",
            height=10,
        )
        for column, title in (
            ("heat", "漂移热力"),
            ("artifact", "产物版本"),
            ("invalid", "失效来源"),
        ):
            self.resource_tree.heading(column, text=title)
            self.resource_tree.column(column, width=110, anchor="center")
        self.resource_tree.grid(row=0, column=0, sticky="nsew")

        self.diagnosis = self.ttk.Treeview(
            side, columns=("line",), show="headings", height=12
        )
        self.diagnosis.heading("line", text="诊断结论 / 收敛轨迹")
        self.diagnosis.column("line", width=360, anchor="w")
        self.diagnosis.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def action_recompute(self):
        try:
            compute_drifts(self.workspace)
            build_plan(self.workspace)
        except WorkbenchError as exc:
            self.tk.messagebox.showerror(exc.code, exc.message)
            return
        self.status_var.set("drift recomputed")
        self.refresh_all()

    def action_converge(self):
        try:
            self.last_result = run_convergence(self.workspace)
        except WorkbenchError as exc:
            self.tk.messagebox.showerror(exc.code, exc.message)
            return
        self.status_var.set("convergence finished")
        self.refresh_all()

    def action_save(self):
        target = Path("runtime/gui_session.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            save_snapshot(self.workspace, target, result=self.last_result)
        except WorkbenchError as exc:
            self.tk.messagebox.showerror(exc.code, exc.message)
            return
        self.status_var.set(f"snapshot saved: {target}")

    def action_restore(self):
        target = Path("runtime/gui_session.json")
        if not target.exists():
            self.tk.messagebox.showwarning(
                "restore", "runtime/gui_session.json does not exist"
            )
            return
        try:
            restore_snapshot(self.workspace, target)
        except WorkbenchError as exc:
            self.tk.messagebox.showerror(exc.code, exc.message)
            return
        self.last_result = None
        self.status_var.set("workspace wholesale replaced from snapshot")
        self.refresh_all()

    def action_compare(self):
        import json

        window = self.tk.Toplevel(self.root)
        window.title("历史视图对比")
        tree = self.ttk.Treeview(
            window,
            columns=("view", "groups", "graph", "changed", "invalidated"),
            show="headings",
            width=900,
        )
        for column, title in (
            ("view", "视图"),
            ("groups", "声明组版本"),
            ("graph", "图版本"),
            ("changed", "重编译资源"),
            ("invalidated", "失效传播"),
        ):
            tree.heading(column, text=title)
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        for view in self.workspace.views:
            tree.insert(
                "",
                "end",
                values=(
                    view.view_id,
                    json.dumps(view.group_versions, sort_keys=True),
                    view.graph_version,
                    ",".join(view.changed_keys),
                    ",".join(view.invalidated_keys),
                ),
            )

    def refresh_all(self):
        self._refresh_graph()
        self._refresh_resources()
        self._refresh_diagnosis()

    def _refresh_graph(self):
        self.canvas.clear()
        view = self.workspace.current_view
        if view is None:
            return
        nodes = list(view.graph.nodes)
        layout = stable_node_layout(nodes)
        for source, target in view.graph.edges:
            if source in layout and target in layout:
                x1, y1 = layout[source]
                x2, y2 = layout[target]
                self.canvas.create_line(
                    x1, y1, x2, y2, arrow="last", fill="#607d8b", width=1.5
                )
        invalid_keys = set(view.invalidated_keys)
        for node in sorted(nodes):
            x, y = layout[node]
            severity, drift_type, color = heat_index(
                node, self.workspace.last_drifts, invalid_keys
            )
            radius = 22 + severity * 4
            self.canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                fill=color,
                outline="#263238",
                width=2,
            )
            self.canvas.create_text(
                x, y, text=node, fill="white", width=radius * 2
            )
        legend_x, legend_y = 18, 18
        legend = (
            ("缺失", HEAT_COLORS["missing"]),
            ("值不符", HEAT_COLORS["value_mismatch"]),
            ("产物失效", HEAT_COLORS["artifact_invalid"]),
            ("多余", HEAT_COLORS["extra"]),
            ("正常", HEAT_COLORS["clean"]),
        )
        for index, (label, color) in enumerate(legend):
            y = legend_y + index * 24
            self.canvas.create_rectangle(
                legend_x, y, legend_x + 14, y + 14, fill=color, outline=""
            )
            self.canvas.create_text(
                legend_x + 72, y + 7, text=label, fill="#263238"
            )

    def _refresh_resources(self):
        for item in self.resource_tree.get_children():
            self.resource_tree.delete(item)
        view = self.workspace.current_view
        if view is None:
            return
        invalid_keys = set(view.invalidated_keys)
        for key in sorted(view.artifacts):
            artifact = view.artifacts[key]
            severity, drift_type, _ = heat_index(
                key, self.workspace.last_drifts, invalid_keys
            )
            self.resource_tree.insert(
                "",
                "end",
                iid=key,
                values=(
                    f"{severity}:{drift_type}",
                    artifact.artifact_version,
                    artifact.invalidated_by,
                ),
            )
            if severity:
                self.resource_tree.item(key, tags=(drift_type,))
        for drift_type, color in HEAT_COLORS.items():
            self.resource_tree.tag_configure(drift_type, foreground=color)

    def _refresh_diagnosis(self):
        for item in self.diagnosis.get_children():
            self.diagnosis.delete(item)
        lines = render_diagnosis_lines(self.workspace, self.last_result)
        for line in lines:
            self.diagnosis.insert("", "end", values=(line,))
        if self.last_result is not None:
            for trace in self.last_result.traces:
                self.diagnosis.tag_configure(
                    trace.status, foreground=status_color(trace.status)
                )


class _ScrolledCanvas:
    def __init__(self, master, tk_module, ttk_module):
        self.frame = ttk_module.Frame(master)
        self.canvas = tk_module.Canvas(
            self.frame,
            bg="#f5f7fa",
            width=760,
            height=640,
            scrollregion=(0, 0, 760, 640),
        )
        vertical = ttk_module.Scrollbar(
            self.frame, orient="vertical", command=self.canvas.yview
        )
        horizontal = ttk_module.Scrollbar(
            self.frame, orient="horizontal", command=self.canvas.xview
        )
        self.canvas.configure(
            yscrollcommand=vertical.set, xscrollcommand=horizontal.set
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)

    def clear(self):
        self.canvas.delete("all")

    def create_line(self, *args, **kwargs):
        return self.canvas.create_line(*args, **kwargs)

    def create_oval(self, *args, **kwargs):
        return self.canvas.create_oval(*args, **kwargs)

    def create_text(self, *args, **kwargs):
        return self.canvas.create_text(*args, **kwargs)

    def create_rectangle(self, *args, **kwargs):
        return self.canvas.create_rectangle(*args, **kwargs)

    def grid(self, *args, **kwargs):
        return self.frame.grid(*args, **kwargs)


def launch(workspace=None):
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "tkinter is not available in this Python installation. "
            "On Windows use the standard python.org installer; on Debian/WSL "
            "install python3-tk for the matching interpreter."
        ) from exc
    tk.messagebox = messagebox
    if workspace is None:
        workspace = Workspace.from_directories(
            Path(__file__).resolve().parents[1] / "samples" / "declarations",
            Path(__file__).resolve().parents[1]
            / "samples"
            / "actual"
            / "snapshot_v1.json",
        )
    root = tk.Tk()
    DriftWorkbenchApp(root, workspace, tk, ttk)
    root.mainloop()
    return 0
