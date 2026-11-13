"""本地模拟供应商页面服务器（仅回环访问，标准库实现）。

职责：
  - 只绑定回环地址（127.0.0.1 / ::1），不对外网暴露；
  - 根据 URL 路径中的页面标识返回 samples 目录下对应的确定性 HTML 样本；
  - 支持故障注入，便于验收采集器的容错行为：
      --flaky-fail N   某个页面标识前 N 次访问返回 HTTP 503，之后恢复正常
      --flaky-page ID  指定故障注入的页面标识（默认不注入）

用法：
  python supplier_server.py --samples-dir samples --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# Windows 控制台默认可能是 GBK，统一强制 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


class SampleHandler(BaseHTTPRequestHandler):
    # 由 make_server 在创建子类时注入
    samples_dir: Path = Path()
    flaky_page: str | None = None
    flaky_remaining: dict[str, int] = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server 固定命名
        page_id = unquote(urlparse(self.path).path).lstrip("/")

        if self.flaky_page and page_id == self.flaky_page and self.flaky_remaining.get(page_id, 0) > 0:
            self.flaky_remaining[page_id] -= 1
            self._send(503, b"Service Temporarily Unavailable", "text/plain; charset=utf-8")
            return

        sample_path = (self.samples_dir / f"{page_id}.html").resolve()
        # 防止路径穿越：样本文件必须位于 samples_dir 之内
        if not self._is_within_samples(sample_path) or not sample_path.is_file():
            self._send(404, b"Sample Not Found", "text/plain; charset=utf-8")
            return

        body = sample_path.read_bytes()
        self._send(200, body, "text/html; charset=utf-8")

    def _is_within_samples(self, target: Path) -> bool:
        try:
            target.relative_to(self.samples_dir.resolve())
        except ValueError:
            return False
        return True

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # 纯文本访问日志写到 stderr，不污染采集器自身的日志文件
        sys.stderr.write(f"[supplier_server] {self.address_string()} {fmt % args}\n")


def make_server(host: str, port: int, samples_dir: Path,
                flaky_page: str | None = None, flaky_fail: int = 0) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"仅允许绑定回环地址 {LOOPBACK_HOSTS}，收到: {host!r}")

    samples_dir = samples_dir.resolve()
    if not samples_dir.is_dir():
        raise FileNotFoundError(f"样本目录不存在: {samples_dir}")

    flaky_remaining: dict[str, int] = {}
    if flaky_page is not None:
        if flaky_fail <= 0:
            raise ValueError("--flaky-fail 必须为正整数")
        flaky_remaining[flaky_page] = flaky_fail

    handler = type(
        "ConfiguredSampleHandler",
        (SampleHandler,),
        {
            "samples_dir": samples_dir,
            "flaky_page": flaky_page,
            "flaky_remaining": flaky_remaining,
        },
    )
    return ThreadingHTTPServer((host, port), handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地模拟供应商页面服务器（仅回环）")
    parser.add_argument("--samples-dir", type=Path, default=Path("samples"),
                        help="样本 HTML 所在目录（默认 samples）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="绑定地址，只允许 127.0.0.1 / ::1 / localhost")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，0 表示由系统分配")
    parser.add_argument("--flaky-page", default=None,
                        help="故障注入：前若干次访问返回 503 的页面标识")
    parser.add_argument("--flaky-fail", type=int, default=0,
                        help="故障注入：指定页面标识前 N 次访问失败")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        httpd = make_server(args.host, args.port, args.samples_dir,
                            flaky_page=args.flaky_page, flaky_fail=args.flaky_fail)
    except (ValueError, FileNotFoundError) as exc:
        print(f"[supplier_server] 启动失败: {exc}", file=sys.stderr)
        return 2

    actual_host, actual_port = httpd.server_address[:2]
    print(f"[supplier_server] 样本服务已启动: http://{actual_host}:{actual_port}/ "
          f"(样本目录: {args.samples_dir.resolve()})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[supplier_server] 收到中断，关闭服务", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
