"""Tkinter dashboard; all heavyweight work stays in the verification command."""

from __future__ import annotations

import json
from typing import Any

from .config import load_declaration
from .dashboard import Dashboard
from .errors import E_SERVICE, BusinessError
from .ledger import Ledger
from .logging_utils import EventLogger
from .snapshot import save_snapshot
from .verifier import AuthorityVerifier


class DashboardApp:
    def __init__(self, *, declaration, sample_data, ledger: Ledger, logger: EventLogger,
                 snapshot_path: str, query_budget_ms: int | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.declaration = declaration
        self.sample_data = sample_data
        self.ledger = ledger
        self.logger = logger
        self.snapshot_path = snapshot_path
        self.dashboard = Dashboard(
            declaration, ledger, logger, query_budget_ms=query_budget_ms
        )
        self.verifier = AuthorityVerifier(declaration, sample_data, ledger, logger)
        self.root = tk.Tk()
        self.root.title("多集群配置分发账本诊断台")
        self.root.geometry("1240x820")
        self.root.minsize(960, 640)
        self._build()
        self.refresh()
        self.root.after(1000, self._auto_refresh)

    def _build(self) -> None:
        tk = self.tk
        ttk = self.ttk
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill="both", expand=True)
        self.header = ttk.Label(frame, text="", font=("TkDefaultFont", 11, "bold"))
        self.header.pack(fill="x", pady=(0, 6))
        self.notice = ttk.Label(frame, text="", foreground="darkgreen")
        self.notice.pack(fill="x", pady=(0, 6))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(0, 8))
        ttk.Button(buttons, text="快速刷新", command=self.refresh).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="权威重算", command=self.verify).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="保存快照", command=self.save).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="退出", command=self.root.destroy).pack(side="right")

        tasks_label = ttk.Label(frame, text="分发任务进度")
        tasks_label.pack(anchor="w")
        self.tasks_tree = ttk.Treeview(
            frame, columns=("task", "progress", "complete", "unknown", "mode"),
            show="headings", height=5,
        )
        for column, heading, width in (
            ("task", "任务", 220), ("progress", "进度", 120),
            ("complete", "完成集群", 110), ("unknown", "未知/需重跑", 130),
            ("mode", "聚合口径", 110),
        ):
            self.tasks_tree.heading(column, text=heading)
            self.tasks_tree.column(column, width=width, anchor="w")
        self.tasks_tree.pack(fill="x", pady=(2, 8))

        clusters_label = ttk.Label(frame, text="集群配置版本与结果摘要")
        clusters_label.pack(anchor="w")
        self.clusters_tree = ttk.Treeview(
            frame, columns=("cluster", "version", "port", "results"),
            show="headings", height=4,
        )
        for column, heading, width in (
            ("cluster", "集群", 160), ("version", "配置版本", 100),
            ("port", "回环端口", 100), ("results", "当前结果摘要", 760),
        ):
            self.clusters_tree.heading(column, text=heading)
            self.clusters_tree.column(column, width=width, anchor="w")
        self.clusters_tree.pack(fill="x", pady=(2, 8))

        results_label = ttk.Label(frame, text="账本当前结果（快速缓存视图）")
        results_label.pack(anchor="w")
        results_frame = ttk.Frame(frame)
        results_frame.pack(fill="both", expand=True, pady=(2, 8))
        self.results_tree = ttk.Treeview(
            results_frame,
            columns=("task", "cluster", "version", "summary", "duration"),
            show="headings", height=9,
        )
        for column, heading, width in (
            ("task", "任务", 170), ("cluster", "集群", 120),
            ("version", "版本", 70), ("summary", "聚合摘要", 620),
            ("duration", "服务计算 ms", 110),
        ):
            self.results_tree.heading(column, text=heading)
            self.results_tree.column(column, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(results_frame, orient="vertical", command=self.results_tree.yview)
        self.results_tree.configure(yscrollcommand=scrollbar.set)
        self.results_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.detail = tk.Text(frame, height=8, wrap="word")
        self.detail.pack(fill="x", pady=(4, 0))

    @staticmethod
    def _clear(tree) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _apply_view(self, view: dict[str, Any]) -> None:
        latency = view["query_latency"]
        hit = "缓存命中" if view["cache_hit"] else "全量重建"
        self.header.configure(
            text=(
                f"声明 {view['declaration_version']}  |  数据版本 {view['data_version']}  |  "
                f"账本修订 {view['ledger_revision']}  |  查询预算 {latency['budget_ms']} ms"
            )
        )
        notice = (
            f"查询 {latency['elapsed_fixed'] / 1000:.3f} ms（定点整数 {latency['elapsed_fixed']} / 1000 ms）"
            f" · {hit} · 陈旧结果 {view['stale_result_count']} 条"
        )
        if view["latency_budget_exceeded"]:
            notice += " · E_LATENCY 超预算事件已记录"
            self.notice.configure(text=notice, foreground="firebrick")
        else:
            self.notice.configure(text=notice, foreground="darkgreen")

        self._clear(self.tasks_tree)
        for task in view["tasks"]:
            self.tasks_tree.insert(
                "", "end",
                values=(
                    task["task_id"], f"{task['progress_fixed'] / 10:.1f}%",
                    f"{task['completed_units']}/{task['total_units']}",
                    task["unknown_units"], task["aggregation_mode"],
                ),
            )

        self._clear(self.clusters_tree)
        for cluster in view["clusters"]:
            summaries = " | ".join(
                f"{row['task_id']}={row['summary']}" for row in cluster["results"]
            ) or "（等待新版本结果）"
            self.clusters_tree.insert(
                "", "end",
                values=(cluster["cluster_id"], cluster["config_version"], cluster["port"], summaries),
            )

        self._clear(self.results_tree)
        for result in view["results"]:
            self.results_tree.insert(
                "", "end",
                values=(
                    result["task_id"], result["cluster_id"], result["config_version"],
                    result["summary"], result["calculation_duration_ms"],
                ),
            )
        changes = view.get("changed_clusters", [])
        if changes:
            text = "版本替换：" + "；".join(
                f"{item['cluster_id']} v{item['old_config_version']}→v{item['new_config_version']}"
                for item in changes
            )
        else:
            text = "版本替换：无"
        self.detail.delete("1.0", "end")
        self.detail.insert("end", text + "\n")
        self.detail.insert(
            "end",
            "时钟基准：客户端与服务端均使用各自进程的 time.perf_counter_ns 单调时钟；"
            "真值为同配置版本的本地确定性重算。\n",
        )

    def refresh(self) -> None:
        try:
            self._apply_view(self.dashboard.query_view())
        except BusinessError as exc:
            self.notice.configure(text=f"{exc.code}: {exc.message}", foreground="firebrick")
            self.logger.error(exc, event="ui_refresh_failed")

    def _auto_refresh(self) -> None:
        self.refresh()
        self.root.after(1000, self._auto_refresh)

    def verify(self) -> None:
        try:
            view = self.dashboard.query_view()
            self._apply_view(view)
            report = self.verifier.verify(view, dashboard=self.dashboard)
            if report["ok"]:
                summary = (
                    f"权威重算通过：账本 {report['checked_ledger_results']} 条，"
                    f"界面 {report['checked_view_results']} 条。"
                )
                self.notice.configure(text=summary, foreground="darkgreen")
            else:
                summary = json.dumps(report["errors"][:10], ensure_ascii=False, indent=2)
                self.notice.configure(text="权威重算失败，详见日志和详情。", foreground="firebrick")
            self.detail.delete("1.0", "end")
            self.detail.insert("end", summary + "\n")
        except BusinessError as exc:
            self.logger.error(exc, event="ui_verify_failed")
            self.notice.configure(text=f"{exc.code}: {exc.message}", foreground="firebrick")

    def save(self) -> None:
        try:
            snap = save_snapshot(self.snapshot_path, self.declaration, self.ledger, self.logger)
            message = (
                f"快照已保存：{self.snapshot_path}；声明 {snap['declaration_version']}；"
                f"已完成集群 {', '.join(snap['completed_clusters']) or '无'}；"
                f"未完成 {len(snap['incomplete_units'])} 项需重跑。"
            )
            self.notice.configure(text=message, foreground="darkgreen")
        except BusinessError as exc:
            self.logger.error(exc, event="ui_snapshot_failed")
            self.notice.configure(text=f"{exc.code}: {exc.message}", foreground="firebrick")

    def run(self) -> None:
        self.root.mainloop()


def run_ui(*, config_path: str, sample_path: str, ledger_path: str,
           logger: EventLogger, snapshot_path: str,
           query_budget_ms: int | None = None) -> None:
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        raise BusinessError(
            E_SERVICE,
            "tkinter is unavailable; use the CLI query/verify commands on this host",
            details={"python": "install the Windows official Python distribution with Tcl/Tk"},
        ) from exc
    from .config import load_sample_data

    declaration = load_declaration(config_path)
    sample_data = load_sample_data(sample_path)
    ledger = Ledger(ledger_path)
    try:
        ledger.reconcile(declaration)
        app = DashboardApp(
            declaration=declaration,
            sample_data=sample_data,
            ledger=ledger,
            logger=logger,
            snapshot_path=snapshot_path,
            query_budget_ms=query_budget_ms,
        )
        app.run()
    finally:
        ledger.close()
