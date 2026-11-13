"""确定性本地模拟 HTTP 服务（默认端口 8765）。

所有异常行为（HTTP 错误、延迟、坏 JSON）均由服务启动规则文件显式声明，
不使用任何随机数。每个请求记录单调时钟毫秒时间戳写入侧写文件，
供限速/并发验收断言使用。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import FATAL_HTTP_CODES


class MockState:
    def __init__(self, rules: dict):
        self.rules = rules
        with open(rules["rest_data"], "r", encoding="utf-8") as fh:
            rest_doc = json.load(fh)
        with open(rules["ts_data"], "r", encoding="utf-8") as fh:
            ts_doc = json.load(fh)
        self.rest_rows = rest_doc["rows"]
        self.rest_meta = rest_doc["meta"]
        self.ts_rows = ts_doc["rows"]
        self.ts_meta = ts_doc["meta"]
        self.profile_path = rules["profile_path"]
        self.shutdown_token = rules.get("shutdown_token", "shutdown-token")
        self._lock = threading.Lock()
        self.rest_attempt = 0
        self.ts_attempt = 0
        self.total_requests = 0
        self.arrivals: list[dict] = []
        self._stop_event = threading.Event()

    # ------------------------------------------------------------ profiling
    def record_arrival(self, source: str, detail: str) -> int:
        ts_ms = int(time.monotonic() * 1000)
        with self._lock:
            self.total_requests += 1
            self.arrivals.append({"source": source, "ts_ms": ts_ms, "detail": detail})
            # 每请求落盘最新侧写，测试可在运行中途读取
            tmp = self.profile_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"total_requests": self.total_requests,
                           "arrivals": list(self.arrivals)}, fh)
            os.replace(tmp, self.profile_path)
        return ts_ms

    # ------------------------------------------------------------ error rules
    def _match_error(self, rules: list[dict], attempt: int) -> dict | None:
        for rule in rules:
            if rule.get("all"):
                return rule
            if rule.get("attempt") == attempt:
                return rule
        return None

    def rest_error(self) -> dict | None:
        with self._lock:
            self.rest_attempt += 1
            return self._match_error(self.rules.get("rest_errors", []), self.rest_attempt)

    def ts_error(self) -> dict | None:
        with self._lock:
            self.ts_attempt += 1
            return self._match_error(self.rules.get("ts_errors", []), self.ts_attempt)

    # ------------------------------------------------------------ data views
    def rest_page(self, page: int, page_size: int) -> dict:
        meta = self.rest_meta
        size = page_size or meta["page_size"]
        # 服务端按自己固化的分页边界切分（生成器已把重复行追加进对应页）
        server_size = meta["page_size"]
        total_pages = meta["total_pages"]
        if page < 1 or page > total_pages:
            return {"page": page, "total_pages": total_pages, "records": [],
                    "server_page_size": server_size}
        all_rows = self.rest_rows
        start = (page - 1) * server_size
        end = start + server_size
        page_rows = all_rows[start:end]
        if size < server_size:  # 客户端要求更小页时按服务端页内再切
            page_rows = page_rows[:size]
        return {
            "page": page,
            "total_pages": total_pages,
            "server_page_size": server_size,
            "records": page_rows,
        }

    def ts_window(self, start: int, end: int, limit: int) -> dict:
        # 半开区间 [start, end)，边界重复记录由生成器固化在数据尾部
        rows = [r for r in self.ts_rows if start <= r["event_ts"] < end]
        truncated = False
        if limit and len(rows) > limit:
            rows = rows[:limit]
            truncated = True
        return {"start": start, "end": end, "records": rows, "truncated": truncated}


def make_handler(state: MockState):

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静默默认 stderr 访问日志
            return

        def _send_json(self, code: int, payload: dict | list,
                       bad_json: bool = False) -> None:
            if bad_json:
                body = b"{\"records\": [BROKEN-JSON"
            else:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_code(self, status: int, error_code: str) -> None:
            self._send_json(status, {"error": error_code, "status": status})

        def _apply_error(self, rule: dict | None):
            if rule is None:
                return None
            status = rule.get("status", 500)
            if rule.get("bad_json"):
                self._send_json(200, {}, bad_json=True)
                return "sent"
            self._send_error_code(status, rule.get("code") or FATAL_HTTP_CODES.get(status, "E_HTTP_%d" % status))
            return "sent"

        def _maybe_delay(self, source: str, page: int | None = None) -> None:
            rules = state.rules
            delay_ms = 0
            if source == "rest":
                if rules.get("rest_delay_after_page") and page == rules["rest_delay_after_page"]:
                    delay_ms = rules.get("rest_delay_ms", 0)
                elif rules.get("rest_delay_concurrency1"):
                    # 仅对每个进程第一次到达的 REST 请求延迟一次
                    with state._lock:
                        if not getattr(state, "_c1_delay_used", False):
                            state._c1_delay_used = True
                            delay_ms = rules.get("rest_delay_ms", 300)
            if delay_ms:
                self._interruptible_sleep(delay_ms / 1000.0)

        def _interruptible_sleep(self, seconds: float) -> None:
            state._stop_event.wait(seconds)

        # ---------------------------------------------------------- routing
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            state.record_arrival(
                "rest" if path.startswith("/rest") else ("ts" if path.startswith("/ts") else "other"),
                path + "?" + parsed.query,
            )
            if path == "/health":
                self._send_json(200, {"ok": True})
                return
            if path == "/shutdown":
                if qs.get("token") != state.shutdown_token:
                    self._send_error_code(403, "E_FORBIDDEN")
                    return
                self._send_json(200, {"shutting_down": True,
                                      "total_requests": state.total_requests})
                state._stop_event.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            try:
                if path == "/rest":
                    self._handle_rest(qs)
                elif path == "/ts":
                    self._handle_ts(qs)
                else:
                    self._send_error_code(404, "E_NOT_FOUND")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # 服务端自身异常也以 500 暴露
                try:
                    self._send_error_code(500, "E_INTERNAL: %s" % type(exc).__name__)
                except Exception:
                    pass

        # ---------------------------------------------------------- endpoints
        def _handle_rest(self, qs: dict) -> None:
            page = int(qs.get("page", "1"))
            limit = int(qs.get("limit", "200"))
            # 中断/恢复场景：第 2 页响应后插入可控延迟窗口
            self._maybe_delay("rest", page=page)
            rule = state.rest_error()
            if self._apply_error(rule) == "sent":
                return
            self._send_json(200, state.rest_page(page, limit))

        def _handle_ts(self, qs: dict) -> None:
            start = int(qs["start"])
            end = int(qs["end"])
            limit = int(qs.get("limit", "200"))
            self._maybe_delay("ts")
            rule = state.ts_error()
            if self._apply_error(rule) == "sent":
                return
            self._send_json(200, state.ts_window(start, end, limit))

    return Handler


def build_server(rules_path: str, host: str = "127.0.0.1",
                 port: int = 8765) -> tuple[ThreadingHTTPServer, MockState]:
    with open(rules_path, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    state = MockState(rules)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.mock_state = state  # type: ignore[attr-defined]
    return server, state


def serve(rules_path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    server, state = build_server(rules_path, host, port)

    def _signal_handler(*_args):
        state._stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    # Windows 支持 CTRL_BREAK；POSIX 支持 SIGTERM/SIGINT
    if hasattr(signal, "SIGBREAK"):  # type: ignore[attr-defined]
        signal.signal(signal.SIGBREAK, _signal_handler)  # type: ignore[attr-defined]
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    print(json.dumps({"event": "server_start", "host": host, "port": port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        # 停止信号后输出已处理请求计数，并落盘最终侧写
        with open(state.profile_path, "w", encoding="utf-8") as fh:
            json.dump({"total_requests": state.total_requests,
                       "arrivals": state.arrivals}, fh)
        print(json.dumps({"event": "server_stop",
                          "total_requests": state.total_requests}), flush=True)


import signal  # noqa: E402  (信号注册在 serve() 内使用，模块级导入置于此保持阅读顺序)


if __name__ == "__main__":
    rules = sys.argv[1]
    pt = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
    host = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
    serve(rules, host=host, port=pt)
