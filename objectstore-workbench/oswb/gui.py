"""Tkinter workbench UI.

The GUI runs every operation on a worker thread so the loop stays responsive.
Tkinter ships with the official Windows Python installer; on minimal Linux
interpreters it may be absent, in which case a friendly message is shown.
"""

import json
import os
import threading

from .client import StorageClient
from .errors import BusinessError
from .runner import start_inprocess, stop_inprocess
from .sample import generate_sample, DEFAULT_SAMPLE_SIZE
from .uploader import UploadWorkflow

try:
    import tkinter as tk
    from tkinter import ttk, filedialog
    TK_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    TK_AVAILABLE = False


class WorkbenchApp:
    def __init__(self, root, host, port, base_dir, log_dir):
        self.root = root
        self.host = host
        self.port = port
        self.base_dir = base_dir
        self.log_dir = log_dir
        self.service = None
        self.client = None
        self.workflow = None
        root.title("对象存储分片上传与元数据一致性工作台")
        root.geometry("900x680")
        self._build_form()
        self._build_progress()
        self._build_output()

    def _build_form(self):
        frame = ttk.LabelFrame(self.root, text="会话参数")
        frame.pack(fill="x", padx=8, pady=6)
        self.vars = {}
        fields = [
            ("sample_path", "样本文件", os.path.join(self.base_dir, "sample.bin")),
            ("object_key", "对象键", "gui/object-a"),
            ("chunk_size", "分片大小(字节)", str(128 * 1024)),
            ("algorithm", "摘要算法", "sha256"),
            ("global_concurrency", "全局并发上限", "4"),
            ("session_quota", "单会话配额", "2"),
        ]
        for row, (key, label, default) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w",
                                              padx=6, pady=3)
            var = tk.StringVar(value=default)
            ttk.Entry(frame, textvariable=var, width=48).grid(
                row=row, column=1, sticky="we", padx=6, pady=3)
            self.vars[key] = var
        frame.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", padx=8)
        ttk.Button(buttons, text="生成样本", command=self.on_make_sample).pack(
            side="left", padx=4, pady=4)
        ttk.Button(buttons, text="选择文件", command=self.on_choose).pack(
            side="left", padx=4, pady=4)
        ttk.Button(buttons, text="上传并合并", command=self.on_upload).pack(
            side="left", padx=4, pady=4)
        ttk.Button(buttons, text="中断后续传", command=self.on_resume).pack(
            side="left", padx=4, pady=4)
        ttk.Button(buttons, text="审计", command=self.on_audit).pack(
            side="left", padx=4, pady=4)
        ttk.Button(buttons, text="快照/恢复", command=self.on_snapshot).pack(
            side="left", padx=4, pady=4)
        ttk.Button(buttons, text="版本差异", command=self.on_versions).pack(
            side="left", padx=4, pady=4)

        self.status_var = tk.StringVar(value="未连接")
        ttk.Label(self.root, textvariable=self.status_var,
                  foreground="#1a5276").pack(anchor="w", padx=10)

    def _build_progress(self):
        frame = ttk.LabelFrame(self.root, text="分片进度与摘要校验")
        frame.pack(fill="both", padx=8, pady=6)
        columns = ("index", "state", "digest", "attempt", "timing")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        headings = {"index": "分片序号", "state": "状态", "digest": "登记摘要",
                    "attempt": "尝试序号", "timing": "开始-结束(单调ms)"}
        widths = {"index": 80, "state": 100, "digest": 360,
                  "attempt": 80, "timing": 200}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_output(self):
        frame = ttk.LabelFrame(self.root, text="对象元数据版本与操作结论")
        frame.pack(fill="both", padx=8, pady=6)
        self.output = tk.Text(frame, height=12, wrap="none")
        self.output.pack(fill="both", expand=True, padx=6, pady=6)

    # ------------------------------------------------------------ threading
    def run_async(self, action):
        def worker():
            try:
                result = action()
                self.root.after(0, lambda: self.show_result("OK", result))
            except BusinessError as error:
                self.root.after(0, lambda: self.show_result(
                    error.code, {"message": error.message, "detail": error.detail}))
            except Exception as error:  # pragma: no cover - UI guard
                self.root.after(0, lambda: self.show_result("E_TRANSPORT",
                                                             {"message": repr(error)}))
        threading.Thread(target=worker, daemon=True).start()

    def show_result(self, code, result):
        self.status_var.set(f"最近操作业务码: {code}")
        self.output.delete("1.0", "end")
        self.output.insert("end", json.dumps(
            {"code": code, "result": result}, ensure_ascii=False,
            sort_keys=True, indent=2))

    def render_attempts(self, session, attempts):
        for item in self.tree.get_children():
            self.tree.delete(item)
        completed = set(session["completed"])
        digest_map = {chunk["index"]: chunk["digest"] for chunk in session["chunks"]}
        attempt_map = {}
        for attempt in attempts:
            attempt_map.setdefault(attempt["index"], []).append(attempt)
        for index in sorted(digest_map):
            chunk_attempts = attempt_map.get(index, [])
            last = chunk_attempts[-1] if chunk_attempts else None
            state = "已上传" if index in completed else "待上传"
            attempt = last["attempt"] if last else "-"
            timing = (f"{last['start_ms']}-{last['end_ms']}" if last else "-")
            self.tree.insert("", "end", values=(
                index, state, digest_map[index], attempt, timing))

    def ensure_connected(self):
        if self.client is None:
            self.service, _ = start_inprocess(
                self.host, self.port, base_dir=self.base_dir, log_dir=self.log_dir)
            self.client = StorageClient(self.host, self.port)
            self.client.ping()
            self.workflow = UploadWorkflow(
                self.client,
                int(self.vars["global_concurrency"].get()),
                int(self.vars["session_quota"].get()))
            self.status_var.set(f"已连接回环服务 {self.host}:{self.port}")
        return self.client, self.workflow

    # ------------------------------------------------------------ actions
    def _params(self):
        return {
            "file_path": self.vars["sample_path"].get(),
            "object_key": self.vars["object_key"].get(),
            "chunk_size": int(self.vars["chunk_size"].get()),
            "algorithm": self.vars["algorithm"].get(),
        }

    def on_make_sample(self):
        def task():
            path = self.vars["sample_path"].get()
            generate_sample(path, DEFAULT_SAMPLE_SIZE)
            return {"sample": path, "size": DEFAULT_SAMPLE_SIZE}
        self.run_async(task)

    def on_choose(self):
        path = filedialog.askopenfilename()
        if path:
            self.vars["sample_path"].set(path)

    def on_upload(self):
        def task():
            client, workflow = self.ensure_connected()
            params = self._params()
            result = workflow.start_upload(
                params["file_path"], params["object_key"],
                params["chunk_size"], algorithm=params["algorithm"])
            commit = workflow.complete(result["session"]["session_id"])
            self.root.after(0, lambda: self.render_attempts(
                result["session"], result["attempts"]))
            obj = client.get_object(params["object_key"])
            versions = client.list_versions(params["object_key"])
            return {"commit": commit, "object": obj,
                    "version_count": len(versions),
                    "chunk_attempts": result["attempts"]}
        self.run_async(task)

    def on_resume(self):
        def task():
            client, workflow = self.ensure_connected()
            params = self._params()
            sessions = client.list_sessions()
            open_sessions = [item for item in sessions
                             if item["object_key"] == params["object_key"]
                             and item["state"] not in ("committed", "aborted")]
            if not open_sessions:
                partial = workflow.start_upload(
                    params["file_path"], params["object_key"],
                    params["chunk_size"], algorithm=params["algorithm"],
                    only_indices=[0])
                session_id = partial["session"]["session_id"]
                prior_attempts = partial["attempts"]
            else:
                session_id = open_sessions[-1]["session_id"]
                prior_attempts = []
            result = workflow.resume_upload(
                params["file_path"], session_id, params["chunk_size"],
                algorithm=params["algorithm"])
            commit = workflow.complete(session_id)
            attempts = prior_attempts + result["attempts"]
            self.root.after(0, lambda: self.render_attempts(
                result["session"], attempts))
            return {"resumed": session_id, "skipped": result["skipped"],
                    "commit": commit}
        self.run_async(task)

    def on_audit(self):
        self.run_async(lambda: self.ensure_connected()[0].audit())

    def on_snapshot(self):
        def task():
            client, _ = self.ensure_connected()
            snapshot = client.create_snapshot()
            restore = client.restore_snapshot(snapshot["snapshot_id"])
            return {"snapshot": snapshot, "restore": restore,
                    "audit": client.audit()}
        self.run_async(task)

    def on_versions(self):
        def task():
            client, _ = self.ensure_connected()
            object_key = self.vars["object_key"].get()
            versions = client.list_versions(object_key)
            diff = None
            if len(versions) >= 2:
                diff = client.diff_versions(
                    object_key, versions[-2]["version"], versions[-1]["version"])
            return {"versions": versions, "latest_diff": diff}
        self.run_async(task)

    def close(self):
        if self.service:
            stop_inprocess(self.service)


def launch_gui(host="127.0.0.1", port=9101, base_dir=None, log_dir=None):
    if not TK_AVAILABLE:
        raise SystemExit(
            "Tkinter is unavailable in this Python installation. On Windows use "
            "the official installer from python.org which includes Tkinter.")
    root = tk.Tk()
    app = WorkbenchApp(root, host, port,
                       base_dir or os.path.join(os.getcwd(), "runtime",
                                                 "workbench-data"),
                       log_dir or os.path.join(os.getcwd(), "runtime", "logs"))
    root.protocol("WM_DELETE_WINDOW", lambda: (app.close(), root.destroy()))
    root.mainloop()
