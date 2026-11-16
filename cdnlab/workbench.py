"""Tkinter diagnostic workbench.

Displays, with stable render ordering:
  - per-edge cache entries (key, ETag, Last-Modified, entry version, TTL)
  - eviction events
  - request trace with hit attribution and consistency verdicts
  - current config version and the snapshot list

The UI is a read-only viewer over a session SQLite database plus (optionally)
a live orchestrator. Launch: python -m cdnlab.workbench --db <session.sqlite>
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from .config import load_policy
from .session import SessionStore


class WorkbenchApp(tk.Tk):
    def __init__(self, db_path, config_path=None):
        super().__init__()
        self.title("CDN Edge Cache Consistency Workbench")
        self.geometry("1100x700")
        self.db_path = db_path
        self.config_path = config_path
        self.store = SessionStore(db_path)
        self._build_widgets()
        self.refresh()

    # ------------------------------------------------------------------ UI ---
    def _build_widgets(self):
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        self.config_var = tk.StringVar(value="config version: -")
        ttk.Label(top, textvariable=self.config_var, font=("TkDefaultFont", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side=tk.RIGHT)

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        # Left: cache entries per edge node.
        left = ttk.LabelFrame(paned, text="Edge cache entries")
        paned.add(left, weight=1)
        columns = ("node", "key", "etag", "entry_version", "remaining_ttl", "stale")
        self.cache_tree = ttk.Treeview(left, columns=columns, show="headings", height=12)
        for col, width in zip(columns, (70, 220, 150, 60, 60, 50)):
            self.cache_tree.heading(col, text=col)
            self.cache_tree.column(col, width=width, anchor=tk.W)
        self.cache_tree.pack(fill=tk.BOTH, expand=True)

        evict_frame = ttk.LabelFrame(left, text="Eviction events")
        evict_frame.pack(fill=tk.BOTH, expand=True)
        self.evict_tree = ttk.Treeview(
            evict_frame, columns=("seq", "node", "key", "rule", "tick"),
            show="headings", height=6)
        for col, width in zip(("seq", "node", "key", "rule", "tick"), (50, 70, 240, 60, 50)):
            self.evict_tree.heading(col, text=col)
            self.evict_tree.column(col, width=width, anchor=tk.W)
        self.evict_tree.pack(fill=tk.BOTH, expand=True)

        # Right: request trace + snapshots.
        right = ttk.Frame(paned)
        paned.add(right, weight=2)

        trace_frame = ttk.LabelFrame(right, text="Request trace / hit attribution / consistency")
        trace_frame.pack(fill=tk.BOTH, expand=True)
        columns = ("seq", "tick", "kind", "node", "action", "outcome", "etag", "code", "consistency")
        self.trace_tree = ttk.Treeview(trace_frame, columns=columns, show="headings")
        widths = (50, 50, 80, 70, 200, 110, 150, 90, 110)
        for col, width in zip(columns, widths):
            self.trace_tree.heading(col, text=col)
            self.trace_tree.column(col, width=width, anchor=tk.W)
        scrollbar = ttk.Scrollbar(trace_frame, orient=tk.VERTICAL, command=self.trace_tree.yview)
        self.trace_tree.configure(yscrollcommand=scrollbar.set)
        self.trace_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.trace_tree.tag_configure("stale", background="#ffe0e0")
        self.trace_tree.tag_configure("injection", background="#fff6cc")

        snap_frame = ttk.LabelFrame(right, text="Snapshots")
        snap_frame.pack(fill=tk.X, pady=4)
        self.snap_tree = ttk.Treeview(
            snap_frame, columns=("id", "name", "config_version", "last_seq"),
            show="headings", height=4)
        for col, width in zip(("id", "name", "config_version", "last_seq"), (40, 200, 160, 70)):
            self.snap_tree.heading(col, text=col)
            self.snap_tree.column(col, width=width, anchor=tk.W)
        self.snap_tree.pack(fill=tk.X)

    # --------------------------------------------------------------- data ---
    def refresh(self):
        try:
            self._refresh_config()
            self._refresh_trace()
            self._refresh_evictions()
            self._refresh_snapshots()
            self._refresh_cache_entries()
        except sqlite3.Error as exc:
            messagebox.showerror("database error", str(exc))

    def _refresh_config(self):
        sid = self.store.latest_session_id()
        version = self.store.session_config_version(sid) if sid else None
        self.config_var.set(f"config version: {version or '-'}   session: {sid or '-'}")

    def _refresh_trace(self):
        self.trace_tree.delete(*self.trace_tree.get_children())
        sid = self.store.latest_session_id()
        if not sid:
            return
        for row in self.store.load_trace(sid):  # ordered by seq: stable
            result = row["result"]
            outcome = result.get("outcome", "") if row["kind"] == "request" else "<injection>"
            etag = result.get("etag", "")
            tags = ()
            if row["kind"] == "injection":
                tags = ("injection",)
            elif result.get("stale") or result.get("consistency") == "STALE_SERVED":
                tags = ("stale",)
            self.trace_tree.insert("", tk.END, values=(
                row["seq"], row["tick"], row["kind"], row["node"] or "",
                row["action"], outcome, etag, row["code"] or "",
                row["consistency"] or "",
            ), tags=tags)

    def _refresh_evictions(self):
        self.evict_tree.delete(*self.evict_tree.get_children())
        sid = self.store.latest_session_id()
        if not sid:
            return
        for ev in self.store.load_evictions(sid):  # ordered by seq: stable
            self.evict_tree.insert("", tk.END, values=(
                ev["seq"], ev["node"], ev["key"], ev["rule"], ev["tick"]))

    def _refresh_snapshots(self):
        self.snap_tree.delete(*self.snap_tree.get_children())
        for snap in self.store.list_snapshots():  # ordered by id: stable
            self.snap_tree.insert("", tk.END, values=(
                snap["id"], snap["name"], snap["config_version"], snap["last_seq"]))

    def _refresh_cache_entries(self):
        self.cache_tree.delete(*self.cache_tree.get_children())
        snapshots = self.store.list_snapshots()
        if not snapshots:
            return
        snapshot = self.store.load_snapshot(snapshots[-1]["id"])
        if not snapshot:
            return
        tick = snapshot["state"]["tick"]
        for node in sorted(snapshot["state"]["edges"]):  # stable node order
            entries = snapshot["state"]["edges"][node]["entries"]
            for entry in sorted(entries, key=lambda e: e["key"]):  # stable key order
                remaining = max(0, entry["ttl"] - (tick - entry["write_tick"]))
                self.cache_tree.insert("", tk.END, values=(
                    node, entry["key"], entry["etag"], entry["entry_version"],
                    remaining, "STALE" if entry["stale"] else "",
                ))


def main(argv=None):
    parser = argparse.ArgumentParser(description="CDN cache consistency workbench")
    parser.add_argument("--db", default=os.path.join("artifacts", "demo", "scenario4_restore_replay.sqlite"))
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    if not os.path.exists(args.db):
        print(f"session database not found: {args.db}", file=sys.stderr)
        print("run `python -m cdnlab.demo` first to produce a session.", file=sys.stderr)
        return 2
    app = WorkbenchApp(args.db, args.config)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
