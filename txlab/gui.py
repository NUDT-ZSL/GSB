"""Tkinter desktop interface for the transaction and lock laboratory."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .errors import LabError
from .models import SchedulerConfig
from .replay import replay_snapshot
from .reporting import format_event, format_replay_report
from .service import execute_script_file
from .snapshot import list_snapshots, restore_snapshot, save_snapshot
from .storage import SQLiteStore


class LabApp:
    def __init__(
        self,
        root: tk.Tk,
        *,
        script_path: str,
        init_sql_path: str,
        db_path: str,
        snapshot_dir: str,
    ) -> None:
        self.root = root
        self.root.title("SQLite 事务隔离与行锁实验台")
        self.root.geometry("1280x820")
        self.root.minsize(1050, 680)

        self.script_var = tk.StringVar(value=script_path)
        self.init_sql_var = tk.StringVar(value=init_sql_path)
        self.db_var = tk.StringVar(value=db_path)
        self.snapshot_dir_var = tk.StringVar(value=snapshot_dir)
        self.budget_var = tk.StringVar(value="500")
        self.concurrency_var = tk.StringVar(value="2")
        self.tick_var = tk.StringVar(value="10")
        self.snapshot_name_var = tk.StringVar(value="gui-snapshot")
        self.snapshot_choice_var = tk.StringVar()
        self.animate_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="就绪")

        self.event_queue: queue.Queue[tuple[str, Any, Any]] = queue.Queue()
        self.running = False
        self.last_result: dict[str, Any] | None = None
        self.last_initial_sql_text: str | None = None
        self.snapshot_paths: dict[str, str] = {}
        self._closing = False

        self._build_ui()
        self._refresh_snapshot_choices()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(60, self._poll_events)

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=24)

        controls = ttk.LabelFrame(self.root, text="执行参数")
        controls.pack(fill=tk.X, padx=10, pady=(10, 6))
        for column in range(8):
            controls.columnconfigure(column, weight=0)
        controls.columnconfigure(1, weight=3)
        controls.columnconfigure(5, weight=2)

        ttk.Label(controls, text="编辑脚本").grid(row=0, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Entry(controls, textvariable=self.script_var).grid(
            row=0, column=1, columnspan=4, sticky=tk.EW, padx=4, pady=4
        )
        ttk.Button(
            controls,
            text="浏览…",
            command=lambda: self._browse_file(self.script_var, [("JSON", "*.json")]),
        ).grid(row=0, column=5, sticky=tk.W, padx=4, pady=4)

        ttk.Label(controls, text="初始 SQL").grid(row=1, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Entry(controls, textvariable=self.init_sql_var).grid(
            row=1, column=1, columnspan=4, sticky=tk.EW, padx=4, pady=4
        )
        ttk.Button(
            controls,
            text="浏览…",
            command=lambda: self._browse_file(self.init_sql_var, [("SQL", "*.sql")]),
        ).grid(row=1, column=5, sticky=tk.W, padx=4, pady=4)

        ttk.Label(controls, text="SQLite 文件").grid(row=2, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Entry(controls, textvariable=self.db_var).grid(
            row=2, column=1, columnspan=4, sticky=tk.EW, padx=4, pady=4
        )
        ttk.Button(
            controls,
            text="浏览…",
            command=lambda: self._browse_save_file(self.db_var, [("SQLite", "*.sqlite3")]),
        ).grid(row=2, column=5, sticky=tk.W, padx=4, pady=4)

        ttk.Label(controls, text="等待预算(ms)").grid(row=3, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Entry(controls, textvariable=self.budget_var, width=9).grid(
            row=3, column=1, sticky=tk.W, padx=4, pady=4
        )
        ttk.Label(controls, text="并发上限").grid(row=3, column=2, sticky=tk.W, padx=6, pady=4)
        ttk.Entry(controls, textvariable=self.concurrency_var, width=7).grid(
            row=3, column=3, sticky=tk.W, padx=4, pady=4
        )
        ttk.Label(controls, text="调度 tick(ms)").grid(row=3, column=4, sticky=tk.W, padx=6, pady=4)
        ttk.Entry(controls, textvariable=self.tick_var, width=7).grid(
            row=3, column=5, sticky=tk.W, padx=4, pady=4
        )
        ttk.Checkbutton(
            controls,
            text="可视化节流",
            variable=self.animate_var,
        ).grid(row=3, column=6, sticky=tk.W, padx=10, pady=4)

        button_bar = ttk.Frame(self.root)
        button_bar.pack(fill=tk.X, padx=10, pady=4)
        self.run_button = ttk.Button(button_bar, text="执行脚本", command=self._execute_async)
        self.run_button.pack(side=tk.LEFT, padx=(0, 6))
        self.save_button = ttk.Button(
            button_bar, text="保存快照", command=self._save_snapshot, state=tk.DISABLED
        )
        self.save_button.pack(side=tk.LEFT, padx=6)
        ttk.Label(button_bar, text="快照名").pack(side=tk.LEFT, padx=(18, 4))
        ttk.Entry(button_bar, textvariable=self.snapshot_name_var, width=20).pack(
            side=tk.LEFT, padx=4
        )
        self.snapshot_combo = ttk.Combobox(
            button_bar,
            textvariable=self.snapshot_choice_var,
            state="readonly",
            width=26,
        )
        self.snapshot_combo.pack(side=tk.LEFT, padx=(18, 4))
        ttk.Button(button_bar, text="刷新", command=self._refresh_snapshot_choices).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(button_bar, text="恢复快照", command=self._restore_selected).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(button_bar, text="重放校验", command=self._replay_selected_async).pack(
            side=tk.LEFT, padx=3
        )

        snapshot_row = ttk.Frame(self.root)
        snapshot_row.pack(fill=tk.X, padx=10, pady=(0, 4))
        ttk.Label(snapshot_row, text="快照目录").pack(side=tk.LEFT)
        ttk.Entry(snapshot_row, textvariable=self.snapshot_dir_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=5
        )
        ttk.Button(
            snapshot_row,
            text="浏览…",
            command=lambda: self._browse_directory(self.snapshot_dir_var),
        ).pack(side=tk.LEFT)

        main_pane = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        left = ttk.Frame(main_pane)
        right = ttk.Frame(main_pane)
        main_pane.add(left, weight=3)
        main_pane.add(right, weight=2)

        session_frame = ttk.LabelFrame(left, text="会话事务阶段与等待预算")
        session_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        self.session_tree = ttk.Treeview(
            session_frame,
            columns=(
                "session",
                "isolation",
                "phase",
                "locks",
                "wait_pos",
                "budget",
                "outcome",
            ),
            show="headings",
            height=8,
        )
        self._configure_columns(
            self.session_tree,
            {
                "session": ("会话", 90),
                "isolation": ("隔离级别", 120),
                "phase": ("阶段", 110),
                "locks": ("持有行锁", 170),
                "wait_pos": ("等待位置", 75),
                "budget": ("已用预算", 80),
                "outcome": ("结局", 130),
            },
        )
        session_scroll = ttk.Scrollbar(
            session_frame, orient=tk.VERTICAL, command=self.session_tree.yview
        )
        self.session_tree.configure(yscrollcommand=session_scroll.set)
        self.session_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        session_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        lock_frame = ttk.LabelFrame(left, text="行锁持有者与稳定 FIFO 等待队列")
        lock_frame.pack(fill=tk.BOTH, expand=True)
        self.lock_tree = ttk.Treeview(
            lock_frame,
            columns=("row", "holders", "waiters"),
            show="headings",
            height=7,
        )
        self._configure_columns(
            self.lock_tree,
            {
                "row": ("行键", 150),
                "holders": ("持有者", 240),
                "waiters": ("等待队列", 420),
            },
        )
        lock_scroll = ttk.Scrollbar(
            lock_frame, orient=tk.VERTICAL, command=self.lock_tree.yview
        )
        self.lock_tree.configure(yscrollcommand=lock_scroll.set)
        self.lock_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lock_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        trace_frame = ttk.LabelFrame(right, text="执行轨迹")
        trace_frame.pack(fill=tk.BOTH, expand=True)
        self.trace_text = tk.Text(trace_frame, wrap=tk.NONE, font=("Consolas", 9))
        trace_v = ttk.Scrollbar(trace_frame, orient=tk.VERTICAL, command=self.trace_text.yview)
        trace_h = ttk.Scrollbar(trace_frame, orient=tk.HORIZONTAL, command=self.trace_text.xview)
        self.trace_text.configure(
            yscrollcommand=trace_v.set,
            xscrollcommand=trace_h.set,
            state=tk.DISABLED,
        )
        self.trace_text.grid(row=0, column=0, sticky="nsew")
        trace_v.grid(row=0, column=1, sticky="ns")
        trace_h.grid(row=1, column=0, sticky="ew")
        trace_frame.rowconfigure(0, weight=1)
        trace_frame.columnconfigure(0, weight=1)

        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            relief=tk.SUNKEN,
            anchor=tk.W,
            padding=(8, 4),
        )
        status.pack(fill=tk.X, side=tk.BOTTOM)

    @staticmethod
    def _configure_columns(tree: ttk.Treeview, columns: dict[str, tuple[str, int]]) -> None:
        for key, (label, width) in columns.items():
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=55, stretch=True)

    def _execute_async(self) -> None:
        if self.running:
            return
        try:
            config = SchedulerConfig(
                lock_wait_budget_ms=int(self.budget_var.get()),
                max_concurrency=int(self.concurrency_var.get()),
                tick_ms=int(self.tick_var.get()),
            )
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        script_path = self.script_var.get().strip()
        init_sql_path = self.init_sql_var.get().strip() or None
        db_path = self.db_var.get().strip()
        if not script_path or not db_path:
            messagebox.showerror("参数错误", "脚本路径和 SQLite 文件不能为空")
            return
        try:
            self.last_initial_sql_text = (
                Path(init_sql_path).read_text(encoding="utf-8") if init_sql_path else None
            )
        except OSError as exc:
            messagebox.showerror("读取失败", f"无法读取初始 SQL：{exc}")
            return

        self.trace_text.configure(state=tk.NORMAL)
        self.trace_text.delete("1.0", tk.END)
        self.trace_text.configure(state=tk.DISABLED)
        for item in self.session_tree.get_children():
            self.session_tree.delete(item)
        for item in self.lock_tree.get_children():
            self.lock_tree.delete(item)
        self.status_var.set("正在执行…")
        self.run_button.configure(state=tk.DISABLED)
        self.save_button.configure(state=tk.DISABLED)
        self.last_result = None
        self.running = True

        animate = self.animate_var.get()

        def worker() -> None:
            def on_event(event: dict[str, Any], state: dict[str, Any]) -> None:
                self.event_queue.put(("event", event, state))
                if animate:
                    time.sleep(0.02)

            try:
                result = execute_script_file(
                    script_path,
                    db_path=db_path,
                    init_sql_path=init_sql_path,
                    config=config,
                    on_event=on_event,
                )
                self.event_queue.put(("done", result, None))
            except LabError as exc:
                self.event_queue.put(("error", exc.to_dict(), None))
            except Exception as exc:  # Keep the UI usable for unexpected failures.
                self.event_queue.put(
                    (
                        "error",
                        {"code": "E_STORAGE", "message": str(exc)},
                        None,
                    )
                )

        threading.Thread(target=worker, daemon=True, name="txlab-scheduler").start()

    def _poll_events(self) -> None:
        if self._closing:
            return
        processed = 0
        while processed < 200:
            try:
                kind, payload, state = self.event_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if kind == "event":
                self._apply_state(state)
                self._append_trace(format_event(payload))
            elif kind == "done":
                self.running = False
                self.last_result = payload
                self.run_button.configure(state=tk.NORMAL)
                self.save_button.configure(state=tk.NORMAL)
                self._apply_final_result(payload)
                if not self.snapshot_name_var.get().strip():
                    self.snapshot_name_var.set(Path(self.script_var.get()).stem)
            elif kind == "error":
                self.running = False
                self.run_button.configure(state=tk.NORMAL)
                self.status_var.set(f"失败：{payload.get('code')} {payload.get('message')}")
                self._append_trace(
                    f"ERROR {payload.get('code')} {payload.get('message')}"
                )
                messagebox.showerror(
                    payload.get("code", "ERROR"),
                    payload.get("message", "执行失败"),
                )
            elif kind == "replay":
                self._show_replay_report(payload)
        self.root.after(60, self._poll_events)

    def _apply_state(self, state: dict[str, Any]) -> None:
        for session in state.get("sessions", []):
            values = (
                session["session_id"],
                session["isolation"],
                session["phase"],
                ", ".join(
                    f"{lock['row_key']}:{lock['mode']}"
                    for lock in session.get("held_locks", [])
                )
                or "-",
                session.get("wait_position") or "-",
                f"{session.get('wait_budget_used_ms', 0)}ms",
                session.get("outcome") or "-",
            )
            if self.session_tree.exists(session["session_id"]):
                self.session_tree.item(session["session_id"], values=values)
            else:
                self.session_tree.insert("", tk.END, iid=session["session_id"], values=values)
        self._render_locks(state.get("locks", []))
        self.status_var.set(
            f"仿真时间 {state.get('sim_time_ms', 0)}ms | "
            f"活跃会话 {state.get('active_sessions', 0)} | "
            f"事件 {state.get('events_count', 0)}"
        )

    def _apply_final_result(self, result: dict[str, Any]) -> None:
        final_state = {
            "sessions": [
                {
                    "session_id": session["session_id"],
                    "isolation": session["isolation"],
                    "phase": (
                        "committed"
                        if session["outcome"] == "commit"
                        else "rolled_back"
                    ),
                    "held_locks": [],
                    "wait_position": None,
                    "wait_budget_used_ms": session["wait_budget_used_ms"],
                    "outcome": session["outcome"],
                    "code": session["code"],
                }
                for session in result["sessions"]
            ],
            "locks": result.get("final_lock_state", []),
            "sim_time_ms": result.get("sim_time_ms", 0),
            "active_sessions": 0,
            "events_count": len(result.get("events", [])),
        }
        self._apply_state(final_state)
        code = result.get("code") or "OK"
        self.status_var.set(
            f"完成：{code} | 数据版本 {result['final_data_version']} | "
            f"提交 {result['summary']['committed']} | "
            f"回滚 {result['summary']['rolled_back']} | "
            f"死锁 {result['summary']['deadlocks']} | "
            f"隔离摘要一致 {result['isolation_ok']}"
        )

    def _render_locks(self, locks: list[dict[str, Any]]) -> None:
        for item in self.lock_tree.get_children():
            self.lock_tree.delete(item)
        for index, lock in enumerate(locks):
            holders = ", ".join(
                f"{holder['session_id']}:{holder['mode']}"
                for holder in lock.get("holders", [])
            ) or "-"
            waiters = " -> ".join(
                f"#{waiter['position']} {waiter['session_id']}:{waiter['mode']}"
                for waiter in lock.get("waiters", [])
            ) or "-"
            self.lock_tree.insert(
                "",
                tk.END,
                iid=f"lock-{index}",
                values=(lock["row_key"], holders, waiters),
            )

    def _append_trace(self, line: str) -> None:
        self.trace_text.configure(state=tk.NORMAL)
        self.trace_text.insert(tk.END, line + "\n")
        self.trace_text.see(tk.END)
        self.trace_text.configure(state=tk.DISABLED)

    def _save_snapshot(self) -> None:
        if self.last_result is None:
            messagebox.showinfo("保存快照", "请先成功执行一个脚本")
            return
        name = self.snapshot_name_var.get().strip()
        if not name:
            messagebox.showerror("快照名称", "快照名称不能为空")
            return
        try:
            path = save_snapshot(
                self.snapshot_dir_var.get().strip(),
                name,
                self.last_result,
                initial_sql=self.last_initial_sql_text,
            )
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._refresh_snapshot_choices(preferred=name)
        self.status_var.set(f"快照已保存：{path}")
        messagebox.showinfo("保存快照", f"已保存：\n{path}")

    def _restore_selected(self) -> None:
        path = self._selected_snapshot_path()
        if path is None:
            return
        try:
            with SQLiteStore(self.db_var.get().strip()) as store:
                report = restore_snapshot(path, store)
        except Exception as exc:
            messagebox.showerror("恢复失败", str(exc))
            return
        self.status_var.set(
            f"已整体替换恢复 | 快照版本 {report['snapshot_session_version']} | "
            f"恢复前版本 {report['current_session_version']} | "
            f"版本差异 {report['version_mismatch']}"
        )
        self._append_trace(
            "RESTORE full_replace "
            f"snapshot_version={report['snapshot_session_version']} "
            f"current_version={report['current_session_version']} "
            f"version_mismatch={report['version_mismatch']} "
            f"digest={report['restored_digest']}"
        )
        messagebox.showinfo(
            "恢复完成",
            "已整体替换全部会话状态，不做增量合并。\n"
            f"快照版本：{report['snapshot_session_version']}\n"
            f"恢复前版本：{report['current_session_version']}\n"
            f"版本不同：{report['version_mismatch']}",
        )

    def _replay_selected_async(self) -> None:
        path = self._selected_snapshot_path()
        if path is None or self.running:
            return
        self.running = True
        self.run_button.configure(state=tk.DISABLED)
        self.status_var.set("正在重放校验…")

        def worker() -> None:
            try:
                report = replay_snapshot(path)
                self.event_queue.put(("replay", report, None))
            except Exception as exc:
                self.event_queue.put(
                    (
                        "error",
                        {"code": "E_REPLAY", "message": str(exc)},
                        None,
                    )
                )

        threading.Thread(target=worker, daemon=True, name="txlab-replay").start()

    def _show_replay_report(self, report: dict[str, Any]) -> None:
        self.running = False
        self.run_button.configure(state=tk.NORMAL)
        self._append_trace(format_replay_report(report))
        self.status_var.set(
            f"重放：{report['code']} | 分歧事务 {report['diverged_sessions']}"
        )
        if report["code"] == "E_REPLAY":
            messagebox.showwarning(
                "E_REPLAY",
                "重放不可复现。\n分歧事务："
                + ", ".join(report["diverged_sessions"]),
            )
        else:
            messagebox.showinfo("重放完成", "事务结局与事件序列完全一致")

    def _selected_snapshot_path(self) -> str | None:
        choice = self.snapshot_choice_var.get()
        path = self.snapshot_paths.get(choice)
        if path is None:
            path = filedialog.askopenfilename(
                title="选择快照",
                initialdir=self.snapshot_dir_var.get(),
                filetypes=[("Snapshot JSON", "*.json")],
            )
        if not path:
            return None
        return path

    def _refresh_snapshot_choices(self, preferred: str | None = None) -> None:
        directory = self.snapshot_dir_var.get().strip()
        try:
            snapshots = list_snapshots(directory)
        except Exception:
            snapshots = []
        choices = []
        self.snapshot_paths = {}
        for item in snapshots:
            if item.get("error"):
                label = f"{item['name']} [invalid]"
            else:
                label = f"{item['name']} (v{item['state_version']})"
            choices.append(label)
            self.snapshot_paths[label] = item["path"]
        self.snapshot_combo.configure(values=choices)
        if preferred:
            for label, path in self.snapshot_paths.items():
                if Path(path).stem == preferred:
                    self.snapshot_choice_var.set(label)
                    return
        if choices and self.snapshot_choice_var.get() not in choices:
            self.snapshot_choice_var.set(choices[0])

    def _browse_file(self, variable: tk.StringVar, filetypes: list[tuple[str, str]]) -> None:
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path:
            variable.set(path)

    def _browse_save_file(
        self,
        variable: tk.StringVar,
        filetypes: list[tuple[str, str]],
    ) -> None:
        path = filedialog.asksaveasfilename(filetypes=filetypes)
        if path:
            variable.set(path)

    def _browse_directory(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            variable.set(path)
            self._refresh_snapshot_choices()

    def _on_close(self) -> None:
        self._closing = True
        self.root.destroy()


def launch_gui(
    *,
    script_path: str,
    init_sql_path: str,
    db_path: str,
    snapshot_dir: str,
) -> None:
    root = tk.Tk()
    LabApp(
        root,
        script_path=script_path,
        init_sql_path=init_sql_path,
        db_path=db_path,
        snapshot_dir=snapshot_dir,
    )
    root.mainloop()
