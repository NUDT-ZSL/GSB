"""Tkinter desktop workbench with latest-version-wins refresh semantics."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import load_catalog
from .cost import CostParams
from .errors import JoinLabError, SchemaError, StaleVersionError
from .queries import load_query_file
from .session import Evaluation, ExperimentSession, SessionVersions
from .snapshot import restore_snapshot, save_snapshot
from .typesys import json_dumps_stable


@dataclass(frozen=True)
class RefreshTicket:
    serial: int
    versions: SessionVersions


class RefreshGate:
    """Only the newest request at the newest session version is publishable."""

    def __init__(self) -> None:
        self._serial = 0

    @property
    def serial(self) -> int:
        return self._serial

    def issue(self, versions: SessionVersions) -> RefreshTicket:
        self._serial += 1
        return RefreshTicket(self._serial, versions)

    def accepts(self, ticket: RefreshTicket, session: ExperimentSession) -> bool:
        return ticket.serial == self._serial and ticket.versions == session.versions


def parse_float(value: str, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{name} must be a number", f"gui.{name}") from exc


def parse_order_bias(text: str) -> dict[str, float]:
    result: dict[str, float] = {}
    if not text.strip():
        return result
    for part in text.split(","):
        if "=" not in part:
            raise SchemaError("join order bias must use table=value entries", "gui.join_order_bias")
        table, raw = part.split("=", 1)
        table = table.strip().lower()
        if not table:
            raise SchemaError("join order bias table name must not be empty", "gui.join_order_bias")
        if table in result:
            raise SchemaError(f"duplicate join order bias table {table!r}", "gui.join_order_bias")
        try:
            result[table] = float(raw.strip())
        except ValueError as exc:
            raise SchemaError(f"bias for {table!r} must be numeric", "gui.join_order_bias") from exc
    return result


def launch(
    catalog_path: str | Path,
    queries_path: str | Path | None = None,
    *,
    snapshot_path: str | Path | None = None,
) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    root_dir = Path(__file__).resolve().parents[2]
    catalog = load_catalog(catalog_path)
    query_path = Path(queries_path).resolve() if queries_path else root_dir / "samples" / "queries.json"
    named_queries = load_query_file(query_path, catalog)
    session = ExperimentSession(catalog, params=CostParams(), sample_scale=1.0)
    if snapshot_path:
        session = restore_snapshot(snapshot_path, current_session=session).session

    root = tk.Tk()
    root.title("JoinLab - 多表连接顺序与代价模型实验台")
    root.geometry("1320x900")
    root.minsize(1080, 720)

    outer = ttk.Frame(root, padding=10)
    outer.pack(fill=tk.BOTH, expand=True)
    top = ttk.Frame(outer)
    top.pack(fill=tk.X)
    ttk.Label(top, text="查询样本").pack(side=tk.LEFT)
    query_var = tk.StringVar(value=named_queries[0].name if named_queries else "")
    query_combo = ttk.Combobox(top, textvariable=query_var, values=[item.name for item in named_queries], width=28, state="readonly")
    query_combo.pack(side=tk.LEFT, padx=(8, 16))
    version_var = tk.StringVar()
    ttk.Label(top, textvariable=version_var).pack(side=tk.LEFT)
    state_var = tk.StringVar(value="CURRENT")
    state_label = ttk.Label(top, textvariable=state_var)
    state_label.pack(side=tk.RIGHT)

    sql_frame = ttk.LabelFrame(outer, text="SQL（仅支持最多三表的 INNER/LEFT 连接，AND 条件）", padding=6)
    sql_frame.pack(fill=tk.BOTH, expand=False, pady=(10, 8))
    sql_text = ScrolledText(sql_frame, height=7, wrap=tk.WORD)
    sql_text.pack(fill=tk.BOTH, expand=True)
    sql_text.insert("1.0", named_queries[0].sql if named_queries else "")

    params_frame = ttk.LabelFrame(outer, text="代价参数 / 连接顺序偏置 / 样本规模", padding=6)
    params_frame.pack(fill=tk.X, pady=(0, 8))
    defaults = session.params
    field_specs = [
        ("scan_row_cost", str(defaults.scan_row_cost), 9),
        ("filter_row_cost", str(defaults.filter_row_cost), 9),
        ("hash_build_row_cost", str(defaults.hash_build_row_cost), 9),
        ("hash_probe_row_cost", str(defaults.hash_probe_row_cost), 9),
        ("nested_loop_pair_cost", str(defaults.nested_loop_pair_cost), 9),
        ("output_row_cost", str(defaults.output_row_cost), 9),
        ("skew_factor", str(defaults.skew_factor), 8),
        ("cost_quantum", str(defaults.cost_quantum), 8),
        ("sample_scale", str(session.sample_scale), 8),
        ("join_order_bias", ",".join(f"{key}={value}" for key, value in sorted(defaults.join_order_bias)), 24),
    ]
    param_vars: dict[str, tk.StringVar] = {}
    for index, (name, value, width) in enumerate(field_specs):
        row = index // 5
        column = (index % 5) * 2
        ttk.Label(params_frame, text=name).grid(row=row, column=column, sticky=tk.W, padx=(0, 4), pady=3)
        var = tk.StringVar(value=value)
        param_vars[name] = var
        entry = ttk.Entry(params_frame, textvariable=var, width=width)
        entry.grid(row=row, column=column + 1, sticky=tk.W, padx=(0, 14), pady=3)

    button_frame = ttk.Frame(outer)
    button_frame.pack(fill=tk.X, pady=(0, 8))
    refresh_button = ttk.Button(button_frame, text="刷新估算与执行", command=lambda: request_refresh(immediate=True))
    refresh_button.pack(side=tk.LEFT)
    verify_button = ttk.Button(button_frame, text="查看跨路径校验", command=show_consistency)
    verify_button.pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(button_frame, text="保存快照", command=save_current_snapshot).pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(button_frame, text="整体恢复快照", command=load_snapshot).pack(side=tk.LEFT, padx=(8, 0))

    panes = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
    panes.pack(fill=tk.BOTH, expand=True)
    plan_frame = ttk.LabelFrame(panes, text="候选计划（按定点代价 + 稳定签名排序）", padding=4)
    result_frame = ttk.LabelFrame(panes, text="结果与逐算子对照", padding=4)
    panes.add(plan_frame, weight=3)
    panes.add(result_frame, weight=4)

    plan_columns = ("selected", "cost", "quantized", "order", "operators", "plan_id")
    plan_tree = ttk.Treeview(plan_frame, columns=plan_columns, show="headings", height=16)
    for column, label, width in [
        ("selected", "选中", 55),
        ("cost", "精确代价", 90),
        ("quantized", "定点代价", 90),
        ("order", "连接顺序", 230),
        ("operators", "算子", 150),
        ("plan_id", "计划 ID", 180),
    ]:
        plan_tree.heading(column, text=label)
        plan_tree.column(column, width=width, anchor=tk.W)
    plan_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
    plan_scroll = ttk.Scrollbar(plan_frame, orient=tk.VERTICAL, command=plan_tree.yview)
    plan_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    plan_tree.configure(yscrollcommand=plan_scroll.set)

    result_text = ScrolledText(result_frame, wrap=tk.WORD)
    result_text.pack(fill=tk.BOTH, expand=True)

    status_var = tk.StringVar(value="就绪")
    status = ttk.Label(outer, textvariable=status_var, relief=tk.SUNKEN, anchor=tk.W)
    status.pack(fill=tk.X, pady=(8, 0))

    message_queue: queue.Queue[tuple[RefreshTicket, Evaluation | JoinLabError]] = queue.Queue()
    gate = RefreshGate()
    display_versions: SessionVersions | None = None
    latest_evaluation: Evaluation | None = None
    after_id: str | None = None
    busy_ticket: RefreshTicket | None = None

    def current_query() -> str:
        sql = sql_text.get("1.0", tk.END).strip()
        if not sql:
            raise SchemaError("SQL must not be empty", "gui.sql")
        return sql

    def mark_display_stale() -> None:
        nonlocal display_versions, latest_evaluation
        if display_versions is not None and display_versions != session.versions:
            state_var.set(
                "STALE | displayed "
                f"pv={display_versions.param_version},dv={display_versions.data_version} "
                f"| current pv={session.versions.param_version},dv={session.versions.data_version}"
            )
            state_label.configure(foreground="#b00020")
            # Stale plans are never passed to selection or verification.
            latest_evaluation = None
        else:
            state_var.set("CURRENT")
            state_label.configure(foreground="#1b5e20")

    def update_version_label() -> None:
        versions = session.versions
        version_var.set(
            f"schema={versions.schema_version}  data={versions.data_version}  params={versions.param_version}  "
            f"scale={session.sample_scale:g}"
        )

    def apply_parameters() -> None:
        patch = {
            "scan_row_cost": parse_float(param_vars["scan_row_cost"].get(), "scan_row_cost"),
            "filter_row_cost": parse_float(param_vars["filter_row_cost"].get(), "filter_row_cost"),
            "hash_build_row_cost": parse_float(param_vars["hash_build_row_cost"].get(), "hash_build_row_cost"),
            "hash_probe_row_cost": parse_float(param_vars["hash_probe_row_cost"].get(), "hash_probe_row_cost"),
            "nested_loop_pair_cost": parse_float(param_vars["nested_loop_pair_cost"].get(), "nested_loop_pair_cost"),
            "output_row_cost": parse_float(param_vars["output_row_cost"].get(), "output_row_cost"),
            "skew_factor": parse_float(param_vars["skew_factor"].get(), "skew_factor"),
            "cost_quantum": parse_float(param_vars["cost_quantum"].get(), "cost_quantum"),
            "join_order_bias": parse_order_bias(param_vars["join_order_bias"].get()),
        }
        scale = parse_float(param_vars["sample_scale"].get(), "sample_scale")
        session.update_cost_params(patch)
        session.update_sample_scale(scale)
        update_version_label()
        mark_display_stale()

    def poll_queue() -> None:
        nonlocal display_versions, latest_evaluation, busy_ticket
        try:
            while True:
                ticket, payload = message_queue.get_nowait()
                if not gate.accepts(ticket, session):
                    status_var.set(
                        f"已丢弃陈旧结果 request={ticket.serial} "
                        f"pv={ticket.versions.param_version} dv={ticket.versions.data_version}"
                    )
                    continue
                busy_ticket = None
                if isinstance(payload, JoinLabError):
                    if payload.code == "E_STALE":
                        status_var.set("本次执行已被更新版本淘汰；等待最新刷新")
                    else:
                        status_var.set(f"{payload.code}: {payload.message}")
                        result_text.delete("1.0", tk.END)
                        result_text.insert(tk.END, json_dumps_stable(payload.to_dict(), pretty=True))
                    continue
                if payload.versions != session.versions:
                    status_var.set("已丢弃版本不匹配的结果")
                    continue
                latest_evaluation = payload
                display_versions = payload.versions
                show_evaluation(payload)
                status_var.set(
                    f"完成：{payload.execution.selected.row_count} 行，计划 {payload.selected_plan.plan_id}，"
                    f"pv={payload.versions.param_version} dv={payload.versions.data_version}"
                )
        except queue.Empty:
            pass
        root.after(60, poll_queue)

    def worker(ticket: RefreshTicket, sql: str) -> None:
        try:
            evaluation = session.evaluate(sql, expected_versions=ticket.versions)
            message_queue.put((ticket, evaluation))
        except JoinLabError as exc:
            message_queue.put((ticket, exc))
        except Exception as exc:  # pragma: no cover - GUI safety net
            message_queue.put((ticket, SchemaError(f"unexpected GUI worker failure: {exc}", "gui.worker")))

    def request_refresh(immediate: bool = False) -> None:
        nonlocal after_id, busy_ticket
        if after_id is not None:
            try:
                root.after_cancel(after_id)
            except tk.TclError:
                pass
            after_id = None
        if not immediate:
            after_id = root.after(260, lambda: request_refresh(immediate=True))
            return
        try:
            apply_parameters()
            sql = current_query()
            session.parse(sql)
        except JoinLabError as exc:
            state_var.set("ERROR")
            state_label.configure(foreground="#b00020")
            status_var.set(f"{exc.code}: {exc.message}")
            result_text.delete("1.0", tk.END)
            result_text.insert(tk.END, json_dumps_stable(exc.to_dict(), pretty=True))
            return
        update_version_label()
        ticket = gate.issue(session.versions)
        busy_ticket = ticket
        state_var.set("REFRESHING")
        state_label.configure(foreground="#8a5a00")
        status_var.set(f"正在执行 request={ticket.serial} pv={ticket.versions.param_version}")
        threading.Thread(target=worker, args=(ticket, sql), daemon=True).start()

    def schedule_refresh(_event: Any = None) -> None:
        nonlocal after_id
        if after_id is not None:
            root.after_cancel(after_id)
        after_id = root.after(260, lambda: request_refresh(immediate=True))

    def show_evaluation(evaluation: Evaluation) -> None:
        selected_id = evaluation.selected_plan.plan_id
        for item in plan_tree.get_children():
            plan_tree.delete(item)
        table_dict = evaluation.plans.to_dict()
        selected_dict = None
        for plan in table_dict["candidates"]:
            marker = "✓" if plan["plan_id"] == selected_id else ""
            plan_tree.insert(
                "",
                tk.END,
                values=(
                    marker,
                    f"{plan['total_cost']:.6f}",
                    f"{plan['quantized_cost']:.6f}",
                    " > ".join(plan["relation_order"]),
                    " > ".join(plan["operators"]),
                    plan["plan_id"],
                ),
            )
            if plan["plan_id"] == selected_id:
                selected_dict = plan
        lines = []
        lines.append("选中计划")
        lines.append(json_dumps_stable(selected_dict or {}, pretty=True))
        lines.append("\n逐算子真实统计（稳定字段）")
        timing_map = dict(evaluation.execution.selected.timings_ms)
        for stat in evaluation.execution.selected.stats:
            lines.append(json_dumps_stable(stat, pretty=False))
            lines.append(f"  observational elapsed_ms={timing_map.get(stat['operator_id'], 0.0):.6f}")
        lines.append("\n估算与真实偏差归因链")
        for stage in evaluation.execution.selected.attribution_chain:
            lines.append(json_dumps_stable(stage, pretty=False))
        lines.append("\n跨计划结果一致性")
        lines.append(json_dumps_stable(evaluation.execution.consistency.to_dict(), pretty=True))
        lines.append("\n有序结果预览（最多 200 行，字节摘要 "
                     f"{evaluation.execution.selected.digest[:24]}）")
        for row in evaluation.execution.selected.rows[:200]:
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        if evaluation.execution.selected.row_count > 200:
            lines.append(f"... 省略 {evaluation.execution.selected.row_count - 200} 行")
        result_text.delete("1.0", tk.END)
        result_text.insert(tk.END, "\n".join(lines))

    def show_consistency() -> None:
        if latest_evaluation is None or display_versions != session.versions:
            messagebox.showwarning("结果已陈旧", "当前展示结果不是最新参数/数据版本，请先刷新。")
            return
        messagebox.showinfo(
            "跨路径一致性",
            json_dumps_stable(latest_evaluation.execution.consistency.to_dict(), pretty=True),
        )

    def save_current_snapshot() -> None:
        path = filedialog.asksaveasfilename(
            title="保存实验会话快照",
            defaultextension=".json",
            filetypes=[("JSON snapshot", "*.json")],
        )
        if not path:
            return
        try:
            result = save_snapshot(session, path)
            status_var.set(f"快照已保存：{result['snapshot_path']}")
        except JoinLabError as exc:
            messagebox.showerror(exc.code, exc.message)

    def load_snapshot() -> None:
        nonlocal session, display_versions, latest_evaluation
        path = filedialog.askopenfilename(
            title="整体恢复实验会话快照",
            filetypes=[("JSON snapshot", "*.json")],
        )
        if not path:
            return
        try:
            restored = restore_snapshot(path, current_session=session)
        except JoinLabError as exc:
            messagebox.showerror(exc.code, json_dumps_stable(exc.to_dict(), pretty=True))
            return
        session = restored.session
        display_versions = None
        latest_evaluation = None
        params = session.params
        for name, var in param_vars.items():
            if name == "join_order_bias":
                var.set(",".join(f"{key}={value}" for key, value in sorted(params.join_order_bias)))
            elif name == "sample_scale":
                var.set(str(session.sample_scale))
            else:
                var.set(str(getattr(params, name)))
        update_version_label()
        state_var.set("CURRENT")
        state_label.configure(foreground="#1b5e20")
        status_var.set(f"快照已整体替换并回放 {restored.replayed_queries} 个查询")
        request_refresh(immediate=True)

    def on_query_selected(_event: Any = None) -> None:
        selected = query_var.get()
        for item in named_queries:
            if item.name == selected:
                sql_text.delete("1.0", tk.END)
                sql_text.insert("1.0", item.sql)
                request_refresh(immediate=True)
                break

    query_combo.bind("<<ComboboxSelected>>", on_query_selected)
    for var in param_vars.values():
        var.trace_add("write", schedule_refresh)
    sql_text.bind("<Control-Return>", lambda _event: request_refresh(immediate=True))

    update_version_label()
    root.after(80, poll_queue)
    root.after(80, lambda: request_refresh(immediate=True))
    root.mainloop()


def gui_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JoinLab Tkinter workbench")
    parser.add_argument("--catalog", default=str(Path(__file__).resolve().parents[2] / "samples" / "catalog.json"))
    parser.add_argument("--queries", default=str(Path(__file__).resolve().parents[2] / "samples" / "queries.json"))
    parser.add_argument("--snapshot", default=None)
    args = parser.parse_args(argv)
    try:
        launch(args.catalog, args.queries, snapshot_path=args.snapshot)
        return 0
    except ImportError as exc:
        error = JoinLabError(
            "E_GUI",
            f"Tkinter is unavailable in this Python installation: {exc}",
            "gui.tkinter",
        )
        sys.stdout.write(json_dumps_stable(error.to_dict(), pretty=True) + "\n")
        return 3
    except JoinLabError as exc:
        sys.stdout.write(json_dumps_stable(exc.to_dict(), pretty=True) + "\n")
        return 2
    except Exception as exc:  # pragma: no cover - GUI startup safety net
        error = JoinLabError("E_GUI", f"GUI startup failed: {exc}", "gui.startup", {"traceback": traceback.format_exc()})
        sys.stdout.write(json_dumps_stable(error.to_dict(), pretty=True) + "\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(gui_main())
