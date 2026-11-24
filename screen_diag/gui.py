from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional


def _show_in_file_manager(path: Path) -> None:
    path = path.resolve()
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


class ShardViewer:
    def __init__(self, master: Any, path: Path):
        import tkinter as tk
        from tkinter import ttk

        self.window = tk.Toplevel(master)
        self.window.title(f"Shard: {path.name}")
        self.window.geometry("900x600")
        ttk.Label(self.window, text=str(path), padding=8).pack(anchor="w")
        button_bar = ttk.Frame(self.window, padding=(8, 0))
        button_bar.pack(fill="x")
        ttk.Button(button_bar, text="Reveal in file manager", command=lambda: _show_in_file_manager(path)).pack(side="left")

        frame = ttk.Frame(self.window, padding=8)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="none")
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        try:
            content = path.read_text(encoding="utf-8")
            try:
                parsed = json.loads(content)
                content = json.dumps(parsed, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
        except OSError as exc:
            content = f"Cannot read shard: {exc}"
        text.insert("1.0", content)
        text.configure(state="disabled")


class DiagnosticApp:
    def __init__(self, root: Any, samples_dir: str = "samples"):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.root.title("Multi-screen Casting Replay Diagnostic")
        self.root.geometry("1180x760")
        self.samples_var = tk.StringVar(value=samples_dir)
        self.workers_var = tk.IntVar(value=4)
        self.budget_var = tk.IntVar(value=32 * 1024 * 1024)
        self.status_var = tk.StringVar(value="Select a deterministic sample directory and run diagnosis.")
        self.report: Optional[Dict[str, Any]] = None

        self._build_toolbar()
        self._build_tables()
        self.status = ttk.Label(root, textvariable=self.status_var, relief="sunken", anchor="w", padding=6)
        self.status.pack(fill="x", side="bottom")

    def _build_toolbar(self) -> None:
        ttk = self.ttk
        bar = ttk.Frame(self.root, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, text="Samples").pack(side="left")
        ttk.Entry(bar, textvariable=self.samples_var, width=55).pack(side="left", padx=6)
        ttk.Button(bar, text="Browse...", command=self._browse).pack(side="left")
        ttk.Label(bar, text="Workers").pack(side="left", padx=(12, 2))
        ttk.Spinbox(bar, from_=1, to=16, textvariable=self.workers_var, width=5).pack(side="left")
        ttk.Label(bar, text="Memory bytes").pack(side="left", padx=(12, 2))
        ttk.Entry(bar, textvariable=self.budget_var, width=12).pack(side="left")
        ttk.Button(bar, text="Run diagnosis", command=self._run_async).pack(side="left", padx=12)
        ttk.Button(bar, text="Open selected shard", command=self._open_selected_shard).pack(side="left")

    def _build_tables(self) -> None:
        ttk = self.ttk
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.screen_tree = self._make_tree(
            notebook,
            "Screens",
            ("canonical_id", "status", "generation", "keyframe_seq", "decoded_frame_seq", "play_position_ms", "logic_ms", "shard_id"),
        )
        self.screen_tree.bind("<Double-1>", lambda _event: self._open_selected_shard())

        self.conflict_tree = self._make_tree(
            notebook,
            "Conflicts",
            ("logic_ms", "canonical_screen_id", "field", "selected_value", "adjudication", "shard_id", "source_locator"),
        )
        self.conflict_tree.bind("<Double-1>", lambda _event: self._open_selected_shard())

        self.decision_tree = self._make_tree(
            notebook,
            "Decisions",
            ("event_seq", "screen_id", "logic_ms", "verdict", "late_by_ms", "generation_before", "generation_after", "rebuild_keyframe_seq", "reason"),
        )

        self.shard_tree = self._make_tree(
            notebook,
            "Shards",
            ("shard_id", "valid", "logic_ms", "actual_screen_count", "declared_screen_count", "actual_digest", "path"),
        )
        self.shard_tree.bind("<Double-1>", lambda _event: self._open_selected_shard())

        report_frame = ttk.Frame(notebook)
        notebook.add(report_frame, text="Report JSON")
        self.report_text = self.tk.Text(report_frame, wrap="none")
        report_scroll = ttk.Scrollbar(report_frame, orient="vertical", command=self.report_text.yview)
        self.report_text.configure(yscrollcommand=report_scroll.set)
        self.report_text.pack(side="left", fill="both", expand=True)
        report_scroll.pack(side="right", fill="y")

    def _make_tree(self, parent: Any, title: str, columns: tuple[str, ...]) -> Any:
        frame = self.ttk.Frame(parent)
        parent.add(frame, text=title)
        tree = self.ttk.Treeview(frame, columns=columns, show="headings")
        tree.column_names = columns
        for column in columns:
            tree.heading(column, text=column)
            width = 150 if column in {"source_locator", "path", "reason", "canonical_id"} else 105
            tree.column(column, width=width, anchor="w")
        y_scroll = self.ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        x_scroll = self.ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _browse(self) -> None:
        from tkinter import filedialog

        path = filedialog.askdirectory(initialdir=self.samples_var.get() or ".")
        if path:
            self.samples_var.set(path)

    def _run_async(self) -> None:
        self.status_var.set("Loading and validating...")
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _run(self) -> None:
        try:
            from .pipeline import analyze_sample

            analysis = analyze_sample(
                self.samples_var.get(),
                max_workers=int(self.workers_var.get()),
                memory_budget_bytes=int(self.budget_var.get()),
            )
            self.root.after(0, lambda: self._render(analysis["report"]))
        except Exception as exc:  # GUI should remain alive for another attempt.
            message = str(exc)
            self.root.after(0, lambda: self.status_var.set(f"E_SCHEMA/error: {message}"))

    def _clear_tree(self, tree: Any) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _render(self, report: Dict[str, Any]) -> None:
        self.report = report
        for tree in (self.screen_tree, self.conflict_tree, self.decision_tree, self.shard_tree):
            self._clear_tree(tree)

        for canonical_id, winner in report.get("merge", {}).get("current", {}).items():
            self.screen_tree.insert(
                "",
                "end",
                iid=canonical_id,
                values=(
                    canonical_id,
                    "tombstone" if winner.get("tombstone") else "playing",
                    winner.get("generation"),
                    winner.get("keyframe_seq"),
                    winner.get("decoded_frame_seq"),
                    winner.get("play_position_ms"),
                    winner.get("logic_ms"),
                    winner.get("shard_id") or "v1",
                ),
            )

        for conflict in report.get("merge", {}).get("conflicts", []):
            self.conflict_tree.insert(
                "",
                "end",
                values=(
                    conflict.get("logic_ms"),
                    conflict.get("canonical_screen_id"),
                    conflict.get("field"),
                    conflict.get("selected_value"),
                    conflict.get("adjudication"),
                    conflict.get("shard_id"),
                    conflict.get("source_locator"),
                ),
            )

        for decision in report.get("replay", {}).get("decisions", []):
            self.decision_tree.insert(
                "",
                "end",
                values=(
                    decision.get("event_seq"),
                    decision.get("screen_id"),
                    decision.get("logic_ms"),
                    decision.get("verdict"),
                    decision.get("late_by_ms"),
                    decision.get("generation_before"),
                    decision.get("generation_after"),
                    decision.get("rebuild_keyframe_seq"),
                    decision.get("reason"),
                ),
            )

        for shard in report.get("merge", {}).get("shards", []):
            digest = shard.get("actual_digest") or ""
            self.shard_tree.insert(
                "",
                "end",
                iid=str(shard.get("shard_id")),
                values=(
                    shard.get("shard_id"),
                    shard.get("valid"),
                    shard.get("logic_ms"),
                    shard.get("actual_screen_count"),
                    shard.get("declared_screen_count"),
                    digest[:16],
                    shard.get("path"),
                ),
            )

        self.report_text.configure(state="normal")
        self.report_text.delete("1.0", "end")
        self.report_text.insert("1.0", json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        self.report_text.configure(state="disabled")
        stats = report.get("statistics", {})
        self.status_var.set(
            f"{report.get('status')} | decisions={stats.get('decision_counts')} | "
            f"conflicts={stats.get('conflict_count')} | digest_failures={stats.get('digest_failure_count')}"
        )

    def _selected_locator(self) -> Optional[Path]:
        if self.report is None:
            return None
        for tree in (self.screen_tree, self.conflict_tree, self.shard_tree):
            selection = tree.selection()
            if not selection:
                continue
            selected_item = tree.item(selection[0])
            values = selected_item.get("values", ())
            column_names = getattr(tree, "column_names", ())
            value_by_column = dict(zip(column_names, values))

            for column_name in ("source_locator", "path"):
                text = str(value_by_column.get(column_name, ""))
                if not text or text == "None":
                    continue
                path = Path(text.split("#", 1)[0])
                if path.suffix == ".json" and path.exists():
                    return path

            shard_id = value_by_column.get("shard_id")
            for shard in self.report.get("merge", {}).get("shards", []):
                if shard.get("shard_id") == shard_id and shard.get("path"):
                    path = Path(shard["path"])
                    if path.exists():
                        return path
        return None

    def _open_selected_shard(self) -> None:
        path = self._selected_locator()
        if path is None:
            self.status_var.set("Select a row with a valid shard path first.")
            return
        ShardViewer(self.root, path)


def launch_gui(samples_dir: str = "samples") -> int:
    try:
        import tkinter as tk
    except ImportError as exc:
        print(f"Tkinter is unavailable in this Python installation: {exc}", file=sys.stderr)
        print("On Windows standard Python 3.11 includes Tkinter.", file=sys.stderr)
        return 1
    root = tk.Tk()
    DiagnosticApp(root, samples_dir=samples_dir)
    root.mainloop()
    return 0
