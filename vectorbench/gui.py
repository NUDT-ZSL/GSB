"""Tkinter interface with a pure, headless-testable view model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .execution import QueryResult
from .session import ExperimentSession


@dataclass(frozen=True)
class TreeRow:
    node: str
    detail: str
    depth: int


@dataclass(frozen=True)
class GuiViewModel:
    title: str
    tree_rows: tuple[TreeRow, ...]
    decision_lines: tuple[str, ...]
    ranking_rows: tuple[tuple[object, ...], ...]
    result_rows: tuple[tuple[object, ...], ...]


def _tree_lines(node, depth: int = 0) -> list[TreeRow]:
    indent = "  " * depth
    if node.kind == "literal":
        return [TreeRow("literal", f"{indent}{node.value}:{node.type_name}", depth)]
    if node.kind == "column":
        return [TreeRow("column", f"{indent}{node.name}", depth)]
    if node.kind == "unary":
        rows = [TreeRow("unary", f"{indent}{node.op}", depth)]
        rows.extend(_tree_lines(node.children[0], depth + 1))
        return rows
    if node.kind == "ternary":
        rows = [TreeRow("ternary", f"{indent}?:", depth)]
        for child in node.children:
            rows.extend(_tree_lines(child, depth + 1))
        return rows
    rows = [TreeRow("binary", f"{indent}{node.op}", depth)]
    for child in node.children:
        rows.extend(_tree_lines(child, depth + 1))
    return rows


def build_view_model(session: ExperimentSession) -> GuiViewModel:
    evaluation = session.evaluations[0]
    from .parser import parse_expression

    root = parse_expression(evaluation.expression, evaluation.query_index)
    tree_rows = tuple(_tree_lines(root))
    decision = evaluation.decision
    decision_lines = (
        f"selected_path={decision.selected_path}",
        f"batch_size={decision.batch_size} batch_count={decision.batch_count}",
        f"reason_code={decision.reason_code}: {decision.reason}",
        f"rows={decision.scale.row_count} columns={decision.scale.column_count} "
        f"depth={decision.scale.expression_depth} intermediates={decision.scale.intermediate_count}",
    )
    ranking_rows = tuple(
        (
            item.batch_size,
            item.batch_count,
            item.naive_peak_bytes,
            item.fused_peak_bytes,
            item.naive_work,
            item.fused_work,
            item.selected_path,
            item.reason_code,
        )
        for item in decision.ranking
    )
    result_rows = tuple(
        (
            item.query_index,
            item.canonical,
            item.selected.path,
            item.selected.batch_size,
            item.selected.result_type,
            item.selected.result_summary().null_count,
            item.selected.result_summary().digest[:16],
            item.naive.elapsed_seconds,
            item.fused.elapsed_seconds,
        )
        for item in session.evaluations
    )
    return GuiViewModel(
        title=f"VectorBench - {session.block.name} ({session.block.row_count} rows)",
        tree_rows=tree_rows,
        decision_lines=decision_lines,
        ranking_rows=ranking_rows,
        result_rows=result_rows,
    )


def launch_gui(session: ExperimentSession) -> None:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "tkinter is unavailable in this Python runtime; use the official Windows Python installer"
        ) from exc

    model = build_view_model(session)
    root = tk.Tk()
    root.title(model.title)
    root.geometry("1180x720")

    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    tree_frame = ttk.Frame(notebook)
    notebook.add(tree_frame, text="Expression Tree")
    tree = ttk.Treeview(tree_frame, columns=("detail",), show="tree headings")
    tree.heading("#0", text="Node")
    tree.heading("detail", text="Canonical Detail")
    tree.pack(fill=tk.BOTH, expand=True)
    parent_ids: list[str] = []
    for row in model.tree_rows:
        depth = row.depth
        parent = "" if depth == 0 else parent_ids[depth - 1]
        item_id = tree.insert(parent, tk.END, text=row.node, values=(row.detail.strip(),))
        if depth == len(parent_ids):
            parent_ids.append(item_id)
        else:
            parent_ids[depth] = item_id

    decision_frame = ttk.Frame(notebook)
    notebook.add(decision_frame, text="Path Decision")
    decision_text = tk.Text(decision_frame, wrap=tk.WORD, height=12)
    decision_text.pack(fill=tk.BOTH, expand=True)
    decision_text.insert(tk.END, "\n".join(model.decision_lines))
    decision_text.configure(state=tk.DISABLED)

    ranking_frame = ttk.Frame(notebook)
    notebook.add(ranking_frame, text="Batch Ranking")
    columns = ("batch", "batches", "naive_bytes", "fused_bytes", "naive_work",
               "fused_work", "path", "reason")
    ranking = ttk.Treeview(ranking_frame, columns=columns, show="headings")
    for column in columns:
        ranking.heading(column, text=column)
    ranking.pack(fill=tk.BOTH, expand=True)
    for row in model.ranking_rows:
        ranking.insert("", tk.END, values=row)

    result_frame = ttk.Frame(notebook)
    notebook.add(result_frame, text="Results")
    result_columns = ("q", "canonical", "path", "batch", "type", "nulls", "digest",
                      "naive_s", "fused_s")
    result_view = ttk.Treeview(result_frame, columns=result_columns, show="headings")
    for column in result_columns:
        result_view.heading(column, text=column)
    result_view.pack(fill=tk.BOTH, expand=True)
    for row in model.result_rows:
        result_view.insert("", tk.END, values=row)

    root.mainloop()
