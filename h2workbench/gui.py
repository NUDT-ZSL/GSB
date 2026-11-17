"""Tkinter visualization workbench.

Panels (all rendered in a stable, explicitly sorted order):
  * frame timeline canvas: HEADERS / DATA / WINDOW_UPDATE / STALL lanes
  * per-stream window-remaining curve canvas
  * current policy selection + reason
  * fairness diagnostics table
  * snapshot list with save / load (whole-replace) / replay verify
"""

import os
import tkinter as tk
from tkinter import filedialog, ttk

from .errors import DiagError
from .manifest import load_manifest
from .policy import STRATEGY_LABELS
from .scheduler import STRATEGIES
from .session import Session
from .snapshot import (list_snapshots, load_snapshot, restore_into_session,
                       save_snapshot, verify_replay)

FRAME_COLORS = {
    "HEADERS": "#4C9AFF",
    "DATA": "#36B37E",
    "WINDOW_UPDATE": "#FF8B00",
    "STALL": "#8993A4",
}
STREAM_PALETTE = [
    "#0052CC", "#00875A", "#DE350B", "#5243AA", "#FF991F", "#00B8D9",
    "#BF2600", "#006644", "#403294", "#974F0C", "#0747A6", "#5E6C84",
]
LANE_HEIGHT = 26
FRAME_WIDTH = 8


class WorkbenchApp:
    def __init__(self, root, samples_dir, snapshots_dir):
        self.root = root
        self.samples_dir = samples_dir
        self.snapshots_dir = snapshots_dir
        self.session = None
        self.report = None
        root.title("HTTP/2 Multiplexing Diagnostic Workbench")
        self._build_controls()
        self._build_panels()
        self.refresh_snapshot_list()

    # ------------------------------------------------------------------
    def _build_controls(self):
        bar = ttk.Frame(self.root)
        bar.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        ttk.Label(bar, text="Manifest:").pack(side=tk.LEFT)
        self.manifest_var = tk.StringVar(
            value=os.path.join(self.samples_dir, "manifest_small.json"))
        ttk.Entry(bar, textvariable=self.manifest_var, width=44).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Browse", command=self.on_browse).pack(
            side=tk.LEFT)
        ttk.Button(bar, text="Load", command=self.on_load).pack(
            side=tk.LEFT, padx=4)
        ttk.Label(bar, text="Strategy:").pack(side=tk.LEFT, padx=(12, 2))
        self.strategy_var = tk.StringVar(value="auto")
        ttk.OptionMenu(bar, self.strategy_var, "auto",
                       "auto", *STRATEGIES).pack(side=tk.LEFT)
        ttk.Button(bar, text="Run", command=self.on_run).pack(
            side=tk.LEFT, padx=8)
        ttk.Button(bar, text="Save snapshot",
                   command=self.on_save_snapshot).pack(side=tk.LEFT)
        ttk.Button(bar, text="Load snapshot",
                   command=self.on_load_snapshot).pack(side=tk.LEFT,
                                                       padx=4)

    def _build_panels(self):
        self.policy_var = tk.StringVar(value="policy: (no run yet)")
        ttk.Label(self.root, textvariable=self.policy_var,
                  wraplength=1400, justify=tk.LEFT).pack(
            side=tk.TOP, fill=tk.X, padx=6)

        middle = ttk.Frame(self.root)
        middle.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        left = ttk.Frame(middle)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(left, text="Frame timeline (sequence order)").pack(
            anchor=tk.W)
        self.timeline = tk.Canvas(left, bg="white", height=360,
                                  scrollregion=(0, 0, 4000, 360))
        scroll = ttk.Scrollbar(left, orient=tk.HORIZONTAL,
                               command=self.timeline.xview)
        self.timeline.configure(xscrollcommand=scroll.set)
        self.timeline.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.TOP, fill=tk.X)

        right = ttk.Frame(middle)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(right, text="Window remaining per stream").pack(anchor=tk.W)
        self.curves = tk.Canvas(right, bg="white", height=360)
        self.curves.pack(fill=tk.BOTH, expand=True)

        bottom = ttk.Frame(self.root)
        bottom.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        fair_frame = ttk.Frame(bottom)
        fair_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(fair_frame, text="Fairness diagnostics").pack(anchor=tk.W)
        columns = ("stream", "weight", "expected", "actual", "deviation",
                   "starved_rounds", "annotations")
        self.fairness = ttk.Treeview(fair_frame, columns=columns,
                                     show="headings", height=8)
        for col, width in zip(columns, (70, 70, 90, 90, 90, 110, 220)):
            self.fairness.heading(col, text=col)
            self.fairness.column(col, width=width, anchor=tk.E)
        self.fairness.pack(fill=tk.BOTH, expand=True)

        diag_frame = ttk.Frame(bottom)
        diag_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(diag_frame, text="Diagnostics").pack(anchor=tk.W)
        self.diagnostics = tk.Text(diag_frame, height=9, width=64)
        self.diagnostics.pack(fill=tk.BOTH, expand=True)

        snap_frame = ttk.Frame(bottom)
        snap_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(snap_frame, text="Snapshots").pack(anchor=tk.W)
        self.snapshot_list = tk.Listbox(snap_frame, height=9, width=36)
        self.snapshot_list.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    def log_diag(self, text):
        self.diagnostics.insert(tk.END, text + "\n")
        self.diagnostics.see(tk.END)

    def on_browse(self):
        path = filedialog.askopenfilename(
            initialdir=self.samples_dir, filetypes=[("JSON", "*.json")])
        if path:
            self.manifest_var.set(path)

    def on_load(self):
        try:
            self.session = Session.from_file(self.manifest_var.get())
        except DiagError as exc:
            self.session = None
            self.log_diag(f"{exc.code}: {exc.message} [{exc.path}]")
            return
        self.log_diag(
            f"manifest loaded: {self.session.manifest['name']} "
            f"v{self.session.manifest['version']} "
            f"streams={len(self.session.manifest['streams'])}")

    def on_run(self):
        if self.session is None:
            self.on_load()
        if self.session is None:
            return
        forced = self.strategy_var.get()
        forced = None if forced == "auto" else forced
        try:
            self.report = self.session.run(forced_strategy=forced)
        except DiagError as exc:
            self.log_diag(f"{exc.code}: {exc.message}")
            return
        except Exception as exc:  # defensive: surface as diagnostic text
            self.log_diag(f"E_PROTO: run failed: {exc}")
            return
        self.policy_var.set(
            f"policy: {self.report['policy']['selected']} "
            f"({STRATEGY_LABELS[self.report['policy']['selected']]}) — "
            f"{self.report['policy']['reason']}")
        self.render_all()

    # ------------------------------------------------------------------
    def render_all(self):
        self.render_timeline()
        self.render_curves()
        self.render_fairness()
        self.render_diagnostics()

    def render_timeline(self):
        canvas = self.timeline
        canvas.delete("all")
        report = self.report
        plan = report["plan"]
        grants = report["runtime"]["observed_grants"]
        stalls = report["runtime"]["stalls"]
        stream_ids = sorted(s["id"] for s in
                            self.session.manifest["streams"])
        lanes = {sid: index for index, sid in enumerate(stream_ids)}
        conn_lane = len(stream_ids)
        for sid in stream_ids:
            y = lanes[sid] * LANE_HEIGHT + 4
            canvas.create_text(2, y + LANE_HEIGHT // 2, anchor=tk.W,
                               text=f"s{sid}", fill="#333")
        canvas.create_text(2, conn_lane * LANE_HEIGHT + LANE_HEIGHT // 2,
                           anchor=tk.W, text="conn", fill="#333")
        x = 60
        for frame in plan["frames"]:  # already in sequence order
            lane = lanes[frame["stream_id"]]
            y = lane * LANE_HEIGHT + 4
            color = FRAME_COLORS[frame["type"]]
            canvas.create_rectangle(x, y, x + FRAME_WIDTH,
                                    y + LANE_HEIGHT - 8, fill=color,
                                    outline="")
            x += FRAME_WIDTH + 2
        grant_x = 60
        for stream_id, _increment in grants:
            lane = conn_lane if stream_id == 0 else lanes.get(stream_id)
            if lane is None:
                continue
            y = lane * LANE_HEIGHT + LANE_HEIGHT - 6
            canvas.create_rectangle(grant_x, y, grant_x + 4, y + 4,
                                    fill=FRAME_COLORS["WINDOW_UPDATE"],
                                    outline="")
            grant_x += 5
        for stall in stalls:
            lane_y = conn_lane * LANE_HEIGHT + LANE_HEIGHT + 2
            canvas.create_rectangle(60 + stall["start_ms"], lane_y,
                                    60 + stall["end_ms"] + 2, lane_y + 6,
                                    fill=FRAME_COLORS["STALL"], outline="")
        canvas.configure(scrollregion=(0, 0, max(x, grant_x) + 80,
                                       (conn_lane + 1) * LANE_HEIGHT + 20))

    def render_curves(self):
        canvas = self.curves
        canvas.delete("all")
        ledger = self.report["runtime"]["ledger"]
        width = max(canvas.winfo_width(), 400)
        height = max(canvas.winfo_height(), 200)
        series = []
        max_window = 1
        for entry in ledger["streams"]:  # sorted by stream id
            points = entry["window_curve"]
            if not points:
                continue
            series.append((entry["stream_id"], points))
            max_window = max(max_window, max(points))
        max_seq = max((len(p) for _, p in series), default=1)
        for index, (stream_id, points) in enumerate(series):
            color = STREAM_PALETTE[index % len(STREAM_PALETTE)]
            coords = []
            step = max(1, (width - 60) // max(max_seq, 1))
            for seq, value in enumerate(points):
                x = 40 + seq * step
                y = height - 20 - (value / max_window) * (height - 50)
                coords.extend((x, y))
            if len(coords) >= 4:
                canvas.create_line(*coords, fill=color, width=1.5)
            canvas.create_text(4, 12 + index * 14, anchor=tk.W,
                               text=f"s{stream_id}", fill=color)

    def render_fairness(self):
        for item in self.fairness.get_children():
            self.fairness.delete(item)
        for row in self.report["fairness"]["streams"]:  # sorted by id
            self.fairness.insert(
                "", tk.END, values=(
                    row["stream_id"], row["effective_weight"],
                    row["expected_bytes"], row["actual_bytes"],
                    row["deviation_bytes"], row["max_starved_rounds"],
                    ",".join(row["annotations"])))

    def render_diagnostics(self):
        self.log_diag(
            f"run done: frames={self.report['runtime']['frames_sent']} "
            f"stalls={len(self.report['runtime']['stalls'])} "
            f"progress_consistent={self.report['progress_consistent']} "
            f"window_reconciliation="
            f"{self.report['window_reconciliation']}")
        for diag in self.report["diagnostics"]:
            self.log_diag(f"  {diag['code']} stream={diag.get('stream_id')} "
                          f"{diag.get('detail') or diag.get('message')}")

    # ------------------------------------------------------------------
    def refresh_snapshot_list(self):
        self.snapshot_list.delete(0, tk.END)
        for name in list_snapshots(self.snapshots_dir):
            self.snapshot_list.insert(tk.END, name)

    def on_save_snapshot(self):
        if self.session is None or self.session.plan is None:
            self.log_diag("nothing to snapshot: run a session first")
            return
        os.makedirs(self.snapshots_dir, exist_ok=True)
        name = f"snapshot_{self.session.manifest['name']}_" \
               f"v{self.session.manifest['version']}.json"
        save_snapshot(self.session, os.path.join(self.snapshots_dir, name))
        self.log_diag(f"snapshot saved: {name}")
        self.refresh_snapshot_list()

    def on_load_snapshot(self):
        selection = self.snapshot_list.curselection()
        if not selection:
            self.log_diag("select a snapshot first")
            return
        if self.session is None:
            self.on_load()
        if self.session is None:
            return
        name = self.snapshot_list.get(selection[0])
        snap = load_snapshot(os.path.join(self.snapshots_dir, name))
        try:
            result = restore_into_session(self.session, snap)
        except DiagError as exc:
            self.log_diag(f"{exc.code}: {exc.message} [{exc.path}]")
            return
        self.log_diag(
            f"snapshot restored: replaced={result['replaced']} "
            f"replay={result['replay']['status']} "
            f"frames={result['replay']['frames_replayed']}")
        try:
            verify_replay(snap)
        except DiagError as exc:
            self.log_diag(f"{exc.code}: {exc.message}")


def launch(samples_dir, snapshots_dir):
    root = tk.Tk()
    WorkbenchApp(root, samples_dir, snapshots_dir)
    root.mainloop()
