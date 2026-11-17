"""确定性模拟对端：绑定 127.0.0.1 高位端口，按 JSON 脚本逐条应答。

脚本格式见 scripts/*.json：
- events 按到达顺序匹配（match 中列出的字段全部相等才命中并消费该事件）；
- action.kind: ack / dup_ack(count) / drop / reorder（缓存本次确认，先应答下一段）；
- action.delay_ms 为虚拟延迟，写入应答报文，由客户端时钟解释；
- action.ack / action.expect 可显式覆盖累计确认号与对端期望序号；
- 脚本耗尽后按默认策略：按序到达则累计确认，乱序/缺包则回重复确认。

真值来源：脚本文件本身 + 运行时 truth_log（收/发/丢弃的完整报文序列）。
"""

import json
import socket
import threading


class ScriptedPeer(threading.Thread):
    def __init__(self, script, host="127.0.0.1", port=0):
        super().__init__(daemon=True)
        self.script = script
        self.events = list(script.get("events", []))
        self.default_delay = script.get("defaults", {}).get("delay_ms", 5)
        self.expected = script.get("expected_start", 1)
        self.truth_log = []
        self.held = None
        self.idx = 0
        self.host = host
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.ready = threading.Event()

    def run(self):
        self.ready.set()
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            f = conn.makefile("r", encoding="utf-8")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                self.truth_log.append({"dir": "in", "msg": msg})
                self._handle(msg, conn)

    def _send(self, conn, msg):
        self.truth_log.append({"dir": "out", "msg": msg})
        conn.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def _next_action(self, msg):
        if self.idx < len(self.events):
            ev = self.events[self.idx]
            match = ev.get("match", {})
            if all(msg.get(k) == v for k, v in match.items()):
                self.idx += 1
                return ev.get("action", {"kind": "ack"})
        return None

    def _handle(self, msg, conn):
        mtype = msg.get("type")
        if mtype == "SYN":
            action = self._next_action(msg) or {}
            self._send(conn, {"type": "SYN_ACK", "ack": 1,
                              "delay_ms": action.get("delay_ms",
                                                     self.default_delay)})
        elif mtype == "ACK":
            pass  # 纯确认（如握手 ACK）不应答
        elif mtype == "FIN":
            action = self._next_action(msg) or {}
            d = action.get("delay_ms", self.default_delay)
            self._send(conn, {"type": "FIN", "delay_ms": d})
            self._send(conn, {"type": "FIN_ACK", "delay_ms": d})
        elif mtype == "DATA":
            if self.held is not None:
                # 乱序注入：先应答当前段，再补发被缓存的确认
                self._handle_data(msg, conn, None)
                self._send(conn, self.held)
                self.held = None
                return
            action = self._next_action(msg)
            kind = (action or {}).get("kind", "ack")
            if kind == "drop":
                self.truth_log.append({"dir": "drop", "msg": msg})
            elif kind == "reorder":
                if msg["seq"] == self.expected:
                    self.expected += msg["len"]
                self.held = {"type": "ACK", "ack": self.expected, "dup": False,
                             "delay_ms": action.get("delay_ms",
                                                    self.default_delay)}
            else:
                self._handle_data(msg, conn, action)

    def _handle_data(self, msg, conn, action):
        action = action or {}
        kind = action.get("kind", "ack")
        delay = action.get("delay_ms", self.default_delay)
        if "expect" in action:
            self.expected = action["expect"]
        elif msg["seq"] == self.expected:
            self.expected += msg["len"]
        if kind == "dup_ack":
            for _ in range(action.get("count", 3)):
                self._send(conn, {"type": "ACK",
                                  "ack": action.get("ack", self.expected),
                                  "dup": True, "delay_ms": delay})
            return
        in_order = msg["seq"] + msg["len"] <= self.expected
        self._send(conn, {"type": "ACK",
                          "ack": action.get("ack", self.expected),
                          "dup": not in_order, "delay_ms": delay})

    def stop(self):
        try:
            self._srv.close()
        except OSError:
            pass
