"""Tkinter desktop interface.

The core package has no Tk dependency.  This module imports Tk only when the
GUI is explicitly launched so headless tests and Windows servers remain usable
without a display.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import LabError
from .util import parse_null_overrides


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERY = ROOT / "samples" / "query.sql"
DEFAULT_SAMPLE = ROOT / "samples" / "demo.sql"

HEAT_COLORS = {
    "low": "#d9f2d9",
    "medium": "#fff2b3",
    "high": "#ffc680",
    "critical": "#ff8a80",
}


def launch_gui(db_path: str | Path = "lab.sqlite3") -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, simpledialog, ttk
    except Exception as exc:  # pragma: no cover - depends on host Tk installation
        print(
            "Tkinter is unavailable. Install the Python Tk component and retry.\n"
            f"Details: {exc}"
        )
        return 1

    class LabApp:
        def __init__(self, window: "tk.Tk"):
            self.window = window
            self.engine = Engine(db_path)
            self.last_plan: dict[str, Any] | None = None
            self.last_analysis: dict[str, Any] | None = None
            self.last_heat_rows: list[dict[str, Any]] = []
            self.window.title("SQL Plan Attribution Heat-Lab")
            self.window.geometry("1260x820")
            self.window.minsize(980, 680)
            self._build_ui(tk, ttk)
            self._load_defaults()
            self._refresh_versions_and_snapshots()
            self.window.protocol("WM_DELETE_WINDOW", self._on_close)

        def _build_ui(self, tk: Any, ttk: Any) -> None:
            outer = ttk.Frame(self.window, padding=8)
            outer.pack(fill="both", expand=True)

            query_frame = ttk.LabelFrame(outer, text="SQL SELECT", padding=6)
            query_frame.pack(fill="x")
            self.query_text = tk.Text(query_frame, height=7, wrap="word", undo=True)
            self.query_text.pack(side="left", fill="both", expand=True)
            query_scroll = ttk.Scrollbar(query_frame, orient="vertical", command=self.query_text.yview)
            query_scroll.pack(side="right", fill="y")
            self.query_text.configure(yscrollcommand=query_scroll.set)

            controls = ttk.Frame(outer, padding=(0, 6))
            controls.pack(fill="x")
            for text, command in (
                ("Initialize sample", self._initialize_sample),
                ("Build plan", self._build_plan),
                ("Analyze", self._analyze),
                ("New statistics", self._new_stats),
                ("Save snapshot", self._save_snapshot),
                ("Restore snapshot", self._restore_snapshot),
            ):
                ttk.Button(controls, text=text, command=command).pack(side="left", padx=(0, 6))

            self.version_label = ttk.Label(controls, text="data=- stats=-")
            self.version_label.pack(side="right")

            panes = ttk.Panedwindow(outer, orient="horizontal")
            panes.pack(fill="both", expand=True)

            plan_frame = ttk.LabelFrame(panes, text="Logical plan tree", padding=6)
            self.plan_tree = ttk.Treeview(plan_frame, show="tree", selectmode="browse")
            plan_scroll = ttk.Scrollbar(plan_frame, orient="vertical", command=self.plan_tree.yview)
            self.plan_tree.configure(yscrollcommand=plan_scroll.set)
            self.plan_tree.pack(side="left", fill="both", expand=True)
            plan_scroll.pack(side="right", fill="y")
            panes.add(plan_frame, weight=1)

            right = ttk.Frame(panes)
            panes.add(right, weight=2)

            op_frame = ttk.LabelFrame(right, text="Operator estimates, actual rows and heat", padding=6)
            op_frame.pack(fill="both", expand=True)
            columns = ("operator", "type", "estimated", "actual", "ratio", "score", "heat")
            self.operator_table = ttk.Treeview(op_frame, columns=columns, show="headings", height=10)
            headings = {
                "operator": "Operator",
                "type": "Type",
                "estimated": "Estimated",
                "actual": "Actual",
                "ratio": "Est/Actual",
                "score": "Deviation",
                "heat": "Heat",
            }
            widths = {
                "operator": 300,
                "type": 70,
                "estimated": 85,
                "actual": 75,
                "ratio": 90,
                "score": 85,
                "heat": 70,
            }
            for name in columns:
                self.operator_table.heading(name, text=headings[name])
                self.operator_table.column(name, width=widths[name], anchor="w" if name in {"operator", "type"} else "center")
            op_scroll = ttk.Scrollbar(op_frame, orient="vertical", command=self.operator_table.yview)
            self.operator_table.configure(yscrollcommand=op_scroll.set)
            self.operator_table.pack(side="left", fill="both", expand=True)
            op_scroll.pack(side="right", fill="y")
            for band, color in HEAT_COLORS.items():
                self.operator_table.tag_configure(band, background=color)

            heat_frame = ttk.LabelFrame(right, text="Deviation heatmap", padding=6)
            heat_frame.pack(fill="x", pady=(6, 0))
            self.heat_canvas = tk.Canvas(heat_frame, height=82, background="#f7f7f7", highlightthickness=0)
            self.heat_canvas.pack(fill="x")
            self.heat_canvas.bind("<Configure>", lambda _event: self._draw_heat())

            bottom = ttk.Frame(outer)
            bottom.pack(fill="both", expand=False, pady=(6, 0))
            snapshot_frame = ttk.LabelFrame(bottom, text="Snapshots", padding=6)
            snapshot_frame.pack(side="left", fill="both", expand=True)
            self.snapshot_list = tk.Listbox(snapshot_frame, height=7, exportselection=False)
            self.snapshot_list.pack(side="left", fill="both", expand=True)
            snapshot_scroll = ttk.Scrollbar(snapshot_frame, orient="vertical", command=self.snapshot_list.yview)
            self.snapshot_list.configure(yscrollcommand=snapshot_scroll.set)
            snapshot_scroll.pack(side="right", fill="y")

            message_frame = ttk.LabelFrame(bottom, text="Activity", padding=6)
            message_frame.pack(side="right", fill="both", expand=True, padx=(6, 0))
            self.message_text = tk.Text(message_frame, height=7, wrap="word", state="disabled")
            self.message_text.pack(fill="both", expand=True)

        def _load_defaults(self) -> None:
            if DEFAULT_QUERY.exists():
                self.query_text.insert("1.0", DEFAULT_QUERY.read_text(encoding="utf-8"))
            else:
                self.query_text.insert(
                    "1.0",
                    "SELECT o.id FROM orders AS o JOIN customers AS c "
                    "ON o.customer_id = c.id WHERE o.status = 'paid';",
                )

        def _message(self, text: str) -> None:
            self.message_text.configure(state="normal")
            self.message_text.insert("end", text.rstrip() + "\n")
            self.message_text.see("end")
            self.message_text.configure(state="disabled")

        def _error(self, exc: Exception) -> None:
            if isinstance(exc, LabError):
                message = f"{exc.code}: {exc.message}"
                self._message(message)
            else:
                message = f"ERROR: {exc}"
                self._message(message)
            from tkinter import messagebox

            messagebox.showerror("SQL Plan Lab", message)

        def _refresh_versions_and_snapshots(self) -> None:
            state = self.engine.session_state()
            self.version_label.configure(
                text=(
                    f"data={state.get('data_version') or '-'}  "
                    f"stats={state.get('stats_version') or '-'}  "
                    f"plans={len(state.get('plan_history', []))}"
                )
            )
            self.snapshot_list.delete(0, "end")
            for snapshot in self.engine.list_snapshots():
                self.snapshot_list.insert(
                    "end",
                    f"{snapshot['name']}  [{snapshot['plan_count']} plans]  {snapshot['stats_version']}",
                )

        def _initialize_sample(self) -> None:
            try:
                result = self.engine.ingest_sql_file(DEFAULT_SAMPLE)
                self._message(f"Ingested data version {result['data_version']}")
                self._refresh_versions_and_snapshots()
            except Exception as exc:
                self._error(exc)

        def _query(self) -> str:
            return self.query_text.get("1.0", "end").strip()

        def _build_plan(self) -> None:
            try:
                plan = self.engine.build_plan(self._query())
                self.last_plan = plan
                self._render_plan(plan)
                self._message(f"Plan {plan['plan_id']} built; normalized SQL: {plan['normalized_sql']}")
            except Exception as exc:
                self._error(exc)

        def _render_plan(self, plan: dict[str, Any]) -> None:
            self.plan_tree.delete(*self.plan_tree.get_children())
            nodes = {node["op_id"]: node for node in plan["nodes"]}

            def add(op_id: str, parent: str = "") -> None:
                node = nodes[op_id]
                suffix = ""
                if node["kind"] == "scan":
                    suffix = f" {node['table']} as {node['alias']}"
                elif node.get("predicate_text"):
                    suffix = f" {node['predicate_text']}"
                iid = self.plan_tree.insert(
                    parent,
                    "end",
                    iid=op_id,
                    text=f"{op_id} {node['kind'].upper()}{suffix}",
                )
                for child in node["children"]:
                    add(child, iid)

            add(plan["root"])
            self.plan_tree.item(plan["root"], open=True)
            for iid in self.plan_tree.get_children():
                self.plan_tree.item(iid, open=True)

        def _analyze(self) -> None:
            try:
                state = self.engine.session_state()
                stats_version = state.get("stats_version")
                if not stats_version:
                    created = self.engine.create_stats_version(8, 1.0, {})
                    stats_version = created["stats_version"]
                    self._message(f"Created default statistics {stats_version}")
                analysis = self.engine.analyze(self._query(), stats_version)
                self.last_analysis = analysis
                self.last_heat_rows = analysis["heatmap"]["rows"]
                plan = self.engine.build_plan(self._query())
                self.last_plan = plan
                self._render_plan(plan)
                self._render_operators(analysis)
                self._draw_heat()
                self._refresh_versions_and_snapshots()
                self._message(
                    f"Analysis complete: estimated {analysis['root_estimated_rows']} / actual "
                    f"{analysis['root_actual_rows']}; primary operator {analysis['primary_operator_id']}"
                )
            except Exception as exc:
                self._error(exc)

        def _render_operators(self, analysis: dict[str, Any]) -> None:
            self.operator_table.delete(*self.operator_table.get_children())
            for row in analysis["operators"]:
                ratio = row["estimated_to_actual_ratio"]
                ratio_text = str(ratio) if isinstance(ratio, str) else f"{ratio:.6f}"
                self.operator_table.insert(
                    "",
                    "end",
                    values=(
                        row["label"],
                        row["kind"].upper(),
                        row["estimated_rows"],
                        row["actual_rows"],
                        ratio_text,
                        row["deviation_score"],
                        row["heat_band"],
                    ),
                    tags=(row["heat_band"],),
                )

        def _draw_heat(self) -> None:
            self.heat_canvas.delete("all")
            width = max(200, self.heat_canvas.winfo_width())
            rows = self.last_heat_rows
            if not rows:
                self.heat_canvas.create_text(
                    width / 2,
                    40,
                    text="Run Analyze to populate the heatmap",
                    fill="#666666",
                )
                return
            gap = 4
            cell_width = max(34, (width - gap * (len(rows) + 1)) / len(rows))
            for index, row in enumerate(rows):
                x0 = gap + index * (cell_width + gap)
                x1 = x0 + cell_width
                color = HEAT_COLORS.get(row["heat_band"], "#dddddd")
                self.heat_canvas.create_rectangle(x0, 4, x1, 54, fill=color, outline="#777777")
                self.heat_canvas.create_text(
                    (x0 + x1) / 2,
                    20,
                    text=row["op_id"],
                    font=("TkDefaultFont", 8, "bold"),
                )
                self.heat_canvas.create_text(
                    (x0 + x1) / 2,
                    39,
                    text=str(row["deviation_score"]),
                    font=("TkDefaultFont", 9),
                )

        def _new_stats(self) -> None:
            from tkinter import simpledialog

            buckets = simpledialog.askinteger(
                "Statistics parameters", "Histogram buckets (1-128):", initialvalue=8, minvalue=1, maxvalue=128
            )
            if buckets is None:
                return
            sample_rate = simpledialog.askfloat(
                "Statistics parameters", "Sample rate (0-1):", initialvalue=1.0, minvalue=0.01, maxvalue=1.0
            )
            if sample_rate is None:
                return
            override_text = simpledialog.askstring(
                "Statistics parameters",
                "Null overrides, comma separated table.column=fraction (optional):",
                initialvalue="orders.status=0.12,orders.amount=0.05",
            )
            try:
                overrides = parse_null_overrides(
                    [item.strip() for item in (override_text or "").split(",") if item.strip()]
                )
                result = self.engine.create_stats_version(buckets, sample_rate, overrides)
                self._message(
                    f"Statistics {result['stats_version']} created; heatmap cleared for whole-set refresh"
                )
                self.operator_table.delete(*self.operator_table.get_children())
                self.last_heat_rows = []
                self._draw_heat()
                self._refresh_versions_and_snapshots()
            except Exception as exc:
                self._error(exc)

        def _save_snapshot(self) -> None:
            from tkinter import simpledialog

            name = simpledialog.askstring("Save snapshot", "Snapshot name:", initialvalue="baseline")
            if not name:
                return
            try:
                result = self.engine.save_snapshot(name)
                self._message(f"Saved snapshot {result['name']} ({result['plan_count']} plans)")
                self._refresh_versions_and_snapshots()
            except Exception as exc:
                self._error(exc)

        def _restore_snapshot(self) -> None:
            from tkinter import simpledialog

            selection = self.snapshot_list.curselection()
            default = "baseline"
            if selection:
                default = self.snapshot_list.get(selection[0]).split()[0]
            name = simpledialog.askstring("Restore snapshot", "Snapshot name:", initialvalue=default)
            if not name:
                return
            try:
                result = self.engine.restore_snapshot(name)
                self._message(
                    f"Restored {name}; replaced={result['replaced']}; "
                    f"{len(result['history_verification'])} plans replayed"
                )
                heat = self.engine.load_heatmap()
                self.last_heat_rows = heat
                self._draw_heat()
                self._refresh_versions_and_snapshots()
            except Exception as exc:
                self._error(exc)

        def _on_close(self) -> None:
            self.engine.close()
            self.window.destroy()

    window = tk.Tk()
    LabApp(window)
    window.mainloop()
    return 0
