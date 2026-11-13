"""未加固的文档预览服务（漏洞演示版本）。

漏洞点：直接把客户端给的文档名与 uploads 目录拼接，
不做任何校验，形如 ../outside/secret.txt 的名字可越权读取目录外文件。

仅用于本地授权演示，请勿部署。
用法: python vulnerable_server.py [port]   默认 18081
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from common import UPLOADS, generate_samples


class VulnerableHandler(BaseHTTPRequestHandler):
    server_version = "VulnerablePreview/1.0"

    def _send(self, code, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/preview":
            self._send(404, "unknown endpoint\n")
            return
        name = parse_qs(parsed.query, keep_blank_values=True).get("name", [""])[0]

        # === 漏洞：未校验，直接拼接路径 ===
        path = os.path.join(UPLOADS, name)

        if not os.path.isfile(path):
            self._send(404, "document not found: %s\n" % name)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except (UnicodeDecodeError, OSError):
            self._send(400, "document is not valid UTF-8 text\n")
            return
        self._send(200, content)

    def log_message(self, fmt, *args):  # 静默日志，保持演示输出干净
        pass


def make_server(port):
    generate_samples()
    return ThreadingHTTPServer(("127.0.0.1", port), VulnerableHandler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18081
    srv = make_server(port)
    print("未加固预览服务已启动: http://127.0.0.1:%d/preview?name=hello.txt" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
