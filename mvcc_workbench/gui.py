"""Tkinter desktop workbench.

Tkinter is imported only inside ``run_gui`` so this module remains importable
on headless Linux CI while the deliverable targets the Windows Python build.
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

from .background import BackgroundCommitter
from .engine import Workbench
from .errors import E_INPUT, BusinessError
from .script import SCRIPT_FORMAT, ScriptRunner
from .util import pretty_json


SAMPLE_PATH = Path(__file__).resolve().parent.parent / "samples" / "deterministic_session.json"


def run_gui(*, session_dir: str | Path, budget_ms: float = 5.0) -> None:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:  # pragma: no cover - Windows builds include Tk
        raise BusinessError(
            E_INPUT,
            "Tkinter is not available in this Python installation",
            {"hint": "Install the Windows official Python package or enable the tcl/tk component."},
        ) from exc

    session_path = Path(session_dir).resolve()
    workbench = Workbench(session_path, budget_ms=budget_ms)
    events: queue.Queue[tuple[str, Any]] = queue.Queue()

    class WorkbenchApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.workbench = workbench
            self.background: BackgroundCommitter | None = None
            self._closed = False
            self._busy = False

            self.root.title("MVCC Visibility Workbench")
            self.root.geometry("1280x860")
            self.root.minsize(1040, 700)
            self._configure_style(ttk)
            self._build(ttk)
            self.refresh_transactions()
            self.refresh_snapshots()
            self.refresh_logs()
            self.root.after(100, self._drain_events)
            self.root.after(700, self._periodic_refresh)
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        @staticmethod
        def _configure_style(ttk: Any) -> None:
            style = ttk.Style()
            try:
                style.theme_use("vista" if "vista" in style.theme_names() else "clam")
            except Exception:
                pass
            style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
            style.configure("Small.TLabel", font=("Segoe UI", 9))
            style.configure("Danger.TLabel", foreground="#9b1c1c")

        def _build(self, ttk: Any) -> None:
            outer = ttk.Frame(self.root, padding=10)
            outer.pack(fill="both", expand=True)

            header = ttk.Frame(outer)
            header.pack(fill="x", pady=(0, 8))
            ttk.Label(header, text="MVCC Visibility Workbench", style="Title.TLabel").pack(side="left")
            self.session_label = ttk.Label(header, text="", style="Small.TLabel")
            self.session_label.pack(side="right")

            toolbar = ttk.Frame(outer)
            toolbar.pack(fill="x", pady=(0, 8))
            ttk.Button(toolbar, text="刷新", command=self.refresh_all).pack(side="left", padx=(0, 6))
            ttk.Button(toolbar, text="权威重放全部历史", command=self.replay_history).pack(side="left", padx=6)
            ttk.Button(toolbar, text="查看指标", command=self.show_metrics).pack(side="left", padx=6)
            self.status_var = tk.StringVar(value="就绪")
            ttk.Label(toolbar, textvariable=self.status_var, style="Small.TLabel").pack(side="right")

            paned = ttk.Panedwindow(outer, orient="horizontal")
            paned.pack(fill="both", expand=True)

            left = ttk.Frame(paned, padding=(0, 0, 8, 0))
            right = ttk.Frame(paned, padding=(8, 0, 0, 0))
            paned.add(left, weight=1)
            paned.add(right, weight=3)

            self._build_transactions(left, ttk)
            self._build_query(left, ttk)
            self._build_snapshots(left, ttk)
            self._build_background(left, ttk)
            self._build_right(right, ttk)

        def _build_transactions(self, parent: Any, ttk: Any) -> None:
            card = ttk.LabelFrame(parent, text="事务与快照", padding=8)
            card.pack(fill="both", expand=True, pady=(0, 8))
            columns = ("txid", "state", "xmin", "xmax", "active", "label")
            self.tx_tree = ttk.Treeview(card, columns=columns, show="headings", height=10)
            widths = {"txid": 52, "state": 86, "xmin": 52, "xmax": 52, "active": 100, "label": 140}
            headings = {"txid": "txid", "state": "状态", "xmin": "snap xmin", "xmax": "snap xmax",
                        "active": "活跃集合", "label": "标签"}
            for column in columns:
                self.tx_tree.heading(column, text=headings[column])
                self.tx_tree.column(column, width=widths[column], stretch=False)
            scroll = ttk.Scrollbar(card, orient="vertical", command=self.tx_tree.yview)
            self.tx_tree.configure(yscrollcommand=scroll.set)
            self.tx_tree.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
            self.tx_tree.bind("<<TreeviewSelect>>", self._tx_selected)

        def _build_query(self, parent: Any, ttk: Any) -> None:
            card = ttk.LabelFrame(parent, text="可见性查询", padding=8)
            card.pack(fill="x", pady=(0, 8))
            row1 = ttk.Frame(card)
            row1.pack(fill="x", pady=2)
            ttk.Label(row1, text="事务视角").pack(side="left")
            self.tx_choice = ttk.Combobox(row1, state="readonly", width=17)
            self.tx_choice.pack(side="left", padx=(6, 10))
            self.tx_choice.set("LATEST")
            ttk.Label(row1, text="表").pack(side="left")
            self.table_var = tk.StringVar(value="inventory")
            ttk.Entry(row1, textvariable=self.table_var, width=13).pack(side="left", padx=(5, 10))
            ttk.Label(row1, text="键").pack(side="left")
            self.key_var = tk.StringVar(value="sku-1")
            ttk.Entry(row1, textvariable=self.key_var, width=16).pack(side="left", padx=5)

            row2 = ttk.Frame(card)
            row2.pack(fill="x", pady=(6, 0))
            ttk.Button(row2, text="快速查询", command=lambda: self.run_query(False)).pack(side="left")
            ttk.Button(row2, text="校验查询", command=lambda: self.run_query(True)).pack(side="left", padx=6)
            ttk.Button(row2, text="重复字节比对", command=self.repeat_byte_check).pack(side="left", padx=6)
            self.query_status = ttk.Label(row2, text="", style="Small.TLabel")
            self.query_status.pack(side="left", padx=8)

        def _build_snapshots(self, parent: Any, ttk: Any) -> None:
            card = ttk.LabelFrame(parent, text="命名快照", padding=8)
            card.pack(fill="x", pady=(0, 8))
            row = ttk.Frame(card)
            row.pack(fill="x")
            self.snapshot_name = tk.StringVar(value="baseline")
            ttk.Entry(row, textvariable=self.snapshot_name, width=22).pack(side="left")
            ttk.Button(row, text="保存", command=self.save_snapshot).pack(side="left", padx=5)
            ttk.Button(row, text="刷新列表", command=self.refresh_snapshots).pack(side="left")
            row2 = ttk.Frame(card)
            row2.pack(fill="x", pady=(6, 0))
            self.snapshot_choice = ttk.Combobox(row2, state="readonly", width=25)
            self.snapshot_choice.pack(side="left")
            ttk.Button(row2, text="整体恢复", command=lambda: self.restore_snapshot(False)).pack(side="left", padx=5)
            ttk.Button(row2, text="恢复并重放", command=lambda: self.restore_snapshot(True)).pack(side="left")

        def _build_background(self, parent: Any, ttk: Any) -> None:
            card = ttk.LabelFrame(parent, text="后台提交", padding=8)
            card.pack(fill="x")
            row = ttk.Frame(card)
            row.pack(fill="x")
            ttk.Label(row, text="并发上限").pack(side="left")
            self.concurrency_var = tk.IntVar(value=3)
            ttk.Spinbox(row, from_=1, to=8, textvariable=self.concurrency_var, width=5).pack(side="left", padx=5)
            ttk.Label(row, text="间隔 ms").pack(side="left")
            self.interval_var = tk.IntVar(value=100)
            ttk.Spinbox(row, from_=10, to=5000, increment=10, textvariable=self.interval_var, width=7).pack(side="left", padx=5)
            self.bg_button = ttk.Button(row, text="启动", command=self.toggle_background)
            self.bg_button.pack(side="left", padx=6)
            self.bg_status = ttk.Label(card, text="后台未运行", style="Small.TLabel")
            self.bg_status.pack(anchor="w", pady=(6, 0))

        def _build_right(self, parent: Any, ttk: Any) -> None:
            notebook = ttk.Notebook(parent)
            notebook.pack(fill="both", expand=True)

            self.script_text = self._text_tab(notebook, "事务脚本", ttk)
            self.result_text = self._text_tab(notebook, "快速结果", ttk)
            self.authority_text = self._text_tab(notebook, "权威结论 / 候选链", ttk)
            self.basis_text = self._text_tab(notebook, "判定依据", ttk)
            self.metrics_text = self._text_tab(notebook, "指标", ttk)
            self.log_text = self._text_tab(notebook, "结构化日志", ttk)

            controls = ttk.Frame(parent)
            controls.pack(fill="x", pady=(8, 0))
            ttk.Button(controls, text="加载样本脚本", command=self.load_sample).pack(side="left")
            ttk.Button(controls, text="选择 JSON 脚本", command=self.choose_script).pack(side="left", padx=5)
            ttk.Button(controls, text="执行脚本", command=self.execute_script).pack(side="left")

        @staticmethod
        def _text_tab(notebook: Any, title: str, ttk: Any) -> Any:
            frame = ttk.Frame(notebook, padding=4)
            notebook.add(frame, text=title)
            text = tk.Text(frame, wrap="none", font=("Consolas", 10), undo=False)
            yscroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
            xscroll = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
            text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
            text.grid(row=0, column=0, sticky="nsew")
            yscroll.grid(row=0, column=1, sticky="ns")
            xscroll.grid(row=1, column=0, sticky="ew")
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            return text

        # ---------------------------------------------------------- refresh
        def refresh_all(self) -> None:
            self.refresh_transactions()
            self.refresh_snapshots()
            self.refresh_logs()
            self.show_metrics()

        def refresh_transactions(self) -> None:
            try:
                rows = self.workbench.transactions()
            except BusinessError as exc:
                self._show_error(exc)
                return
            selected = self.tx_choice.get()
            for item in self.tx_tree.get_children():
                self.tx_tree.delete(item)
            choices = ["LATEST"]
            for row in rows:
                self.tx_tree.insert(
                    "",
                    "end",
                    values=(
                        row["txid"],
                        row["state"],
                        row["snapshot_xmin"],
                        row["snapshot_xmax"],
                        ",".join(str(value) for value in row["active"]) or "-",
                        row["label"],
                    ),
                )
                choices.append(f"{row['txid']} ({row['state']})")
            self.tx_choice.configure(values=choices)
            if selected in choices:
                self.tx_choice.set(selected)
            meta = self.workbench.metadata()
            self.session_label.configure(
                text=f"session v{meta['state_version']} | next txid {meta['next_txid']} | {meta['db_path']}"
            )
            if self.background:
                stats = self.background.stats()
                self.bg_status.configure(
                    text=f"已提交 {stats['completed']} / {stats['submitted']}，错误 {stats['errors']}"
                )

        def refresh_snapshots(self) -> None:
            names = sorted(path.stem.removesuffix(".mvccsnap") for path in self.workbench.snapshot_dir.glob("*.mvccsnap.json"))
            self.snapshot_choice.configure(values=names)
            if names and not self.snapshot_choice.get():
                self.snapshot_choice.set(names[-1])

        def refresh_logs(self) -> None:
            records = self.workbench.logger.tail(limit=100)
            self._set_text(self.log_text, "\n".join(pretty_json(row) for row in records))

        def _periodic_refresh(self) -> None:
            if self._closed:
                return
            self.refresh_transactions()
            self.refresh_logs()
            self.root.after(700, self._periodic_refresh)

        # ----------------------------------------------------------- query
        def _scope(self) -> int | None:
            value = self.tx_choice.get().strip()
            if not value or value == "LATEST":
                return None
            return int(value.split()[0])

        def run_query(self, verify: bool) -> None:
            if self._busy:
                return
            self._busy = True
            self.query_status.configure(text="查询中…")
            txid = self._scope()
            table = self.table_var.get()
            key = self.key_var.get()

            def task() -> None:
                try:
                    result = (
                        self.workbench.verify(table, key, txid=txid)
                        if verify
                        else self.workbench.query(table, key, txid=txid)
                    )
                    events.put(("query", (verify, result)))
                except BusinessError as exc:
                    events.put(("error", exc))

            threading.Thread(target=task, name="mvcc-gui-query", daemon=True).start()

        def _show_query(self, verify: bool, response: dict[str, Any]) -> None:
            self._busy = False
            if verify:
                result = response["authoritative"]
                self._set_text(self.result_text, pretty_json(response["fast"]))
                self._set_text(self.authority_text, pretty_json(result))
                self._set_text(
                    self.basis_text,
                    pretty_json({"basis": result["basis"], "candidates": result["candidates"]}),
                )
                self.query_status.configure(
                    text=f"权威一致 | {response['timing']['elapsed_us']} µs | cache={'hit' if response['cache_hit'] else 'miss'}"
                )
            else:
                result = response["result"]
                self._set_text(self.result_text, pretty_json(result))
                self._set_text(self.authority_text, "点击“校验查询”运行事务日志与版本链权威重放。")
                self._set_text(self.basis_text, pretty_json(result["basis"]))
                self.query_status.configure(
                    text=f"快速路径 | {response['timing']['elapsed_us']} µs | cache={'hit' if response['cache_hit'] else 'miss'}"
                )
            self.show_metrics()
            self.refresh_logs()

        def repeat_byte_check(self) -> None:
            txid = self._scope()
            table = self.table_var.get()
            key = self.key_var.get()
            first = self.workbench.query(table, key, txid=txid, record=False)
            second = self.workbench.query(table, key, txid=txid, record=False)
            equal = first["canonical"] == second["canonical"]
            self.query_status.configure(text="逐字节一致" if equal else "逐字节不一致")
            self._set_text(
                self.basis_text,
                pretty_json(
                    {
                        "byte_identical": equal,
                        "first_sha256": first["canonical_sha256"],
                        "second_sha256": second["canonical_sha256"],
                        "canonical": first["canonical"] if equal else None,
                    }
                ),
            )

        # --------------------------------------------------------- scripts
        def load_sample(self) -> None:
            try:
                self.script_text.delete("1.0", "end")
                self.script_text.insert("1.0", SAMPLE_PATH.read_text(encoding="utf-8"))
                self.status_var.set(f"已加载 {SAMPLE_PATH.name}")
            except OSError as exc:
                messagebox.showerror("脚本", str(exc))

        def choose_script(self) -> None:
            path = filedialog.askopenfilename(
                title="选择事务脚本",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if path:
                try:
                    self.script_text.delete("1.0", "end")
                    self.script_text.insert("1.0", Path(path).read_text(encoding="utf-8"))
                    self.status_var.set(f"已加载 {path}")
                except OSError as exc:
                    messagebox.showerror("脚本", str(exc))

        def execute_script(self) -> None:
            if self._busy:
                return
            try:
                script = json.loads(self.script_text.get("1.0", "end"))
            except json.JSONDecodeError as exc:
                messagebox.showerror("脚本 JSON 错误", str(exc))
                return
            if script.get("format") != SCRIPT_FORMAT:
                messagebox.showerror("脚本", f"format 必须是 {SCRIPT_FORMAT}")
                return
            self._busy = True
            self.status_var.set("正在执行事务脚本…")

            def task() -> None:
                try:
                    result = ScriptRunner(self.workbench).run(script)
                    events.put(("script", result))
                except BusinessError as exc:
                    events.put(("error", exc))

            threading.Thread(target=task, name="mvcc-gui-script", daemon=True).start()

        def _show_script(self, result: dict[str, Any]) -> None:
            self._busy = False
            self.status_var.set(
                f"脚本完成：{result['steps']} 步，{len(result['reads'])} 次读取，canonical_equal={result['all_canonical_equal']}"
            )
            self._set_text(self.authority_text, pretty_json(result))
            self.refresh_all()

        # -------------------------------------------------------- snapshots
        def save_snapshot(self) -> None:
            name = self.snapshot_name.get().strip()
            if not name:
                return
            try:
                result = self.workbench.save_snapshot(name)
                self.status_var.set(f"快照已保存：{result['path']}")
                self.refresh_snapshots()
            except BusinessError as exc:
                self._show_error(exc)

        def restore_snapshot(self, replay: bool) -> None:
            name = self.snapshot_choice.get().strip()
            if not name:
                messagebox.showinfo("快照", "请选择一个命名快照")
                return
            if self.background:
                self.toggle_background()
            try:
                result = self.workbench.restore_snapshot(name, replay=replay)
                self.status_var.set(
                    f"整体恢复完成：v{result['current_version_before']} -> v{result['snapshot_version']}"
                )
                self._set_text(self.authority_text, pretty_json(result))
                self.refresh_all()
            except BusinessError as exc:
                self._show_error(exc)

        # ------------------------------------------------------- background
        def toggle_background(self) -> None:
            if self.background:
                stats = self.background.stop()
                self.background = None
                self.bg_button.configure(text="启动")
                self.bg_status.configure(text=f"后台已停止，完成 {stats['completed']}，错误 {stats['errors']}")
                self.refresh_transactions()
                return
            self.background = BackgroundCommitter(
                self.workbench,
                concurrency=int(self.concurrency_var.get()),
                interval_s=max(0.01, int(self.interval_var.get()) / 1000.0),
                callback=lambda event: events.put(("background", event)),
            )
            try:
                self.background.start()
            except BusinessError as exc:
                self.background = None
                self._show_error(exc)
                return
            self.bg_button.configure(text="停止")
            self.bg_status.configure(text="后台运行中：持续 begin/write/commit 到 background_events/counter")

        # ------------------------------------------------------------ misc
        def replay_history(self) -> None:
            try:
                result = self.workbench.replay_history()
                self.status_var.set(f"历史重放通过：{result['matched']} / {result['checked']}")
                self._set_text(self.authority_text, pretty_json(result))
            except BusinessError as exc:
                self._show_error(exc)

        def show_metrics(self) -> None:
            try:
                self._set_text(self.metrics_text, pretty_json(self.workbench.metrics_summary()))
            except BusinessError as exc:
                self._show_error(exc)

        def _tx_selected(self, _event: Any = None) -> None:
            selection = self.tx_tree.selection()
            if not selection:
                return
            values = self.tx_tree.item(selection[0], "values")
            self.tx_choice.set(f"{values[0]} ({values[1]})")

        def _show_error(self, exc: BusinessError) -> None:
            self._busy = False
            self.query_status.configure(text=f"{exc.code}: {exc.message}")
            self._set_text(self.authority_text, pretty_json(exc.as_dict()))
            self.workbench.logger.event(
                "gui_business_error", level="error", code=exc.code, message=exc.message,
                details=exc.details,
            )
            self.refresh_logs()

        @staticmethod
        def _set_text(widget: Any, text: str) -> None:
            widget.delete("1.0", "end")
            widget.insert("1.0", text)

        def _drain_events(self) -> None:
            if self._closed:
                return
            while True:
                try:
                    kind, payload = events.get_nowait()
                except queue.Empty:
                    break
                if kind == "query":
                    verify, response = payload
                    self._show_query(verify, response)
                elif kind == "script":
                    self._show_script(payload)
                elif kind == "background":
                    if payload.get("kind") == "background_error":
                        self.bg_status.configure(text=f"后台错误：{payload.get('error')}")
                    self.refresh_transactions()
                elif kind == "error":
                    self._show_error(payload)
            self.root.after(100, self._drain_events)

        def on_close(self) -> None:
            if self.background:
                self.background.stop()
                self.background = None
            self._closed = True
            self.workbench.logger.event("gui_closed", exit_report_generated=False)
            self.root.destroy()

    root = tk.Tk()
    app = WorkbenchApp(root)
    root.mainloop()
