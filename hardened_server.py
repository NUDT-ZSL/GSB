"""加固后的文档预览服务。

加固措施：
1. 文档名非空、长度受限（<=128），且必须是不含路径分隔符的纯文件名；
2. 解析真实路径后用 commonpath 校验必须落在 uploads 目录内；
3. 拒绝响应使用通用文案，不回显绝对路径、不泄露目录外文件内容；
4. 文本按 UTF-8 解码，非 UTF-8 文件返回可读的 415 错误，进程不中断。

用法: python hardened_server.py [port]   默认 18082
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from common import UPLOADS, generate_samples

MAX_NAME_LEN = 128
UPLOADS_REAL = os.path.realpath(UPLOADS)


class HardenedHandler(BaseHTTPRequestHandler):
    server_version = "HardenedPreview/1.0"

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

        # --- 输入校验：空名 / 超长名 / 非纯文件名 ---
        if not name:
            self._send(400, "invalid document name\n")
            return
        if len(name) > MAX_NAME_LEN:
            self._send(400, "invalid document name\n")
            return
        if name != os.path.basename(name) or name in (".", ".."):
            # 含路径分隔符或 .. 等，直接拒绝；不回显任何路径信息
            self._send(403, "access denied\n")
            return

        # --- 路径校验：解析后的真实路径必须仍在 uploads 内 ---
        candidate = os.path.realpath(os.path.join(UPLOADS_REAL, name))
        try:
            inside = os.path.commonpath([candidate, UPLOADS_REAL]) == UPLOADS_REAL
        except ValueError:
            inside = False
        if not inside:
            self._send(403, "access denied\n")
            return

        if not os.path.isfile(candidate):
            self._send(404, "document not found\n")
            return
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            self._send(415, "document is not valid UTF-8 text\n")
            return
        except OSError:
            self._send(400, "cannot read document\n")
            return
        self._send(200, content)

    def log_message(self, fmt, *args):
        pass


def make_server(port):
    generate_samples()
    return ThreadingHTTPServer(("127.0.0.1", port), HardenedHandler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18082
    srv = make_server(port)
    print("加固预览服务已启动: http://127.0.0.1:%d/preview?name=hello.txt" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
