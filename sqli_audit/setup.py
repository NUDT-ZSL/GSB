"""任务环境准备：确定性生成配置、SQLite 样本库与本地接口脚本。

运行 ``python -m sqli_audit setup <workspace>`` 后，workspace 内得到：
- sample_orders.db            确定性 SQLite 订单库；
- server_vulnerable.py 等     5 个可独立启动的接口脚本（零外部依赖）；
- target_*.json               5 份目标描述文件；
- README_lab.txt              环境内操作说明。
"""

from __future__ import annotations

from pathlib import Path

from .lab import CUSTOMERS, ORDERS, build_sample_db

# 各模式固定端口（仅回环），保证配置与演示完全可复现
PORT_MAP = {
    "vulnerable": 8031,
    "fixed": 8032,
    "partial": 8033,
    "echo": 8034,
    "flaky": 8035,
}

_TARGET_TEMPLATE = {
    "target_name": "lab-order-api-{mode}",
    "base_url": "http://127.0.0.1:{port}",
    "endpoint_path": "/orders",
    "method": "GET",
    "baseline": {"order_no": "ORD-1001", "customer_id": "C001"},
    "params": [
        {"name": "customer_id", "location": "query", "type": "string", "label": "客户号参数"},
        {"name": "order_no", "location": "query", "type": "string", "label": "订单号参数"},
    ],
    "data_path": ["data", "orders"],
    "order_id_field": "order_id",
    "timeout_seconds": 5,
}

# 独立服务脚本模板（与 sqli_audit.lab 行为一致，单文件即可启动，无包依赖）
_SERVER_TEMPLATE = '''#!/usr/bin/env python3
"""由 sqli_audit setup 确定性生成的本地订单接口脚本（请勿手工修改）。

模式: {mode}  端口: {port}
仅监听回环地址，数据为合成样本。启动: python {filename}
故障门控环境变量(仅 flaky 模式生效): SQLI_AUDIT_FAIL_AFTER=N
"""
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MODE = "{mode}"
PORT = {port}
DB_PATH = Path(__file__).with_name("sample_orders.db")
COLUMNS = ("order_id", "customer_id", "sku", "qty", "status", "amount", "region")
QUERY = ("SELECT order_id, customer_id, sku, qty, status, amount, region "
         "FROM orders WHERE order_id = {{oe}} AND customer_id = {{ce}}")


def quote(v):
    return "'" + v + "'"


def query_orders(order_no, customer_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        if MODE == "fixed":
            cur.execute(QUERY.format(oe="?", ce="?"), (order_no, customer_id))
        elif MODE == "partial":
            cur.execute(QUERY.format(oe=quote(order_no), ce="?"), (customer_id,))
        else:
            cur.execute(QUERY.format(oe=quote(order_no), ce=quote(customer_id)))
        rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(zip(COLUMNS, row)) for row in rows]


class Gate:
    def __init__(self):
        self.counter = 0
        self.lock = threading.Lock()

    def check(self):
        with self.lock:
            self.counter += 1
            seq = self.counter
        raw = os.environ.get("SQLI_AUDIT_FAIL_AFTER", "")
        try:
            threshold = int(raw)
        except ValueError:
            return None
        if threshold > 0 and seq > threshold:
            return "flaky gate: request #%d connection dropped (fail_after=%s)" % (seq, raw)
        return None


GATE = Gate() if MODE == "flaky" else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        parsed = urlparse(self.path)
        if parsed.path != "/orders":
            self.send_json(404, {{"error": "not_found", "data": {{"orders": []}}}})
            return
        fields = {{k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}}
        order_no = fields.get("order_no", "")
        customer_id = fields.get("customer_id", "C001")
        if GATE is not None:
            reason = GATE.check()
            if reason:
                self.close_connection = True
                try:
                    self.wfile.flush()
                    self.connection.shutdown(1)
                except OSError:
                    pass
                self.connection.close()
                return
        if MODE == "echo":
            self.send_json(200, {{"query": {{"order_no": order_no, "customer_id": customer_id}},
                                 "data": {{"orders": [{{"order_id": "ORD-1001"}}]}}}})
            return
        try:
            rows = query_orders(order_no, customer_id)
        except sqlite3.Error as exc:
            self.send_json(500, {{"error": "database_error",
                                 "error_type": type(exc).__name__,
                                 "data": {{"orders": []}}}})
            return
        self.send_json(200, {{"query": {{"order_no": order_no, "customer_id": customer_id}},
                             "data": {{"orders": rows}}}})


if __name__ == "__main__":
    if not DB_PATH.exists():
        raise SystemExit("样本库不存在，请先运行: python -m sqli_audit setup <workspace>")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("lab server mode=%s listening on http://127.0.0.1:%d/orders" % (MODE, PORT))
    server.serve_forever()
'''

_LAB_README = """本地 SQL 注入自查实验环境（确定性生成，请勿手工修改）
=====================================================

目录内容：
  sample_orders.db        SQLite 合成订单库（{n_orders} 条订单 / {n_customers} 个客户）
  server_<mode>.py        五种模式的独立接口脚本
  target_<mode>.json      对应的目标描述文件
  reports/                扫描报告输出目录（运行扫描后生成）
  state/                  断点续扫状态目录（运行扫描后生成）

接口模式与端口：
  vulnerable  :8031       历史缺陷：order_no、customer_id 均直接拼接 SQL
  fixed      :8032       修复版本：两个参数全部参数化
  partial    :8033       部分修复：仅 customer_id 参数化，order_no 仍可注入
  echo       :8034       正常接口：只回显订单号、不查库（验证不误报）
  flaky      :8035       同 vulnerable，带故障门控 SQLI_AUDIT_FAIL_AFTER=N

启动任一接口（新终端窗口，保持运行）：
  python server_vulnerable.py

运行扫描（在项目根目录）：
  python -m sqli_audit scan target_vulnerable.json --workspace <本目录>
"""


def setup_workspace(workspace: str | Path) -> list[Path]:
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    # 1. 样本库
    db = build_sample_db(ws / "sample_orders.db")
    created.append(db)

    # 2. 独立服务脚本
    for mode, port in PORT_MAP.items():
        fname = f"server_{mode}.py"
        script = _SERVER_TEMPLATE.format(mode=mode, port=port, filename=fname)
        path = ws / fname
        # 固定换行写入，保证生成内容跨机器一致
        path.write_text(script, encoding="utf-8", newline="\n")
        created.append(path)

    # 3. 目标描述文件（排序后写出，键序稳定）
    import json

    for mode, port in PORT_MAP.items():
        cfg = json.loads(json.dumps(_TARGET_TEMPLATE))  # 深拷贝
        cfg["target_name"] = f"lab-order-api-{mode}"
        cfg["base_url"] = f"http://127.0.0.1:{port}"
        target_path = ws / f"target_{mode}.json"
        target_path.write_text(
            json.dumps(cfg, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        created.append(target_path)

    # 4. 环境说明
    readme = ws / "README_lab.txt"
    readme.write_text(
        _LAB_README.format(n_orders=len(ORDERS), n_customers=len(CUSTOMERS)),
        encoding="utf-8",
        newline="\n",
    )
    created.append(readme)
    return created
