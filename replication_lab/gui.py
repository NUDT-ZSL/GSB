"""Tkinter workbench with stable rendering order and no third-party dependencies."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:  # Tkinter is present in the target Windows installation, but import stays optional.
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover - exercised on headless Linux without Tk
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

from .errors import GuiUnavailableError, LabError, ParameterError
from .reports import render_observation_report, render_verification_report
from .service import LabSession


class ReplicationLabApp:
    def __init__(self, root: tk.Tk, lab_root: Path) -> None:
        self.root = root
        self.root.title("复制延迟观测与多版本格式互操作编辑台")
        self.root.geometry("1380x900")
        self.root.minsize(1120, 720)
        self.session = LabSession.open(lab_root)
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.refresh()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=3)
        self.root.rowconfigure(3, weight=2)

        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.grid(row=0, column=0, sticky="ew")
        for column in range(16):
            toolbar.columnconfigure(column, weight=0)
        toolbar.columnconfigure(15, weight=1)

        ttk.Label(toolbar, text="回放速率(txn/s)").grid(row=0, column=0, padx=(0, 3))
        self.rate_var = tk.StringVar(value="10")
        ttk.Spinbox(toolbar, from_=1, to=100000, width=7, textvariable=self.rate_var).grid(
            row=0, column=1, padx=(0, 9)
        )
        ttk.Label(toolbar, text="并发度").grid(row=0, column=2, padx=(0, 3))
        self.concurrency_var = tk.StringVar(value="2")
        ttk.Spinbox(
            toolbar, from_=1, to=10000, width=7, textvariable=self.concurrency_var
        ).grid(row=0, column=3, padx=(0, 9))
        ttk.Button(toolbar, text="刷新", command=self.refresh).grid(row=0, column=4, padx=2)
        ttk.Button(toolbar, text="格式升级", command=self.upgrade).grid(
            row=0, column=5, padx=2
        )
        ttk.Button(toolbar, text="冷启动回放", command=self.cold_replay).grid(
            row=0, column=6, padx=2
        )
        ttk.Button(toolbar, text="恢复状态", command=self.recover).grid(
            row=0, column=7, padx=2
        )
        ttk.Button(toolbar, text="保存快照", command=self.save_snapshot).grid(
            row=0, column=8, padx=2
        )
        ttk.Button(toolbar, text="恢复选中快照", command=self.restore_snapshot).grid(
            row=0, column=9, padx=2
        )
        ttk.Button(toolbar, text="校验选中快照", command=self.verify_snapshot).grid(
            row=0, column=10, padx=2
        )

        status_frame = ttk.Frame(self.root, padding=(8, 2))
        status_frame.grid(row=1, column=0, sticky="ew")
        self.version_var = tk.StringVar(value="版本集合: -")
        self.progress_var = tk.StringVar(value="回放进度: -")
        self.estimator_var = tk.StringVar(value="估算参数版本: -")
        ttk.Label(status_frame, textvariable=self.version_var).pack(side="left", padx=(0, 18))
        ttk.Label(status_frame, textvariable=self.progress_var).pack(side="left", padx=(0, 18))
        ttk.Label(status_frame, textvariable=self.estimator_var).pack(side="left")

        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)

        segments_frame = ttk.LabelFrame(main, text="日志段与格式版本", padding=4)
        main.add(segments_frame, weight=2)
        segments_frame.columnconfigure(0, weight=1)
        segments_frame.rowconfigure(0, weight=1)
        self.segment_tree = ttk.Treeview(
            segments_frame,
            columns=("file", "id", "version", "record_versions", "count", "digest"),
            show="headings",
            height=12,
        )
        headings = {
            "file": "段文件",
            "id": "段ID",
            "version": "段版本",
            "record_versions": "记录版本",
            "count": "记录数",
            "digest": "SHA256(前12位)",
        }
        widths = {
            "file": 155,
            "id": 65,
            "version": 65,
            "record_versions": 80,
            "count": 60,
            "digest": 115,
        }
        for name in self.segment_tree["columns"]:
            self.segment_tree.heading(name, text=headings[name])
            self.segment_tree.column(name, width=widths[name], anchor="w", stretch=False)
        segment_scroll = ttk.Scrollbar(
            segments_frame, orient="vertical", command=self.segment_tree.yview
        )
        self.segment_tree.configure(yscrollcommand=segment_scroll.set)
        self.segment_tree.grid(row=0, column=0, sticky="nsew")
        segment_scroll.grid(row=0, column=1, sticky="ns")

        samples_frame = ttk.LabelFrame(main, text="延迟样本序列", padding=4)
        main.add(samples_frame, weight=3)
        samples_frame.columnconfigure(0, weight=1)
        samples_frame.rowconfigure(0, weight=1)
        self.sample_tree = ttk.Treeview(
            samples_frame,
            columns=(
                "index",
                "seq",
                "version",
                "commit_ts",
                "completion_ts",
                "delay",
                "status",
            ),
            show="headings",
            height=12,
        )
        sample_headings = {
            "index": "#",
            "seq": "提交序号",
            "version": "记录版本",
            "commit_ts": "主库逻辑ms",
            "completion_ts": "回放完成ms",
            "delay": "延迟ms",
            "status": "状态",
        }
        sample_widths = {
            "index": 48,
            "seq": 72,
            "version": 68,
            "commit_ts": 100,
            "completion_ts": 100,
            "delay": 72,
            "status": 100,
        }
        for name in self.sample_tree["columns"]:
            self.sample_tree.heading(name, text=sample_headings[name])
            self.sample_tree.column(
                name, width=sample_widths[name], anchor="e", stretch=False
            )
        sample_scroll = ttk.Scrollbar(
            samples_frame, orient="vertical", command=self.sample_tree.yview
        )
        self.sample_tree.configure(yscrollcommand=sample_scroll.set)
        self.sample_tree.grid(row=0, column=0, sticky="nsew")
        sample_scroll.grid(row=0, column=1, sticky="ns")

        right = ttk.Frame(main)
        main.add(right, weight=2)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        stats_frame = ttk.LabelFrame(right, text="当前估算统计与误差界", padding=5)
        stats_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
        stats_frame.columnconfigure(0, weight=1)
        self.stats_text = tk.Text(stats_frame, height=12, wrap="word", state="disabled")
        self.stats_text.grid(row=0, column=0, sticky="nsew")

        snapshots_frame = ttk.LabelFrame(right, text="快照列表", padding=4)
        snapshots_frame.grid(row=1, column=0, sticky="nsew")
        snapshots_frame.columnconfigure(0, weight=1)
        snapshots_frame.rowconfigure(0, weight=1)
        self.snapshot_tree = ttk.Treeview(
            snapshots_frame,
            columns=("id", "versions", "records", "samples", "status"),
            show="headings",
            height=9,
        )
        snapshot_headings = {
            "id": "快照",
            "versions": "格式版本",
            "records": "记录",
            "samples": "样本",
            "status": "估算状态",
        }
        for name in self.snapshot_tree["columns"]:
            self.snapshot_tree.heading(name, text=snapshot_headings[name])
            self.snapshot_tree.column(name, width=90, anchor="w", stretch=True)
        snapshot_scroll = ttk.Scrollbar(
            snapshots_frame, orient="vertical", command=self.snapshot_tree.yview
        )
        self.snapshot_tree.configure(yscrollcommand=snapshot_scroll.set)
        self.snapshot_tree.grid(row=0, column=0, sticky="nsew")
        snapshot_scroll.grid(row=0, column=1, sticky="ns")

        bottom = ttk.PanedWindow(self.root, orient="horizontal")
        bottom.grid(row=3, column=0, sticky="nsew", padx=8, pady=(2, 8))
        editor_frame = ttk.LabelFrame(bottom, text="确定性写入操作序列 (JSON)", padding=4)
        bottom.add(editor_frame, weight=2)
        editor_frame.columnconfigure(0, weight=1)
        editor_frame.rowconfigure(0, weight=1)
        self.operation_text = tk.Text(editor_frame, height=10, wrap="none")
        self.operation_text.grid(row=0, column=0, sticky="nsew")
        self.operation_text.insert("1.0", self._sample_operations_json())
        ttk.Button(editor_frame, text="写入编辑器序列", command=self.write_operations).grid(
            row=1, column=0, sticky="e", pady=(4, 0)
        )

        report_frame = ttk.LabelFrame(bottom, text="观测 / 恢复 / 校验报告", padding=4)
        bottom.add(report_frame, weight=3)
        report_frame.columnconfigure(0, weight=1)
        report_frame.rowconfigure(0, weight=1)
        self.report_text = tk.Text(report_frame, height=10, wrap="word", state="disabled")
        self.report_text.grid(row=0, column=0, sticky="nsew")

    @staticmethod
    def _sample_operations_json() -> str:
        sample = Path(__file__).resolve().parent.parent / "samples" / "deterministic_operations.json"
        if sample.exists():
            try:
                data = json.loads(sample.read_text(encoding="utf-8"))
                return json.dumps(data.get("operations", data), ensure_ascii=False, indent=2)
            except (OSError, ValueError):
                pass
        return "[]"

    def _set_text(self, widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _append_report(self, value: str) -> None:
        self.report_text.configure(state="normal")
        self.report_text.delete("1.0", "end")
        self.report_text.insert("1.0", value)
        self.report_text.configure(state="disabled")

    def _params(self) -> tuple[int, int]:
        try:
            rate = int(self.rate_var.get())
            concurrency = int(self.concurrency_var.get())
        except ValueError as exc:
            raise ParameterError("速率与并发度必须为整数") from exc
        if rate <= 0 or concurrency <= 0:
            raise ParameterError("速率与并发度必须为正整数")
        return rate, concurrency

    def _show_error(self, exc: Exception) -> None:
        if isinstance(exc, LabError):
            details = exc.to_dict()
            text = f"{exc.code}\n{exc.message}"
            if details.get("segment") is not None:
                text += f"\nsegment={details['segment']} offset={details['offset']}"
            messagebox.showerror("操作失败", text)
            self._append_report(json.dumps(details, ensure_ascii=False, indent=2))
        else:
            messagebox.showerror("操作失败", str(exc))

    def _run_action(self, action: Any) -> None:
        try:
            before = self.report_text.get("1.0", "end-1c")
            action()
            after = self.report_text.get("1.0", "end-1c")
            self.refresh()
            if after != before:
                self._append_report(after)
        except Exception as exc:  # GUI boundary: all errors are surfaced explicitly
            self._show_error(exc)

    def refresh(self) -> None:
        try:
            self.session.refresh()
            report = self.session.observation_report()
        except Exception as exc:
            self._show_error(exc)
            return
        self.version_var.set(f"版本集合: {report['format_version_set']}")
        state = report["replay"]["state"]
        if state is None:
            self.progress_var.set("回放进度: 未开始")
            self.estimator_var.set(
                f"估算参数版本: {self.session.estimator_parameters.version}"
            )
        else:
            completed = sum(
                1
                for batch in report["replay"]["batches"]
                if batch["status"] == "COMPLETE"
            )
            self.progress_var.set(
                f"回放进度: seq={state['last_commit_seq']} 已完成批次={completed} "
                f"未知批次={state['unknown_batch_count']} "
                f"rate={state['replay_rate']} concurrency={state['concurrency']}"
            )
            self.estimator_var.set(
                f"估算参数版本: {state['estimator_version']} / 回放参数版本: {state['params_version']}"
            )
        self.segment_tree.delete(*self.segment_tree.get_children())
        for segment in report["segments"]:
            record_versions = ",".join(str(v) for v in segment["versions"])
            original_versions = ",".join(
                str(v) for v in segment.get("original_versions", segment["versions"])
            )
            if original_versions != record_versions:
                record_versions = f"{record_versions} (源{original_versions})"
            self.segment_tree.insert(
                "",
                "end",
                iid=f"segment-{segment['segment_id']}",
                values=(
                    segment["filename"],
                    segment["segment_id"],
                    segment["format_version"],
                    record_versions,
                    segment["record_count"],
                    segment["digest_short"],
                ),
            )
        self.sample_tree.delete(*self.sample_tree.get_children())
        for sample in report["replay"]["samples"]:
            self.sample_tree.insert(
                "",
                "end",
                iid=f"sample-{sample['sample_index']}",
                values=(
                    sample["sample_index"],
                    sample["commit_seq"],
                    sample["commit_version"],
                    sample["commit_ts_ms"] if sample["commit_ts_ms"] is not None else "未知",
                    sample["completion_ts_ms"],
                    sample["delay_ms"] if sample["delay_ms"] is not None else "未知",
                    sample["status"],
                ),
            )
        estimate = report["estimation"]
        sensitivity = estimate.get("parameter_sensitivity") or {}
        bound = estimate.get("total_estimate_error_upper_bound")
        stats_lines = [
            f"版本集合: {report['format_version_set']}",
            f"样本: total={estimate['sample_count']} known={estimate['known_delay_count']} unknown={estimate['unknown_delay_count']}",
            f"未知批次: {estimate['incomplete_unknown_batches']}",
            f"均值定点: {estimate['mean_fixed']} (Q={estimate['mean_fixed_scale']}, {estimate['mean_ms']} ms)",
            f"P95: {estimate['p95_ms']} ms (nearest-rank, rank={estimate['p95_rank']})",
            f"最大延迟: {estimate['max_delay_ms']} ms",
            f"状态/置信: {estimate['estimate_status']} / {estimate['confidence']}",
            f"原因: {estimate['status_reason']}",
            f"批时长/批数: {sensitivity.get('batch_duration_ms')} ms / {sensitivity.get('batch_count')}",
            f"调度量化上界: {estimate['schedule_error_upper_bound_ms']} ms",
            f"总误差保守上界: {bound['text'] if bound else '不可用（无回放参数）'}",
            f"未知样本策略: {estimate['unknown_policy']}",
            "误差推导:",
        ]
        stats_lines.extend(
            f"{i}. {step}" for i, step in enumerate(estimate["error_bound_steps"], 1)
        )
        self._set_text(self.stats_text, "\n".join(stats_lines))
        self.snapshot_tree.delete(*self.snapshot_tree.get_children())
        for snapshot in self.session.list_snapshots():
            self.snapshot_tree.insert(
                "",
                "end",
                iid=snapshot["snapshot_id"],
                values=(
                    snapshot["snapshot_id"],
                    ",".join(str(v) for v in snapshot["format_version_set"]),
                    snapshot["record_count"],
                    snapshot["sample_count"],
                    snapshot["estimate_status"],
                ),
            )
        self._append_report(render_observation_report(report))

    def write_operations(self) -> None:
        def action() -> None:
            raw = self.operation_text.get("1.0", "end").strip()
            try:
                value = json.loads(raw)
            except ValueError as exc:
                raise ParameterError(f"操作序列 JSON 无效: {exc}") from exc
            operations = value.get("operations", value) if isinstance(value, dict) else value
            if not isinstance(operations, list):
                raise ParameterError("操作序列必须是 JSON 数组")
            result = self.session.write_operations(operations)
            self._append_report(json.dumps(result, ensure_ascii=False, indent=2))

        self._run_action(action)

    def cold_replay(self) -> None:
        def action() -> None:
            rate, concurrency = self._params()
            self.session.replay(
                replay_rate=rate, concurrency=concurrency, reset=True
            )
            self._append_report(render_observation_report(self.session.observation_report()))

        self._run_action(action)

    def recover(self) -> None:
        def action() -> None:
            result = self.session.recover()
            self._append_report(json.dumps(result, ensure_ascii=False, indent=2))

        self._run_action(action)

    def upgrade(self) -> None:
        def action() -> None:
            result = self.session.upgrade_all()
            self._append_report(json.dumps(result, ensure_ascii=False, indent=2))

        self._run_action(action)

    def save_snapshot(self) -> None:
        def action() -> None:
            result = self.session.save_snapshot()
            self._append_report(json.dumps(result, ensure_ascii=False, indent=2))

        self._run_action(action)

    def _selected_snapshot(self) -> str:
        selection = self.snapshot_tree.selection()
        if not selection:
            raise ParameterError("请先在快照列表中选择一项")
        return selection[0]

    def restore_snapshot(self) -> None:
        def action() -> None:
            snapshot_id = self._selected_snapshot()
            if not messagebox.askyesno(
                "整体替换确认",
                f"恢复 {snapshot_id} 将整体替换日志视图、回放进度与样本状态。继续？",
            ):
                return
            result = self.session.restore_snapshot(snapshot_id)
            self._append_report(json.dumps(result, ensure_ascii=False, indent=2))

        self._run_action(action)

    def verify_snapshot(self) -> None:
        def action() -> None:
            snapshot_id = self._selected_snapshot()
            result = self.session.verify_snapshot(snapshot_id)
            self._append_report(render_verification_report(result))
            if not result.get("ok"):
                messagebox.showerror("E_REPLAY", "\n".join(result["mismatch_items"]))

        self._run_action(action)

    def _close(self) -> None:
        self.session.close()
        self.root.destroy()


def run_gui(lab_root: str | Path) -> None:
    if tk is None:
        raise GuiUnavailableError(
            "Tkinter is not installed in this Python; on Windows use the official Python installer"
        )
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise GuiUnavailableError(
            "Tkinter display is unavailable; run the CLI/demo or launch on Windows desktop"
        ) from exc
    ReplicationLabApp(root, Path(lab_root))
    root.mainloop()


def main() -> int:
    try:
        run_gui(Path.cwd() / "lab-data")
    except LabError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
