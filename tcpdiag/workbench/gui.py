"""Tkinter 交互界面（仅标准库）。

展示顺序固定：TCP 状态 -> 拥塞窗口 -> 慢启动阈值 -> 平滑 RTT -> RTO
-> 已发送未确认 -> 事件轨迹（追加式，顺序稳定）。
运行：python -m workbench.gui
"""

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .client import SessionClient
from .core import SessionCore
from .errors import BizError
from .peer import ScriptedPeer
from .replay import verify_replay
from .snapshot import load_snapshot, restore_into, save_snapshot

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SNAPSHOT_PATH = Path("session_snapshot.json")

FIELDS = (
    ("TCP 状态", lambda c: c.sm.state),
    ("拥塞窗口 cwnd", lambda c: c.cc.cwnd),
    ("慢启动阈值 ssthresh", lambda c: c.cc.ssthresh),
    ("平滑 RTT (ms)", lambda c: c.rtt.srtt_ms),
    ("RTO (ms)", lambda c: c.rtt.rto_ms),
    ("已发送未确认", lambda c: c.next_seq - c.send_una),
)


class WorkbenchGUI:
    def __init__(self, root):
        self.root = root
        root.title("TCP 连接状态与拥塞控制诊断工作台")
        self.core = SessionCore()
        self.client = None
        self.peer = None
        self._shown_events = 0

        top = ttk.Frame(root, padding=6)
        top.grid(row=0, column=0, sticky="ew")
        self.vars = {}
        for row, (label, _) in enumerate(FIELDS):
            ttk.Label(top, text=label, width=18).grid(row=row, column=0,
                                                      sticky="w")
            var = tk.StringVar(value="-")
            ttk.Label(top, textvariable=var, width=24,
                      font=("Consolas", 10)).grid(row=row, column=1,
                                                  sticky="w")
            self.vars[label] = var

        ctrl = ttk.Frame(root, padding=6)
        ctrl.grid(row=0, column=1, rowspan=2, sticky="n")
        scripts = sorted(p.name for p in SCRIPTS_DIR.glob("*.json")) \
            if SCRIPTS_DIR.is_dir() else []
        self.script_var = tk.StringVar(value=scripts[0] if scripts else "")
        ttk.Label(ctrl, text="对端脚本").grid(row=0, column=0, sticky="w")
        ttk.OptionMenu(ctrl, self.script_var, self.script_var.get(),
                       *scripts).grid(row=0, column=1, sticky="ew")
        buttons = (
            ("启动本地对端", self.start_peer),
            ("连接(三次握手)", self.connect),
            ("发送 4 段数据", lambda: self.send(4000)),
            ("发送 1 段数据", lambda: self.send(1000)),
            ("保存快照", self.save_snap),
            ("恢复快照", self.load_snap),
            ("重放校验", self.replay),
            ("关闭连接", self.close_conn),
        )
        for i, (text, cmd) in enumerate(buttons, start=1):
            ttk.Button(ctrl, text=text, command=cmd).grid(
                row=i, column=0, columnspan=2, sticky="ew", pady=1)

        self.log_text = tk.Text(root, width=96, height=22, state="disabled",
                                font=("Consolas", 9))
        self.log_text.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(root, command=self.log_text.yview)
        sb.grid(row=1, column=0, sticky="nse")
        self.log_text.configure(yscrollcommand=sb.set)
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        self.status = tk.StringVar(value="就绪（时钟基准：monotonic，真值来源：对端脚本）")
        ttk.Label(root, textvariable=self.status, relief="sunken",
                  anchor="w").grid(row=2, column=0, columnspan=2, sticky="ew")

        self.root.after(100, self._poll)

    # ---- 动作 ----
    def start_peer(self):
        try:
            script = json.loads(
                (SCRIPTS_DIR / self.script_var.get()).read_text("utf-8"))
            self.peer = ScriptedPeer(script)
            self.peer.start()
            self.peer.ready.wait(2.0)
            self.status.set(f"对端已启动 127.0.0.1:{self.peer.port} "
                            f"脚本={self.script_var.get()}")
        except Exception as exc:  # noqa: BLE001 - 界面统一兜底
            messagebox.showerror("启动对端失败", str(exc))

    def connect(self):
        if not self.peer:
            messagebox.showwarning("无对端", "请先启动本地对端")
            return
        self._safe(lambda: self._do_connect())

    def _do_connect(self):
        self.client = SessionClient(self.core, port=self.peer.port)
        self.client.connect()

    def send(self, n):
        self._safe(lambda: self.client and self.client.send_data(n))

    def save_snap(self):
        self._safe(lambda: save_snapshot(self.core, str(SNAPSHOT_PATH))
                   or self.status.set(f"快照已保存 {SNAPSHOT_PATH}"))

    def load_snap(self):
        def _load():
            snap = load_snapshot(str(SNAPSHOT_PATH))
            restore_into(self.core, snap)
            self.status.set("快照已恢复（整体替换，可继续交互）")
        self._safe(_load)

    def replay(self):
        def _replay():
            snap = load_snapshot(str(SNAPSHOT_PATH))
            result = verify_replay(snap)
            if result["ok"]:
                self.status.set(f"重放校验通过 checked={result['checked']}")
            else:
                self.status.set(
                    f"{result['code']} index={result['input_index']} "
                    f"seq={result['seq']} field={result['field']} "
                    f"expected={result['expected']} actual={result['actual']}")
        self._safe(_replay)

    def close_conn(self):
        self._safe(lambda: self.client and self.client.close())

    def _safe(self, fn):
        try:
            fn()
        except BizError as exc:
            self.status.set(f"{exc.code}: {exc.message}")
        except Exception as exc:  # noqa: BLE001
            self.status.set(f"错误: {exc}")

    # ---- 渲染 ----
    def _poll(self):
        if self.client:
            try:
                self.client.pump_nonblocking()
                self.client.tick()
            except BizError as exc:
                self.status.set(f"{exc.code}: {exc.message}")
            except OSError:
                pass
        self._refresh()
        self.root.after(100, self._poll)

    def _refresh(self):
        for label, getter in FIELDS:
            self.vars[label].set(str(getter(self.core)))
        events = self.core.events
        if len(events) > self._shown_events:
            self.log_text.configure(state="normal")
            for e in events[self._shown_events:]:
                self.log_text.insert("end", json.dumps(
                    e, ensure_ascii=False) + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
            self._shown_events = len(events)


def main():
    root = tk.Tk()
    WorkbenchGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
