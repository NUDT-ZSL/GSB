from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from .declarations import load_declarations
from .diagnostics import build_diagnostic_report
from .engine import GatewayEngine
from .errors import GatewayError
from .logger import StructuredLogger
from .storage import SessionStore


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DiagnosticWorkbench:
    def __init__(
        self,
        *,
        root: tk.Tk,
        engine: GatewayEngine,
        declaration_path: str,
        rules_path: str,
        db_path: str,
    ) -> None:
        self.root = root
        self.engine = engine
        self.declaration_path = declaration_path
        self.rules_path = rules_path
        self.db_path = db_path
        self.root.title("ML 推理网关指纹诊断工作台")
        self.root.geometry("1480x900")
        self.root.minsize(1100, 680)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", font=("Consolas", 9), rowheight=22)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        self.header = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        self.header.pack(fill=tk.X)
        self.status_var = tk.StringVar()
        ttk.Label(
            self.header,
            textvariable=self.status_var,
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(self.header, text="刷新", command=self.refresh).pack(side=tk.RIGHT, padx=4)
        ttk.Button(
            self.header, text="保存快照", command=self.save_snapshot
        ).pack(side=tk.RIGHT, padx=4)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.backend_tree = self._tab_tree(
            "后端 / 指纹",
            (
                "backend_id",
                "model_id",
                "endpoint",
                "concurrency",
                "load_sequence",
                "fingerprint",
                "config_digest",
            ),
            {
                "backend_id": ("后端", 115),
                "model_id": ("模型", 150),
                "endpoint": ("回环地址", 120),
                "concurrency": ("并发上限", 75),
                "load_sequence": ("加载序号", 70),
                "fingerprint": ("当前指纹", 420),
                "config_digest": ("配置摘要", 250),
            },
        )
        self.rule_tree = self._tab_tree(
            "规则 / 命中分布",
            (
                "rule_id",
                "priority",
                "target_backend",
                "batch_size",
                "match",
                "effective_hits",
                "invalid_hits",
            ),
            {
                "rule_id": ("规则", 170),
                "priority": ("优先级", 65),
                "target_backend": ("目标后端", 110),
                "batch_size": ("批大小", 65),
                "match": ("匹配条件", 300),
                "effective_hits": ("有效命中", 80),
                "invalid_hits": ("失效命中", 80),
            },
        )
        self.ledger_tree = self._tab_tree(
            "逐请求账本",
            (
                "request_index",
                "request_id",
                "rule_id",
                "backend_id",
                "batch_id",
                "bound_fingerprint",
                "effective",
                "binding_valid",
                "invalid_reason",
                "void",
                "output_summary",
            ),
            {
                "request_index": ("序号", 55),
                "request_id": ("请求", 85),
                "rule_id": ("规则", 125),
                "backend_id": ("后端", 90),
                "batch_id": ("批次", 90),
                "bound_fingerprint": ("绑定指纹", 400),
                "effective": ("有效", 55),
                "binding_valid": ("绑定有效", 65),
                "invalid_reason": ("失效原因", 180),
                "void": ("作废", 55),
                "output_summary": ("输出摘要", 380),
            },
        )
        self.batch_tree = self._tab_tree(
            "批次 / 作废重派",
            (
                "batch_id",
                "status",
                "rule_id",
                "backend_id",
                "bound_fingerprint",
                "request_ids",
                "redispatch_of",
                "superseded_by",
                "error_code",
                "output_summary",
            ),
            {
                "batch_id": ("批次", 90),
                "status": ("状态", 85),
                "rule_id": ("规则", 125),
                "backend_id": ("后端", 90),
                "bound_fingerprint": ("绑定指纹", 400),
                "request_ids": ("请求", 200),
                "redispatch_of": ("重派自", 90),
                "superseded_by": ("被替代为", 90),
                "error_code": ("错误码", 120),
                "output_summary": ("输出摘要", 340),
            },
        )
        version_container = ttk.Frame(self.notebook)
        self.notebook.add(version_container, text="统计版本 / 快照")
        version_panes = ttk.Panedwindow(version_container, orient=tk.VERTICAL)
        version_panes.pack(fill=tk.BOTH, expand=True)
        stats_frame = ttk.Frame(version_panes)
        snapshot_frame = ttk.Frame(version_panes)
        version_panes.add(stats_frame, weight=3)
        version_panes.add(snapshot_frame, weight=2)
        self.stats_tree = self._tree_in(
            stats_frame,
            (
                "stats_version",
                "readonly",
                "reason",
                "view_version",
                "affected_backends",
                "invalidated",
                "void",
                "redispatched",
                "recalculated",
                "effective_requests",
            ),
            {
                "stats_version": ("统计版本", 80),
                "readonly": ("只读", 55),
                "reason": ("原因", 235),
                "view_version": ("视图版本", 75),
                "affected_backends": ("受影响后端", 160),
                "invalidated": ("失效归属", 75),
                "void": ("作废批次", 170),
                "redispatched": ("重派批次", 170),
                "recalculated": ("重算项数", 75),
                "effective_requests": ("有效请求", 75),
            },
        )
        self.snapshot_tree = self._tree_in(
            snapshot_frame,
            (
                "ordinal",
                "snapshot_id",
                "label",
                "stats_version",
                "view_version",
                "fingerprints",
                "state_digest",
            ),
            {
                "ordinal": ("序号", 55),
                "snapshot_id": ("快照", 240),
                "label": ("标签", 200),
                "stats_version": ("统计版本", 75),
                "view_version": ("视图版本", 75),
                "fingerprints": ("指纹集合", 650),
                "state_digest": ("状态摘要", 330),
            },
        )
        self.refresh()

    def _tab_tree(
        self,
        title: str,
        columns: tuple[str, ...],
        headings: dict[str, tuple[str, int]],
    ) -> ttk.Treeview:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=title)
        return self._tree_in(frame, columns, headings)

    def _tree_in(
        self,
        frame: ttk.Frame,
        columns: tuple[str, ...],
        headings: dict[str, tuple[str, int]],
    ) -> ttk.Treeview:
        container = ttk.Frame(frame)
        container.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse")
        for column in columns:
            label, width = headings[column]
            tree.heading(column, text=label)
            tree.column(column, width=width, minwidth=45, stretch=False, anchor=tk.W)
        y_scroll = ttk.Scrollbar(container, orient=tk.VERTICAL, command=tree.yview)
        x_scroll = ttk.Scrollbar(container, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        return tree

    @staticmethod
    def _clear(tree: ttk.Treeview) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def refresh(self) -> None:
        report = build_diagnostic_report(self.engine)
        self.status_var.set(
            "  |  ".join(
                [
                    f"DB: {self.db_path}",
                    f"view={report['current_view_version']}",
                    f"stats={report['current_stats_version']}",
                    f"有效归属={report['counts']['effective_ledger_entries']}",
                    f"失效归属={report['counts']['invalid_ledger_entries']}",
                    f"作废批次={report['counts']['void_batches']}",
                ]
            )
        )
        self._clear(self.backend_tree)
        for row in report["backends"]:
            self.backend_tree.insert(
                "",
                tk.END,
                values=(
                    row["backend_id"],
                    row["model_id"],
                    f"{row['host']}:{row['port']}",
                    row["max_concurrency"],
                    row["load_sequence"],
                    row["fingerprint"],
                    row["config_digest"],
                ),
            )
        self._clear(self.rule_tree)
        for row in report["rules"]:
            self.rule_tree.insert(
                "",
                tk.END,
                values=(
                    row["rule_id"],
                    row["priority"],
                    row["target_backend"],
                    row["batch_size"],
                    _compact_json(row["match"]),
                    row["effective_hits"],
                    row["invalid_hits"],
                ),
            )
        self._clear(self.ledger_tree)
        for row in report["ledger"]:
            self.ledger_tree.insert(
                "",
                tk.END,
                values=(
                    row["request_index"],
                    row["request_id"],
                    row["rule_id"],
                    row["backend_id"],
                    row["batch_id"],
                    row["bound_fingerprint"],
                    row["effective"],
                    row["binding_valid"],
                    row["invalid_reason"] or "",
                    row["void"],
                    row["output_summary"] or "",
                ),
            )
        self._clear(self.batch_tree)
        for row in report["batches"]:
            self.batch_tree.insert(
                "",
                tk.END,
                values=(
                    row["batch_id"],
                    row["status"],
                    row["rule_id"],
                    row["backend_id"],
                    row["bound_fingerprint"],
                    ",".join(row["request_ids"]),
                    row["redispatch_of"] or "",
                    row["superseded_by"] or "",
                    row["error_code"] or "",
                    row["output_summary"] or "",
                ),
            )
        self._clear(self.stats_tree)
        for row in report["stats_versions"]:
            summary = row["stats_summary"]
            self.stats_tree.insert(
                "",
                tk.END,
                values=(
                    row["stats_version"],
                    row["readonly"],
                    row["reason"],
                    row["view_version"],
                    ",".join(row["affected_backends"]),
                    row["invalidated_entry_count"],
                    ",".join(row["void_batch_ids"]),
                    ",".join(row["redispatched_batch_ids"]),
                    len(row["recalculated_stat_keys"]),
                    summary["effective_requests"],
                ),
            )
        self._clear(self.snapshot_tree)
        for row in report["snapshots"]:
            self.snapshot_tree.insert(
                "",
                tk.END,
                values=(
                    row["ordinal"],
                    row["snapshot_id"],
                    row["label"],
                    row["stats_version"],
                    row["view_version"],
                    _compact_json(row["backend_fingerprints"]),
                    row["state_digest"],
                ),
            )

    def save_snapshot(self) -> None:
        try:
            ordinal = len(self.engine.store.list_snapshots()) + 1
            snapshot = self.engine.save_snapshot(f"manual-gui-{ordinal}")
            self.refresh()
            messagebox.showinfo(
                "快照已保存",
                f"{snapshot['snapshot_id']}\nstats={snapshot['stats_version']}",
            )
        except GatewayError as exc:
            messagebox.showerror(exc.code, exc.message)


def _compact_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tkinter ML gateway diagnostic workbench")
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "artifacts" / "demo" / "session.db")
    )
    parser.add_argument(
        "--backends", default=str(PROJECT_ROOT / "data" / "backends_v2.json")
    )
    parser.add_argument("--rules", default=str(PROJECT_ROOT / "data" / "rules.json"))
    parser.add_argument(
        "--log", default=str(PROJECT_ROOT / "artifacts" / "demo" / "logs" / "gateway.jsonl")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store: SessionStore | None = None
    try:
        bundle = load_declarations(args.backends, args.rules)
        store = SessionStore(args.db)
        logger = StructuredLogger(store, args.log, fresh=False)
        engine = GatewayEngine(store=store, bundle=bundle, logger=logger)
        root = tk.Tk()
        DiagnosticWorkbench(
            root=root,
            engine=engine,
            declaration_path=args.backends,
            rules_path=args.rules,
            db_path=args.db,
        )
        root.mainloop()
        return 0
    except GatewayError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except tk.TclError as exc:
        print(f"E_TK: cannot open Tkinter display: {exc}", file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
