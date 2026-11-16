"""Tkinter diagnostics UI: per-pod score propagation drill-down.

Run:  python -m scheduler_sim.ui
The view is rebuilt wholesale on every state change (weights, node
state, snapshot restore) so stale scores are never overlaid.
"""

import json
import sys
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # headless / tkinter not installed
    tk = None

from .report import render_pod_report
from .session import Session, load_weights

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


if tk is not None:
    class WorkbenchApp(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("K8s Scheduling Simulation Workbench")
            self.geometry("1100x700")
            self.session = Session(load_weights(SAMPLES / "weights.yaml"))
            self.session.load_manifest_files(SAMPLES / "nodes.json",
                                             SAMPLES / "pods.json")
            self.session.run()
            self._build_widgets()
            self.refresh()

        def _build_widgets(self):
            toolbar = ttk.Frame(self)
            toolbar.pack(side=tk.TOP, fill=tk.X)
            ttk.Button(toolbar, text="Reschedule",
                       command=self.on_reschedule).pack(side=tk.LEFT, padx=2)
            ttk.Button(toolbar, text="Apply alt weights",
                       command=self.on_alt_weights).pack(side=tk.LEFT, padx=2)
            ttk.Button(toolbar, text="Save snapshot",
                       command=self.on_save_snapshot).pack(side=tk.LEFT, padx=2)
            ttk.Button(toolbar, text="Restore snapshot",
                       command=self.on_restore).pack(side=tk.LEFT, padx=2)
            self.status = ttk.Label(toolbar, text="")
            self.status.pack(side=tk.RIGHT, padx=6)

            pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
            pane.pack(fill=tk.BOTH, expand=True)

            left = ttk.Frame(pane)
            self.pod_list = tk.Listbox(left, exportselection=False, width=24)
            self.pod_list.pack(fill=tk.BOTH, expand=True)
            self.pod_list.bind("<<ListboxSelect>>", lambda _e: self._show_pod())
            pane.add(left, weight=1)

            right = ttk.Frame(pane)
            columns = ("raw", "weight", "contrib", "bound")
            self.tree = ttk.Treeview(right, columns=columns, show="tree headings")
            self.tree.heading("#0", text="Score propagation path")
            for col, text in zip(columns, ("raw", "weight", "contrib(units)",
                                           "err-bound(units)")):
                self.tree.heading(col, text=text)
                self.tree.column(col, width=110, anchor=tk.E)
            self.tree.pack(fill=tk.BOTH, expand=True)
            pane.add(right, weight=4)

            self.detail = tk.Text(self, height=10, wrap=tk.NONE)
            self.detail.pack(fill=tk.X)

        # -- rendering --------------------------------------------------------
        def refresh(self):
            """Wholesale view rebuild; no incremental overlay of old scores."""
            self.pod_list.delete(0, tk.END)
            for report in self.session.result["reports"]:
                self.pod_list.insert(tk.END, report["pod"])
            self.tree.delete(*self.tree.get_children())
            self.detail.delete("1.0", tk.END)
            s = self.session
            self.status.config(
                text=f"session={s.session_version} nodes={s.nodes_version} "
                     f"pods={s.pod_queue_version} weights="
                     f"{json.dumps(s.weights, sort_keys=True)}")
            if self.session.result["reports"]:
                self.pod_list.selection_set(0)
                self._show_pod()

        def _show_pod(self):
            sel = self.pod_list.curselection()
            if not sel:
                return
            report = self.session.result["reports"][sel[0]]
            self.tree.delete(*self.tree.get_children())
            for score in report["scores"]:
                mark = " <== WINNER" if score["node"] == report["winner"] else ""
                node_id = self.tree.insert(
                    "", tk.END,
                    text=f"{score['node']}  score={score['score_points']}{mark}",
                    values=("", "", score["score_units"],
                            score["error_bound_units"]))
                for plugin in score["plugins"]:
                    if plugin["status"] == "ok":
                        contrib = plugin["contribution_units"]
                        self.tree.insert(
                            node_id, tk.END, text=plugin["plugin"],
                            values=(plugin["raw"], plugin["weight"],
                                    "-" if contrib is None else contrib, ""))
                    else:
                        self.tree.insert(
                            node_id, tk.END,
                            text=f"{plugin['plugin']} UNAVAILABLE",
                            values=("", plugin["weight"], "excluded", ""))
            self.detail.delete("1.0", tk.END)
            self.detail.insert(tk.END, "\n".join(render_pod_report(report)))

        # -- actions ----------------------------------------------------------
        def on_reschedule(self):
            self.session.run()
            self.refresh()

        def on_alt_weights(self):
            self.session.weights = load_weights(SAMPLES / "weights_alt.yaml")
            self.session.run()
            self.refresh()

        def on_save_snapshot(self):
            path = filedialog.asksaveasfilename(defaultextension=".json")
            if path:
                self.session.save_snapshot(path)
                messagebox.showinfo("snapshot", f"saved: {path}")

        def on_restore(self):
            path = filedialog.askopenfilename()
            if not path:
                return
            try:
                actions = self.session.restore(path)
            except Exception as exc:  # business errors carry their code
                messagebox.showerror("restore failed", str(exc))
                return
            self.refresh()
            messagebox.showinfo("restore", "\n".join(actions))


def main():
    if tk is None:
        sys.exit("tkinter is not available; install Python with tk "
                 "support (on Windows use the python.org installer, "
                 "on Debian/Ubuntu: sudo apt install python3-tk)")
    app = WorkbenchApp()
    app.mainloop()


if __name__ == "__main__":
    main()
