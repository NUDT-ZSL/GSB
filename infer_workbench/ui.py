"""Tkinter diagnostics workbench with deterministic render ordering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .assets import load_assets
from .canonical import canonical_json, relative_path
from .config import BatchConfig
from .errors import WorkbenchError
from .session import InferenceSession, human_summary
from .snapshots import load_snapshot


LAYER_COLORS = ("#e8f5e9", "#fff8e1", "#ffe0b2", "#ffcc80", "#ef9a9a")


class WorkbenchApp:
    def __init__(self, root: tk.Tk, *, data_dir: Path, snapshot_dir: Path):
        self.root = root
        self.data_dir = Path(data_dir)
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.assets = load_assets(self.data_dir)
        self.session = InferenceSession(self.assets, snapshot_dir=self.snapshot_dir)
        self.batch_lookup: dict[str, dict[str, Any]] = {}
        self.snapshot_paths: dict[str, Path] = {}

        self.root.title("Inference Batch & Precision Diagnostics Workbench")
        self.root.geometry("1180x760")
        self.config_path = tk.StringVar(
            value=str((self.data_dir.parent / "configs" / "fixed_q3_b3.json").resolve())
        )
        self.stop_after = tk.StringVar(value="")
        self.sample_choice = tk.StringVar()
        self.status_text = tk.StringVar(value="idle")

        self._build_layout()
        self._refresh_all()

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)

        controls = ttk.LabelFrame(outer, text="运行控制", padding=8)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="批配置 JSON").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(controls, textvariable=self.config_path, width=70).grid(
            row=0, column=1, columnspan=5, sticky=tk.EW, padx=4
        )
        ttk.Button(controls, text="浏览", command=self._choose_config).grid(row=0, column=6)
        ttk.Button(controls, text="运行", command=self._run).grid(row=0, column=7, padx=(8, 2))
        ttk.Label(controls, text="最多批次(空=全部)").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(controls, textvariable=self.stop_after, width=8).grid(
            row=1, column=1, sticky=tk.W, padx=4, pady=(6, 0)
        )
        ttk.Button(controls, text="保存快照", command=self._save_snapshot).grid(
            row=1, column=2, sticky=tk.W, pady=(6, 0)
        )
        ttk.Button(controls, text="继续剩余批次", command=self._continue).grid(
            row=1, column=3, sticky=tk.W, padx=4, pady=(6, 0)
        )
        ttk.Label(controls, textvariable=self.status_text).grid(
            row=1, column=4, columnspan=4, sticky=tk.W, padx=(12, 0), pady=(6, 0)
        )
        controls.columnconfigure(1, weight=1)

        main = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        left = ttk.Labelframe(main, text="批次列表", padding=6)
        right = ttk.Labelframe(main, text="诊断详情", padding=6)
        main.add(left, weight=2)
        main.add(right, weight=5)

        self.batch_tree = ttk.Treeview(
            left,
            columns=("batch", "samples", "precision", "drift", "deviation"),
            show="headings",
            height=22,
        )
        for key, heading, width in (
            ("batch", "批次", 70),
            ("samples", "批内样本", 210),
            ("precision", "精度", 70),
            ("drift", "漂移", 70),
            ("deviation", "最大输出偏差", 110),
        ):
            self.batch_tree.heading(key, text=heading)
            self.batch_tree.column(key, width=width, anchor=tk.W)
        batch_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.batch_tree.yview)
        self.batch_tree.configure(yscrollcommand=batch_scroll.set)
        self.batch_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        batch_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.batch_tree.bind("<<TreeviewSelect>>", lambda _event: self._render_selected_batch())

        overview = ttk.Frame(right)
        overview.pack(fill=tk.X, pady=(0, 6))
        self.config_label = ttk.Label(overview, text="")
        self.config_label.pack(side=tk.LEFT)
        ttk.Label(overview, text="归因样本").pack(side=tk.LEFT, padx=(16, 4))
        self.sample_box = ttk.Combobox(
            overview, textvariable=self.sample_choice, state="readonly", width=14
        )
        self.sample_box.pack(side=tk.LEFT)
        self.sample_box.bind("<<ComboboxSelected>>", lambda _event: self._render_attribution())

        notebook = ttk.Notebook(right)
        notebook.pack(fill=tk.BOTH, expand=True)
        self.overview_tab = self._text_tab(notebook, "概览")
        self.heat_tab = ttk.Frame(notebook)
        notebook.add(self.heat_tab, text="逐层误差界热力")
        self.heat_canvas = tk.Canvas(self.heat_tab, background="white", highlightthickness=0)
        heat_x = ttk.Scrollbar(self.heat_tab, orient=tk.HORIZONTAL, command=self.heat_canvas.xview)
        heat_y = ttk.Scrollbar(self.heat_tab, orient=tk.VERTICAL, command=self.heat_canvas.yview)
        self.heat_canvas.configure(xscrollcommand=heat_x.set, yscrollcommand=heat_y.set)
        self.heat_canvas.grid(row=0, column=0, sticky=tk.NSEW)
        heat_y.grid(row=0, column=1, sticky=tk.NS)
        heat_x.grid(row=1, column=0, sticky=tk.EW)
        self.heat_tab.rowconfigure(0, weight=1)
        self.heat_tab.columnconfigure(0, weight=1)
        self.attribution_tab = ttk.Frame(notebook)
        notebook.add(self.attribution_tab, text="偏差归因链")
        self.attribution_tree = ttk.Treeview(
            self.attribution_tab,
            columns=("layer", "relative", "local", "cumulative", "share", "cum_share"),
            show="headings",
        )
        for key, heading, width in (
            ("layer", "层", 110),
            ("relative", "相对偏差", 110),
            ("local", "局部误差界(格)", 120),
            ("cumulative", "累计误差界(格)", 130),
            ("share", "本层归因占比", 130),
            ("cum_share", "累计归因占比", 130),
        ):
            self.attribution_tree.heading(key, text=heading)
            self.attribution_tree.column(key, width=width, anchor=tk.E)
        self.attribution_tree.column("layer", anchor=tk.W)
        self.attribution_tree.pack(fill=tk.BOTH, expand=True)
        self.snapshot_tab = ttk.Frame(notebook)
        notebook.add(self.snapshot_tab, text="快照")
        self.snapshot_tree = ttk.Treeview(
            self.snapshot_tab,
            columns=("file", "snapshot", "version", "completed", "status"),
            show="headings",
        )
        for key, heading, width in (
            ("file", "文件", 280),
            ("snapshot", "快照标识", 150),
            ("version", "配置版本", 130),
            ("completed", "完成批次", 90),
            ("status", "状态", 100),
        ):
            self.snapshot_tree.heading(key, text=heading)
            self.snapshot_tree.column(key, width=width, anchor=tk.W)
        self.snapshot_tree.pack(fill=tk.BOTH, expand=True)
        snapshot_controls = ttk.Frame(self.snapshot_tab)
        snapshot_controls.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(snapshot_controls, text="恢复并整体替换", command=self._restore_snapshot).pack(
            side=tk.LEFT
        )
        ttk.Button(snapshot_controls, text="逐位重放校验", command=self._replay_snapshot).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(snapshot_controls, text="刷新列表", command=self._refresh_snapshots).pack(
            side=tk.LEFT
        )
        self.log = self._text_tab(notebook, "操作日志")

    def _text_tab(self, notebook: ttk.Notebook, title: str) -> tk.Text:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)
        text = tk.Text(frame, wrap=tk.WORD, height=12)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        return text

    def _choose_config(self) -> None:
        path = filedialog.askopenfilename(
            title="选择批处理配置",
            initialdir=str(self.data_dir.parent / "configs"),
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.config_path.set(path)

    def _append_log(self, message: str) -> None:
        self.log.insert(tk.END, message.rstrip() + "\n")
        self.log.see(tk.END)

    def _stop_after_value(self) -> int | None:
        text = self.stop_after.get().strip()
        if not text:
            return None
        return int(text)

    def _run(self) -> None:
        try:
            config = BatchConfig.load(Path(self.config_path.get()))
            report = self.session.run(config, stop_after=self._stop_after_value())
            self._after_state_change("run", report)
        except (WorkbenchError, ValueError, OSError) as exc:
            self._show_error(exc)

    def _continue(self) -> None:
        try:
            report = self.session.continue_remaining(stop_after=self._stop_after_value())
            self._after_state_change("continue", report)
        except (WorkbenchError, ValueError, OSError) as exc:
            self._show_error(exc)

    def _save_snapshot(self) -> None:
        if self.session.config is None:
            messagebox.showinfo("快照", "当前没有可保存的会话")
            return
        default_name = f"ui_{self.session.config.config_version}_{len(self.session.batches):02d}.json"
        path = filedialog.asksaveasfilename(
            title="保存推理快照",
            initialdir=str(self.snapshot_dir),
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            saved = self.session.save_snapshot(path)
            self._append_log(f"snapshot_saved={saved}")
            self._refresh_snapshots()
        except (WorkbenchError, OSError) as exc:
            self._show_error(exc)

    def _selected_snapshot_path(self) -> Path | None:
        selection = self.snapshot_tree.selection()
        if not selection:
            messagebox.showinfo("快照", "请先选择快照")
            return None
        return self.snapshot_paths.get(selection[0])

    def _restore_snapshot(self) -> None:
        path = self._selected_snapshot_path()
        if path is None:
            return
        try:
            result = self.session.restore_snapshot(path)
            self._append_log(
                "snapshot_restored="
                + canonical_json(
                    {
                        "status": result.get("status"),
                        "code": result.get("code"),
                        "config_version": result.get("snapshot_config_version"),
                        "loaded_batches": result.get("loaded_completed_batches"),
                        "readonly_backup": result.get("readonly_backup"),
                    }
                )
            )
            self._refresh_all()
        except (WorkbenchError, OSError) as exc:
            self._show_error(exc)

    def _replay_snapshot(self) -> None:
        path = self._selected_snapshot_path()
        if path is None:
            return
        try:
            replay = self.session.replay_snapshot(path, expected_config=self.session.config)
            self._append_log("snapshot_replay=" + canonical_json(replay))
            self._refresh_overview()
        except (WorkbenchError, OSError) as exc:
            self._show_error(exc)

    def _show_error(self, exc: BaseException) -> None:
        if isinstance(exc, WorkbenchError):
            message = json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True)
        else:
            message = str(exc)
        self._append_log("error=" + message)
        messagebox.showerror("操作失败", message)

    def _after_state_change(self, action: str, report: dict[str, Any]) -> None:
        self._append_log(f"{action}=" + canonical_json(report["state"]))
        self._append_log(human_summary(report).rstrip())
        self._refresh_all()

    def _refresh_all(self) -> None:
        self._refresh_batches()
        self._refresh_overview()
        self._refresh_snapshots()
        if self.session.config is not None:
            self.config_label.configure(
                text=(
                    f"config_version={self.session.config.config_version}  "
                    f"mode={self.session.config.precision_mode}  "
                    f"scale={self.session.config.quant_scale}  "
                    f"batches={len(self.session.batches)}/{len(self.session.plans)}"
                )
            )
            self.status_text.set(self.session.status)
        else:
            self.config_label.configure(text="config_version=none")
            self.status_text.set("idle")

    def _refresh_batches(self) -> None:
        self.batch_tree.delete(*self.batch_tree.get_children())
        self.batch_lookup.clear()
        for batch in sorted(self.session.batches, key=lambda item: int(item["batch_index"])):
            iid = f"batch-{int(batch['batch_index']):04d}"
            self.batch_lookup[iid] = batch
            self.batch_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    batch["batch_id"],
                    ",".join(batch["sample_ids"]),
                    batch["precision_mode"],
                    "E_DRIFT" if batch.get("drift") else "none",
                    f"{float(batch.get('max_output_relative_deviation', 0.0)):.8g}",
                ),
            )
        children = self.batch_tree.get_children()
        if children:
            first = children[0]
            self.batch_tree.selection_set(first)
            self.batch_tree.focus(first)
            self._render_selected_batch()
        else:
            self.sample_box.configure(values=())
            self.heat_canvas.delete("all")
            self.attribution_tree.delete(*self.attribution_tree.get_children())

    def _selected_batch(self) -> dict[str, Any] | None:
        selection = self.batch_tree.selection()
        if not selection:
            return None
        return self.batch_lookup.get(selection[0])

    def _render_selected_batch(self) -> None:
        batch = self._selected_batch()
        if batch is None:
            return
        sample_ids = list(batch["sample_ids"])
        self.sample_box.configure(values=sample_ids)
        if self.sample_choice.get() not in sample_ids:
            self.sample_choice.set(sample_ids[0] if sample_ids else "")
        self._render_heatmap(batch)
        self._render_attribution()

    def _render_heatmap(self, batch: dict[str, Any]) -> None:
        self.heat_canvas.delete("all")
        layer_names = list(self.assets.layer_names)
        samples = {sample["sample_id"]: sample for sample in batch.get("samples", [])}
        row_ids = list(batch["sample_ids"])
        values: list[list[int]] = []
        for sample_id in row_ids:
            row = []
            layer_map = {
                layer["layer"]: layer
                for layer in samples[sample_id].get("diagnostics", {}).get("layers", [])
            }
            for layer_name in layer_names:
                row.append(int(layer_map[layer_name]["cumulative_error_bound_grid"]))
            values.append(row)
        maximum = max((value for row in values for value in row), default=0)
        cell_w, cell_h = 150, 30
        left_w, top_h = 100, 28
        width = left_w + cell_w * len(layer_names)
        height = top_h + cell_h * len(row_ids) + 28
        self.heat_canvas.configure(scrollregion=(0, 0, width, height), width=min(width, 760), height=min(height, 500))
        for column, layer_name in enumerate(layer_names):
            self.heat_canvas.create_text(
                left_w + column * cell_w + cell_w / 2,
                14,
                text=layer_name,
                font=("TkDefaultFont", 10, "bold"),
            )
        for row_index, sample_id in enumerate(row_ids):
            y = top_h + row_index * cell_h
            self.heat_canvas.create_text(8, y + cell_h / 2, text=sample_id, anchor=tk.W)
            for column, value in enumerate(values[row_index]):
                x = left_w + column * cell_w
                color = self._heat_color(value, maximum)
                self.heat_canvas.create_rectangle(x, y, x + cell_w - 2, y + cell_h - 2, fill=color, outline="#9e9e9e")
                self.heat_canvas.create_text(
                    x + cell_w / 2,
                    y + cell_h / 2,
                    text=str(value),
                    font=("TkDefaultFont", 9),
                )
        legend_y = top_h + len(row_ids) * cell_h + 10
        self.heat_canvas.create_text(
            8,
            legend_y,
            anchor=tk.W,
            text=f"颜色按累计误差界归一化；当前最大值={maximum} 格",
        )

    @staticmethod
    def _heat_color(value: int, maximum: int) -> str:
        if maximum <= 0 or value <= 0:
            return LAYER_COLORS[0]
        ratio = min(1.0, float(value) / float(maximum))
        index = min(len(LAYER_COLORS) - 1, int(ratio * len(LAYER_COLORS)))
        return LAYER_COLORS[index]

    def _render_attribution(self) -> None:
        self.attribution_tree.delete(*self.attribution_tree.get_children())
        batch = self._selected_batch()
        if batch is None:
            return
        sample_id = self.sample_choice.get()
        sample = next(
            (item for item in batch.get("samples", []) if item["sample_id"] == sample_id), None
        )
        if sample is None:
            return
        layers = sample.get("diagnostics", {}).get("layers", [])
        for layer in layers:
            self.attribution_tree.insert(
                "",
                tk.END,
                iid=f"layer-{layer['layer']}",
                values=(
                    layer["layer"],
                    f"{float(layer['relative_error']):.8g}",
                    layer["local_error_bound_grid"],
                    layer["cumulative_error_bound_grid"],
                    f"{float(layer['increase_share']):.4f}",
                    f"{float(layer['cumulative_share']):.4f}",
                ),
            )

    def _refresh_overview(self) -> None:
        self.overview_tab.delete("1.0", tk.END)
        if self.session.config is None:
            text = "尚无配置版本。选择 JSON 后点击“运行”。\n"
        else:
            text = human_summary(self.session.build_report())
            text += f"\nsnapshot_dir={self.snapshot_dir}\n"
        self.overview_tab.insert("1.0", text)

    def _refresh_snapshots(self) -> None:
        self.snapshot_tree.delete(*self.snapshot_tree.get_children())
        self.snapshot_paths.clear()
        if not self.snapshot_dir.exists():
            return
        for path in sorted(self.snapshot_dir.glob("*.json"), key=lambda item: item.name):
            iid = f"snapshot-{path.name}"
            self.snapshot_paths[iid] = path
            try:
                payload = load_snapshot(path)
                values = (
                    path.name,
                    payload.get("snapshot_id"),
                    payload.get("config_version"),
                    f"{len(payload.get('completed_batches', []))}/{len(payload.get('batch_plan', []))}",
                    payload.get("status"),
                )
            except WorkbenchError as exc:
                values = (path.name, "invalid", "-", "-", exc.code)
            self.snapshot_tree.insert("", tk.END, iid=iid, values=values)


def launch_ui(*, data_dir: Path, snapshot_dir: Path) -> None:
    root = tk.Tk()
    try:
        WorkbenchApp(root, data_dir=Path(data_dir), snapshot_dir=Path(snapshot_dir))
    except WorkbenchError as exc:
        messagebox.showerror("启动失败", json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
        root.destroy()
        raise
    root.mainloop()
