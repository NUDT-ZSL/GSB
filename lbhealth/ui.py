"""Tkinter desktop application for live health-state and ledger inspection."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .engine import HealthController, HealthEngine
from .errors import HealthError
from .ledger import Ledger
from .models import HealthState, RoundResult
from .prober import LoopbackProber
from .recovery import recover_session
from .snapshot import SnapshotService


class LBHealthApp:
    def __init__(self, root: Any, config_path: str, ledger_path: str) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.root.title("LB Health · 状态演进与恢复一致性")
        self.root.geometry("1260x790")
        self.root.minsize(1040, 650)
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.style.configure("Metric.TLabel", font=("TkDefaultFont", 11, "bold"))
        self.style.configure("Muted.TLabel", foreground="#5b6472")
        self.style.configure("Healthy.Treeview", foreground="#166534")
        self.style.configure("Unhealthy.Treeview", foreground="#991b1b")
        self.style.configure("Draining.Treeview", foreground="#92400e")
        self.style.configure("Unknown.Treeview", foreground="#475569")

        self.config_path_var = tk.StringVar(value=config_path)
        self.ledger_path_var = tk.StringVar(value=ledger_path)
        self.round_var = tk.StringVar(value="轮次: -")
        self.available_var = tk.StringVar(value="可用成员: -")
        self.weight_var = tk.StringVar(value="生效权重: -")
        self.full_sum_var = tk.StringVar(value="满额权重和: -")
        self.snapshot_count_var = tk.StringVar(value="快照: -")
        self.status_var = tk.StringVar(value="就绪")
        self.busy = False
        self.auto_running = False
        self.auto_thread: threading.Thread | None = None
        self.auto_stop = threading.Event()
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.latest_result: RoundResult | None = None
        self.config: Config | None = None
        self.ledger: Ledger | None = None
        self.engine: HealthEngine | None = None
        self.controller: HealthController | None = None
        self.snapshot_service: SnapshotService | None = None

        self._build_layout()
        self._load()
        self.root.after(100, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_layout(self) -> None:
        tk = self.tk
        ttk = self.ttk
        outer = ttk.Frame(self.root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        outer.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(outer)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="配置").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(toolbar, textvariable=self.config_path_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 10)
        )
        ttk.Label(toolbar, text="账本").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(toolbar, textvariable=self.ledger_path_var, width=34).grid(
            row=0, column=3, sticky="ew", padx=(0, 10)
        )
        ttk.Button(toolbar, text="打开 / 重载", command=self._load).grid(row=0, column=4)

        metrics = ttk.Frame(outer, padding=(0, 0, 0, 8))
        metrics.grid(row=1, column=0, sticky="ew")
        for column in range(5):
            metrics.columnconfigure(column, weight=1)
        ttk.Label(metrics, textvariable=self.round_var, style="Metric.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(metrics, textvariable=self.available_var, style="Metric.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(metrics, textvariable=self.weight_var, style="Metric.TLabel").grid(
            row=0, column=2, sticky="w"
        )
        ttk.Label(metrics, textvariable=self.full_sum_var, style="Metric.TLabel").grid(
            row=0, column=3, sticky="w"
        )
        ttk.Label(metrics, textvariable=self.snapshot_count_var, style="Metric.TLabel").grid(
            row=0, column=4, sticky="w"
        )

        controls = ttk.Frame(outer)
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(controls, text="单轮探测", command=self._step_round).pack(side="left")
        self.auto_button = ttk.Button(controls, text="开始连续探测", command=self._toggle_auto)
        self.auto_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Drain 选中成员", command=self._drain_selected).pack(
            side="left", padx=(16, 0)
        )
        ttk.Button(controls, text="Resume 选中成员", command=self._resume_selected).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(controls, text="保存快照", command=self._save_snapshot).pack(
            side="left", padx=(16, 0)
        )
        ttk.Button(controls, text="恢复选中快照", command=self._restore_snapshot).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(controls, textvariable=self.status_var, style="Muted.TLabel").pack(
            side="right"
        )

        paned = ttk.Panedwindow(outer, orient="vertical")
        paned.grid(row=3, column=0, sticky="nsew")

        top_pane = ttk.Panedwindow(paned, orient="horizontal")
        paned.add(top_pane, weight=3)

        members_frame = ttk.LabelFrame(top_pane, text="后端成员与轮末生效状态", padding=8)
        members_frame.rowconfigure(0, weight=1)
        members_frame.columnconfigure(0, weight=1)
        columns = (
            "member",
            "endpoint",
            "protocol",
            "state",
            "success",
            "failure",
            "slow",
            "base",
            "effective",
            "available",
        )
        self.members_tree = ttk.Treeview(
            members_frame, columns=columns, show="headings", height=15
        )
        headings = {
            "member": "成员",
            "endpoint": "回环端点",
            "protocol": "协议",
            "state": "状态",
            "success": "连续成功",
            "failure": "连续失败",
            "slow": "慢启动进度",
            "base": "满额权重",
            "effective": "生效权重",
            "available": "可用",
        }
        widths = {
            "member": 90,
            "endpoint": 150,
            "protocol": 70,
            "state": 100,
            "success": 80,
            "failure": 80,
            "slow": 130,
            "base": 80,
            "effective": 85,
            "available": 65,
        }
        for column in columns:
            self.members_tree.heading(column, text=headings[column])
            self.members_tree.column(
                column, width=widths[column], minwidth=55, anchor="center", stretch=True
            )
        member_scroll = ttk.Scrollbar(
            members_frame, orient="vertical", command=self.members_tree.yview
        )
        self.members_tree.configure(yscrollcommand=member_scroll.set)
        self.members_tree.grid(row=0, column=0, sticky="nsew")
        member_scroll.grid(row=0, column=1, sticky="ns")
        self.members_tree.tag_configure("healthy", foreground="#166534")
        self.members_tree.tag_configure("unhealthy", foreground="#991b1b")
        self.members_tree.tag_configure("draining", foreground="#92400e")
        self.members_tree.tag_configure("unknown", foreground="#475569")
        top_pane.add(members_frame, weight=3)

        snapshots_frame = ttk.LabelFrame(top_pane, text="快照列表", padding=8)
        snapshots_frame.rowconfigure(0, weight=1)
        snapshots_frame.columnconfigure(0, weight=1)
        snapshot_columns = ("id", "round", "label", "version", "digest", "created")
        self.snapshot_tree = ttk.Treeview(
            snapshots_frame, columns=snapshot_columns, show="headings", height=15
        )
        snapshot_headings = {
            "id": "ID",
            "round": "轮次",
            "label": "标签",
            "version": "配置版本",
            "digest": "成员摘要",
            "created": "创建时间",
        }
        snapshot_widths = {
            "id": 45,
            "round": 55,
            "label": 140,
            "version": 90,
            "digest": 105,
            "created": 170,
        }
        for column in snapshot_columns:
            self.snapshot_tree.heading(column, text=snapshot_headings[column])
            self.snapshot_tree.column(
                column, width=snapshot_widths[column], minwidth=40, anchor="center", stretch=True
            )
        snapshot_scroll = ttk.Scrollbar(
            snapshots_frame, orient="vertical", command=self.snapshot_tree.yview
        )
        self.snapshot_tree.configure(yscrollcommand=snapshot_scroll.set)
        self.snapshot_tree.grid(row=0, column=0, sticky="nsew")
        snapshot_scroll.grid(row=0, column=1, sticky="ns")
        top_pane.add(snapshots_frame, weight=2)

        events_frame = ttk.LabelFrame(paned, text="探测 / 迁移 / 恢复账本事件", padding=8)
        events_frame.rowconfigure(0, weight=1)
        events_frame.columnconfigure(0, weight=1)
        self.event_text = tk.Text(
            events_frame,
            height=9,
            wrap="none",
            font=("Consolas" if os.name == "nt" else "TkFixedFont", 9),
            background="#0f172a",
            foreground="#dbeafe",
            insertbackground="#dbeafe",
            relief="flat",
        )
        event_scroll_y = ttk.Scrollbar(
            events_frame, orient="vertical", command=self.event_text.yview
        )
        event_scroll_x = ttk.Scrollbar(
            events_frame, orient="horizontal", command=self.event_text.xview
        )
        self.event_text.configure(
            yscrollcommand=event_scroll_y.set, xscrollcommand=event_scroll_x.set
        )
        self.event_text.grid(row=0, column=0, sticky="nsew")
        event_scroll_y.grid(row=0, column=1, sticky="ns")
        event_scroll_x.grid(row=1, column=0, sticky="ew")
        self.event_text.configure(state="disabled")
        paned.add(events_frame, weight=2)

    def _load(self) -> None:
        if self.auto_running or self.busy:
            self.status_var.set("请先停止连续探测再重载")
            return
        try:
            config = load_config(self.config_path_var.get())
            ledger = Ledger(self.ledger_path_var.get())
            session = recover_session(config, ledger)
        except HealthError as exc:
            self._log(f"{exc.code}: {exc.message}")
            self.status_var.set(exc.code)
            self.tk.messagebox.showerror("加载失败", exc.message)
            return
        self.config = config
        self.ledger = ledger
        self.engine = session.engine
        self.controller = HealthController(
            config, ledger, session.engine, LoopbackProber(config)
        )
        self.snapshot_service = SnapshotService(ledger)
        self.latest_result = None
        self.root.title(
            f"LB Health · {config.config_version} · {len(config.members)} backends"
        )
        self._log(
            json.dumps(
                {
                    "event": "recovery",
                    "ledger": self.ledger_path_var.get(),
                    "report": session.report.to_dict(),
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        self._refresh_snapshots()
        self._refresh_members()
        self.status_var.set("已加载")

    def _step_round(self) -> None:
        if self.controller is None or self.busy or self.auto_running:
            return
        self.busy = True
        self.status_var.set("探测中...")
        thread = threading.Thread(target=self._run_one, name="ui-step", daemon=True)
        thread.start()

    def _run_one(self) -> None:
        try:
            assert self.controller is not None
            result = self.controller.run_round()
            self.events.put({"kind": "round", "result": result})
        except BaseException as exc:
            self.events.put({"kind": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            self.events.put({"kind": "step_done"})

    def _toggle_auto(self) -> None:
        if self.auto_running:
            self._stop_auto()
        else:
            self._start_auto()

    def _start_auto(self) -> None:
        if self.controller is None or self.busy:
            return
        if self.busy:
            return
        self.auto_running = True
        self.auto_stop.clear()
        self.auto_button.configure(text="停止连续探测")
        self.status_var.set("连续探测运行中")
        self.auto_thread = threading.Thread(target=self._auto_loop, name="ui-auto", daemon=True)
        self.auto_thread.start()

    def _auto_loop(self) -> None:
        assert self.controller is not None
        assert self.config is not None
        interval_s = self.config.pool.interval_ms / 1000.0
        next_tick = time.monotonic()
        while not self.auto_stop.is_set():
            delay = next_tick - time.monotonic()
            if delay > 0 and self.auto_stop.wait(delay):
                break
            try:
                result = self.controller.run_round()
                self.events.put({"kind": "round", "result": result, "auto": True})
            except BaseException as exc:
                self.events.put({"kind": "error", "error": f"{type(exc).__name__}: {exc}"})
                break
            next_tick += interval_s
            if next_tick < time.monotonic():
                next_tick = time.monotonic()
        self.events.put({"kind": "auto_stopped"})

    def _stop_auto(self) -> None:
        self.auto_stop.set()
        self.auto_running = False
        self.auto_button.configure(text="开始连续探测")
        self.status_var.set("连续探测已停止")

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("kind")
            if kind == "round":
                self.latest_result = event["result"]
                self._log_round(event["result"])
                self._refresh_members()
                self._refresh_snapshots()
                self.status_var.set("轮次完成" if not event.get("auto") else "连续探测运行中")
            elif kind == "error":
                self._log(f"ERROR: {event.get('error')}")
                self.status_var.set("运行错误")
                self.tk.messagebox.showerror("运行错误", str(event.get("error")))
            elif kind == "step_done":
                self.busy = False
            elif kind == "auto_stopped":
                self.auto_running = False
                self.auto_button.configure(text="开始连续探测")
                if self.status_var.get() == "连续探测运行中":
                    self.status_var.set("连续探测已停止")
        self.root.after(100, self._poll_events)

    def _refresh_members(self) -> None:
        if self.config is None:
            return
        state = self.latest_result.state_after if self.latest_result else self.engine.snapshot_state()
        table = self.config.pool.slow_start_weight_table
        for item in self.members_tree.get_children():
            self.members_tree.delete(item)
        for member in self.config.members:
            member_state = state.member_states[member.id]
            index = min(max(0, member_state.slow_start_index), len(table) - 1)
            display_index = index if member_state.state == HealthState.HEALTHY else 0
            percent = table[display_index] if member_state.state == HealthState.HEALTHY else 0
            available = member.id in state.available_members
            self.members_tree.insert(
                "",
                "end",
                values=(
                    member.id,
                    member.endpoint(),
                    member.protocol.upper(),
                    member_state.state.value,
                    member_state.success_count,
                    member_state.failure_count,
                    f"{percent}% ({display_index + 1}/{len(table)})",
                    member.weight,
                    state.effective_weights.get(member.id, 0),
                    "yes" if available else "no",
                ),
                tags=(member_state.state.value,),
            )
        self.round_var.set(f"轮次: {state.current_round}")
        self.available_var.set(
            "可用成员: " + (", ".join(state.available_members) if state.available_members else "∅")
        )
        weight_text = ", ".join(
            f"{member.id}={state.effective_weights.get(member.id, 0)}/{member.weight}"
            for member in self.config.members
        )
        self.weight_var.set("生效/满额权重: " + weight_text)
        self.full_sum_var.set(
            f"权重和: {sum(state.effective_weights.values())} / 满额 {state.full_weight_sum}"
        )

    def _refresh_snapshots(self) -> None:
        if self.snapshot_service is None:
            return
        items = self.snapshot_service.list()
        for item in self.snapshot_tree.get_children():
            self.snapshot_tree.delete(item)
        for snapshot in reversed(items):
            self.snapshot_tree.insert(
                "",
                "end",
                iid=str(snapshot["snapshot_id"]),
                values=(
                    snapshot["snapshot_id"],
                    snapshot["round"],
                    snapshot["label"],
                    snapshot["config_version"],
                    snapshot["member_digest"][:12],
                    snapshot["created_utc"],
                ),
            )
        self.snapshot_count_var.set(f"快照: {len(items)}")

    def _selected_member(self) -> str | None:
        selection = self.members_tree.selection()
        if not selection:
            return None
        values = self.members_tree.item(selection[0], "values")
        return str(values[0]) if values else None

    def _drain_selected(self) -> None:
        member_id = self._selected_member()
        if member_id is None or self.engine is None:
            self.tk.messagebox.showinfo("Drain", "请先在成员表中选择一个后端")
            return
        self.engine.request_drain(member_id, "UI drain request")
        self._log(f"ADMIN drain queued: member={member_id}; applies at next round boundary")
        self.status_var.set(f"{member_id} 将在下一轮完成后退出可用集")

    def _resume_selected(self) -> None:
        member_id = self._selected_member()
        if member_id is None or self.engine is None:
            self.tk.messagebox.showinfo("Resume", "请先在成员表中选择一个后端")
            return
        self.engine.request_undrain(member_id, "UI resume request")
        self._log(f"ADMIN resume queued: member={member_id}; state becomes unknown next round")
        self.status_var.set(f"{member_id} 将在下一轮恢复为 unknown")

    def _save_snapshot(self) -> None:
        if self.auto_running or self.busy:
            self.tk.messagebox.showinfo("快照", "请先停止连续探测")
            return
        if self.snapshot_service is None or self.engine is None or self.config is None:
            return
        round_no = self.engine.current_round
        if round_no <= 0:
            self.tk.messagebox.showinfo("快照", "至少完成一轮探测后才能保存快照")
            return
        try:
            snapshot = self.snapshot_service.create(
                self.config,
                self.engine,
                label=f"ui-round-{round_no}",
                window=4,
            )
        except HealthError as exc:
            self.tk.messagebox.showerror("快照失败", exc.message)
            return
        self._log(f"SNAPSHOT created: id={snapshot.snapshot_id} round={snapshot.round_no}")
        self._refresh_snapshots()

    def _restore_snapshot(self) -> None:
        if self.auto_running:
            self.tk.messagebox.showinfo("恢复快照", "请先停止连续探测")
            return
        if (
            self.snapshot_service is None
            or self.engine is None
            or self.config is None
            or self.ledger is None
        ):
            return
        selection = self.snapshot_tree.selection()
        if not selection:
            self.tk.messagebox.showinfo("恢复快照", "请先选择快照")
            return
        snapshot_id = int(selection[0])
        try:
            restore = self.snapshot_service.restore(
                snapshot_id,
                self.config,
                previous_state=self.engine.snapshot_state(),
            )
        except HealthError as exc:
            self.tk.messagebox.showerror("恢复失败", exc.message)
            return
        self.engine = HealthEngine(self.config, restore.state)
        self.controller = HealthController(
            self.config,
            self.ledger,
            self.engine,
            LoopbackProber(self.config),
        )
        self.latest_result = None
        self._log(json.dumps({"event": "snapshot_restore", **restore.to_dict()}, sort_keys=True))
        self._refresh_members()
        self._refresh_snapshots()
        self.status_var.set(
            f"快照 {snapshot_id} 已整体替换；版本匹配={restore.config_version_match}"
        )
        self.tk.messagebox.showinfo(
            "快照恢复",
            "已完成全量替换。\n"
            f"快照轮次: {restore.state.current_round}\n"
            f"替换成员: {', '.join(item['member'] for item in restore.replaced_members) or '无'}\n"
            f"重置连续计数: {', '.join(item['member'] for item in restore.reset_counters) or '无'}",
        )

    def _log_round(self, result: RoundResult) -> None:
        for probe in result.probes:
            self._log(
                f"R{probe.round_no:03d} PROBE member={probe.member_id} "
                f"protocol={probe.protocol} ok={str(probe.ok).lower()} "
                f"summary={probe.summary}"
            )
        for transition in result.transitions:
            self._log(
                f"R{transition.round_no:03d} TRANSITION member={transition.member_id} "
                f"{transition.from_state.value}->{transition.to_state.value} "
                f"reason={transition.reason}"
            )
        self._log(
            f"R{result.round_no:03d} SETTLE available={list(result.available_members)} "
            f"weights={json.dumps(result.effective_weights, sort_keys=True)} "
            f"sum={result.effective_weight_sum}/{result.full_weight_sum}"
        )

    def _log(self, message: str) -> None:
        self.event_text.configure(state="normal")
        self.event_text.insert("end", message + "\n")
        lines = int(self.event_text.index("end-1c").split(".")[0])
        if lines > 5000:
            self.event_text.delete("1.0", "501.0")
        self.event_text.see("end")
        self.event_text.configure(state="disabled")

    def _on_close(self) -> None:
        self.auto_stop.set()
        self.root.destroy()


def run_ui(config_path: str, ledger_path: str, smoke: bool = False) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as exc:
        raise HealthError(
            "E_TK",
            "tkinter is not available in this Python installation",
            {"detail": str(exc)},
        ) from exc
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise HealthError(
            "E_DISPLAY",
            "Tk could not open a display",
            {"detail": str(exc)},
        ) from exc
    root.messagebox = messagebox
    app = LBHealthApp(root, config_path, ledger_path)
    if smoke:
        def finish() -> None:
            app._on_close()

        root.after(1200, finish)
        root.mainloop()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "ui": "opened",
                    "config": config_path,
                    "ledger": ledger_path,
                },
                sort_keys=True,
            )
        )
        return 0
    root.mainloop()
    return 0
