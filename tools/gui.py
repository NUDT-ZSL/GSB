#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from verify import JOURNAL_NAMES, Verification, read_journal, verify_session


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tkinter dashboard for message conversion and delivery state")
    parser.add_argument("--data-dir", type=Path, default=Path(".runtime/demo-data"))
    parser.add_argument("--interval", type=float, default=1.0, help="auto-refresh interval in seconds")
    parser.add_argument("--check", action="store_true", help="parse data and print a summary without opening a window")
    parser.add_argument("--smoke-test", action="store_true", help="render once, print row counts, then close")
    return parser.parse_args()


def compact(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_dashboard(data_dir: Path) -> dict[str, Any]:
    verification = Verification()
    journals = {
        name: read_journal(data_dir / filename, verification)
        for name, filename in JOURNAL_NAMES.items()
    }
    deliveries = sorted(journals["deliveries"], key=lambda item: item[0])
    messages = sorted(journals["messages"], key=lambda item: item[0])
    acks = sorted(journals["acks"], key=lambda item: item[0])
    offsets: dict[str, Any] = {}
    offsets_path = data_dir / "consumer-offsets.json"
    if offsets_path.exists():
        try:
            offsets = json.loads(offsets_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            offsets = {}
    recovery: dict[str, Any] = {}
    recovery_path = data_dir / "recovery-report.json"
    if recovery_path.exists():
        try:
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            recovery = {}
    return {
        "messages": messages,
        "deliveries": deliveries,
        "acks": acks,
        "dedup": journals["dedup"],
        "offsets": offsets,
        "recovery": recovery,
        "journal_errors": verification.errors,
    }


def delivery_rows(data: dict[str, Any]) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for event_seq, record in data["deliveries"]:
        conversion = record.get("conversion", {})
        semantic = record.get("semantic", {})
        rows.append(
            (
                str(event_seq),
                str(record.get("delivery_id", "")),
                str(record.get("consumer_id", "")),
                f"v{conversion.get('source_version', '?')}",
                f"v{conversion.get('declared_version', '?')}",
                str(conversion.get("direction", "")),
                ",".join(conversion.get("stripped_fields", [])) or "-",
                ",".join(conversion.get("filled_fields", [])) or "-",
                str(record.get("consumer_offset", "")),
                str(semantic.get("mode", "")),
                str(semantic.get("decision", "")),
                str(semantic.get("retry_count", "")),
            )
        )
    return rows


def run_gui(data_dir: Path, interval: float, smoke_test: bool = False) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ModuleNotFoundError:
        print(
            "Tkinter is not installed. Install python3-tk on Linux, or use the official Windows Python build which includes Tk.",
            file=sys.stderr,
        )
        return 2

    root = tk.Tk()
    root.title("Message Interop Middleware - 本地投递观察台")
    root.geometry("1280x760")
    root.minsize(980, 600)
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    elif "clam" in style.theme_names():
        style.theme_use("clam")

    path_var = tk.StringVar(value=str(data_dir.resolve()))
    status_var = tk.StringVar(value="准备就绪")
    auto_var = tk.BooleanVar(value=True)

    toolbar = ttk.Frame(root, padding=(8, 6))
    toolbar.pack(fill="x")
    ttk.Label(toolbar, text="数据目录").pack(side="left")
    ttk.Entry(toolbar, textvariable=path_var, width=72).pack(side="left", padx=6)
    refresh_button = ttk.Button(toolbar, text="刷新")
    refresh_button.pack(side="left", padx=3)
    ttk.Checkbutton(toolbar, text="自动刷新", variable=auto_var).pack(side="left", padx=6)
    verify_button = ttk.Button(toolbar, text="运行验证")
    verify_button.pack(side="left", padx=3)
    ttk.Label(toolbar, textvariable=status_var).pack(side="right")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    delivery_frame = ttk.Frame(notebook)
    message_frame = ttk.Frame(notebook)
    consumer_frame = ttk.Frame(notebook)
    recovery_frame = ttk.Frame(notebook)
    notebook.add(delivery_frame, text="投递流")
    notebook.add(message_frame, text="已接收消息")
    notebook.add(consumer_frame, text="消费者偏移")
    notebook.add(recovery_frame, text="恢复与验证")

    delivery_columns = (
        "event_seq",
        "delivery_id",
        "consumer",
        "source_version",
        "declared_version",
        "direction",
        "stripped",
        "filled",
        "offset",
        "semantics",
        "decision",
        "retry",
    )
    delivery_headings = {
        "event_seq": "日志序号",
        "delivery_id": "投递标识",
        "consumer": "消费者",
        "source_version": "源版本",
        "declared_version": "声明版本",
        "direction": "转换方向",
        "stripped": "被剥离字段",
        "filled": "补齐字段",
        "offset": "消费者偏移",
        "semantics": "投递语义",
        "decision": "语义判定",
        "retry": "重试计数",
    }
    delivery_tree = ttk.Treeview(delivery_frame, columns=delivery_columns, show="headings", height=20)
    for column in delivery_columns:
        delivery_tree.heading(column, text=delivery_headings[column])
        width = 240 if column in {"stripped", "filled", "delivery_id"} else 110
        delivery_tree.column(column, width=width, stretch=False, anchor="w")
    delivery_scroll_y = ttk.Scrollbar(delivery_frame, orient="vertical", command=delivery_tree.yview)
    delivery_scroll_x = ttk.Scrollbar(delivery_frame, orient="horizontal", command=delivery_tree.xview)
    delivery_tree.configure(yscrollcommand=delivery_scroll_y.set, xscrollcommand=delivery_scroll_x.set)
    delivery_tree.grid(row=0, column=0, sticky="nsew")
    delivery_scroll_y.grid(row=0, column=1, sticky="ns")
    delivery_scroll_x.grid(row=1, column=0, sticky="ew")
    delivery_frame.rowconfigure(0, weight=1)
    delivery_frame.columnconfigure(0, weight=1)

    message_columns = ("event_seq", "delivery_id", "version", "semantics", "partition", "body")
    message_tree = ttk.Treeview(message_frame, columns=message_columns, show="headings", height=20)
    for column, heading, width in (
        ("event_seq", "日志序号", 100),
        ("delivery_id", "投递标识", 220),
        ("version", "源版本", 90),
        ("semantics", "语义", 120),
        ("partition", "分区键", 160),
        ("body", "消息体", 620),
    ):
        message_tree.heading(column, text=heading)
        message_tree.column(column, width=width, stretch=column == "body", anchor="w")
    message_scroll_y = ttk.Scrollbar(message_frame, orient="vertical", command=message_tree.yview)
    message_scroll_x = ttk.Scrollbar(message_frame, orient="horizontal", command=message_tree.xview)
    message_tree.configure(yscrollcommand=message_scroll_y.set, xscrollcommand=message_scroll_x.set)
    message_tree.grid(row=0, column=0, sticky="nsew")
    message_scroll_y.grid(row=0, column=1, sticky="ns")
    message_scroll_x.grid(row=1, column=0, sticky="ew")
    message_frame.rowconfigure(0, weight=1)
    message_frame.columnconfigure(0, weight=1)

    consumer_columns = ("consumer", "version", "partition", "next_offset", "acked_offset", "pending", "completed")
    consumer_tree = ttk.Treeview(consumer_frame, columns=consumer_columns, show="headings", height=16)
    for column, heading in (
        ("consumer", "消费者"),
        ("version", "声明版本"),
        ("partition", "分区过滤"),
        ("next_offset", "下一个偏移"),
        ("acked_offset", "已确认偏移"),
        ("pending", "未完成"),
        ("completed", "已完成"),
    ):
        consumer_tree.heading(column, text=heading)
        consumer_tree.column(column, width=160, anchor="w")
    consumer_tree.pack(fill="both", expand=True, padx=4, pady=4)

    report_text = tk.Text(recovery_frame, wrap="none", font=("Consolas", 10))
    report_scroll_y = ttk.Scrollbar(recovery_frame, orient="vertical", command=report_text.yview)
    report_scroll_x = ttk.Scrollbar(recovery_frame, orient="horizontal", command=report_text.xview)
    report_text.configure(yscrollcommand=report_scroll_y.set, xscrollcommand=report_scroll_x.set)
    report_text.grid(row=0, column=0, sticky="nsew")
    report_scroll_y.grid(row=0, column=1, sticky="ns")
    report_scroll_x.grid(row=1, column=0, sticky="ew")
    recovery_frame.rowconfigure(0, weight=1)
    recovery_frame.columnconfigure(0, weight=1)

    def render() -> None:
        current_dir = Path(path_var.get())
        try:
            data = load_dashboard(current_dir)
        except OSError as exc:
            status_var.set(f"读取失败: {exc}")
            return
        for tree in (delivery_tree, message_tree, consumer_tree):
            children = tree.get_children()
            if children:
                tree.delete(*children)
        for row in delivery_rows(data):
            delivery_tree.insert("", "end", values=row)
        for event_seq, message in data["messages"]:
            message_tree.insert(
                "",
                "end",
                values=(
                    event_seq,
                    message.get("delivery_id", ""),
                    f"v{message.get('version', '?')}",
                    message.get("semantics", ""),
                    message.get("partition_key", ""),
                    compact(message.get("body")),
                ),
            )
        consumers = (data.get("offsets") or {}).get("consumers", {})
        for consumer_id in sorted(consumers):
            state = consumers[consumer_id]
            consumer_tree.insert(
                "",
                "end",
                values=(
                    consumer_id,
                    f"v{state.get('declared_version', '?')}",
                    state.get("partition_key", "") or "-",
                    state.get("next_offset", ""),
                    state.get("acked_offset", ""),
                    state.get("pending", ""),
                    state.get("completed", ""),
                ),
            )
        combined = {
            "journal_errors": data["journal_errors"],
            "recovery_report": data["recovery"],
            "consumer_offsets": data["offsets"],
            "ack_events": len(data["acks"]),
            "dedup_entries": len(data["dedup"]),
        }
        report_text.delete("1.0", "end")
        report_text.insert("1.0", json.dumps(combined, ensure_ascii=False, indent=2, sort_keys=True))
        problem = "；日志问题 " + str(len(data["journal_errors"])) if data["journal_errors"] else ""
        status_var.set(
            f"消息 {len(data['messages'])} / 投递 {len(data['deliveries'])} / 确认 {len(data['acks'])}"
            f" / 去重 {len(data['dedup'])}{problem}"
        )

    def verify() -> None:
        try:
            result = verify_session(Path(path_var.get()))
        except OSError as exc:
            messagebox.showerror("验证失败", str(exc))
            return
        if result["ok"]:
            messagebox.showinfo("验证通过", json.dumps(result["summary"], ensure_ascii=False, indent=2, sort_keys=True))
        else:
            messagebox.showerror("E_VERIFY", json.dumps(result["errors"], ensure_ascii=False, indent=2, sort_keys=True))

    def tick() -> None:
        if auto_var.get():
            render()
        root.after(max(200, int(interval * 1000)), tick)

    refresh_button.configure(command=render)
    verify_button.configure(command=verify)
    render()
    if smoke_test:
        root.update_idletasks()
        print(
            json.dumps(
                {
                    "gui_smoke": True,
                    "delivery_rows": len(delivery_tree.get_children()),
                    "message_rows": len(message_tree.get_children()),
                    "consumer_rows": len(consumer_tree.get_children()),
                    "window": [root.winfo_width(), root.winfo_height()],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        root.after(150, root.destroy)
    root.after(max(200, int(interval * 1000)), tick)
    root.mainloop()
    return 0


def main() -> int:
    args = parse_args()
    if args.check:
        data = load_dashboard(args.data_dir)
        summary = {
            "data_dir": str(args.data_dir),
            "messages": len(data["messages"]),
            "deliveries": len(data["deliveries"]),
            "acks": len(data["acks"]),
            "dedup_entries": len(data["dedup"]),
            "consumers": sorted((data.get("offsets") or {}).get("consumers", {}).keys()),
            "journal_errors": data["journal_errors"],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not data["journal_errors"] else 1
    return run_gui(args.data_dir, args.interval, args.smoke_test)


if __name__ == "__main__":
    sys.exit(main())
