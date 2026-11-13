"""本地确定性靶场服务（仅监听 127.0.0.1，无外部依赖）。

三种运行模式：
  vulnerable  漏洞版：username 参数存在 SQL 注入 / 反射型 XSS / 命令注入 / SSTI / NoSQL 缺陷，
              item 参数对特定载荷返回普通 500（无注入特征），用于验证参数级错误隔离。
  fixed       修复版：参数化处理 + HTML 转义 + 命令执行与模板求值全部移除，无任何注入特征。

用法：
  python -m lab.vuln_server --mode vulnerable --port 8011
  python -m lab.vuln_server --mode fixed      --port 8012
  python -m lab.vuln_server --mode vulnerable --port 8011 --start-delay 8   # 延迟启动
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SQL_STRING_MARKERS = ("'", '"')
SQL_KEYWORDS = (" or ", " and ", " union ", " select ", " drop ", "--", ";")


def _is_sql_attack(value: str) -> bool:
    """模拟存在缺陷的字符串拼接 SQL：单/双引号未闭合或携带 SQL 关键字即触发数据库报错。"""
    lowered = value.lower()
    if any(marker in value for marker in SQL_STRING_MARKERS):
        return True
    return any(kw in lowered for kw in SQL_KEYWORDS)


def _sql_error_response(value: str) -> tuple[int, str]:
    body = (
        "<html><body><h1>500 Internal Server Error</h1>"
        "<pre>sqlite3.OperationalError: near &#x27;"
        + html.escape(value)
        + "&#x27;: SQL syntax error (unclosed quotation mark) "
        "[SQL: SELECT id FROM users WHERE name=&#x27;" + html.escape(value) + "&#x27;]</pre></body></html>"
    )
    return 500, body


def handle_vulnerable_username(value: str) -> tuple[int, str]:
    """漏洞版 username：直接拼接 SQL、原样回显输入、执行命令、求值模板。"""
    # 1) SQL 注入 -> 数据库报错
    if _is_sql_attack(value):
        return _sql_error_response(value)

    # 2) 命令注入 -> shell 执行结果回显
    if any(token in value for token in (";", "|", "$(", "`")) and re.search(r"id|whoami", value):
        return 200, (
            "<html><body>查询结果: <pre>uid=0(root) gid=0(root) groups=0(root)</pre></body></html>"
        )

    # 3) SSTI -> 模板表达式被服务端求值
    if "{{7*7}}" in value or "${7*7}" in value:
        rendered = value.replace("{{7*7}}", "49").replace("${7*7}", "49")
        return 200, f"<html><body>模板渲染结果: {rendered}</body></html>"

    # 4) NoSQL 操作符注入 -> 存储层报错
    if "$ne" in value or "$gt" in value or "bson" in value.lower():
        return 500, (
            "<html><body><pre>MongoDB BSONError: unknown operator: $ne "
            "(bad query against users collection)</pre></body></html>"
        )

    # 5) 反射型 XSS -> 输入未编码原样出现在响应中
    if re.search(r"<\s*script|onerror|onload|<\s*img", value, re.IGNORECASE):
        return 200, f"<html><body>搜索用户 {value} 的结果页</body></html>"

    # 正常响应：普通 200，不含任何注入特征
    return 200, json.dumps({"ok": True, "echo": html.escape(value), "rows": []}, ensure_ascii=False)


def handle_vulnerable_item(value: str) -> tuple[int, str]:
    """漏洞版 item：本身无注入缺陷，但对堆叠查询载荷返回普通 500（无注入特征）。"""
    if "drop" in value.lower():
        # 普通内部错误：不含数据库/命令/模板等任何注入签名
        return 500, "<html><body><h1>500 Internal Server Error</h1><pre>item service internal failure</pre></body></html>"
    return 200, json.dumps({"ok": True, "item": html.escape(value)}, ensure_ascii=False)


def handle_fixed(value: str, param: str) -> tuple[int, str]:
    """修复版：统一安全处理（参数化查询 + HTML 转义），任何载荷都只得到普通响应。"""
    safe = html.escape(value)
    return 200, json.dumps(
        {"ok": True, "param": param, "echo": safe, "rows": [], "note": "parameterized query"},
        ensure_ascii=False,
    )


def make_handler(mode: str) -> type[BaseHTTPRequestHandler]:
    class VulnHandler(BaseHTTPRequestHandler):
        server_mode = mode

        def log_message(self, *_args) -> None:  # 静默，保持演示输出干净
            return

        def handle_one_request(self) -> None:
            # 客户端主动断连（扫描器超时/连接复用）时静默，不打印堆栈
            try:
                super().handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/query":
                self._send(404, "<html><body>404 Not Found</body></html>")
                return

            params = parse_qs(parsed.query, keep_blank_values=True)
            # 可达性探测：任何参数都返回 200，证明服务存活
            if "__probe" in params:
                self._send(200, json.dumps({"ok": True, "service": self.server_mode}))
                return

            if "username" in params:
                value = params["username"][0]
                if self.server_mode == "vulnerable":
                    status, body = handle_vulnerable_username(value)
                else:
                    status, body = handle_fixed(value, "username")
                self._send(status, body)
                return

            if "item" in params:
                value = params["item"][0]
                if self.server_mode == "vulnerable":
                    status, body = handle_vulnerable_item(value)
                else:
                    status, body = handle_fixed(value, "item")
                self._send(status, body)
                return

            self._send(400, "<html><body>400 Bad Request: missing username/item</body></html>")

        def _send(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return VulnHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="本地注入靶场服务（仅监听 127.0.0.1）")
    parser.add_argument("--mode", choices=["vulnerable", "fixed"], required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--start-delay", type=float, default=0.0,
                        help="进程启动后延迟 N 秒再监听（用于验证扫描器的 --delay 等待能力）")
    args = parser.parse_args()

    if args.start_delay > 0:
        print(f"[lab] 将在 {args.start_delay} 秒后启动 {args.mode} 服务 ...", flush=True)
        time.sleep(args.start_delay)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.mode))
    print(f"[lab] {args.mode} 服务已启动: http://{args.host}:{args.port}/query", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
