"""Tkinter decision workbench with deterministic rendering."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .workbench import Workbench


def tkinter_availability() -> tuple[bool, str]:
    try:
        import tkinter  # noqa: F401

        return True, "tkinter available"
    except Exception as exc:  # pragma: no cover - platform dependent
        return False, f"tkinter unavailable: {type(exc).__name__}: {exc}"


def _short_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    )


class WorkbenchWindow:
    def __init__(self, root: Any, workbench: Workbench, declaration_path: Path) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.workbench = workbench
        self.declaration_path = declaration_path
        self.root.title("本地模型服务弹性伸缩决策工作台")
        self.root.geometry("1280x820")
        self.root.minsize(1024, 720)

        self.path_var = tk.StringVar(value=str(declaration_path))
        self.cluster_var = tk.StringVar(value="未加载")
        self.version_var = tk.StringVar(value="声明版本: -")
        self.run_var = tk.StringVar(value="当前运行: -")
        self.budget_var = tk.StringVar(value="全局副本预算: -")
        self.fingerprint_var = tk.StringVar(value="指纹: -")

        self._build()
        self._load_declaration(show_error=False)

    def _build(self) -> None:
        tk = self.tk
        ttk = self.ttk

        controls = ttk.LabelFrame(self.root, text="声明与运行控制")
        controls.pack(fill=tk.X, padx=10, pady=(10, 6))
        ttk.Label(controls, text="声明 JSON").grid(row=0, column=0, sticky=tk.W, padx=6, pady=6)
        ttk.Entry(controls, textvariable=self.path_var, width=70).grid(
            row=0, column=1, sticky=tk.EW, padx=4, pady=6
        )
        ttk.Button(controls, text="加载声明", command=self._load_declaration).grid(
            row=0, column=2, padx=4, pady=6
        )
        ttk.Button(controls, text="全量推进", command=self._run_all).grid(
            row=0, column=3, padx=4, pady=6
        )
        ttk.Button(controls, text="推进 1 分钟", command=self._advance_one).grid(
            row=0, column=4, padx=4, pady=6
        )
        ttk.Button(controls, text="恢复", command=self._recover).grid(
            row=0, column=5, padx=4, pady=6
        )
        ttk.Button(controls, text="恢复并续跑", command=self._resume).grid(
            row=0, column=6, padx=4, pady=6
        )
        ttk.Button(controls, text="重放校验", command=self._replay).grid(
            row=0, column=7, padx=4, pady=6
        )
        controls.columnconfigure(1, weight=1)

        snapshot_controls = ttk.Frame(controls)
        snapshot_controls.grid(row=1, column=0, columnspan=8, sticky=tk.W, padx=6, pady=(0, 6))
        ttk.Button(
            snapshot_controls,
            text="保存当前整轮快照",
            command=self._save_snapshot,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            snapshot_controls,
            text="恢复选中快照",
            command=self._restore_snapshot,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            snapshot_controls,
            text="刷新视图",
            command=self._refresh,
        ).pack(side=tk.LEFT, padx=6)

        status = ttk.Frame(self.root)
        status.pack(fill=tk.X, padx=10, pady=2)
        ttk.Label(status, textvariable=self.cluster_var, font=("Segoe UI", 10, "bold")).pack(
            side=tk.LEFT, padx=(2, 16)
        )
        ttk.Label(status, textvariable=self.version_var).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(status, textvariable=self.run_var).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(status, textvariable=self.budget_var).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(status, textvariable=self.fingerprint_var).pack(side=tk.LEFT)

        body = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=4)
        body.add(right, weight=2)

        table_frame = ttk.LabelFrame(left, text="逐分钟决策、副本占用与拒绝量")
        table_frame.pack(fill=tk.BOTH, expand=True)
        columns = (
            "minute",
            "service",
            "requests",
            "unclamped",
            "demand",
            "final",
            "capacity",
            "accepted",
            "rejected",
            "reason",
        )
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=20,
        )
        headings = {
            "minute": "分钟",
            "service": "服务",
            "requests": "请求",
            "unclamped": "未夹取需求",
            "demand": "需求副本",
            "final": "最终副本",
            "capacity": "可承载",
            "accepted": "接受",
            "rejected": "拒绝",
            "reason": "裁剪原因",
        }
        widths = {
            "minute": 52,
            "service": 82,
            "requests": 64,
            "unclamped": 90,
            "demand": 74,
            "final": 74,
            "capacity": 74,
            "accepted": 64,
            "rejected": 64,
            "reason": 300,
        }
        for name in columns:
            self.tree.heading(name, text=headings[name])
            self.tree.column(name, width=widths[name], anchor=tk.CENTER, stretch=name == "reason")
        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky=tk.NSEW)
        y_scroll.grid(row=0, column=1, sticky=tk.NS)
        x_scroll.grid(row=1, column=0, sticky=tk.EW)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.tag_configure("clipped", background="#fff4e5")
        self.tree.tag_configure("rejected", background="#fde8e8")

        chart_frame = ttk.LabelFrame(left, text="全局副本预算占用曲线")
        chart_frame.pack(fill=tk.X, pady=(6, 0))
        self.chart = tk.Canvas(chart_frame, height=185, background="#ffffff", highlightthickness=0)
        self.chart.pack(fill=tk.X, padx=8, pady=8)
        self.chart.bind("<Configure>", lambda _event: self._draw_chart())

        snapshot_frame = ttk.LabelFrame(right, text="快照列表")
        snapshot_frame.pack(fill=tk.BOTH, expand=False)
        self.snapshot_list = tk.Listbox(
            snapshot_frame,
            height=7,
            exportselection=False,
            font=("Consolas", 9),
        )
        self.snapshot_list.pack(fill=tk.X, padx=8, pady=8)

        output_frame = ttk.LabelFrame(right, text="版本差异 / 恢复 / 重放报告")
        output_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.output = tk.Text(
            output_frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            background="#111827",
            foreground="#e5e7eb",
            insertbackground="#e5e7eb",
        )
        output_scroll = ttk.Scrollbar(output_frame, orient=tk.VERTICAL, command=self.output.yview)
        self.output.configure(yscrollcommand=output_scroll.set)
        self.output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=8)
        output_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=8)

    def _show_error(self, error: Exception) -> None:
        if isinstance(error, BusinessError):
            self._write_output(error.to_dict())
        else:
            self._write_output(
                {
                    "ok": False,
                    "code": "E_UI",
                    "message": f"{type(error).__name__}: {error}",
                }
            )

    def _write_output(self, value: Any) -> None:
        self.output.delete("1.0", "end")
        self.output.insert("1.0", _short_json(value))

    def _load_declaration(self, show_error: bool = True) -> None:
        try:
            registration = self.workbench.load_declaration(self.path_var.get())
            self._write_output(registration.to_dict())
            if self.workbench.declaration is not None:
                self.cluster_var.set(
                    f"集群: {self.workbench.declaration.cluster_name}"
                )
                self.budget_var.set(
                    f"全局副本预算: {self.workbench.declaration.global_replica_budget}"
                )
                self.fingerprint_var.set(
                    f"指纹: {self.workbench.declaration.fingerprint[:16]}..."
                )
            self._refresh()
        except Exception as exc:
            if show_error:
                self._show_error(exc)
            else:
                raise

    def _require_current_run(self) -> dict[str, Any] | None:
        run = self.workbench.ledger.get_current_run()
        if run is None:
            self._write_output(
                {
                    "ok": False,
                    "code": "E_STATE",
                    "message": "当前没有可推进的运行，请先执行全量推进或推进 1 分钟以创建新运行。",
                }
            )
        return run

    def _run_all(self) -> None:
        try:
            result = self.workbench.run_all()
            self._write_output(
                {
                    key: value
                    for key, value in result.items()
                    if key != "view"
                }
            )
            self._refresh()
        except Exception as exc:
            self._show_error(exc)

    def _advance_one(self) -> None:
        try:
            run = self.workbench.ledger.get_current_run()
            if run is None or run["status"] == "completed":
                run_id = self.workbench.start_run()
            else:
                run_id = run["run_id"]
            result = self.workbench.advance(run_id, limit_minutes=1)
            self._write_output(result)
            self._refresh()
        except Exception as exc:
            self._show_error(exc)

    def _recover(self) -> None:
        try:
            run = self._require_current_run()
            if run is None:
                return
            result = self.workbench.recover(run["run_id"])
            self._write_output(result)
            self._refresh()
        except Exception as exc:
            self._show_error(exc)

    def _resume(self) -> None:
        try:
            run = self._require_current_run()
            if run is None:
                return
            result = self.workbench.resume(run["run_id"])
            self._write_output(result)
            self._refresh()
        except Exception as exc:
            self._show_error(exc)

    def _replay(self) -> None:
        try:
            run = self._require_current_run()
            if run is None:
                return
            result = self.workbench.replay(run["run_id"])
            self._write_output(result)
        except Exception as exc:
            self._show_error(exc)

    def _save_snapshot(self) -> None:
        try:
            run = self._require_current_run()
            if run is None:
                return
            result = self.workbench.save_snapshot(run["run_id"])
            self._write_output(result)
            self._refresh()
        except Exception as exc:
            self._show_error(exc)

    def _restore_snapshot(self) -> None:
        try:
            selection = self.snapshot_list.curselection()
            if not selection:
                self._write_output(
                    {
                        "ok": False,
                        "code": "E_STATE",
                        "message": "请先在快照列表中选择一个快照。",
                    }
                )
                return
            labels = list(self.snapshot_list.get(0, "end"))
            selected = labels[selection[0]]
            snapshot_id = selected.split(" | ", 1)[0]
            result = self.workbench.restore_snapshot(snapshot_id)
            self._write_output(result)
            self._refresh()
        except Exception as exc:
            self._show_error(exc)

    def _refresh(self) -> None:
        view = self.workbench.get_current_view()
        minutes = sorted(view.get("minutes", []), key=lambda item: item["minute"])
        self.tree.delete(*self.tree.get_children())
        for minute in minutes:
            for service in sorted(minute.get("services", []), key=lambda item: item["service"]):
                reasons = ", ".join(
                    f"{reason['dimension']}:{reason['before_replicas']}→{reason['after_replicas']}"
                    for reason in service.get("clip_reasons", [])
                ) or "-"
                tags: list[str] = []
                if service.get("clip_reasons"):
                    tags.append("clipped")
                if int(service.get("rejected_requests", 0)) > 0:
                    tags.append("rejected")
                self.tree.insert(
                    "",
                    "end",
                    values=(
                        minute["minute"],
                        service["service"],
                        service["request_count"],
                        service["unclamped_demand"],
                        service["demand_replicas"],
                        service["final_replicas"],
                        service["capacity_requests"],
                        service["accepted_requests"],
                        service["rejected_requests"],
                        reasons,
                    ),
                    tags=tuple(tags),
                )

        run = view.get("run")
        if run is None:
            self.run_var.set("当前运行: 无（声明变更后旧结论已失效）")
        else:
            self.run_var.set(
                f"当前运行: {run['run_id']} | {run['completed_minutes']}/{run['total_minutes']} | {run['status']}"
            )
            if run["declaration_version"] is not None:
                self.version_var.set(
                    f"声明版本: {run['declaration_version']} | 当前视图"
                )
        if run is None and self.workbench.declaration is not None:
            self.version_var.set(
                f"声明版本: {self.workbench.declaration.declaration_version} | 待推进"
            )

        snapshots = sorted(view.get("snapshots", []), key=lambda item: item["snapshot_id"])
        self.snapshot_list.delete(0, "end")
        for snapshot in snapshots:
            self.snapshot_list.insert(
                "end",
                f"{snapshot['snapshot_id']} | v{snapshot['declaration_version']} | "
                f"{snapshot['completed_minutes']} 分钟",
            )
        self._draw_chart()

    def _draw_chart(self) -> None:
        self.chart.delete("all")
        view = self.workbench.get_current_view()
        minutes = sorted(view.get("minutes", []), key=lambda item: item["minute"])
        width = max(200, int(self.chart.winfo_width()))
        height = max(120, int(self.chart.winfo_height()))
        margin_left = 46
        margin_right = 20
        margin_top = 18
        margin_bottom = 50
        plot_w = max(1, width - margin_left - margin_right)
        plot_h = max(1, height - margin_top - margin_bottom)
        budget = int(minutes[0]["budget_limit"]) if minutes else 1
        if budget <= 0:
            budget = 1
        self.chart.create_text(
            margin_left,
            8,
            anchor="w",
            text=f"预算上限 {budget}",
            fill="#374151",
            font=("Segoe UI", 9, "bold"),
        )
        self.chart.create_line(
            margin_left,
            margin_top,
            margin_left,
            margin_top + plot_h,
            fill="#9ca3af",
        )
        self.chart.create_line(
            margin_left,
            margin_top + plot_h,
            margin_left + plot_w,
            margin_top + plot_h,
            fill="#9ca3af",
        )
        for tick in range(0, budget + 1, max(1, budget // 5)):
            y = margin_top + plot_h - (tick / budget) * plot_h
            self.chart.create_line(margin_left - 4, y, margin_left, y, fill="#9ca3af")
            self.chart.create_text(
                margin_left - 8,
                y,
                anchor="e",
                text=str(tick),
                fill="#4b5563",
                font=("Segoe UI", 8),
            )
        if not minutes:
            self.chart.create_text(
                width / 2,
                height / 2,
                text="尚无当前版本的决策数据",
                fill="#6b7280",
                font=("Segoe UI", 10),
            )
            return
        count = len(minutes)
        slot = plot_w / max(1, count)
        bar_width = max(8, min(44, slot * 0.55))
        for index, minute in enumerate(minutes):
            x = margin_left + slot * (index + 0.5)
            used = int(minute["budget_used"])
            bar_h = (used / budget) * plot_h
            y0 = margin_top + plot_h - bar_h
            y1 = margin_top + plot_h
            color = "#2563eb" if used < budget else "#dc2626"
            self.chart.create_rectangle(
                x - bar_width / 2,
                y0,
                x + bar_width / 2,
                y1,
                fill=color,
                outline="",
            )
            self.chart.create_text(
                x,
                y0 - 6,
                text=str(used),
                fill="#111827",
                font=("Segoe UI", 8, "bold"),
            )
            self.chart.create_text(
                x,
                margin_top + plot_h + 10,
                text=f"m{minute['minute']}",
                fill="#4b5563",
                font=("Segoe UI", 8),
            )
            rejected = int(minute["rejected_total"])
            if rejected:
                self.chart.create_text(
                    x,
                    margin_top + plot_h + 26,
                    text=f"拒绝{rejected}",
                    fill="#b91c1c",
                    font=("Segoe UI", 8),
                )


def launch(
    declaration_path: str | Path,
    database_path: str | Path,
    snapshot_dir: str | Path | None = None,
) -> int:
    available, message = tkinter_availability()
    if not available:
        print(f"E_ENV {message}")
        print("Windows 官方 Python 通常自带 tkinter；请使用带 Tcl/Tk 的 Python 3.11+。")
        return 2
    import tkinter as tk

    workbench = Workbench(declaration_path, database_path, snapshot_dir)
    root = tk.Tk()
    try:
        WorkbenchWindow(root, workbench, Path(declaration_path))
        root.mainloop()
    finally:
        workbench.close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tkinter ML scaling decision workbench")
    parser.add_argument("--declaration", default="samples/cluster.json")
    parser.add_argument("--ledger", default="artifacts/workbench/ledger.sqlite")
    parser.add_argument("--snapshots", default="artifacts/workbench/snapshots")
    parser.add_argument("--check", action="store_true", help="only check Tkinter availability")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check:
        available, message = tkinter_availability()
        print(message)
        return 0 if available else 2
    return launch(args.declaration, args.ledger, args.snapshots)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
