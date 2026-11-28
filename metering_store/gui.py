"""Local tkinter diagnostics view for the shared chunk-index database."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .database import ChunkStore

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = None
    ttk = None
    messagebox = None


class DiagnosticView:
    """Three-pane auditing UI backed by ChunkStore."""

    def __init__(self, root: Any, db_path: Path) -> None:
        self.root = root
        self.db_path = db_path
        self.store = ChunkStore(db_path, initialize=False)
        self.closed = False
        self.search_results: list[dict[str, Any]] = []

        root.title("电网计量块存储诊断视图")
        root.geometry("1360x820")
        root.minsize(1050, 650)
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.status_var = tk.StringVar()
        top = ttk.Frame(root, padding=(10, 8))
        top.pack(fill=tk.X)
        ttk.Label(
            top,
            text=f"块索引：{db_path}",
            font=("TkDefaultFont", 10, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Button(top, text="刷新视图", command=self.refresh_all).pack(side=tk.RIGHT)
        ttk.Label(top, textvariable=self.status_var).pack(side=tk.RIGHT, padx=12)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.files_tab = ttk.Frame(self.notebook, padding=6)
        self.blocks_tab = ttk.Frame(self.notebook, padding=6)
        self.search_tab = ttk.Frame(self.notebook, padding=6)
        self.audit_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.files_tab, text="文件与块边界")
        self.notebook.add(self.blocks_tab, text="块索引")
        self.notebook.add(self.search_tab, text="台区/时间检索")
        self.notebook.add(self.audit_tab, text="账目核对")

        self._build_files_tab()
        self._build_blocks_tab()
        self._build_search_tab()
        self._build_audit_tab()
        self.refresh_all()
        self.root.after(2000, self._poll_refresh)

    @staticmethod
    def _scrollable_tree(
        parent: Any,
        columns: tuple[str, ...],
        headings: tuple[str, ...],
        widths: tuple[int, ...],
        *,
        height: int = 16,
    ) -> tuple[Any, Any]:
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=height,
        )
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=50, stretch=column in {"digest"})
        vertical = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return frame, tree

    def _build_files_tab(self) -> None:
        controls = ttk.Frame(self.files_tab)
        controls.pack(fill=tk.X, pady=(0, 6))
        self.files_include_deleted = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls,
            text="显示已删除文件（保留审计映射）",
            variable=self.files_include_deleted,
            command=self.refresh_files,
        ).pack(side=tk.LEFT)
        ttk.Label(
            controls,
            text="选择文件后查看其全部块映射；active=0 表示该文件已归档。",
        ).pack(side=tk.LEFT, padx=16)

        paned = ttk.Panedwindow(self.files_tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=2)
        paned.add(right, weight=3)

        self.files_tree_frame, self.files_tree = self._scrollable_tree(
            left,
            ("file_id", "status", "policy", "records", "chunks", "bytes", "logical_time"),
            ("文件", "状态", "策略", "记录", "块(有效/总)", "字节", "逻辑时刻"),
            (95, 70, 60, 70, 100, 75, 150),
            height=20,
        )
        self.files_tree_frame.pack(fill=tk.BOTH, expand=True)
        self.files_tree.bind("<<TreeviewSelect>>", self._show_selected_file)

        ttk.Label(right, text="文件块映射").pack(anchor=tk.W, pady=(0, 4))
        self.chunks_tree_frame, self.chunks_tree = self._scrollable_tree(
            right,
            ("index", "range", "length", "refs", "active", "hit", "stored", "digest"),
            ("#", "字节范围 [start:end)", "长度", "引用", "有效", "命中", "留存", "摘要"),
            (45, 145, 60, 55, 55, 55, 55, 560),
            height=20,
        )
        self.chunks_tree_frame.pack(fill=tk.BOTH, expand=True)

    def _build_blocks_tab(self) -> None:
        self.blocks_summary_var = tk.StringVar()
        ttk.Label(self.blocks_tab, textvariable=self.blocks_summary_var).pack(
            anchor=tk.W, pady=(0, 6)
        )
        frame, self.blocks_tree = self._scrollable_tree(
            self.blocks_tab,
            ("length", "refs", "reclaimable", "stored", "policy", "created_seq", "digest"),
            ("长度", "引用数", "可回收", "留存", "创建策略", "创建序号", "块摘要"),
            (70, 65, 65, 55, 75, 75, 590),
            height=28,
        )
        frame.pack(fill=tk.BOTH, expand=True)

    def _build_search_tab(self) -> None:
        time_range = self.store.time_range()
        minimum = time_range["min_ts_ms"] if time_range["min_ts_ms"] is not None else 0
        maximum = time_range["max_ts_ms"] if time_range["max_ts_ms"] is not None else 0

        form = ttk.Frame(self.search_tab)
        form.pack(fill=tk.X, pady=(0, 8))
        self.search_area_var = tk.StringVar(value="A")
        self.search_start_var = tk.StringVar(value=str(minimum))
        self.search_end_var = tk.StringVar(value=str(maximum))
        ttk.Label(form, text="台区前缀").pack(side=tk.LEFT)
        ttk.Entry(form, textvariable=self.search_area_var, width=14).pack(
            side=tk.LEFT, padx=(4, 14)
        )
        ttk.Label(form, text="起始毫秒").pack(side=tk.LEFT)
        ttk.Entry(form, textvariable=self.search_start_var, width=18).pack(
            side=tk.LEFT, padx=(4, 14)
        )
        ttk.Label(form, text="结束毫秒").pack(side=tk.LEFT)
        ttk.Entry(form, textvariable=self.search_end_var, width=18).pack(
            side=tk.LEFT, padx=(4, 14)
        )
        ttk.Button(form, text="检索有效记录", command=self.execute_search).pack(
            side=tk.LEFT
        )
        self.search_summary_var = tk.StringVar(value="尚未检索")
        ttk.Label(form, textvariable=self.search_summary_var).pack(
            side=tk.LEFT, padx=16
        )

        paned = ttk.Panedwindow(self.search_tab, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)
        results_frame = ttk.Frame(paned)
        detail_frame = ttk.Frame(paned)
        paned.add(results_frame, weight=3)
        paned.add(detail_frame, weight=1)

        tree_frame, self.search_tree = self._scrollable_tree(
            results_frame,
            ("file_id", "record", "area", "meter", "ts", "chunk", "offset", "digest"),
            ("文件", "记录#", "台区", "电表", "采集毫秒", "块#", "块偏移", "块摘要"),
            (90, 65, 60, 120, 145, 55, 75, 520),
            height=17,
        )
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self.search_tree.bind("<<TreeviewSelect>>", self._show_search_record)

        ttk.Label(detail_frame, text="命中记录内容").pack(anchor=tk.W)
        self.record_text = tk.Text(detail_frame, height=8, wrap=tk.NONE)
        record_scroll = ttk.Scrollbar(
            detail_frame,
            orient=tk.VERTICAL,
            command=self.record_text.yview,
        )
        self.record_text.configure(yscrollcommand=record_scroll.set)
        self.record_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        record_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_audit_tab(self) -> None:
        self.audit_summary_var = tk.StringVar()
        ttk.Label(
            self.audit_tab,
            textvariable=self.audit_summary_var,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 10))
        ttk.Label(self.audit_tab, text="分块策略命中率与去重率").pack(
            anchor=tk.W, pady=(0, 4)
        )
        frame, self.metrics_tree = self._scrollable_tree(
            self.audit_tab,
            (
                "policy",
                "label",
                "target",
                "mask",
                "writes",
                "refs",
                "hits",
                "hit_rate",
                "dedupe_rate",
                "saved",
                "active",
            ),
            (
                "策略ID",
                "标签",
                "目标大小",
                "掩码",
                "写入",
                "块引用",
                "命中",
                "命中率",
                "去重率",
                "节省字节",
                "当前",
            ),
            (75, 130, 85, 60, 65, 75, 70, 80, 80, 95, 55),
            height=12,
        )
        frame.pack(fill=tk.BOTH, expand=True)

    def _selected_file_id(self) -> str | None:
        selection = self.files_tree.selection()
        if not selection:
            return None
        values = self.files_tree.item(selection[0], "values")
        return str(values[0])

    def refresh_files(self) -> None:
        selected = self._selected_file_id()
        self.files_tree.delete(*self.files_tree.get_children())
        rows = self.store.list_files(include_deleted=self.files_include_deleted.get())
        for row in rows:
            values = (
                row["file_id"],
                row["status"],
                row["policy_id"],
                row["record_count"],
                f"{row['active_chunk_count']}/{row['chunk_count']}",
                row["content_size"],
                row["logical_time"],
            )
            item_id = self.files_tree.insert("", tk.END, values=values)
            if row["file_id"] == selected:
                self.files_tree.selection_set(item_id)
                self.files_tree.focus(item_id)
        if selected and self.files_tree.selection():
            self._show_selected_file()

    def _show_selected_file(self, _event: Any = None) -> None:
        file_id = self._selected_file_id()
        self.chunks_tree.delete(*self.chunks_tree.get_children())
        if file_id is None:
            return
        for chunk in self.store.get_file_chunks(file_id, include_deleted=True):
            self.chunks_tree.insert(
                "",
                tk.END,
                values=(
                    chunk["chunk_index"],
                    f"[{chunk['offset']}:{chunk['offset'] + chunk['length']})",
                    chunk["length"],
                    chunk["ref_count"],
                    int(chunk["active"]),
                    int(chunk["hit"]),
                    int(chunk["stored"]),
                    chunk["digest"],
                ),
            )

    def refresh_blocks(self) -> None:
        self.blocks_tree.delete(*self.blocks_tree.get_children())
        blocks = self.store.list_blocks(include_reclaimable=True)
        for block in blocks:
            self.blocks_tree.insert(
                "",
                tk.END,
                values=(
                    block["length"],
                    block["ref_count"],
                    int(block["reclaimable"]),
                    int(block["stored"]),
                    block["created_policy_id"],
                    block["created_seq"],
                    block["digest"],
                ),
            )
        live = sum(1 for block in blocks if block["ref_count"] > 0)
        zero = sum(1 for block in blocks if block["ref_count"] == 0)
        self.blocks_summary_var.set(
            f"块索引 {len(blocks)} 条；存活 {live} 条，归零可回收 {zero} 条。"
        )

    def execute_search(self) -> None:
        try:
            start_ms = int(self.search_start_var.get().strip())
            end_ms = int(self.search_end_var.get().strip())
            rows = self.store.search_records(
                self.search_area_var.get().strip(),
                start_ms,
                end_ms,
            )
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("检索失败", str(exc))
            return

        self.search_results = rows
        self.search_tree.delete(*self.search_tree.get_children())
        for index, row in enumerate(rows):
            self.search_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    row["file_id"],
                    row["record_index"],
                    row["area_id"],
                    row["meter_id"],
                    row["ts_ms"],
                    row["chunk_index"],
                    row["chunk_offset"],
                    row["digest"],
                ),
            )
        self.search_summary_var.set(f"命中 {len(rows)} 条有效逻辑记录")
        self.record_text.delete("1.0", tk.END)

    def _show_search_record(self, _event: Any = None) -> None:
        selection = self.search_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if index >= len(self.search_results):
            return
        row = self.search_results[index]
        payload = {
            "file_id": row["file_id"],
            "record_index": row["record_index"],
            "chunk_index": row["chunk_index"],
            "chunk_offset": row["chunk_offset"],
            "chunk_length": row["chunk_length"],
            "digest": row["digest"],
            "ref_count": row["ref_count"],
            "record": row["record"],
        }
        self.record_text.delete("1.0", tk.END)
        self.record_text.insert(
            "1.0",
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        )

    def refresh_audit(self) -> None:
        occupancy = self.store.occupancy()
        consistency = self.store.consistency_report()
        metrics = self.store.strategy_metrics()
        failed_checks = [check["name"] for check in consistency["checks"] if not check["ok"]]
        consistency_text = "OK" if consistency["ok"] else "FAILED: " + ", ".join(failed_checks)
        self.audit_summary_var.set(
            f"有效文件 {occupancy['active_files']}；逻辑引用 {occupancy['active_mappings']} 条/"
            f"{occupancy['logical_active_bytes']} B；物理存活 {occupancy['physical_live_blocks']} 块/"
            f"{occupancy['physical_live_bytes']} B；去重节省 "
            f"{occupancy['deduplicated_saved_bytes']} B；归零块 "
            f"{occupancy['reclaimable_blocks']} 个/{occupancy['reclaimable_bytes']} B；"
            f"总引用数 {occupancy['total_block_refs']}；一致性 {consistency_text}"
        )

        self.metrics_tree.delete(*self.metrics_tree.get_children())
        for metric in metrics:
            self.metrics_tree.insert(
                "",
                tk.END,
                values=(
                    metric["policy_id"],
                    metric["label"],
                    metric["target_size"],
                    metric["mask_bits"],
                    metric["writes"],
                    metric["chunk_refs"],
                    metric["hit_refs"],
                    f"{metric['hit_rate'] * 100:.2f}%",
                    f"{metric['dedupe_rate'] * 100:.2f}%",
                    metric["saved_bytes"],
                    int(metric["active"]),
                ),
            )

    def refresh_all(self) -> None:
        try:
            self.refresh_files()
            self.refresh_blocks()
            self.refresh_audit()
            events = self.store.list_events()
            self.status_var.set(
                f"事件 {len(events)}；最近刷新序列 "
                f"{events[-1]['seq'] if events else 0}"
            )
        except sqlite3.Error as exc:
            messagebox.showerror("刷新失败", str(exc))

    def _poll_refresh(self) -> None:
        if self.closed:
            return
        self.refresh_files()
        self.refresh_blocks()
        self.refresh_audit()
        self.root.after(2000, self._poll_refresh)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.store.close()
        self.root.destroy()


def launch_diagnostic_view(db_path: str | Path) -> int:
    path = Path(db_path)
    if tk is None or ttk is None:
        print(
            "缺少 tkinter 图形运行库。请先执行：\n"
            "  sudo apt-get update && sudo apt-get install -y python3-tk",
            file=sys.stderr,
        )
        return 2
    if not path.exists():
        print(f"块索引数据库不存在：{path}", file=sys.stderr)
        return 2
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(
            "无法打开图形显示；请在本地桌面或 WSLg 图形会话中运行。\n"
            f"Tk 错误：{exc}",
            file=sys.stderr,
        )
        return 2
    try:
        DiagnosticView(root, path)
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    return 0
