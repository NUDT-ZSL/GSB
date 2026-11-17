"""Tkinter 编辑与操作界面（懒加载 tkinter，核心逻辑可独立运行）。

展示：变更项列表与来源版本、风险贡献与误差界、回滚依赖图、受影响步骤集合、
账本执行状态与版本列表；渲染顺序稳定（全部按键排序）。
"""

from __future__ import annotations

import argparse
import json
import sys

from .app import OpsApp
from .errors import OpsError
from .graph import topo_order
from .incremental import diff_tickets
from .ledger import open_ledger
from .risk import SCALE


def build_view_model(app: OpsApp) -> dict:
    """把当前状态整理成稳定有序的视图模型（UI 与测试共用）。"""
    current = app.store.current
    if current is None:
        return {"loaded": False}
    ticket, plan, report = current.ticket, current.plan, current.report
    items = [{
        "key": it.key,
        "source_format_version": it.source_format_version,
        "category": it.category,
        "risk_weight": it.risk_weight,
        "impacted_services": list(it.impacted_services),
        "source_system": it.source_system,
    } for it in ticket.items]  # items 已按 key 排序
    contributions = [{
        "key": c.key,
        "contribution": f"{c.contribution / SCALE:.3f}",
        "error_bound": f"{c.error_bound / SCALE:.3f}",
    } for c in report.contributions]
    edges = sorted(
        (s.step_id, dep) for s in plan.steps for dep in s.depends_on
    )
    ledger = [{
        "seq": e.seq, "step_id": e.step_id, "result": e.result,
        "plan_version": e.plan_version, "ticket_revision": e.ticket_revision,
        "executed_at": e.executed_at,
    } for e in app.ledger_entries()] if app.conn is not None else []
    return {
        "loaded": True,
        "version_label": current.version_label(),
        "items": items,
        "risk": {
            "total": f"{report.total_score / SCALE:.3f}",
            "error_bound": f"{report.total_error_bound / SCALE:.3f}",
            "contributions": contributions,
        },
        "graph": {"order": list(current.order), "edges": edges},
        "affected_steps": list(getattr(app, "last_affected_steps", ())),
        "ledger": ledger,
        "versions": app.store.version_labels(),
    }


def run_ui(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rollback-ops-ui")
    parser.add_argument("--ticket", default="samples/ticket_v3.json")
    parser.add_argument("--plan", default="samples/rollback_plan.json")
    parser.add_argument("--ledger", default="artifacts/ledger.db")
    parser.add_argument("--inject", choices=["corrupt-ticket", "cycle-deps", "version-conflict"],
                        help="异常注入：仅改变输入文件或参数")
    args = parser.parse_args(argv)

    ticket_path, plan_path = args.ticket, args.plan
    if args.inject == "corrupt-ticket":
        ticket_path = "samples/broken/ticket_corrupt.json"
    elif args.inject == "cycle-deps":
        plan_path = "samples/broken/rollback_plan_cycle.json"

    app = OpsApp(open_ledger(args.ledger))
    try:
        app.load(ticket_path, plan_path)
        if args.inject == "version-conflict":
            from .loader import load_ticket
            app.execute()
            stale = load_ticket("samples/ticket_v2.json")  # 旧 revision
            app.store.replace_ticket(stale)  # 单调性校验 -> E_VERSION
    except OpsError as exc:
        print(exc.render(), file=sys.stderr)

    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("变更风险与回滚运维")
    model = build_view_model(app)

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    def add_tree(parent, columns, rows):
        tree = ttk.Treeview(parent, columns=columns, show="headings")
        for col in columns:
            tree.heading(col, text=col)
        for row in rows:
            tree.insert("", "end", values=[row.get(c, "") for c in columns])
        tree.pack(fill="both", expand=True)
        return tree

    if model["loaded"]:
        frame_items = ttk.Frame(notebook)
        notebook.add(frame_items, text="变更项")
        add_tree(frame_items,
                 ["key", "source_format_version", "category", "risk_weight",
                  "impacted_services", "source_system"],
                 model["items"])

        frame_risk = ttk.Frame(notebook)
        notebook.add(frame_risk, text="风险")
        ttk.Label(frame_risk,
                  text=f"总分 {model['risk']['total']} ± {model['risk']['error_bound']}").pack()
        add_tree(frame_risk, ["key", "contribution", "error_bound"],
                 model["risk"]["contributions"])

        frame_graph = ttk.Frame(notebook)
        notebook.add(frame_graph, text="回滚依赖图")
        text = tk.Text(frame_graph)
        text.insert("end", "执行顺序: " + " -> ".join(model["graph"]["order"]) + "\n")
        for step_id, dep in model["graph"]["edges"]:
            text.insert("end", f"{step_id} depends_on {dep}\n")
        text.insert("end", "受影响步骤: " + ", ".join(model["affected_steps"]) + "\n")
        text.pack(fill="both", expand=True)

        frame_ledger = ttk.Frame(notebook)
        notebook.add(frame_ledger, text="账本")
        add_tree(frame_ledger,
                 ["seq", "step_id", "result", "plan_version", "ticket_revision", "executed_at"],
                 model["ledger"])

        frame_versions = ttk.Frame(notebook)
        notebook.add(frame_versions, text="版本")
        version_text = tk.Text(frame_versions)
        version_text.insert("end", "\n".join(model["versions"]))
        version_text.pack(fill="both", expand=True)
    else:
        ttk.Label(root, text="加载失败，详见 stderr 错误码输出").pack()

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_ui())
