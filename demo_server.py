#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_server.py — 安全团队自建本地授权演示端点（仅监听 127.0.0.1）

端点：
  /normal   正常页面：原样反射输入（不应误判为漏洞）
  /vuln     漏洞页面：执行 ${A*B} 并返回 "结果:49"
  /suspect  疑似页面：含模板特征的输入使响应长度变化（100 -> 115），不反射
  /error    返回 500
  /big      返回 2MB 响应体
  /slow     睡眠 6 秒（触发客户端超时）
  /headers  回显收到的请求头，并写入 captured_headers.json（验收请求头约定）
"""
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST, PORT = "127.0.0.1", 8377


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 静默默认访问日志，改用自定义输出
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        val = parse_qs(parsed.query).get("input", [""])[0]
        print("[server] {} raw-query={}".format(parsed.path, parsed.query), flush=True)

        if parsed.path == "/normal":
            self._send(200, "欢迎访问演示页面，你输入了：{}。这是正常反射型页面。".format(val))
        elif parsed.path == "/vuln":
            m = re.fullmatch(r"\$\{\s*(\d+)\s*\*\s*(\d+)\s*\}", val)
            body = "结果:{}".format(int(m.group(1)) * int(m.group(2))) if m else "结果:"
            self._send(200, body)
        elif parsed.path == "/suspect":
            self._send(200, "S" * (115 if "{" in val else 100))
        elif parsed.path == "/error":
            self._send(500, "Internal Server Error（模拟）")
        elif parsed.path == "/big":
            self._send(200, "B" * (2 * 1024 * 1024))
        elif parsed.path == "/slow":
            time.sleep(6)
            self._send(200, "slow response")
        elif parsed.path == "/headers":
            headers = {k: v for k, v in self.headers.items()}
            with open("captured_headers.json", "w", encoding="utf-8") as f:
                json.dump(headers, f, ensure_ascii=False, indent=2)
            print("[server] /headers 捕获请求头: {}".format(headers), flush=True)
            self._send(200, json.dumps(headers, ensure_ascii=False))
        else:
            self._send(404, "not found")


if __name__ == "__main__":
    print("[server] 监听 http://{}:{}".format(HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
