# -*- coding: utf-8 -*-
"""本地确定性模拟 HTTP 服务（仅使用 Python 标准库，不访问公网）。

提供以下端点：

页面：
  GET  /                      员工信息表单页面（HTML + 内联 JS）
  GET  /healthz               健康检查

接口：
  POST /api/submit            表单提交，成功返回 {"ok": true, "record_id": "EMP-0001", ...}
  GET  /api/status/<id>       按记录标识回读最终状态

故障注入控制（仅本地测试使用）：
  POST /control/mode          全局模式 {"mode": "normal|fail_load|submit_500|status_500"}
  POST /control/schedule      按行安排故障，每行一条指令：
                              {"rows": [
                                {"page": "normal|missing_element|fail_load",
                                 "submit": "success|no_id|transient",
                                 "status": "confirmed|pending_then_confirmed|rejected|error_500"},
                                ...
                              ]}
                              行序与填报器打开表单页面的次序一一对应（每行只打开一次页面），
                              缺省键取正常值，因此某行的故障不会污染其他行。
  POST /control/reset         清空全部故障安排与已存记录
  GET  /control/snapshot      查看已保存记录与调用计数（供测试核对唯一性等）

故障语义（与需求条目对应）：
  page=missing_element   返回缺少 name 输入框的缺陷页面 -> 必填元素缺失，该行失败（第 5 项）
  page=fail_load         该行打开页面时服务端直接关闭连接 -> 加载失败，后续行不受影响（第 5 项）
  mode=fail_load         服务端对所有页面直接关闭连接（全局加载失败）
  submit=transient       首次提交 503，重试后成功，记录只成功一次（第 6 项）
  mode=submit_500        提交接口持续 500，重试耗尽 -> 不可用（第 7 项）
  submit=no_id           提交返回 200 但不含记录标识 -> 失败且不触发回读（第 3 项）
  status=pending_then_confirmed  首次回读无结论，再次查询给出真实状态（第 4 项）
  status=error_500       该记录的状态查询持续报错 -> 不可用（第 7 项），不影响其他记录（第 8 项）
  mode=status_500        状态接口全局持续 500
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

FORM_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>员工信息录入（本地模拟）</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; margin: 40px; }
  form { width: 420px; padding: 24px; border: 1px solid #999; border-radius: 8px; }
  label { display: block; margin: 12px 0 4px; }
  input { width: 100%; padding: 6px; box-sizing: border-box; }
  button { margin-top: 18px; padding: 8px 22px; }
  #result { margin-top: 20px; padding: 12px; white-space: pre-wrap;
            font-family: Consolas, monospace; display: none; }
</style>
</head>
<body>
<h2>员工信息录入表单</h2>
<form id="employee-form">
  <label for="name">姓名</label>
  <input type="text" id="name" name="name">
  <label for="email">邮箱</label>
  <input type="text" id="email" name="email">
  <label for="department">部门</label>
  <input type="text" id="department" name="department">
  <button type="submit" id="submit-btn">提交</button>
</form>
<div id="result"></div>
<script>
const form = document.getElementById('employee-form');
const resultBox = document.getElementById('result');
form.addEventListener('submit', async function (event) {
  event.preventDefault();
  // 空字段以空字符串提交，不填占位内容
  const payload = {
    name: document.getElementById('name').value,
    email: document.getElementById('email').value,
    department: document.getElementById('department').value
  };
  let resp;
  try {
    resp = await fetch('/api/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
  } catch (err) {
    resultBox.style.display = 'block';
    resultBox.removeAttribute('data-http-status');
    resultBox.textContent = JSON.stringify(
      { ok: false, error: 'network_error', message: String(err) });
    return;
  }
  const text = await resp.text();
  resultBox.style.display = 'block';
  resultBox.setAttribute('data-http-status', String(resp.status));
  resultBox.textContent = text;
});
</script>
</body>
</html>
"""

# 缺陷页面：缺少 name 必填输入元素，其余结构与正常页面一致
BROKEN_FORM_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>员工信息录入（缺陷页面模拟）</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; margin: 40px; }
  form { width: 380px; padding: 24px; border: 1px solid #999; border-radius: 8px; }
  label { display: block; margin: 12px 0 4px; }
  input { width: 100%; padding: 6px; box-sizing: border-box; }
</style>
</head>
<body>
<h2>员工信息录入表单（缺少姓名输入框）</h2>
<form id="employee-form">
  <label for="email">邮箱</label>
  <input type="text" id="email" name="email">
  <label for="department">部门</label>
  <input type="text" id="department" name="department">
  <button type="submit" id="submit-btn">提交</button>
</form>
<div id="result"></div>
</body>
</html>
"""

VALID_GLOBAL_MODES = {"normal", "fail_load", "submit_500", "status_500"}
VALID_PAGE = {"normal", "missing_element", "fail_load"}
VALID_SUBMIT = {"success", "no_id", "transient"}
VALID_STATUS = {"confirmed", "pending_then_confirmed", "rejected", "error_500"}

DEFAULT_DIRECTIVE = {"page": "normal", "submit": "success", "status": "confirmed"}


class MockState:
    """线程安全的模拟服务状态（内存存储，进程退出即清空）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}
        self._record_counter = 0
        self._row_schedule: list[dict] = []
        self._open_rows = 0          # 已打开表单页面的行数
        self._current_row = -1       # 最近打开的行序号（0 起）
        self._submit_attempts: dict[int, int] = {}
        self._status_queries: dict[str, int] = {}
        self.mode = "normal"

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._record_counter = 0
            self._row_schedule.clear()
            self._open_rows = 0
            self._current_row = -1
            self._submit_attempts.clear()
            self._status_queries.clear()
            self.mode = "normal"

    def set_mode(self, mode: str) -> None:
        if mode not in VALID_GLOBAL_MODES:
            raise ValueError(f"未知模式: {mode}")
        with self._lock:
            self.mode = mode

    def set_schedule(self, rows: list[dict]) -> None:
        clean_rows: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("每行指令必须是对象")
            directive = dict(DEFAULT_DIRECTIVE)
            for key, allowed in (("page", VALID_PAGE),
                                 ("submit", VALID_SUBMIT),
                                 ("status", VALID_STATUS)):
                value = row.get(key, DEFAULT_DIRECTIVE[key])
                if value not in allowed:
                    raise ValueError(f"{key} 不支持的取值: {value}")
                directive[key] = value
            clean_rows.append(directive)
        with self._lock:
            self._row_schedule = clean_rows
            self._open_rows = 0
            self._current_row = -1
            self._submit_attempts.clear()
            self._status_queries.clear()

    def _directive(self, row_index: int) -> dict:
        if 0 <= row_index < len(self._row_schedule):
            return self._row_schedule[row_index]
        return DEFAULT_DIRECTIVE

    def open_form_page(self) -> tuple[int, dict]:
        """填报器打开一次表单页面，分配并返回（行序号, 该行指令）。"""
        with self._lock:
            row_index = self._open_rows
            self._open_rows += 1
            self._current_row = row_index
            return row_index, dict(self._directive(row_index))

    def current_submit_directive(self) -> tuple[int, dict]:
        """提交时读取当前行指令，并累计该行的提交尝试次数。"""
        with self._lock:
            row_index = self._current_row
            attempts = self._submit_attempts.get(row_index, 0) + 1
            self._submit_attempts[row_index] = attempts
            return row_index, dict(self._directive(row_index)), attempts

    def save_record(self, data: dict, row_index: int, directive: dict) -> str:
        with self._lock:
            self._record_counter += 1
            record_id = f"EMP-{self._record_counter:04d}"
            self._records[record_id] = {
                "record_id": record_id,
                "row_index": row_index,
                "name": data.get("name", ""),
                "email": data.get("email", ""),
                "department": data.get("department", ""),
                "status_directive": directive["status"],
                "status": "registered",
            }
            return record_id

    def next_status_query(self, record_id: str) -> tuple[dict | None, int]:
        """返回（记录, 该记录第几次查询，从 1 起）；记录不存在返回 (None, 0)。"""
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return None, 0
            times = self._status_queries.get(record_id, 0) + 1
            self._status_queries[record_id] = times
            return dict(record), times

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "records": list(self._records.values()),
                "open_rows": self._open_rows,
                "submit_attempts": dict(self._submit_attempts),
                "status_queries": dict(self._status_queries),
            }


STATE = MockState()


class MockHandler(BaseHTTPRequestHandler):
    server_version = "Project67Mock/1.0"

    # ---- 基础工具 ----
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # 保持控制台整洁
        return

    def handle_one_request(self) -> None:
        # fail_load 注入会主动关闭连接，标准库随后 flush 已关闭的套接字会抛
        # ValueError（I/O operation on closed file）。这是预期行为，静默处理，
        # 其余异常仍交给标准库逻辑。
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except ValueError as exc:
            if self.close_connection:
                self.close_connection = True
            else:
                raise

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802 (标准库命名)
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/healthz":
            self._send_json(200, {"ok": True, "mode": STATE.mode})
            return
        if path == "/":
            # 全局 fail_load：直接关闭连接，模拟页面加载失败
            if STATE.mode == "fail_load":
                self.close_connection = True
                try:
                    self.wfile.close()
                except OSError:
                    pass
                return
            _, directive = STATE.open_form_page()
            if directive["page"] == "fail_load":
                # 仅该行页面加载失败：直接关闭连接，下一行打开的是新连接不受影响
                self.close_connection = True
                try:
                    self.wfile.close()
                except OSError:
                    pass
                return
            self._send_html(BROKEN_FORM_PAGE
                            if directive["page"] == "missing_element"
                            else FORM_PAGE)
            return
        if path == "/control/snapshot":
            self._send_json(200, {"ok": True, "snapshot": STATE.snapshot()})
            return
        if path.startswith("/api/status/"):
            self._handle_status(path[len("/api/status/"):])
            return
        self._send_json(404, {"ok": False, "error": "not_found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/submit":
            self._handle_submit()
        elif path == "/control/mode":
            self._handle_control_mode()
        elif path == "/control/schedule":
            self._handle_control_schedule()
        elif path == "/control/reset":
            STATE.reset()
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"ok": False, "error": "not_found", "path": path})

    # ---- 业务端点 ----
    def _handle_submit(self) -> None:
        # 全局持续故障：每次提交都失败，重试也不会成功
        if STATE.mode == "submit_500":
            self._send_json(500, {"ok": False, "error": "service_unavailable",
                                  "message": "模拟：提交接口持续故障"})
            return
        row_index, directive, attempts = STATE.current_submit_directive()
        if directive["submit"] == "transient" and attempts == 1:
            # 仅第一次尝试失败，同一页面重试即成功，且失败尝试不落库 -> 不会重复
            self._send_json(503, {"ok": False, "error": "temporarily_unavailable",
                                  "message": "模拟：提交接口暂时故障，请重试"})
            return

        data = self._read_json_body()
        if data is None:
            self._send_json(400, {"ok": False, "error": "invalid_json",
                                  "message": "请求体不是合法 JSON"})
            return
        # 空字段保持空字符串，不做任何填充
        clean = {
            "name": str(data.get("name") or ""),
            "email": str(data.get("email") or ""),
            "department": str(data.get("department") or ""),
        }
        if directive["submit"] == "no_id":
            # 200 但响应中不含记录标识：填报器应判为提交失败且不回读
            self._send_json(200, {"ok": True, "message": "模拟：响应缺少记录标识"})
            return
        record_id = STATE.save_record(clean, row_index, directive)
        self._send_json(200, {
            "ok": True,
            "record_id": record_id,
            "message": "登记成功",
            "received": clean,
        })

    def _handle_status(self, record_id: str) -> None:
        # 全局持续故障
        if STATE.mode == "status_500":
            self._send_json(500, {"ok": False, "error": "service_unavailable",
                                  "message": "模拟：状态接口持续故障"})
            return
        record, times = STATE.next_status_query(record_id)
        if record is None:
            self._send_json(404, {"ok": False, "error": "record_not_found",
                                  "record_id": record_id})
            return
        directive_status = record["status_directive"]
        if directive_status == "error_500":
            # 该记录的每次查询都失败，但不影响其他记录
            self._send_json(503, {"ok": False, "error": "temporarily_unavailable",
                                  "message": f"模拟：记录 {record_id} 状态查询暂时故障"})
            return
        if directive_status == "pending_then_confirmed" and times == 1:
            # 首次查询暂无结论，再次查询即返回真实状态
            self._send_json(200, {"record_id": record_id, "status": "pending",
                                  "message": "处理中，暂无结论，请稍后再次查询"})
            return
        final_status = ("rejected" if directive_status == "rejected" else "confirmed")
        self._send_json(200, {
            "record_id": record_id,
            "status": final_status,
            "message": f"最终状态：{final_status}",
            "record": {k: v for k, v in record.items() if k != "status_directive"},
        })

    # ---- 控制端点 ----
    def _handle_control_mode(self) -> None:
        data = self._read_json_body()
        if data is None:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return
        try:
            STATE.set_mode(str(data.get("mode", "")))
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "mode": STATE.mode})

    def _handle_control_schedule(self) -> None:
        data = self._read_json_body()
        if data is None:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return
        rows = data.get("rows")
        if not isinstance(rows, list):
            self._send_json(400, {"ok": False,
                                  "error": "需要 rows 数组，每项为一行故障指令"})
            return
        try:
            STATE.set_schedule(rows)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "rows": rows})


def build_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """创建模拟服务实例（不阻塞）。port=0 时由系统分配端口。"""
    return ThreadingHTTPServer((host, port), MockHandler)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="员工录入本地模拟表单与状态接口服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = build_server(args.host, args.port)
    actual_host, actual_port = server.server_address[:2]
    print(f"模拟服务已启动: http://{actual_host}:{actual_port}")
    print("端点: GET / 表单页 | POST /api/submit | GET /api/status/<id>")
    print("控制: POST /control/reset | /control/mode | /control/schedule | GET /control/snapshot")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n模拟服务停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
