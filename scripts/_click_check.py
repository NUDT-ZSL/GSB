"""零依赖 CDP 驱动（标准库 socket 实现最小 WebSocket 帧）：
点击差异列表首行并截图，验证两侧同时定位高亮。核验后可删。
"""
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

HTML_URL = "file:///E:/Company/demo/projects/project77/out/demo/preview.html"
OUT_SHOT = Path("out/demo/preview_click_shot.png")


class MiniWS:
    def __init__(self, url: str):
        assert url.startswith("ws://")
        host_port, path = url[5:].split("/", 1)
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)

    def send(self, payload: str) -> None:
        data = payload.encode("utf-8")
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        chunks, first = [], True
        while True:
            head = self._recv_n(2)
            opcode = head[0] & 0x0F
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_n(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_n(8))[0]
            payload = self._recv_n(length)
            chunks.append(payload)
            if opcode == 0x8:  # close
                raise ConnectionError("websocket closed")
            if not (head[0] & 0x80):  # 还有续帧
                continue
            return b"".join(chunks).decode("utf-8", "replace")

    def _recv_n(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            part = self.sock.recv(n - len(out))
            if not part:
                raise ConnectionError("socket closed")
            out += part
        return out

    def close(self) -> None:
        self.sock.close()


def main() -> None:
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    profile = tempfile.mkdtemp(prefix="chrome-cdp-")
    port = 9223
    proc = subprocess.Popen([
        chrome, "--headless", "--disable-gpu", "--no-sandbox",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        "--window-size=1600,1000", HTML_URL,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    tabs = []
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as r:
                tabs = json.load(r)
            if tabs:
                break
        except OSError:
            time.sleep(0.25)
    tab = next(t for t in tabs if t.get("type") == "page")
    ws = MiniWS(tab["webSocketDebuggerUrl"])
    counter = 0

    def cdp(method: str, params: dict | None = None) -> dict:
        nonlocal counter
        counter += 1
        mid = counter
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg

    cdp("Page.enable")
    time.sleep(0.8)
    cdp("Runtime.evaluate", {
        "expression": "document.querySelector('tr.diff-row').click(); 'clicked'",
    })
    time.sleep(1.0)
    state = cdp("Runtime.evaluate", {
        "expression": (
            "JSON.stringify({selected: document.querySelector('tr.selected')?.dataset.index,"
            "oldDisplay: getComputedStyle(document.getElementById('hlOld')).display,"
            "newDisplay: getComputedStyle(document.getElementById('hlNew')).display,"
            "oldBox: document.getElementById('hlOld').getBoundingClientRect().toJSON(),"
            "newBox: document.getElementById('hlNew').getBoundingClientRect().toJSON(),"
            "tip: document.getElementById('tipNew').textContent})"
        ),
        "returnByValue": True,
    })
    print(state["result"]["result"]["value"])
    shot = cdp("Page.captureScreenshot", {"format": "png"})
    OUT_SHOT.write_bytes(base64.b64decode(shot["result"]["data"]))
    ws.close()
    proc.terminate()
    print("saved", OUT_SHOT)


if __name__ == "__main__":
    main()
