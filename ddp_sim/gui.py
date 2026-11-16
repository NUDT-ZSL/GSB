"""Tkinter interface: per-worker progress, phases, versions, consistency."""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from .config import SimConfig
from .controller import Controller
from .errors import SimError

COLUMNS = ("rank", "phase", "sent", "recv", "elapsed", "version",
           "digest", "status")
HEADERS = {"rank": "进程", "phase": "通信阶段", "sent": "已发送块",
           "recv": "已接收块", "elapsed": "已用时间(s)", "version": "参数版本",
           "digest": "梯度摘要", "status": "一致性"}


class SimulatorApp:
    def __init__(self, root, cfg: SimConfig):
        self.root = root
        self.cfg = cfg
        self.controller = Controller(cfg)
        self.events = queue.Queue()
        self.busy = False
        self.rows = {r: {"phase": "init", "sent": 0, "recv": 0, "elapsed": 0.0,
                         "version": self.controller.version, "digest": "-",
                         "status": "-"} for r in range(cfg.workers)}
        root.title("DDP 环形 All-Reduce 仿真台")
        self._build_controls()
        self._build_table()
        self._build_log()
        self._refresh_table()
        root.after(100, self._poll_events)

    def _build_controls(self):
        frame = ttk.LabelFrame(self.root, text="配置")
        frame.pack(fill="x", padx=6, pady=4)
        self.vars = {}
        fields = [("workers", self.cfg.workers), ("dim", self.cfg.dim),
                  ("seed", self.cfg.seed), ("block_size", self.cfg.block_size),
                  ("interleave", self.cfg.interleave),
                  ("base_port", self.cfg.base_port), ("lr", self.cfg.lr)]
        for col, (name, value) in enumerate(fields):
            ttk.Label(frame, text=name).grid(row=0, column=col * 2, sticky="e")
            var = tk.StringVar(value=str(value))
            ttk.Entry(frame, textvariable=var, width=8).grid(row=0,
                                                             column=col * 2 + 1)
            self.vars[name] = var
        ttk.Label(frame, text="延迟ms(如 0=50,2=100)").grid(
            row=1, column=0, columnspan=2, sticky="e")
        self.delay_var = tk.StringVar(value=",".join(
            f"{k}={v}" for k, v in self.cfg.delays_ms.items()))
        ttk.Entry(frame, textvariable=self.delay_var, width=24).grid(
            row=1, column=2, columnspan=3, sticky="w")
        ttk.Label(frame, text="快照路径").grid(row=1, column=6, sticky="e")
        self.snap_var = tk.StringVar(value="runs/gui-snapshot.json")
        ttk.Entry(frame, textvariable=self.snap_var, width=24).grid(
            row=1, column=7, columnspan=3, sticky="w")
        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", padx=6, pady=2)
        ttk.Button(buttons, text="单步训练", command=self.on_step).pack(side="left")
        ttk.Button(buttons, text="保存快照", command=self.on_save).pack(side="left")
        ttk.Button(buttons, text="恢复快照", command=self.on_restore).pack(side="left")
        self.consistency_var = tk.StringVar(value="全局梯度一致性: 未运行")
        ttk.Label(buttons, textvariable=self.consistency_var).pack(side="left",
                                                                   padx=16)

    def _build_table(self):
        self.tree = ttk.Treeview(self.root, columns=COLUMNS, show="headings",
                                 height=self.cfg.workers)
        for col in COLUMNS:
            self.tree.heading(col, text=HEADERS[col])
            self.tree.column(col, width=110, anchor="center")
        self.tree.pack(fill="x", padx=6, pady=4)

    def _build_log(self):
        self.log = tk.Text(self.root, height=8, state="disabled")
        self.log.pack(fill="both", expand=True, padx=6, pady=4)

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_table(self):
        for rank, row in self.rows.items():
            values = (rank, row["phase"], row["sent"], row["recv"],
                      f"{row['elapsed']:.2f}", row["version"],
                      row["digest"][:12] if row["digest"] != "-" else "-",
                      row["status"])
            iid = str(rank)
            if self.tree.exists(iid):
                self.tree.item(iid, values=values)
            else:
                self.tree.insert("", "end", iid=iid, values=values)

    def _apply_config(self):
        try:
            cfg = SimConfig(
                workers=int(self.vars["workers"].get()),
                dim=int(self.vars["dim"].get()),
                seed=int(self.vars["seed"].get()),
                block_size=int(self.vars["block_size"].get()),
                interleave=int(self.vars["interleave"].get()),
                base_port=int(self.vars["base_port"].get()),
                lr=float(self.vars["lr"].get()),
                log_dir="runs/gui",
            )
            delays = {}
            for pair in self.delay_var.get().split(","):
                pair = pair.strip()
                if pair:
                    rank, _, ms = pair.partition("=")
                    delays[int(rank)] = float(ms)
            cfg.delays_ms = delays
            cfg.validate()
        except (ValueError, SimError) as exc:
            messagebox.showerror("配置错误", str(exc))
            return None
        if (cfg.workers != self.cfg.workers or cfg.seed != self.cfg.seed
                or cfg.dim != self.cfg.dim):
            self.controller = Controller(cfg)
            self.rows = {r: {"phase": "init", "sent": 0, "recv": 0,
                             "elapsed": 0.0, "version": 0, "digest": "-",
                             "status": "-"} for r in range(cfg.workers)}
            for iid in self.tree.get_children():
                self.tree.delete(iid)
        else:
            self.controller.cfg = cfg
        self.cfg = cfg
        return cfg

    def on_step(self):
        if self.busy:
            return
        if self._apply_config() is None:
            return
        self.busy = True
        for row in self.rows.values():
            row.update({"phase": "init", "sent": 0, "recv": 0, "status": "..."})
        self._refresh_table()
        self.consistency_var.set("全局梯度一致性: 计算中")

        def work():
            try:
                result = self.controller.run_step(event_sink=self.events.put)
                self.events.put({"kind": "step-done", "result": result})
            except SimError as exc:
                self.events.put({"kind": "step-error", "error": exc})

        threading.Thread(target=work, daemon=True).start()

    def on_save(self):
        try:
            checksum = self.controller.save_snapshot(self.snap_var.get())
            self._log(f"快照已保存: {self.snap_var.get()} "
                      f"version={self.controller.version} "
                      f"checksum={checksum[:12]}")
        except SimError as exc:
            messagebox.showerror(exc.code, exc.message)

    def on_restore(self):
        try:
            info = self.controller.restore(self.snap_var.get())
            for row in self.rows.values():
                row["version"] = self.controller.version
                row["phase"] = "restored" if info["replaced"] else row["phase"]
            self._refresh_table()
            self._log(f"快照已恢复(整体替换={info['replaced']}): "
                      f"version={info['version']} steps={info['steps_done']}")
        except SimError as exc:
            messagebox.showerror(exc.code, exc.message)

    def _poll_events(self):
        try:
            while True:
                record = self.events.get_nowait()
                self._handle_event(record)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, record):
        kind = record.get("kind")
        if kind == "status":
            row = self.rows[record["rank"]]
            row.update({"phase": record["phase"], "sent": record["sent"],
                        "recv": record["recv"], "elapsed": record["elapsed"]})
            self._refresh_table()
        elif kind == "done":
            row = self.rows[record["rank"]]
            row.update({"phase": "done", "version": record["version"],
                        "digest": record["grad_digest"], "status": "OK"})
            self._refresh_table()
        elif kind == "step-done":
            self.busy = False
            result = record["result"]
            digests = {rec["grad_digest"] for rec in result.workers}
            ok = len(digests) == 1
            self.consistency_var.set(
                f"全局梯度一致性: {'一致' if ok else '分叉'} "
                f"(version={result.version})")
            self._log(f"训练步完成 version={result.version} "
                      f"grad={result.grad_digest[:12]} "
                      f"param={result.param_digest[:12]} "
                      f"elapsed={result.elapsed:.2f}s")
        elif kind == "step-error":
            self.busy = False
            exc = record["error"]
            self.consistency_var.set(f"错误: {exc.code}")
            self._log(f"{exc.code}: {exc.message}")
            messagebox.showerror(exc.code, exc.message)
        elif kind == "error":
            row = self.rows.get(record.get("rank"))
            if row is not None:
                row["status"] = record.get("code", "ERR")
                self._refresh_table()


def run_gui(cfg):
    root = tk.Tk()
    SimulatorApp(root, cfg)
    root.mainloop()
