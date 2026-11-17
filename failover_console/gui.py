"""Tkinter domain editor and operation console.

The GUI intentionally shares the exact CLI/engine code paths.  JSON remains the
source of truth so the scene can also be versioned and reviewed outside the
application.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
except ImportError:  # pragma: no cover - exercised on minimal Linux installs
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ScrolledText = None  # type: ignore[assignment]

from .codes import BusinessCode
from .engine import FailoverEngine
from .errors import BusinessError
from .jsonl_logger import JsonlLogger
from .models import Scene
from .scene import SceneLoader


class FailoverConsoleApp:
    def __init__(
        self,
        root: Any,
        *,
        scene_path: str | None = None,
        db_path: str | None = None,
        log_path: str | None = None,
    ) -> None:
        if tk is None or ttk is None or ScrolledText is None:
            raise RuntimeError("Tkinter 不可用")
        self.root = root
        self.root.title("故障隔离与容灾切换编辑台")
        self.root.geometry("1260x820")
        self.scene_path_var = tk.StringVar(value=scene_path or "scenes/sample_scene.json")
        self.db_path_var = tk.StringVar(value=db_path or "artifacts/session.sqlite")
        self.log_path_var = tk.StringVar(value=log_path or "artifacts/session_log.jsonl")
        self.status_var = tk.StringVar(value="就绪")
        self.parsed_scene: Scene | None = None
        self.worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self._build_ui()
        self.root.after(120, self._poll_worker)
        if Path(self.scene_path_var.get()).exists():
            self.load_scene()

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="场景").pack(side=tk.LEFT)
        ttk.Entry(toolbar, textvariable=self.scene_path_var, width=42).pack(
            side=tk.LEFT, padx=(4, 6)
        )
        ttk.Button(toolbar, text="打开", command=self.choose_scene).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="保存", command=self.save_scene).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="强校验", command=self.validate_scene).pack(side=tk.LEFT)
        ttk.Label(toolbar, textvariable=self.status_var, foreground="#555").pack(
            side=tk.RIGHT, padx=8
        )

        panes = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        editor_frame = ttk.LabelFrame(panes, text="场景 JSON（可编辑）", padding=4)
        self.editor = ScrolledText(editor_frame, wrap=tk.NONE, undo=True, font=("Consolas", 10))
        self.editor.pack(fill=tk.BOTH, expand=True)
        panes.add(editor_frame, weight=3)

        right = ttk.Frame(panes)
        panes.add(right, weight=2)

        summary_notebook = ttk.Notebook(right)
        summary_notebook.pack(fill=tk.BOTH, expand=True)
        self.instance_tree = self._make_tree(
            summary_notebook,
            "实例",
            ("service", "instance", "role", "port", "timeout", "retries", "thresholds"),
            (90, 120, 70, 65, 70, 55, 90),
        )
        self.policy_tree = self._make_tree(
            summary_notebook,
            "策略",
            ("service", "mode", "cooldown", "min_healthy", "version"),
            (100, 70, 80, 90, 180),
        )
        self.fault_tree = self._make_tree(
            summary_notebook,
            "故障注入",
            ("instance", "kind", "note"),
            (130, 90, 300),
        )

        operation = ttk.LabelFrame(right, text="操作", padding=8)
        operation.pack(fill=tk.X, pady=(8, 0))
        row = ttk.Frame(operation)
        row.pack(fill=tk.X)
        ttk.Label(row, text="会话库").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.db_path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8)
        )
        row2 = ttk.Frame(operation)
        row2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row2, text="结构化日志").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.log_path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0)
        )
        buttons = ttk.Frame(operation)
        buttons.pack(fill=tk.X, pady=(8, 0))
        self.run_button = ttk.Button(
            buttons, text="全量探测 + 切换", command=lambda: self.run_cycle({})
        )
        self.run_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="恢复状态", command=self.restore_status).pack(
            side=tk.LEFT, padx=4
        )
        self.replay_button = ttk.Button(buttons, text="重放最近快照", command=self.replay)
        self.replay_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="查看待确认", command=self.show_pending).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Label(buttons, text="注入实例").pack(side=tk.LEFT, padx=(12, 2))
        self.fault_instance_var = tk.StringVar()
        ttk.Entry(buttons, textvariable=self.fault_instance_var, width=14).pack(side=tk.LEFT)
        self.fault_kind_var = tk.StringVar(value="unreachable")
        ttk.Combobox(
            buttons,
            textvariable=self.fault_kind_var,
            values=("unreachable", "timeout", "error"),
            state="readonly",
            width=12,
        ).pack(side=tk.LEFT, padx=4)

        self.output = ScrolledText(self.root, height=12, wrap=tk.WORD, font=("Consolas", 9))
        self.output.pack(fill=tk.BOTH, expand=False, padx=8, pady=(0, 8))
        self._append_output("编辑 JSON 后点击“强校验”；运行操作只使用通过强校验的场景。")

    def _make_tree(
        self,
        notebook: Any,
        title: str,
        columns: tuple[str, ...],
        widths: tuple[int, ...],
    ) -> Any:
        frame = ttk.Frame(notebook)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=7)
        for column, width in zip(columns, widths):
            tree.heading(column, text=column)
            tree.column(column, width=width, anchor=tk.W)
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        notebook.add(frame, text=title)
        return tree

    def _append_output(self, text: str) -> None:
        self.output.insert(tk.END, text.rstrip() + "\n")
        self.output.see(tk.END)

    def choose_scene(self) -> None:
        path = filedialog.askopenfilename(
            title="打开场景", filetypes=[("JSON", "*.json"), ("All", "*.*")]
        )
        if path:
            self.scene_path_var.set(path)
            self.load_scene()

    def load_scene(self) -> None:
        path = Path(self.scene_path_var.get())
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", text)
        self._parse_and_refresh(text)

    def _parse_and_refresh(self, text: str) -> Scene | None:
        try:
            scene = SceneLoader().loads(
                text, source_name=self.scene_path_var.get() or "<editor>"
            )
        except BusinessError as exc:
            self.parsed_scene = None
            self.status_var.set(exc.code)
            self._append_output(
                json.dumps(exc.as_dict(), ensure_ascii=False, indent=2)
            )
            return None
        self.parsed_scene = scene
        self.status_var.set(f"校验通过 · {scene.scene_version}")
        self._refresh_tables(scene)
        return scene

    def _refresh_tables(self, scene: Scene) -> None:
        for tree in (self.instance_tree, self.policy_tree, self.fault_tree):
            for item in tree.get_children():
                tree.delete(item)
        for service in sorted(scene.services, key=lambda item: item.service_id):
            policy = service.policy
            self.policy_tree.insert(
                "",
                tk.END,
                values=(
                    service.service_id,
                    policy.mode,
                    policy.cooldown_ms,
                    policy.min_healthy_replicas,
                    policy.version,
                ),
            )
            for instance in sorted(service.instances, key=lambda item: item.instance_id):
                probe = instance.probe
                self.instance_tree.insert(
                    "",
                    tk.END,
                    values=(
                        service.service_id,
                        instance.instance_id,
                        instance.role,
                        instance.port,
                        probe.timeout_ms,
                        probe.retries,
                        f"{probe.success_threshold}/{probe.failure_threshold}",
                    ),
                )
        for fault in sorted(scene.faults, key=lambda item: item.instance_id):
            self.fault_tree.insert(
                "", tk.END, values=(fault.instance_id, fault.kind, fault.note)
            )

    def validate_scene(self) -> None:
        scene = self._parse_and_refresh(self.editor.get("1.0", tk.END))
        if scene is not None:
            self._append_output(
                json.dumps(
                    {
                        "ok": True,
                        "code": BusinessCode.OK.value,
                        "scene_version": scene.scene_version,
                        "fingerprint": scene.fingerprint,
                        "services": len(scene.services),
                        "instances": len(scene.instances()),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

    def save_scene(self) -> None:
        scene = self._parse_and_refresh(self.editor.get("1.0", tk.END))
        if scene is None:
            return
        path = Path(self.scene_path_var.get())
        try:
            data = json.loads(self.editor.get("1.0", tk.END))
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.status_var.set(f"已保存 {path}")

    def _scene_from_editor(self) -> Scene | None:
        return self._parse_and_refresh(self.editor.get("1.0", tk.END))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.run_button.configure(state=state)
        self.replay_button.configure(state=state)
        self.status_var.set("处理中..." if busy else "就绪")

    def _logger(self) -> JsonlLogger:
        return JsonlLogger(self.log_path_var.get(), echo=False)

    def _start_worker(self, target: Any, *args: Any) -> None:
        if self.busy:
            return
        self._set_busy(True)
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()

    def run_cycle(self, overrides: dict[str, str]) -> None:
        scene = self._scene_from_editor()
        if scene is None:
            return
        instance_id = self.fault_instance_var.get().strip()
        if instance_id:
            overrides = dict(overrides)
            overrides[instance_id] = self.fault_kind_var.get()
        self._start_worker(self._worker_run, scene, overrides)

    def _worker_run(self, scene: Scene, overrides: dict[str, str]) -> None:
        try:
            with FailoverEngine(
                scene,
                self.db_path_var.get(),
                logger=self._logger(),
            ) as engine:
                result = engine.run_cycle(runtime_faults=overrides)
                status = engine.status()
            self.worker_queue.put(("run", (result, status)))
        except Exception as exc:
            self.worker_queue.put(("error", self._format_exception(exc)))

    def replay(self) -> None:
        scene = self._scene_from_editor()
        if scene is None:
            return
        self._start_worker(self._worker_replay, scene)

    def _worker_replay(self, scene: Scene) -> None:
        try:
            with FailoverEngine(
                scene,
                self.db_path_var.get(),
                logger=self._logger(),
            ) as engine:
                result = engine.replay_latest()
            self.worker_queue.put(("replay", result))
        except Exception as exc:
            self.worker_queue.put(("error", self._format_exception(exc)))

    def restore_status(self) -> None:
        scene = self._scene_from_editor()
        if scene is None:
            return
        self._start_worker(self._worker_restore, scene)

    def _worker_restore(self, scene: Scene) -> None:
        try:
            with FailoverEngine(
                scene,
                self.db_path_var.get(),
                logger=self._logger(),
            ) as engine:
                report = engine.recovery.as_dict()
                status = engine.status()
            self.worker_queue.put(("restore", (report, status)))
        except Exception as exc:
            self.worker_queue.put(("error", self._format_exception(exc)))

    def show_pending(self) -> None:
        scene = self._scene_from_editor()
        if scene is None:
            return
        self._start_worker(self._worker_pending, scene)

    def _worker_pending(self, scene: Scene) -> None:
        try:
            with FailoverEngine(
                scene,
                self.db_path_var.get(),
                logger=self._logger(),
            ) as engine:
                pending = engine.repo.list_pending()
            self.worker_queue.put(("pending", pending))
        except Exception as exc:
            self.worker_queue.put(("error", self._format_exception(exc)))

    @staticmethod
    def _format_exception(exc: Exception) -> str:
        if isinstance(exc, BusinessError):
            return json.dumps(exc.as_dict(), ensure_ascii=False, indent=2)
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    def _poll_worker(self) -> None:
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                self._set_busy(False)
                if kind == "error":
                    self._append_output(str(payload))
                elif kind == "run":
                    result, status = payload
                    self._append_output(
                        json.dumps(
                            {
                                "codes": result.codes,
                                "round_no": result.round_no,
                                "snapshot_id": result.snapshot_id,
                                "peak_global": result.probe_batch.peak_global,
                                "peak_by_service": result.probe_batch.peak_by_service,
                                "switches": [
                                    decision.trace_key()
                                    for decision in result.switch_decisions
                                ],
                                "instances": [
                                    (item["instance_id"], item["role"], item["version"])
                                    for item in status["instances"]
                                ],
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif kind == "replay":
                    self._append_output(json.dumps(payload.as_dict(), ensure_ascii=False, indent=2))
                elif kind == "restore":
                    report, status = payload
                    self._append_output(
                        json.dumps(
                            {"recovery": report, "status": status},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif kind == "pending":
                    self._append_output(
                        json.dumps(payload, ensure_ascii=False, indent=2)
                    )
        except queue.Empty:
            pass
        self.root.after(120, self._poll_worker)


def main(
    *,
    scene_path: str | None = None,
    db_path: str | None = None,
    log_path: str | None = None,
) -> int:
    if tk is None:
        print(
            "Tkinter 不可用。Windows 官方 Python 安装时勾选 tcl/tk；"
            "Linux 请安装 python3-tk。CLI 与引擎仍可正常使用。",
            file=sys.stderr,
        )
        return 2
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法启动图形界面（缺少显示环境）: {exc}", file=sys.stderr)
        return 2
    FailoverConsoleApp(
        root,
        scene_path=scene_path,
        db_path=db_path,
        log_path=log_path,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
