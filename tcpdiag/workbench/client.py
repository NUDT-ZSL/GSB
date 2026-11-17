"""把 SessionCore 绑定到本地回环 TCP 传输（换行分隔的 JSON 报文）。"""

import json
import queue
import socket
import threading


class SessionClient:
    def __init__(self, core, host="127.0.0.1", port=0):
        self.core = core
        self.addr = (host, port)
        self.sock = None
        self.rx = queue.Queue()
        self._reader = None

    def connect(self):
        """新建连接并发起三次握手。"""
        self._connect_socket()
        self.core.open()
        self.flush()

    def attach(self):
        """快照恢复后接管会话：仅建立传输，不重新握手。"""
        self._connect_socket()

    def _connect_socket(self):
        self.sock = socket.create_connection(self.addr, timeout=5.0)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            f = self.sock.makefile("r", encoding="utf-8")
            for line in f:
                line = line.strip()
                if line:
                    self.rx.put(json.loads(line))
        except OSError:
            pass

    def flush(self):
        while self.core.outbox:
            msg = self.core.outbox.pop(0)
            self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def _dispatch(self, msg):
        mtype = msg.get("type")
        if mtype == "SYN_ACK":
            self.core.on_syn_ack(msg.get("delay_ms", 0))
        elif mtype == "ACK":
            self.core.on_ack(msg["ack"], msg.get("dup", False),
                             msg.get("delay_ms", 0))
        elif mtype == "FIN":
            self.core.on_fin(msg.get("delay_ms", 0))
        elif mtype == "FIN_ACK":
            self.core.on_fin_ack(msg.get("delay_ms", 0))
        self.flush()

    def pump(self, first_timeout=0.5, grace=0.05):
        """同步收取并处理一批对端报文；对端丢包时在 first_timeout 后返回 0。"""
        try:
            msg = self.rx.get(timeout=first_timeout)
        except queue.Empty:
            return 0
        n = 0
        while True:
            self._dispatch(msg)
            n += 1
            try:
                msg = self.rx.get(timeout=grace)
            except queue.Empty:
                return n

    def pump_nonblocking(self):
        n = 0
        while True:
            try:
                msg = self.rx.get_nowait()
            except queue.Empty:
                return n
            self._dispatch(msg)
            n += 1

    def send_data(self, total_len):
        remainder = self.core.send_data(total_len)
        self.flush()
        return remainder

    def tick(self):
        fired = self.core.tick()
        if fired:
            self.flush()
        return fired

    def close(self):
        self.core.close()
        self.flush()

    def shutdown(self):
        try:
            self.sock.close()
        except (OSError, AttributeError):
            pass
