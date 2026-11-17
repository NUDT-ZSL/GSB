"""Tkinter interface.  Importing this module does not require Tk to exist."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .catalog import load_pipeline_input
from .demo import generate_demo_files
from .errors import PipeError
from .executor import compare_results, execute
from .plan import PlanBundle, build_plans, walk_plan


def _plan_line(node: dict[str, Any]) -> str:
    attrs = node.get("attrs", {})
    if node["op"] == "Scan":
        columns = ",".join(item["ref"] for item in attrs.get("columns", []))
        return f"Scan {attrs.get('alias')} ({attrs.get('table')}) columns=[{columns}]"
    if node["op"] == "Filter":
        return f"Filter {node['id']} conditions={len(attrs.get('conditions', []))}"
    if node["op"] == "Join":
        return f"InnerJoin {','.join(pair['left'] + '=' + pair['right'] for pair in attrs.get('conditions', []))}"
    if node["op"] == "Project":
        return f"Project {node['id']} columns={len(attrs.get('columns', []))}"
    if node["op"] == "Aggregate":
        return (
            f"Aggregate group_by={len(attrs.get('group_by', []))} "
            f"aggregates={len(attrs.get('aggregates', []))}"
        )
    return node["op"]


def _pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _default_input(workspace: Path) -> Path:
    path = workspace / "artifacts" / "demo" / "pipeline_input.json"
    if not path.exists():
        generate_demo_files(path.parent)
    return path


def run_gui(initial_input: str | None = None, *, workspace: str | Path = ".") -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:  # pragma: no cover - depends on local Tk install
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "code": "E_INTERNAL",
                    "message": f"tkinter is unavailable: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return 2

    workspace_path = Path(workspace).resolve()
    state: dict[str, Any] = {"input": Path(initial_input) if initial_input else _default_input(workspace_path)}
    root = tk.Tk()
    root.title("ColPipe - Columnar Pushdown Workbench")
    root.geometry("1260x820")
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")

    toolbar = ttk.Frame(root, padding=8)
    toolbar.pack(fill=tk.X)
    input_var = tk.StringVar(value=str(state["input"]))
    ttk.Label(toolbar, text="输入 JSON").pack(side=tk.LEFT)
    input_entry = ttk.Entry(toolbar, textvariable=input_var, width=70)
    input_entry.pack(side=tk.LEFT, padx=6)

    def choose_file() -> None:
        selected = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if selected:
            input_var.set(selected)

    ttk.Button(toolbar, text="选择", command=choose_file).pack(side=tk.LEFT)
    run_button = ttk.Button(toolbar, text="运行优化与基线")
    run_button.pack(side=tk.LEFT, padx=6)

    status_var = tk.StringVar(value="待运行")
    status = ttk.Label(root, textvariable=status_var, padding=(8, 3), anchor=tk.W)
    status.pack(fill=tk.X)

    panes = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
    panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    left = ttk.Frame(panes)
    right = ttk.Notebook(panes)
    panes.add(left, weight=1)
    panes.add(right, weight=3)
    ttk.Label(left, text="逻辑计划树").pack(anchor=tk.W)
    tree = ttk.Treeview(left, columns=("op",), show="tree", height=30)
    tree.pack(fill=tk.BOTH, expand=True, pady=4)

    text_tabs: dict[str, tk.Text] = {}
    for title in ("计划与推导", "下推与裁剪", "切分与估算", "聚合结果", "任务轨迹"):
        frame = ttk.Frame(right)
        right.add(frame, text=title)
        widget = tk.Text(frame, wrap=tk.NONE, font=("Consolas", 9))
        ybar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=widget.yview)
        xbar = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=widget.xview)
        widget.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        ybar.pack(side=tk.RIGHT, fill=tk.Y)
        xbar.pack(side=tk.BOTTOM, fill=tk.X)
        widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text_tabs[title] = widget

    timeline_frame = ttk.Frame(right)
    right.add(timeline_frame, text="资源时间线")
    timeline = tk.Canvas(timeline_frame, background="#fbfbfb", height=300)
    timeline.pack(fill=tk.BOTH, expand=True)

    def set_text(title: str, value: str) -> None:
        widget = text_tabs[title]
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, value)
        widget.configure(state=tk.DISABLED)

    def populate_tree(plan: dict[str, Any]) -> None:
        tree.delete(*tree.get_children())
        ids: dict[str, str] = {}

        def add(node: dict[str, Any], parent: str = "") -> None:
            item = tree.insert(parent, tk.END, text=_plan_line(node), open=True)
            ids[node["id"]] = item
            for child in node.get("children", []):
                add(child, item)

        add(plan)

    def draw_timeline(optimized, baseline) -> None:
        timeline.delete("all")
        runs = list(optimized.trace) + list(baseline.trace)
        if not runs:
            return
        start = min(float(run["start_ms"]) for run in runs)
        end = max(float(run["end_ms"]) for run in runs)
        span = max(0.001, end - start)
        width = max(700, timeline.winfo_width() - 160)
        row_height = 17
        colors = {"optimized": "#3a7bd5", "baseline": "#d68140"}
        row = 10
        for mode, trace in (("optimized", optimized.trace), ("baseline", baseline.trace)):
            timeline.create_text(8, row, text=mode, anchor="nw", font=("Segoe UI", 9, "bold"))
            row += row_height
            for task in trace:
                x0 = 110 + (float(task["start_ms"]) - start) / span * width
                x1 = 110 + (float(task["end_ms"]) - start) / span * width
                timeline.create_rectangle(x0, row, max(x0 + 1, x1), row + 12, fill=colors[mode], outline="")
                timeline.create_text(8, row, text=str(task["task_id"]), anchor="nw", font=("Consolas", 8))
                row += row_height
            row += 8

    def render(request, bundle: PlanBundle, optimized, baseline, comparison) -> None:
        populate_tree(bundle.optimized_plan)
        set_text(
            "计划与推导",
            _pretty(
                {
                    "query_summary_sha256": bundle.query_summary_sha256,
                    "optimized_plan_sha256": bundle.optimized_summary_sha256,
                    "baseline_plan_sha256": bundle.baseline_summary_sha256,
                    "optimized_plan": bundle.optimized_plan,
                    "derivation": bundle.pruning["derivation"],
                }
            ),
        )
        set_text(
            "下推与裁剪",
            _pretty({"pushdown": bundle.pushdown, "pruning": bundle.pruning}),
        )
        set_text(
            "切分与估算",
            _pretty({"estimation": bundle.estimates, "split_plan": bundle.split_plan}),
        )
        set_text(
            "聚合结果",
            _pretty(
                {
                    "columns": optimized.columns,
                    "rows": optimized.rows,
                    "group_keys": optimized.group_keys,
                    "canonical_sha256": optimized.canonical_sha256,
                    "comparison": comparison,
                }
            ),
        )
        set_text(
            "任务轨迹",
            _pretty({"optimized": optimized.trace, "baseline": baseline.trace}),
        )
        draw_timeline(optimized, baseline)

    def run_pipeline() -> None:
        run_button.configure(state=tk.DISABLED)
        status_var.set("运行中...")
        root.update_idletasks()
        try:
            request = load_pipeline_input(input_var.get())
            bundle = build_plans(request)
            optimized = execute(bundle, mode="optimized")
            baseline = execute(bundle, mode="baseline")
            comparison = compare_results(optimized, baseline)
            render(request, bundle, optimized, baseline, comparison)
            status_var.set(
                f"OK | 字节一致={comparison['byte_equal']} | "
                f"优化 {optimized.stats['scan_tasks']} 任务 | "
                f"峰值预算占用 {optimized.stats['peak_scheduler_reserved_memory_bytes']}/"
                f"{optimized.stats['memory_budget_bytes']} bytes"
            )
        except PipeError as exc:
            status_var.set(f"{exc.code} | {exc.message}")
            messagebox.showerror(exc.code, f"{exc.message}\n{exc.path or ''}")
        except Exception as exc:  # the GUI must surface a business-style failure
            status_var.set("E_INTERNAL | 运行失败")
            messagebox.showerror("E_INTERNAL", str(exc))
        finally:
            run_button.configure(state=tk.NORMAL)

    run_button.configure(command=run_pipeline)
    root.after(100, run_pipeline)
    root.mainloop()
    return 0

