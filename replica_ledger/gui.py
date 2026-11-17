"""Tkinter diagnostic interface with stable Treeview ordering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .analysis import analyze_local
from .errors import GridError, LedgerDomainError
from .grid import diagnose_grid_local
from .presentation import build_diagnostic_rows
from .spec import load_replica_group
from .util import JsonlEventLog


def launch_gui(
    *,
    spec_path: str | Path | None = None,
    scale: int = 8,
    log_path: str | Path | None = None,
) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        raise RuntimeError(
            "当前 Python 未提供 Tkinter；Windows 官方 Python 安装包默认包含 Tkinter"
        ) from exc

    logger = JsonlEventLog(log_path)
    default_spec = (
        str(spec_path)
        if spec_path is not None
        else str(Path(__file__).resolve().parents[1] / "samples" / "scenarios" / "grid" / "spec.json")
    )
    root = tk.Tk()
    root.title("Replica Ledger Diagnosis")
    root.geometry("1180x760")

    container = ttk.Frame(root, padding=10)
    container.pack(fill="both", expand=True)
    controls = ttk.Frame(container)
    controls.pack(fill="x", pady=(0, 8))
    ttk.Label(controls, text="Spec JSON").pack(side="left")
    spec_var = tk.StringVar(value=default_spec)
    spec_entry = ttk.Entry(controls, textvariable=spec_var, width=70)
    spec_entry.pack(side="left", padx=(6, 6), fill="x", expand=True)

    def choose_spec() -> None:
        selected = filedialog.askopenfilename(
            title="Select replica group JSON",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if selected:
            spec_var.set(selected)

    ttk.Button(controls, text="Browse", command=choose_spec).pack(side="left")
    ttk.Label(controls, text="Scale").pack(side="left", padx=(12, 4))
    scale_var = tk.StringVar(value=str(scale))
    ttk.Entry(controls, textvariable=scale_var, width=10).pack(side="left")

    status_var = tk.StringVar(value="Ready")
    notebook = ttk.Notebook(container)
    notebook.pack(fill="both", expand=True)

    def make_tree(parent: Any, columns: list[tuple[str, str, int]]) -> Any:
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(
            frame, columns=[name for name, _, _ in columns], show="headings"
        )
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, anchor="w", stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return frame, tree

    replica_tab = ttk.Frame(notebook)
    truth_tab = ttk.Frame(notebook)
    task_tab = ttk.Frame(notebook)
    flip_tab = ttk.Frame(notebook)
    notebook.add(replica_tab, text="Replica Matrix")
    notebook.add(truth_tab, text="Truth & Errors")
    notebook.add(task_tab, text="Repair Tasks")
    notebook.add(flip_tab, text="Grid Flips")
    replica_frame, replica_tree = make_tree(
        replica_tab,
        [
            ("node_id", "Node", 100),
            ("block_id", "Block", 140),
            ("role", "Role", 100),
            ("byte_size", "Bytes", 90),
            ("digest", "Observed digest", 520),
            ("truth_digest", "Truth digest", 520),
        ],
    )
    truth_frame, truth_tree = make_tree(
        truth_tab,
        [
            ("block_id", "Block", 130),
            ("replica_count", "Replicas", 80),
            ("truth_nodes", "Truth nodes", 220),
            ("mismatch_nodes", "Mismatch nodes", 220),
            ("strict_majority", "Strict majority", 110),
            ("ratio_fixed", "Ratio fixed", 110),
            ("scale", "Scale", 80),
            ("gap_units", "Gap units", 90),
            ("ratio_error_bound_units", "Bound", 80),
            ("alert_certainty", "Alert", 110),
            ("truth_digest", "Truth digest", 520),
        ],
    )
    task_frame, task_tree = make_tree(
        task_tab,
        [
            ("task_id", "Task", 210),
            ("block_id", "Block", 120),
            ("source_node_id", "Source", 90),
            ("target_node_id", "Target", 90),
            ("repair_amount_fixed", "Transfer fixed", 130),
            ("repair_error_bound_units", "Transfer bound", 120),
            ("workload_fixed", "Work fixed", 120),
            ("workload_error_bound_units", "Work bound", 100),
            ("cost_fixed", "Cost fixed", 120),
            ("cost_error_bound_units", "Cost bound", 100),
            ("tie_channel", "Tie", 60),
            ("source_order", "Source order", 260),
        ],
    )
    flip_frame, flip_tree = make_tree(
        flip_tab,
        [
            ("kind", "Kind", 210),
            ("block_id", "Block", 120),
            ("target_node_id", "Target", 90),
            ("before_scale", "Before scale", 100),
            ("after_scale", "After scale", 100),
            ("error_bound_units", "Bound", 80),
            ("error_bound_share_ppm", "Bound share ppm", 130),
            ("before_decision", "Before", 320),
            ("after_decision", "After", 320),
        ],
    )
    for frame in (replica_frame, truth_frame, task_frame, flip_frame):
        frame.pack(fill="both", expand=True)

    def clear_tree(tree: Any) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def analyze() -> None:
        try:
            try:
                parsed_scale = int(scale_var.get().strip())
            except ValueError as exc:
                raise GridError(
                    "Scale 必须是正整数", "session.scale", details={"actual": scale_var.get()}
                ) from exc
            spec = load_replica_group(spec_var.get())
            result = analyze_local(spec, parsed_scale, logger=logger)
            grid_scales = sorted(
                {
                    max(1, parsed_scale // 2),
                    parsed_scale,
                    min(10**12, max(1, parsed_scale * 2)),
                }
            )
            grid = diagnose_grid_local(spec, grid_scales, logger=logger)
            rows = build_diagnostic_rows(result, grid)
            for tree, key in (
                (replica_tree, "replicas"),
                (truth_tree, "truth"),
                (task_tree, "tasks"),
                (flip_tree, "flips"),
            ):
                clear_tree(tree)
                for index, row in enumerate(rows[key]):
                    tree.insert("", "end", iid=f"{key}-{index}", values=[row[column] for column, _, _ in tree["columns"]])
            status_var.set(
                f"scale={parsed_scale} blocks={len(rows['truth'])} "
                f"tasks={len(rows['tasks'])} flips={len(rows['flips'])}"
            )
        except LedgerDomainError as exc:
            logger.error(exc, event="GUI_ANALYSIS_FAILED")
            messagebox.showerror(
                "Business error",
                f"{exc.code}\n{exc.field_path}\n{exc.message}",
            )
            status_var.set(f"{exc.code} {exc.field_path}")
        except Exception as exc:
            messagebox.showerror("Unexpected error", str(exc))
            status_var.set("unexpected error")

    ttk.Button(controls, text="Analyze", command=analyze).pack(side="left", padx=(12, 4))
    ttk.Label(container, textvariable=status_var, anchor="w").pack(fill="x", pady=(8, 0))
    analyze()
    root.mainloop()
    return 0
