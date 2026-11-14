#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复版登录服务（仅用于本地离线安全检测演示）。

与漏洞版行为一致，但登录 SQL 使用参数化查询，已修补 SQL 注入。

启动: python samples/fixed_server.py --port 18302
接口:
  GET /health                      -> 200 "ok"
  GET /login?username=..&password=.. -> 登录结果
"""
import argparse
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

DB = sqlite3.connect(":memory:", check_same_thread=False)
DB.execute("CREATE TABLE users (username TEXT, password TEXT)")
DB.executemany(
    "INSERT INTO users VALUES (?, ?)",
    [("admin", "S3cret!Admin"), ("alice", "wonderland123"), ("bob", "builder456")],
)


def run_query(username, password):
    # 修复点：参数化查询，用户输入不参与 SQL 结构
    sql = "SELECT username, password FROM users WHERE username = ? AND password = ?"
    return DB.execute(sql, (username, password)).fetchall()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, "ok")
            return
        if parsed.path == "/login":
            q = parse_qs(parsed.query)
            username = q.get("username", [""])[0]
            password = q.get("password", [""])[0]
            try:
                rows = run_query(username, password)
            except Exception:
                self._send(200, "登录失败: 数据库错误")
                return
            if rows:
                lines = ["登录成功"] + ["%s:%s" % (u, p) for u, p in rows]
                self._send(200, "\n".join(lines))
            else:
                self._send(200, "登录失败")
            return
        self._send(404, "not found")

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18302)
    args = ap.parse_args()
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print("fixed_server listening on 127.0.0.1:%d" % args.port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
