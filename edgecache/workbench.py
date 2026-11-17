from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .config import ContentState, Policy
from .controller import CacheController, NodeEndpoint
from .errors import EdgeError, StateError, error_payload, exit_code_for
from .ledger import Ledger
from .orchestrator import PROJECT_ROOT, LocalOrchestrator
from .snapshot import SnapshotService
from .tcp import rpc
from .util import monotonic_ms, read_json_file

try:  # Importing the model layer must work in headless test environments.
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
except ImportError:  # pragma: no cover - exercised on the current Linux image
    tk = None
    ttk = None
    messagebox = None
    simpledialog = None


def _seed_requests() -> list[dict[str, Any]]:
    return [
        {"path": "/index.html", "key_values": {"tenant": "public", "encoding": "identity"}},
        {"path": "/assets/base.css", "key_values": {"tenant": "public", "encoding": "identity"}},
        {"path": "/assets/theme.css", "key_values": {"tenant": "public", "encoding": "identity"}},
        {"path": "/assets/app.css", "key_values": {"tenant": "public", "encoding": "identity"}},
        {
            "path": "/assets/app.manifest.json",
            "key_values": {"tenant": "public", "encoding": "identity"},
        },
        {"path": "/assets/base.css", "key_values": {"tenant": "compact", "encoding": "identity"}},
    ]


class WorkbenchModel:
    """Stable, renderer-independent projection of the ledger."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def build(self, node_states: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        active = self.ledger.active_state()
        now = monotonic_ms()
        if node_states is not None:
            entries = [
                dict(entry)
                for state in node_states
                for entry in state.get("entries", [])
            ]
            evictions = [
                dict(event)
                for state in node_states
                for event in state.get("evictions", [])
            ]
        else:
            entries = []
            for row in self.ledger.list_entries():
                remaining = max(
                    0,
                    int(row["remaining_ttl_ms"]) - max(0, now - int(row["ttl_anchor_mono_ms"])),
                )
                row = dict(row)
                row["remaining_ttl_ms"] = remaining
                row["key_fields"] = json.loads(row["key_fields_json"])
                entries.append(row)
            evictions = self.ledger.list_evictions(limit=1000)

        entries = sorted(
            entries,
            key=lambda item: (
                str(item["node_id"]),
                str(item["path"]),
                str(item["cache_key"]),
            ),
        )
        evictions = sorted(
            evictions,
            key=lambda item: (
                str(item.get("node_id", "")),
                int(item.get("eviction_id", 0)),
            ),
        )
        propagation: list[dict[str, Any]] = []
        for batch in self.ledger.propagation_batches():
            for trace in batch["traces"]:
                propagation.append(
                    {
                        "batch_id": batch["batch_id"],
                        "ledger_version": batch["ledger_version"],
                        "seq": trace["seq"],
                        "trigger_path": trace["trigger_path"],
                        "affected_path": trace["affected_path"],
                        "depth": trace["depth"],
                        "relation": trace["relation"],
                    }
                )
        propagation.sort(
            key=lambda item: (
                int(item["ledger_version"]),
                str(item["batch_id"]),
                int(item["seq"]),
            )
        )
        content = [
            {
                "path": path,
                "etag": value.etag,
                "sources": list(value.sources),
                "rule": value.rule,
                "media_type": value.media_type,
            }
            for path, value in sorted(active.content.effective.items())
        ]
        return {
            "ledger_version": active.ledger_version,
            "policy_version": active.policy_version,
            "content_set_hash": active.content_set_hash,
            "entries": entries,
            "evictions": evictions,
            "propagation": propagation,
            "ledger_history": self.ledger.ledger_history(),
            "content": content,
        }


class WorkbenchRuntime:
    """Owns a local demo process group when the GUI requests one."""

    def __init__(
        self,
        state_dir: str,
        base_port: int,
        policy_path: str,
        policy_v2_path: str,
        content_path: str,
    ) -> None:
        self.state_dir = state_dir
        self.base_port = base_port
        self.policy_path = policy_path
        self.policy_v2_path = policy_v2_path
        self.content_path = content_path
        self.orch = LocalOrchestrator(state_dir, base_port)
        self.ledger = Ledger(self.orch.db_path)
        self.controller: CacheController | None = None
        self.running = False

    def start(self, reset: bool = True) -> str:
        self.orch.prepare(reset)
        self.orch.preflight_ports()
        self.orch.start_all(self.policy_path, self.content_path)
        self.ledger = Ledger(self.orch.db_path)
        self.controller = CacheController(
            self.ledger,
            "127.0.0.1",
            self.orch.source_port,
            [
                NodeEndpoint(node_id, "127.0.0.1", port)
                for node_id, port in self.orch.node_ports.items()
            ],
            str(self.orch.snapshot_dir),
        )
        self.controller.fill_all(_seed_requests())
        self.running = True
        return f"已启动 source:{self.orch.source_port} nodes:{list(self.orch.node_ports.values())}"

    def snapshot(self) -> dict[str, Any]:
        if self.controller is None:
            raise StateError("system is not running")
        return self.controller.snapshot("workbench-latest.json")

    def evolve(self, path: str, etag: str, body: str) -> dict[str, Any]:
        if self.controller is None:
            raise StateError("system is not running")
        return self.controller.evolve_content(path, etag=etag, body=body)

    def apply_policy_v2(self) -> dict[str, Any]:
        if self.controller is None:
            raise StateError("system is not running")
        policy_doc = read_json_file(self.policy_v2_path)
        return self.controller.apply_policy(policy_doc, _seed_requests())

    def latest_snapshot(self) -> Path:
        service = SnapshotService(self.ledger, self.orch.snapshot_dir)
        candidates = [
            Path(item["path"])
            for item in service.list_snapshots()
            if not item["backup"] and Path(item["path"]).suffix == ".json"
        ]
        if not candidates:
            raise StateError("no writable snapshot is available to restore")
        return sorted(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))[-1]

    def restore_latest(self) -> dict[str, Any]:
        path = self.latest_snapshot()
        return SnapshotService(self.ledger, self.orch.snapshot_dir).restore(path)

    def force_recover_edge_c(self) -> dict[str, Any]:
        if self.controller is None:
            raise StateError("system is not running")
        version = self.ledger.current_version()
        endpoint = self.controller.require_node("edge-c")
        orphan = rpc(
            endpoint.host,
            endpoint.port,
            {
                "op": "simulate_orphan_write",
                "ledger_version": version,
                "path": "/assets/app.css",
                "key_values": {"tenant": "public", "encoding": "identity"},
            },
        )
        killed = self.orch.kill("edge-c", reason="workbench_forced_termination")
        self.orch.start_node("edge-c")
        restarted = rpc(endpoint.host, endpoint.port, {"op": "status"}, timeout=10)
        return {"orphan": orphan, "kill": killed, "restart": restarted}

    def view(self) -> dict[str, Any]:
        model = WorkbenchModel(self.ledger)
        if self.controller is not None and self.running:
            return model.build(self.controller.collect_node_states())
        return model.build()

    def shutdown(self) -> list[dict[str, Any]]:
        results = self.orch.shutdown_all()
        self.running = False
        return results


if tk is not None:
    class WorkbenchApp(tk.Tk):
        def __init__(self, runtime_options: Mapping[str, Any] | None = None) -> None:
            super().__init__()
            self.title("边缘缓存一致性诊断工作台")
            self.geometry("1320x820")
            self.minsize(1000, 640)
            self.options = dict(runtime_options or {})
            self.runtime: WorkbenchRuntime | None = None
            self._build_ui()
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        def _build_ui(self) -> None:
            assert ttk is not None
            toolbar = ttk.Frame(self, padding=8)
            toolbar.pack(fill="x")
            ttk.Button(toolbar, text="启动冷缓存", command=self._start).pack(side="left", padx=3)
            ttk.Button(toolbar, text="刷新视图", command=self._refresh).pack(side="left", padx=3)
            ttk.Button(toolbar, text="内容演进", command=self._evolve).pack(side="left", padx=3)
            ttk.Button(toolbar, text="应用策略 v2", command=self._apply_policy).pack(side="left", padx=3)
            ttk.Button(toolbar, text="捕获快照", command=self._snapshot).pack(side="left", padx=3)
            ttk.Button(toolbar, text="恢复最近快照", command=self._restore).pack(side="left", padx=3)
            ttk.Button(toolbar, text="强杀并恢复 edge-c", command=self._recover).pack(side="left", padx=3)
            self.status_var = tk.StringVar(value="未启动")
            ttk.Label(toolbar, textvariable=self.status_var).pack(side="right", padx=8)

            self.notebook = ttk.Notebook(self)
            self.notebook.pack(fill="both", expand=True, padx=8, pady=4)
            self.cache_tree = self._tree_tab(
                "缓存条目",
                [
                    ("node", "节点", 90),
                    ("path", "路径", 220),
                    ("key", "缓存键", 360),
                    ("etag", "内容版本 ETag", 260),
                    ("entry_version", "条目版本", 85),
                    ("remaining", "剩余 TTL(ms)", 110),
                    ("ttl", "TTL(ms)", 90),
                    ("ledger", "账本版本", 90),
                ],
            )
            self.eviction_tree = self._tree_tab(
                "逐出事件",
                [
                    ("node", "节点", 90),
                    ("path", "路径", 230),
                    ("reason", "原因", 190),
                    ("rule", "规则", 70),
                    ("etag", "ETag", 260),
                    ("ledger", "账本版本", 90),
                    ("time", "时间", 220),
                ],
            )
            self.propagation_tree = self._tree_tab(
                "失效传播链",
                [
                    ("batch", "批次", 280),
                    ("seq", "序号", 60),
                    ("trigger", "触发路径", 220),
                    ("affected", "受影响路径", 220),
                    ("depth", "深度", 60),
                    ("relation", "关系", 150),
                    ("ledger", "账本版本", 90),
                ],
            )
            self.ledger_tree = self._tree_tab(
                "账本版本",
                [
                    ("version", "版本", 70),
                    ("kind", "类型", 130),
                    ("policy", "策略版本", 130),
                    ("content", "内容集合哈希", 300),
                    ("time", "时间", 240),
                ],
            )
            self.content_tree = self._tree_tab(
                "内容与派生关系",
                [
                    ("path", "路径", 240),
                    ("etag", "ETag", 330),
                    ("sources", "上游路径", 300),
                    ("rule", "派生规则", 120),
                ],
            )
            self.message = tk.Text(self, height=7, wrap="word")
            self.message.pack(fill="x", padx=8, pady=(0, 8))
            self._log("工作台就绪。点击“启动冷缓存”拉起本地源站与三个节点。")

        def _tree_tab(self, title: str, columns: list[tuple[str, str, int]]) -> Any:
            assert ttk is not None
            frame = ttk.Frame(self.notebook)
            tree = ttk.Treeview(
                frame,
                columns=[item[0] for item in columns],
                show="headings",
                height=12,
            )
            for key, heading, width in columns:
                tree.heading(key, text=heading)
                tree.column(key, width=width, stretch=key in ("key", "etag"), anchor="w")
            scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            tree.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
            self.notebook.add(frame, text=title)
            return tree

        def _log(self, text: str) -> None:
            self.message.insert("end", text + "\n")
            self.message.see("end")

        def _ensure_runtime(self) -> WorkbenchRuntime:
            if self.runtime is None:
                self.runtime = WorkbenchRuntime(**self.options)
            return self.runtime

        def _run(self, action: Any, success: str) -> None:
            try:
                result = action()
                self._log(success)
                if isinstance(result, dict):
                    compact = json.dumps(result, ensure_ascii=False, sort_keys=True)
                    self._log(compact[:1200])
                self._refresh()
            except EdgeError as exc:
                self._log(f"{exc.code}: {exc.message} @ {exc.location or '-'}")
                if messagebox is not None:
                    messagebox.showerror("业务错误", f"{exc.code}\n{exc.message}\n{exc.location or ''}")
            except Exception as exc:  # pragma: no cover - GUI boundary
                self._log(f"E_INTERNAL: {exc}")
                if messagebox is not None:
                    messagebox.showerror("内部错误", str(exc))

        def _start(self) -> None:
            runtime = self._ensure_runtime()
            self._run(lambda: runtime.start(reset=True), "冷启动完成")

        def _refresh(self) -> None:
            try:
                runtime = self._ensure_runtime()
                view = runtime.view()
                for tree in (
                    self.cache_tree,
                    self.eviction_tree,
                    self.propagation_tree,
                    self.ledger_tree,
                    self.content_tree,
                ):
                    tree.delete(*tree.get_children())
                for entry in view["entries"]:
                    self.cache_tree.insert(
                        "",
                        "end",
                        values=(
                            entry["node_id"],
                            entry["path"],
                            entry["cache_key"],
                            entry["etag"],
                            entry["entry_version"],
                            entry["remaining_ttl_ms"],
                            entry["ttl_ms"],
                            entry["ledger_version"],
                        ),
                    )
                for event in view["evictions"]:
                    self.eviction_tree.insert(
                        "",
                        "end",
                        values=(
                            event["node_id"],
                            event["path"],
                            event["reason"],
                            event.get("rule") or "",
                            event.get("etag") or "",
                            event["ledger_version"],
                            event["occurred_at"],
                        ),
                    )
                for trace in view["propagation"]:
                    self.propagation_tree.insert(
                        "",
                        "end",
                        values=(
                            trace["batch_id"],
                            trace["seq"],
                            trace["trigger_path"],
                            trace["affected_path"],
                            trace["depth"],
                            trace["relation"],
                            trace["ledger_version"],
                        ),
                    )
                for row in view["ledger_history"]:
                    self.ledger_tree.insert(
                        "",
                        "end",
                        values=(
                            row["version"],
                            row["kind"],
                            row["policy_version"],
                            row["content_set_hash"],
                            row["created_at"],
                        ),
                    )
                for item in view["content"]:
                    self.content_tree.insert(
                        "",
                        "end",
                        values=(
                            item["path"],
                            item["etag"],
                            ", ".join(item["sources"]),
                            item["rule"] or "",
                        ),
                    )
                self.status_var.set(
                    f"ledger={view['ledger_version']} policy={view['policy_version']} "
                    f"entries={len(view['entries'])}"
                )
            except EdgeError as exc:
                self._log(f"{exc.code}: {exc.message}")

        def _evolve(self) -> None:
            runtime = self._ensure_runtime()
            if simpledialog is None:
                return
            path = simpledialog.askstring("内容演进", "内容路径", initialvalue="/assets/base.css", parent=self)
            if not path:
                return
            etag = simpledialog.askstring("内容演进", "新 ETag", initialvalue='"base-v2-workbench"', parent=self)
            if not etag:
                return
            body = simpledialog.askstring(
                "内容演进",
                "新内容",
                initialvalue=":root{--brand:#0077b6;--surface:#f0fbff}\nbody{margin:0}\n",
                parent=self,
            )
            if body is None:
                return
            self._run(lambda: runtime.evolve(path, etag, body), "内容演进与派生失效传播完成")

        def _apply_policy(self) -> None:
            runtime = self._ensure_runtime()
            self._run(runtime.apply_policy_v2, "策略 v2 已整体替换缓存")

        def _snapshot(self) -> None:
            runtime = self._ensure_runtime()
            self._run(runtime.snapshot, "快照捕获完成")

        def _restore(self) -> None:
            runtime = self._ensure_runtime()
            self._run(runtime.restore_latest, "快照已整体替换恢复")

        def _recover(self) -> None:
            runtime = self._ensure_runtime()
            self._run(runtime.force_recover_edge_c, "edge-c 强杀恢复完成")

        def _on_close(self) -> None:
            if self.runtime is not None and self.runtime.running:
                try:
                    self.runtime.shutdown()
                except Exception:
                    pass
            self.destroy()


def run_gui(options: Mapping[str, Any]) -> int:
    if tk is None:
        raise StateError(
            "Tkinter is not available in this Python installation; install the Windows official Python build",
            location="tkinter",
        )
    app = WorkbenchApp(options)
    app.mainloop()
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tkinter edge-cache diagnostic workbench")
    parser.add_argument("--base-port", type=int, default=28280)
    parser.add_argument("--state-dir", default=str(PROJECT_ROOT / "runtime" / "workbench"))
    parser.add_argument("--policy", default=str(PROJECT_ROOT / "samples" / "policy.json"))
    parser.add_argument("--policy-v2", default=str(PROJECT_ROOT / "samples" / "policy_v2.json"))
    parser.add_argument("--content", default=str(PROJECT_ROOT / "samples" / "content.json"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return run_gui(
            {
                "state_dir": args.state_dir,
                "base_port": args.base_port,
                "policy_path": args.policy,
                "policy_v2_path": args.policy_v2,
                "content_path": args.content,
            }
        )
    except EdgeError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False))
        return exit_code_for(exc)
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"ok": False, "error": error_payload(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

