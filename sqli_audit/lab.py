"""本地确定性实验接口（任务环境准备 + 第 2/3/4/8/9 条验收依赖）。

五种模式：
- vulnerable : 订单号与客户号均字符串拼接（历史缺陷版本）；
- fixed      : 两个参数全部参数化（修复版本）；
- partial    : 仅客户号参数化，订单号仍拼接（部分修复，制造残留项）；
- echo       : 不查库，仅把订单号原样回显（正常回显对照，不应被判疑）；
- flaky      : 与 vulnerable 同逻辑，但带故障门控，可确定性制造中途故障与恢复。

仅监听回环地址，数据全部为合成样本，无真实凭据、无公网访问。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# 确定性 SQLite 样本库
# ---------------------------------------------------------------------------

CUSTOMERS = (
    ("C001", "示例客户甲"),
    ("C002", "示例客户乙"),
    ("C003", "示例客户丙"),
)

# 10 条确定性合成订单：(order_id, customer_id, sku, qty, status, amount, region)
ORDERS = (
    ("ORD-1001", "C001", "SKU-A1", 1, "paid",     1000, "north"),
    ("ORD-1002", "C001", "SKU-A2", 2, "shipped",  2000, "north"),
    ("ORD-1003", "C001", "SKU-A3", 3, "cancelled", 3000, "south"),
    ("ORD-1004", "C002", "SKU-B1", 1, "paid",     1500, "south"),
    ("ORD-1005", "C002", "SKU-B2", 4, "shipped",  2500, "east"),
    ("ORD-1006", "C002", "SKU-B3", 2, "paid",     3500, "east"),
    ("ORD-1007", "C003", "SKU-C1", 5, "paid",     4500, "west"),
    ("ORD-1008", "C003", "SKU-C2", 1, "refunded",  500, "west"),
    ("ORD-1009", "C003", "SKU-C3", 2, "shipped",  6500, "north"),
    ("ORD-1010", "C003", "SKU-C4", 3, "paid",     7500, "south"),
)

MODES = ("vulnerable", "fixed", "partial", "echo", "flaky")


def build_sample_db(db_path: str | Path) -> Path:
    """以固定数据重建样本库（先删后建，保证任何机器上字节级数据一致）。"""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(p)
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE customers ("
            "customer_id TEXT PRIMARY KEY, customer_name TEXT NOT NULL)"
        )
        cur.execute(
            "CREATE TABLE orders ("
            "order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, "
            "sku TEXT NOT NULL, qty INTEGER NOT NULL, status TEXT NOT NULL, "
            "amount INTEGER NOT NULL, region TEXT NOT NULL)"
        )
        cur.executemany("INSERT INTO customers VALUES (?, ?)", CUSTOMERS)
        cur.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", ORDERS)
        conn.commit()
    finally:
        conn.close()
    return p


# ---------------------------------------------------------------------------
# 查询逻辑：拼接 vs 参数化
# ---------------------------------------------------------------------------

_QUERY = (
    "SELECT order_id, customer_id, sku, qty, status, amount, region "
    "FROM orders WHERE order_id = {order_expr} AND customer_id = {cust_expr}"
)
_COLUMNS = ("order_id", "customer_id", "sku", "qty", "status", "amount", "region")


def _quote(value: str) -> str:
    # 实验接口刻意保留的历史缺陷写法：直接拼接、不做转义
    return "'" + value + "'"


def query_orders(db_path: str, mode: str, order_no: str, customer_id: str) -> list[dict]:
    if mode == "fixed":
        sql = _QUERY.format(order_expr="?", cust_expr="?")
        params = (order_no, customer_id)
    elif mode == "partial":
        # 只修了 customer_id；order_no 仍拼接 —— 残留注入点
        sql = _QUERY.format(order_expr=_quote(order_no), cust_expr="?")
        params = (customer_id,)
    else:
        # vulnerable / flaky：两个参数都拼接
        sql = _QUERY.format(order_expr=_quote(order_no), cust_expr=_quote(customer_id))
        params = ()

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(zip(_COLUMNS, row)) for row in rows]


# ---------------------------------------------------------------------------
# 故障门控（flaky 模式）：确定性地让第 N 次请求之后的请求失败
# ---------------------------------------------------------------------------

class FlakyGate:
    """通过环境变量 SQLI_AUDIT_FAIL_AFTER 控制。

    - 未设置/非正整数：正常放行（用于“恢复”场景，无需重启进程即可恢复）；
    - 设置为 N：第 (N+1) 次起的请求在写出任何 HTTP 响应前直接断开连接，
      确定性模拟“探测中途服务停止”，客户端据此标记探测未完成。
    每次请求都重新读取环境变量，便于自检与演示在同一进程内制造故障与恢复。
    """

    def __init__(self) -> None:
        self._counter = 0
        self._lock = threading.Lock()

    def check(self) -> str | None:
        with self._lock:
            self._counter += 1
            seq = self._counter
        raw = os.environ.get("SQLI_AUDIT_FAIL_AFTER", "")
        try:
            threshold = int(raw)
        except ValueError:
            return None
        if threshold > 0 and seq > threshold:
            return f"flaky gate: request #{seq} connection dropped (fail_after={threshold})"
        return None


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    # 由 make_server 注入
    db_path: str = ""
    mode: str = "vulnerable"
    gate: FlakyGate | None = None

    def log_message(self, fmt, *args):  # 静默标准访问日志，保持演示输出干净
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/orders":
            self._send_json(404, {"error": "not_found", "data": {"orders": []}})
            return

        fields: dict[str, str] = {}
        if self.command == "GET":
            fields = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        else:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            fields = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        order_no = fields.get("order_no", "")
        customer_id = fields.get("customer_id", "C001")

        # flaky 门控：触发时直接断开连接，模拟服务中途停止
        if self.mode == "flaky" and self.gate is not None:
            reason = self.gate.check()
            if reason:
                self.close_connection = True
                try:
                    self.wfile.flush()
                    self.connection.shutdown(1)  # 停止写出方向，客户端读响应即失败
                except OSError:
                    pass
                self.connection.close()
                return

        # echo 模式：只回显输入，不查库 —— 用于验证“正常回显不算注入”
        if self.mode == "echo":
            self._send_json(200, {
                "query": {"order_no": order_no, "customer_id": customer_id},
                "data": {"orders": [{"order_id": "ORD-1001"}]},
            })
            return

        try:
            rows = query_orders(self.db_path, self.mode, order_no, customer_id)
        except sqlite3.Error as exc:
            self._send_json(500, {
                "error": "database_error",
                "error_type": type(exc).__name__,
                "data": {"orders": []},
            })
            return

        self._send_json(200, {
            "query": {"order_no": order_no, "customer_id": customer_id},
            "data": {"orders": rows},
        })


def make_server(host: str, port: int, db_path: str | Path, mode: str) -> ThreadingHTTPServer:
    if mode not in MODES:
        raise ValueError(f"未知模式 {mode!r}，支持: {MODES}")

    handler = type("_BoundHandler", (_Handler,), {
        "db_path": str(db_path),
        "mode": mode,
        "gate": FlakyGate() if mode == "flaky" else None,
    })
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
