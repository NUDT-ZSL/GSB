"""Tkinter diagnostic workbench with a headless summary fallback.

The interactive window shows the graph topology, per-node cache status,
invalidation coverage, accuracy, template version and recent business error
codes, and lets the user click a node to inspect its cache-key components and
content hash or run the full deterministic demo.

When no graphical display is available (for example a CI container),
:func:`launch` falls back to rendering the same diagnostic summary as text so
the module stays importable and demonstrable everywhere.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from .config import GRAPH_NODES
from .demo import run_demo
from .workbench import OcrWorkbench


def headless_summary(workspace: str) -> str:
    """Render the workbench state as plain text without a display server."""

    report = run_demo(os.path.join(workspace, "demo_run"))
    lines = ["OCR Diagnostic Workbench (headless summary)", "=" * 44, ""]
    for step in report["steps"]:
        lines.append(f"[step] {step['step']}")
        for key, value in step.items():
            if key == "step":
                continue
            lines.append(f"    {key}={value}")
    lines.append("")
    lines.append(f"template_version={report.get('template_version')}")
    lines.append(f"cache_entry_count={report.get('cache_entry_count')}")
    lines.append(f"metrics={report.get('metrics')}")
    lines.append("")
    lines.append("Recent business error codes demonstrated:")
    for event in report["errors_demonstrated"]:
        lines.append(
            f"  - {event['code']} stage={event['stage']} page={event['page_id']}"
        )
    return "\n".join(lines)


def launch(workspace: str) -> Optional[Any]:
    """Launch the Tk window, or return a headless summary when unavailable."""

    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception:  # pragma: no cover - tkinter missing entirely
        return headless_summary(workspace)

    try:
        root = tk.Tk()
    except tk.TclError:
        return headless_summary(workspace)

    root.title("OCR Diagnostic Workbench")
    root.geometry("980x720")

    state: Dict[str, Any] = {"workbench": None, "report": None, "workspace": workspace}

    header = ttk.Frame(root)
    header.pack(fill="x", padx=8, pady=6)
    version_var = tk.StringVar(value="template version: -")
    metric_var = tk.StringVar(value="accuracy: -")
    ttk.Label(header, textvariable=version_var, font=("Monospace", 11, "bold")).pack(side="left")
    ttk.Label(header, textvariable=metric_var, font=("Monospace", 11)).pack(side="left", padx=20)

    topology = ttk.LabelFrame(root, text="Computation graph topology / node status")
    topology.pack(fill="x", padx=8, pady=6)
    node_buttons: Dict[str, ttk.Button] = {}
    node_status_vars: Dict[str, tk.StringVar] = {}
    for index, node in enumerate(GRAPH_NODES):
        frame = ttk.Frame(topology)
        frame.grid(row=0, column=index * 2, padx=4, pady=6)
        node_status_vars[node] = tk.StringVar(value="pending")
        button = ttk.Button(
            frame,
            text=f"{node}\n(-)",
            command=lambda n=node: _inspect_node(state, n),
        )
        button.pack()
        node_buttons[node] = button
        if index < len(GRAPH_NODES) - 1:
            ttk.Label(topology, text="→").grid(row=0, column=index * 2 + 1)

    body = ttk.Frame(root)
    body.pack(fill="both", expand=True, padx=8, pady=6)

    left = ttk.LabelFrame(body, text="Invalidation / demo log")
    left.pack(side="left", fill="both", expand=True)
    log_text = tk.Text(left, wrap="word", font=("Monospace", 9))
    log_text.pack(fill="both", expand=True)

    right = ttk.LabelFrame(body, text="Node cache-key inspection")
    right.pack(side="right", fill="both", expand=True)
    inspect_text = tk.Text(right, wrap="word", font=("Monospace", 9))
    inspect_text.pack(fill="both", expand=True)
    state["inspect_widget"] = inspect_text

    errors_frame = ttk.LabelFrame(root, text="Recent business error codes")
    errors_frame.pack(fill="x", padx=8, pady=6)
    errors_var = tk.StringVar(value="(run the demo)")
    ttk.Label(errors_frame, textvariable=errors_var, font=("Monospace", 9)).pack(
        anchor="w", padx=6, pady=4
    )

    def run() -> None:
        try:
            demo_root = os.path.join(workspace, "demo_run")
            report = run_demo(demo_root)
        except Exception as exc:  # surface errors in the UI instead of crashing
            messagebox.showerror("Demo failed", str(exc))
            return
        state["report"] = report
        state["workbench"] = OcrWorkbench(demo_root)
        version_var.set(f"template version: {report.get('template_version')}")
        metrics = report.get("metrics", {})
        metric_var.set(
            "char acc={:.3f} line acc={:.3f}".format(
                metrics.get("char_accuracy", 0.0), metrics.get("line_accuracy", 0.0)
            )
        )
        log_text.delete("1.0", "end")
        for step in report["steps"]:
            log_text.insert("end", f"[step] {step['step']}\n")
            for key, value in step.items():
                if key != "step":
                    log_text.insert("end", f"    {key} = {value}\n")
            log_text.insert("end", "\n")
        status_step = next(
            (step for step in report["steps"] if step["step"] == "incremental_run"), {}
        )
        statuses = status_step.get("status", {})
        first_page = next(iter(statuses), None)
        for node in GRAPH_NODES:
            state_value = statuses.get(first_page, {}).get(node, "-")
            node_buttons[node].config(text=f"{node}\n({state_value})")
        errors_var.set(
            ", ".join(sorted({event["code"] for event in report["errors_demonstrated"]}))
        )

    controls = ttk.Frame(root)
    controls.pack(fill="x", padx=8, pady=4)
    ttk.Button(controls, text="Run deterministic demo", command=run).pack(side="left")
    ttk.Button(
        controls,
        text="Quit",
        command=root.destroy,
    ).pack(side="right")

    root.mainloop()
    return root


def _inspect_node(state: Dict[str, Any], node: str) -> None:
    import tkinter as tk
    from tkinter import messagebox

    workbench: Optional[OcrWorkbench] = state.get("workbench")
    if workbench is None:
        messagebox.showinfo("Node inspection", "Run the demo first.")
        return
    try:
        workbench.load_dataset()
        page_id = workbench.page_ids()[0]
        details = workbench.inspect_node(page_id, node)
    except Exception as exc:
        messagebox.showerror("Node inspection", str(exc))
        return
    # Locate the inspection text widget created in the main window.
    widget = state.get("inspect_widget")
    if widget is not None:
        widget.delete("1.0", "end")
        widget.insert("end", f"node: {details['node']}\n")
        widget.insert("end", f"page: {details['page_id']}\n")
        widget.insert("end", f"cache_key: {details['cache_key']}\n")
        widget.insert("end", f"content_hash: {details['content_hash']}\n")
        widget.insert("end", f"reused: {details['reused']}\n")
        widget.insert("end", "key_details:\n")
        for key in sorted(details["key_details"]):
            widget.insert("end", f"  {key} = {details['key_details'][key]}\n")
