"""本地确定性订单查询 HTTP 服务样本（仅标准库）。

一个进程通过环境变量切换三种模式：
- MODE=vulnerable（默认）：
  1) SQL 拼接用户输入，存在真实 SQL 注入（微型 SQL 引擎可演示布尔/UNION 类效果）；
  2) /orders 只按 order_id 查订单，不校验订单属主，存在越权（IDOR）。
- MODE=fixed：
  1) 参数化查询（占位符绑定），注入载荷只会成为普通字符串，查不到数据；
  2) 强制校验 X-User-Id 与订单属主一致；
  3) 入参做数字/非空格式校验。
- MODE=altered：在 fixed 基础上内置一份被改动的订单数据，用于演示功能回退判据。

另提供仅监听回环地址的管理接口 /admin/* 供演示脚本核对"服务端是否收到请求"，
检测工具本身从不访问这些路径。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# 以 `python -I samples/order_service.py` 启动时，sys.path[0] 可能是调用方目录
# 而非脚本目录，导致同名模块被外部环境污染；这里显式把脚本目录放到最前。
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ----------------------------- 确定性数据 -----------------------------

TOKENS = {
    "tok-alice-001": "alice",
    "tok-bob-002": "bob",
    "tok-carol-003": "carol",
}

ORDERS = [
    {"order_id": 1001, "user_id": "alice", "amount": 120.5, "status": "paid",
     "item": "机械键盘", "created_at": "2026-11-01T10:00:00"},
    {"order_id": 1002, "user_id": "alice", "amount": 89.0, "status": "shipped",
     "item": "显示器支架", "created_at": "2026-11-02T11:30:00"},
    {"order_id": 1003, "user_id": "bob", "amount": 350.0, "status": "paid",
     "item": "降噪耳机", "created_at": "2026-11-03T09:15:00"},
    {"order_id": 1004, "user_id": "carol", "amount": 49.9, "status": "pending",
     "item": "USB-C 扩展坞", "created_at": "2026-11-04T16:45:00"},
    {"order_id": 1005, "user_id": "bob", "amount": 999.0, "status": "refunded",
     "item": "便携显示器", "created_at": "2026-11-05T14:20:00"},
]

# altered 模式：业务数据被改动（模拟修复顺带改坏了正常查询）
ORDERS_ALTERED = [dict(o, amount=o["amount"] + 1, item="【变更】" + o["item"]) for o in ORDERS]


# ----------------------- 微型 SQL 引擎（仅演示用） -----------------------
# 支持语法：
#   SELECT * FROM orders WHERE col = 'literal' [AND/OR col = 'literal'] ...
#   字符串内 '' 转义；-- 行注释；参数占位符 ?
# 这不是通用数据库，只为让"拼接 vs 绑定"的差别在本地确定性地可观察。

class SQLError(Exception):
    pass


def _tokenize(sql: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif ch == "?":
            tokens.append(("PLACEHOLDER", "?"))
            i += 1
        elif ch == "=":
            tokens.append(("OP", "="))
            i += 1
        elif ch == "*":
            tokens.append(("OP", "*"))
            i += 1
        elif ch == "'":
            j = i + 1
            buf: list[str] = []
            while j < len(sql):
                if sql[j] == "'":
                    if j + 1 < len(sql) and sql[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            else:
                raise SQLError("字符串未闭合")
            tokens.append(("STRING", "".join(buf)))
            i = j + 1
        elif ch.isdigit():
            j = i
            while j < len(sql) and sql[j].isdigit():
                j += 1
            tokens.append(("NUMBER", sql[i:j]))
            i = j
        elif ch.isalpha() or ch == "_":
            j = i
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j]
            upper = word.upper()
            if upper in ("SELECT", "FROM", "WHERE", "AND", "OR"):
                tokens.append(("KW", upper))
            else:
                tokens.append(("IDENT", word))
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = len(sql) if j == -1 else j + 1
        else:
            raise SQLError(f"不支持的 SQL 字符：{ch!r}")
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], params: list[str]):
        self.tokens = tokens
        self.pos = 0
        self.params = params

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise SQLError("SQL 意外结束")
        self.pos += 1
        return tok

    def _expect(self, kind: str, value: str | None = None) -> tuple[str, str]:
        tok = self._take()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise SQLError(f"SQL 语法错误，期望 {kind}:{value}，实际 {tok}")
        return tok

    def parse(self) -> list:
        self._expect("KW", "SELECT")
        self._expect("OP", "*")
        self._expect("KW", "FROM")
        table = self._expect("IDENT")[1]
        if table != "orders":
            raise SQLError(f"未知表 {table}")
        self._expect("KW", "WHERE")
        cond = self._parse_or()
        if self._peek() is not None:
            raise SQLError(f"SQL 尾部存在多余记号：{self._peek()}")
        return cond

    def _parse_or(self) -> list:
        nodes = [self._parse_and()]
        while self._peek() == ("KW", "OR"):
            self._take()
            nodes.append(self._parse_and())
        return ["OR", *nodes] if len(nodes) > 1 else nodes[0]

    def _parse_and(self) -> list:
        nodes = [self._parse_cmp()]
        while self._peek() == ("KW", "AND"):
            self._take()
            nodes.append(self._parse_cmp())
        return ["AND", *nodes] if len(nodes) > 1 else nodes[0]

    def _parse_cmp(self) -> list:
        # 左值既可能是列名，也可能是注入进来的字符串/数字字面量（如 '1'='1'）
        left_tok = self._take()
        if left_tok[0] == "IDENT":
            col = left_tok[1]
            is_literal_left = False
        elif left_tok[0] in ("STRING", "NUMBER"):
            col = left_tok[1]
            is_literal_left = True
        else:
            raise SQLError(f"WHERE 条件左值非法：{left_tok}")
        self._expect("OP", "=")
        tok = self._take()
        if tok[0] == "STRING":
            value = tok[1]
        elif tok[0] == "NUMBER":
            value = tok[1]
        elif tok[0] == "PLACEHOLDER":
            if not self.params:
                raise SQLError("占位符缺少绑定参数")
            value = str(self.params.pop(0))
        else:
            raise SQLError(f"WHERE 条件右值非法：{tok}")
        if is_literal_left:
            return ["LITCMP", col, value]
        return ["CMP", col, value]


def _eval_node(node: list, row: dict) -> bool:
    kind = node[0]
    if kind == "AND":
        return all(_eval_node(child, row) for child in node[1:])
    if kind == "OR":
        return any(_eval_node(child, row) for child in node[1:])
    _, col, expected = node
    if kind == "LITCMP":
        # 字面量比较：注入的恒真/恒假条件，数字字面量按数值比
        try:
            return float(col) == float(expected)
        except ValueError:
            return str(col) == expected
    actual = row.get(col)
    if actual is None:
        return False
    # 数字按数值比较，其余按字符串比较（与真实数据库的隐式转换直觉一致）
    if isinstance(actual, (int, float)):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return str(actual) == expected
    return str(actual) == expected


def execute_sql(sql: str, params: list[str], rows: list[dict]) -> list[dict]:
    tokens = _tokenize(sql)
    cond = _Parser(tokens, list(params)).parse()
    return [dict(r) for r in rows if _eval_node(cond, r)]


# ------------------------------- 服务 -------------------------------

class OrderHandler(BaseHTTPRequestHandler):
    server_version = "OrderSample/1.0"
    access_log: list[dict] = []  # 类级共享，仅记录非敏感字段
    _log_lock = threading.Lock()

    # 静默默认访问日志，改由我们自己的结构化记录承载
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _record_access(self, method: str, path: str, query: dict, status: int) -> None:
        with self._log_lock:
            self.access_log.append(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "method": method,
                    "path": path,
                    # 只记录参数键名与长度，不记录 token 头部，避免凭据落盘
                    "param_keys": sorted(query.keys()),
                    "status": status,
                }
            )

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self) -> str | None:
        token = self.headers.get("X-Auth-Token", "")
        return TOKENS.get(token)

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}

        if parts.path == "/health":
            self._record_access("GET", parts.path, query, 200)
            self._send_json(200, {"ok": True, "mode": os.environ.get("MODE", "vulnerable")})
            return
        if parts.path == "/admin/access-log":
            with self._log_lock:
                snapshot = list(self.access_log)
            self._send_json(200, {"count": len(snapshot), "requests": snapshot})
            return
        if parts.path == "/admin/reset":
            with self._log_lock:
                self.access_log.clear()
            self._send_json(200, {"ok": True})
            return
        if parts.path != "/orders":
            self._record_access("GET", parts.path, query, 404)
            self._send_json(404, {"error": "not_found", "message": "路径不存在"})
            return

        user = self._authenticate()
        if user is None:
            self._record_access("GET", parts.path, query, 401)
            self._send_json(401, {"error": "unauthorized", "message": "认证失败"})
            return

        mode = os.environ.get("MODE", "vulnerable")
        rows = ORDERS_ALTERED if mode == "altered" else ORDERS
        order_id = query.get("order_id", "")
        user_id_param = query.get("user_id", "")

        # 修复模式：先做业务格式校验
        if mode in ("fixed", "altered"):
            if not user_id_param.strip():
                self._record_access("GET", parts.path, query, 400)
                self._send_json(400, {"error": "invalid_param", "message": "user_id 不能为空"})
                return
            if not order_id.isdigit() or int(order_id) <= 0:
                self._record_access("GET", parts.path, query, 400)
                self._send_json(
                    400, {"error": "invalid_param", "message": "order_id 必须为正整数"}
                )
                return
            if user_id_param != user:
                self._record_access("GET", parts.path, query, 403)
                self._send_json(403, {"error": "forbidden", "message": "用户标识与认证身份不一致"})
                return

        if mode == "vulnerable":
            # 漏洞点 1：字符串直接拼接进 SQL（模拟真实缺陷代码：无转义、无参数化），
            # 攻击者可通过单引号闭合字符串并注入 OR/注释，改变 WHERE 语义
            sql = (
                "SELECT * FROM orders WHERE order_id = '"
                + order_id
                + "' AND user_id = '"
                + user_id_param
                + "'"
            )
            try:
                matched = execute_sql(sql, [], rows)
            except SQLError as exc:
                self._record_access("GET", parts.path, query, 500)
                self._send_json(500, {"error": "sql_error", "message": str(exc)})
                return
            # 漏洞点 2：不校验订单属主，普通用户可读取他人订单（IDOR）
        else:
            # 修复点 1：占位符绑定参数，注入载荷只会被当作普通字符串
            sql = "SELECT * FROM orders WHERE order_id = ? AND user_id = ?"
            try:
                matched = execute_sql(sql, [order_id, user_id_param], rows)
            except SQLError as exc:
                self._record_access("GET", parts.path, query, 500)
                self._send_json(500, {"error": "sql_error", "message": str(exc)})
                return
            # 修复点 2：属主校验（认证身份必须拥有该订单）
            matched = [r for r in matched if r["user_id"] == user]

        self._record_access("GET", parts.path, query, 200)
        self._send_json(
            200,
            {
                "query": {"order_id": order_id, "user_id": user_id_param},
                "count": len(matched),
                "data": matched,
            },
        )


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), OrderHandler)
    return server


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "18080"))
    mode = os.environ.get("MODE", "vulnerable")
    server = build_server(host, port)
    print(f"订单样本服务已启动: http://{host}:{port}  mode={mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
