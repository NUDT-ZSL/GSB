"""本地授权模拟下载服务（仅标准库）。

提供两种服务，均接收 GET ``<endpoint>?filename=...``：

* ``vulnerable``（存在穿越缺陷）：直接拼接基目录与 filename 读取文件，
  可读出基目录之外的标记文件。
* ``fixed``（已修复）：对规范化后的真实路径做基目录边界校验，
  越界访问返回 HTTP 403，并通过 X-Reject-Reason 头与响应体给出原因。

另外支持两种故障注入模式，用于验证扫描器的“未知”判定：

* ``--drop-after N``：处理到第 N 个请求时在其响应期间放下连接闸门，
  已建立的连接照常完成响应，此后新连接被立即关闭（扫描器得到 unknown）。
  即前 N-1 个请求确定完成，第 N 个起的新连接确定失败。
* ``--hang``：每个请求都挂起不响应，配合扫描器超时得到 unknown。

服务会把每个请求收到的**原始查询串**（self.path 的 query 部分，
服务端未经任何解码）追加写入访问日志，供复核“百分号编码逐字一致”。
"""
from __future__ import annotations

import argparse
import datetime
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class GatedHTTPServer(ThreadingHTTPServer):
    """支持“连接闸门”的 HTTP 服务。

    ``arm_drop_gate()`` 之后，新 TCP 连接仍会被 accept（避免 Windows 上
    直接关闭监听 socket 导致 select 报错），但立即关闭，客户端表现为
    连接重置/异常断开 —— 扫描器据此判定 unknown。已建立的连接不受影响。
    """

    daemon_threads = True

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.drop_gate = threading.Event()

    def arm_drop_gate(self) -> None:
        self.drop_gate.set()

    def get_request(self) -> tuple[object, object]:
        request, client_address = self.socket.accept()
        if self.drop_gate.is_set():
            # 闸门已关闭：立刻断开新连接，不交给任何 handler。
            try:
                request.close()
            except OSError:
                pass
            raise ConnectionError("drop gate closed: new connections are refused")
        return request, client_address

    def handle_error(self, request: object, client_address: object) -> None:
        # 闸门主动制造的连接错误属于预期行为，静默处理；其他错误照旧输出。
        if self.drop_gate.is_set():
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    # 以下类属性由 make_server 按实例注入。
    base_dir: str = ""
    mode: str = "vulnerable"
    log_path: str | None = None
    drop_after: int | None = None
    hang: bool = False
    _counter_lock = threading.Lock()
    _served = 0
    _drop_armed = False
    server_ref: "ThreadingHTTPServer | None" = None
    hang_blocker: threading.Event | None = None  # 测试中可置位以解除挂起

    # 静默默认访问日志，原始查询串由我们自己的 _write_raw_log 记录。
    def log_message(self, fmt: str, *args: object) -> None:
        return

    # ------------------------------------------------------------------ #
    def _write_raw_log(self) -> None:
        if not self.log_path:
            return
        line = "{ts}\t{mode}\t{raw_path}\n".format(
            ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            mode=self.mode,
            raw_path=self.path,
        )
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line)

    # ------------------------------------------------------------------ #
    def _send_text(self, status: int, message: str,
                   extra_headers: dict[str, str] | None = None) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        self._write_raw_log()

        if self.hang:
            # 挂起直到超时（或测试代码置位 blocker）。
            blocker = self.hang_blocker or threading.Event()
            blocker.wait(timeout=30)
            return

        # drop-after 计数：记录这是第几个请求。闸门在第 N 个请求的**前一个**
        # 请求处理时放下（即第 max(1, N-1) 个请求开始时）：该请求在已建立
        # 的连接上照常完整响应，而服务端 accept 循环随即开始拒绝新连接。
        # 这样即使客户端并发快速发起请求，也无法在闸门生效前挤进监听队列。
        if self.drop_after is not None:
            with self._counter_lock:
                self._served += 1
                seq = self._served
                arm_at = max(1, self.drop_after - 1)
                hit = seq >= arm_at and not self._drop_armed
                if hit:
                    self._drop_armed = True
            if hit and self.server_ref is not None:
                self.server_ref.arm_drop_gate()
                threading.Thread(
                    target=self.server_ref.shutdown, daemon=True
                ).start()

        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if "filename" not in params:
            self._send_text(400, "missing filename parameter\n")
            return
        filename = params["filename"][0]  # 服务端按自身规则解码后用于读盘

        if self.mode == "fixed":
            self._handle_fixed(filename)
        else:
            self._handle_vulnerable(filename)

    # ------------------------------------------------------------------ #
    def _handle_vulnerable(self, filename: str) -> None:
        """缺陷实现：os.path.join 可被绝对路径/.. 带出基目录，无边界校验。"""
        target = os.path.join(self.base_dir, filename)
        self._serve_file(target)

    def _handle_fixed(self, filename: str) -> None:
        """修复实现：规范化真实路径并强制位于基目录之内。"""
        base_real = os.path.realpath(self.base_dir)
        target = os.path.realpath(os.path.join(base_real, filename))
        if target != base_real and not target.startswith(base_real + os.sep):
            reason = (
                "path traversal rejected: resolved path is outside base directory"
            )
            self._send_text(
                403,
                reason + "\n",
                extra_headers={"X-Reject-Reason": reason},
            )
            return
        self._serve_file(target)

    def _serve_file(self, target: str) -> None:
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            self._send_text(404, f"file not found: {target}\n")
            return
        except IsADirectoryError:
            self._send_text(400, "target is a directory\n")
            return
        except OSError as exc:
            self._send_text(500, f"read error: {exc}\n")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(host: str, port: int, base_dir: str, mode: str,
                log_path: str | None = None,
                drop_after: int | None = None,
                hang: bool = False) -> ThreadingHTTPServer:
    """构造一个绑定好端口的模拟服务实例（尚未开始 serve）。"""
    if mode not in ("vulnerable", "fixed"):
        raise ValueError(f"未知模式: {mode}")
    handler = type("_BoundHandler", (_Handler,), {
        "base_dir": os.path.abspath(base_dir),
        "mode": mode,
        "log_path": log_path,
        "drop_after": drop_after,
        "hang": hang,
        "_served": 0,
        "_drop_armed": False,
        "_counter_lock": threading.Lock(),
        "server_ref": None,
        "hang_blocker": None,
    })
    httpd = GatedHTTPServer((host, port), handler)
    handler.server_ref = httpd
    return httpd


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mock_server",
        description="本地授权文件下载模拟服务（vulnerable / fixed）",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--base-dir", required=True, help="下载基目录")
    parser.add_argument(
        "--mode", choices=("vulnerable", "fixed"), required=True,
    )
    parser.add_argument(
        "--log", default=None, help="原始查询串访问日志输出路径",
    )
    parser.add_argument(
        "--drop-after", type=int, default=None,
        help="成功处理 N 个请求后关闭服务（故障注入）",
    )
    parser.add_argument(
        "--hang", action="store_true",
        help="挂起所有请求不响应（故障注入，配合扫描器超时）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    httpd = make_server(
        args.host, args.port, args.base_dir, args.mode,
        log_path=args.log, drop_after=args.drop_after, hang=args.hang,
    )
    extras = []
    if args.drop_after is not None:
        extras.append(f"drop-after={args.drop_after}")
    if args.hang:
        extras.append("hang")
    print(
        f"mock_server[{args.mode}] 监听 http://{args.host}:{args.port} "
        f"基目录={os.path.abspath(args.base_dir)} {' '.join(extras)}".rstrip()
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("mock_server 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
