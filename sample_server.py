# -*- coding: utf-8 -*-
"""
sample_server.py —— 确定性本地 HTTP 测试服务（仅用于授权体检演示）

纯标准库实现，提供三种模式：

  vulnerable（易受攻击）
      /vuln?file=... 等常见包含参数会把参数值拼到 samples/sensitive/
      目录下读取并原样返回。当扫描器发送目录遍历载荷命中夹具里的
      win.ini / passwd 时，响应中会带敏感文件特征串 -> 被判为疑似LFI。

  safe（安全）
      /safe 忽略任何包含参数，固定返回与敏感文件无关的 HTML 页面，
      响应中永远不出现特征串 -> 被判为确认安全。

  slow（慢响应）
      /slow 每个请求固定睡眠若干秒，配合扫描器的短超时模拟连接超时。

所有“敏感文件”均为仓库内 samples/sensitive/ 下的自制夹具，
服务绝不读取夹具目录之外的任何真实系统文件。

手动启动示例：
  python sample_server.py vulnerable --port 8011
  python sample_server.py safe       --port 8012
  python sample_server.py slow       --port 8013 --delay 3
"""

import argparse
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 与扫描器保持一致的常见包含参数（独立复制，避免跨模块耦合）
INCLUDE_PARAMS = [
    "file", "filename", "path", "page", "include", "doc",
    "document", "template", "view", "load",
]

# 夹具根目录：本文件同级 samples/sensitive，确定性且与真实系统隔离
SAMPLE_ROOT = Path(__file__).resolve().parent / "samples" / "sensitive"

# 遍历载荷 -> 夹具文件名 的白名单映射（只允许命中这两个自制文件）
PAYLOAD_TO_FIXTURE = {
    "win.ini": "win.ini",
    "passwd": "passwd",
}

SAFE_PAGE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<title>内部工作台</title></head><body>"
    "<h1>欢迎使用内部工作台</h1>"
    "<p>本页面仅展示静态公告，不读取任何服务器本地文件。</p>"
    "<ul><li>公告一：系统运行正常</li><li>公告二：例行维护已完成</li></ul>"
    "</body></html>"
).encode("utf-8")


def make_handler(mode, delay):
    """按模式构造 HTTP 请求处理类。"""

    class SampleHandler(BaseHTTPRequestHandler):
        server_version = "AuthorizedLFISample/1.0"

        def log_message(self, *args):
            # 保持演示输出整洁；如需调试可改为打印到 stderr
            return

        def _send(self, body, status=200, content_type="text/plain; charset=utf-8"):
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # 客户端因超时主动断开（演示中的 slow 场景），属于预期行为
                return

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

            if mode == "slow":
                # 确定性慢响应：睡眠超过扫描器超时时间即可触发超时条目
                time.sleep(delay)
                self._send(b"slow page", status=200)
                return

            if mode == "safe":
                # 安全页面：无论带什么参数都只返回无关 HTML，且不含任何特征串
                self._send(SAFE_PAGE, content_type="text/html; charset=utf-8")
                return

            # vulnerable 模式：仅模拟“参数值被拼进文件路径”的缺陷行为
            fixture_name = None
            for param in INCLUDE_PARAMS:
                if param in query and query[param]:
                    candidate = query[param][0]
                    # 只认白名单夹具，绝不触碰真实文件系统
                    for marker, name in PAYLOAD_TO_FIXTURE.items():
                        norm = candidate.replace("\\", "/")
                        if norm.endswith(marker) or norm == marker:
                            fixture_name = name
                            break
                if fixture_name:
                    break

            if fixture_name is None:
                self._send(b"file not found", status=404)
                return

            fixture_path = SAMPLE_ROOT / fixture_name
            try:
                data = fixture_path.read_bytes()
            except OSError as exc:
                self._send(("read error: %s" % exc).encode("utf-8"), status=500)
                return
            self._send(data)

    return SampleHandler


def build_server(mode, port, delay=3.0):
    """供自动化演示以线程方式嵌入调用。"""
    handler = make_handler(mode, delay)
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="LFI 体检用确定性本地样本服务")
    parser.add_argument("mode", choices=["vulnerable", "safe", "slow"],
                        help="服务模式")
    parser.add_argument("--port", type=int, default=8011, help="监听端口")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="slow 模式每个请求的睡眠秒数")
    args = parser.parse_args(argv)

    server = build_server(args.mode, args.port, args.delay)
    print("样本服务[%s]已启动: http://127.0.0.1:%d/ （Ctrl+C 停止）"
          % (args.mode, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n样本服务已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
