"""本地授权演示站点：同一套页面提供脆弱版与修复版。

用法:
    python demo_site/server.py --port 8033 --mode vulnerable
    python demo_site/server.py -- port 8034 --mode fixed

安全约束:
  * 仅绑定 127.0.0.1，不监听任何外部网卡；
  * 仅用于本机授权探测演示，不得部署到非本机环境。

页面说明:
  GET  /reflect?q=   文本上下文回显
  GET  /attr?q=      属性上下文回显（input value）
  GET  /script?q=    脚本上下文回显（<script> 内变量赋值）
  GET  /path/<片段>  路径片段回显（文本上下文）
  POST /form         表单字段 msg 回显（文本上下文）
  GET  /empty?q=     返回空响应体
  GET  /safe?q=      始终做 HTML 转义的页面（两种模式下都安全）
"""

from __future__ import annotations

import argparse
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

PAGE_HEAD = (
    "<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
    "<title>本机授权演示站点</title></head><body>"
)
PAGE_TAIL = "</body></html>"


def build_handler(mode: str) -> type[BaseHTTPRequestHandler]:
    fixed = mode == "fixed"

    def render(value: str) -> str:
        """修复版对回显值统一做 HTML 转义；脆弱版原样拼接。"""
        return html.escape(value, quote=True) if fixed else value

    class DemoHandler(BaseHTTPRequestHandler):
        server_version = "LoopbackDemo/1.0"

        def log_message(self, fmt, *args):  # 安静模式，避免污染演示输出
            return

        def _send_html(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_empty(self, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _query_value(self) -> str:
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            return query.get("q", [""])[0]

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            value = self._query_value()

            if path == "/reflect":
                self._send_html(
                    PAGE_HEAD
                    + f"<h1>文本上下文回显</h1><div>你输入的是：{render(value)}</div>"
                    + PAGE_TAIL
                )
            elif path == "/attr":
                self._send_html(
                    PAGE_HEAD
                    + f"<h1>属性上下文回显</h1>"
                    + f"<input type=\"text\" name=\"q\" value=\"{render(value)}\">"
                    + PAGE_TAIL
                )
            elif path == "/script":
                self._send_html(
                    PAGE_HEAD
                    + f"<script>var q = \"{render(value)}\";</script>"
                    + PAGE_TAIL
                )
            elif path.startswith("/path/"):
                seg = unquote(path[len("/path/"):])
                self._send_html(
                    PAGE_HEAD
                    + f"<h1>路径片段回显</h1><div>片段内容：{render(seg)}</div>"
                    + PAGE_TAIL
                )
            elif path == "/empty":
                # 无论参数是什么都返回空体，用于“未获得有效响应”判定
                self._send_empty()
            elif path == "/safe":
                # 始终转义，作为稳定的无风险对照页
                self._send_html(
                    PAGE_HEAD
                    + f"<h1>安全回显页</h1><div>你输入的是：{html.escape(value, quote=True)}</div>"
                    + PAGE_TAIL
                )
            else:
                self._send_html(PAGE_HEAD + "<p>页面不存在</p>" + PAGE_TAIL, status=404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/form":
                self._send_html(PAGE_HEAD + "<p>页面不存在</p>" + PAGE_TAIL, status=404)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            form = parse_qs(raw, keep_blank_values=True)
            value = form.get("msg", [""])[0]
            self._send_html(
                PAGE_HEAD
                + f"<h1>表单回显</h1><div>提交内容：{render(value)}</div>"
                + PAGE_TAIL
            )

    return DemoHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="本机回环授权演示站点")
    parser.add_argument("--host", default="127.0.0.1",
                        help="绑定地址，固定为本机回环，不允许改为其他地址")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--mode", choices=("vulnerable", "fixed"), required=True)
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("演示站点仅允许绑定本机回环地址")

    server = ThreadingHTTPServer((args.host, args.port), build_handler(args.mode))
    print(f"演示站点已启动: http://{args.host}:{args.port} 模式={args.mode}（仅本机访问）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
