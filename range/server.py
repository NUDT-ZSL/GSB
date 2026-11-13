"""靶场 HTTP 服务：纯标准库实现的越权漏洞接口应用。

三种运行形态（--mode）：
  vulnerable      未修复：仅校验身份令牌，不做资源归属校验，任何登录用户可读取
                  他人账号/资源，响应包含全部敏感字段（典型 IDOR/水平越权）。
  fixed           已修复：在身份校验之外增加归属校验，非所有者访问返回 403，
                  敏感字段不再泄露给非所有者。
  fixed-partial   部分修复：/api/resource 已修复，但 /api/account 仍越权，
                  用于构造“未完全修复”的混合判定演示。

故障注入开关（用于验收第 6、7、8 条）：
  --flaky-first N          服务启动后的前 N 个请求统一返回 503，之后恢复正常
                           （模拟暂时性故障，配合工具重试即可恢复）。
  --flaky-always RES_ID   对指定资源的 /api/resource 请求始终返回 503
                           （模拟某场景在该形态持续不可用）。

启动示例：
  python -m range.server --host 127.0.0.1 --port 8081 --mode vulnerable
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# 兼容 python -m range.server 与 python range/server.py（含 -I 隔离模式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from range.data import RangeData, load_data
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from data import RangeData, load_data  # type: ignore[no-redef]

VALID_MODES = ("vulnerable", "fixed", "fixed-partial")


class RangeServer:
    """持有数据、形态与故障注入状态的线程安全服务对象。"""

    def __init__(
        self,
        data: RangeData,
        mode: str,
        flaky_first: int = 0,
        flaky_always: str | None = None,
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"未知形态: {mode}，可选: {VALID_MODES}")
        self.data = data
        self.mode = mode
        self._flaky_first_remaining = int(flaky_first)
        self._flaky_always = flaky_always
        self._lock = threading.Lock()

    def consume_request(self) -> int:
        """返回该请求的序号（从 1 开始），并在暂时性故障配额内扣减。"""
        with self._lock:
            if self._flaky_first_remaining > 0:
                self._flaky_first_remaining -= 1
                return self._flaky_first_remaining + 1
        return 0

    @property
    def flaky_first_remaining(self) -> int:
        return self._flaky_first_remaining


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class RangeHTTPRequestHandler(BaseHTTPRequestHandler):
    server: RangeServer  # 由 RangeHTTPServer 注入，仅用于类型提示

    # 静默默认访问日志，改为输出精简的单行日志
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.server.mode}] {self.address_string()} {fmt % args}")

    # ---------- 基础工具 ----------
    def _send_json(self, status: int, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": code, "message": message})

    def _authenticate(self) -> dict[str, Any] | None:
        token = self.headers.get("X-Auth-Token", "").strip()
        if not token:
            self._send_error_json(401, "missing_token", "缺少 X-Auth-Token 身份令牌")
            return None
        user = self.server.data.user_by_token(token)
        if user is None:
            self._send_error_json(401, "invalid_token", "身份令牌无效")
            return None
        return user

    @staticmethod
    def _first_query(query: dict[str, list[str]], key: str) -> str | None:
        values = query.get(key)
        if not values:
            return None
        return values[0]

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 约定)
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)

        if path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "mode": self.server.mode,
                    "flaky_first_remaining": self.server.flaky_first_remaining,
                },
            )
            return

        # 故障注入 1：服务级暂时性故障（前 N 个请求 503），/health 不受影响
        remaining = self.server.consume_request()
        if remaining > 0:
            self._send_error_json(
                503,
                "transient_failure",
                "服务暂时不可用（flaky-first 注入），请稍后重试",
            )
            return

        if path == "/api/account":
            self._handle_account(query)
        elif path == "/api/resource":
            self._handle_resource(query)
        else:
            self._send_error_json(404, "not_found", f"接口不存在: {path}")

    def do_POST(self) -> None:  # noqa: N802
        self._send_error_json(405, "method_not_allowed", "靶场仅支持 GET 请求")

    do_PUT = do_DELETE = do_PATCH = do_POST  # type: ignore[assignment]

    # ---------- 业务接口 ----------
    def _handle_account(self, query: dict[str, list[str]]) -> None:
        actor = self._authenticate()
        if actor is None:
            return
        target_id = self._first_query(query, "user_id")
        if not target_id:
            self._send_error_json(400, "missing_param", "缺少查询参数 user_id")
            return
        target = self.server.data.user_by_id(target_id)
        if target is None:
            self._send_error_json(404, "user_not_found", f"账号不存在: {target_id}")
            return

        # vulnerable：任何登录用户都能读取任意账号全量信息（含 ssn/salary 等）
        # fixed：仅本人可读，他人访问 403
        # fixed-partial：账号接口未修复，行为与 vulnerable 一致
        ownership_enforced = self.server.mode == "fixed"
        if ownership_enforced and target["id"] != actor["id"]:
            self._send_error_json(
                403,
                "forbidden",
                f"用户 {actor['id']} 无权访问账号 {target['id']} 的资料",
            )
            return

        self._send_json(200, {"data": dict(target), "requested_by": actor["id"]})

    def _handle_resource(self, query: dict[str, list[str]]) -> None:
        actor = self._authenticate()
        if actor is None:
            return
        resource_id = self._first_query(query, "resource_id")
        if not resource_id:
            self._send_error_json(400, "missing_param", "缺少查询参数 resource_id")
            return
        resource = self.server.data.resource_by_id(resource_id)
        if resource is None:
            self._send_error_json(
                404, "resource_not_found", f"资源不存在: {resource_id}"
            )
            return

        # 故障注入 2：指定资源在该形态持续 503
        if self.server._flaky_always == resource_id:
            self._send_error_json(
                503,
                "transient_failure",
                f"资源 {resource_id} 所在子系统持续不可用（flaky-always 注入）",
            )
            return

        # vulnerable / fixed-partial 下资源接口的差异：
        # fixed-partial 中资源接口已修复，仅账号接口仍越权
        ownership_enforced = self.server.mode in ("fixed", "fixed-partial")
        if ownership_enforced and resource["owner_id"] != actor["id"]:
            self._send_error_json(
                403,
                "forbidden",
                f"用户 {actor['id']} 无权访问用户 {resource['owner_id']} 的资源",
            )
            return

        self._send_json(200, {"data": dict(resource), "requested_by": actor["id"]})


class RangeHTTPServer(ThreadingHTTPServer):
    """把 RangeServer 业务对象挂载到标准库 HTTP 服务器上。

    通过 __getattr__ 将未定义属性实时委托给后端业务对象，
    避免复制快照导致 flaky_always 等动态配置不生效。
    """

    daemon_threads = True
    # 放宽监听积压，兼容判定工具多线程并发请求
    request_queue_size = 128
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], backend: RangeServer) -> None:
        self.backend = backend
        super().__init__(server_address, RangeHTTPRequestHandler)

    def __getattr__(self, item: str) -> Any:
        # 仅在正常属性查找失败时触发，委托给后端业务对象
        return getattr(self.backend, item)


def make_server(
    host: str,
    port: int,
    mode: str,
    data_path: str | None = None,
    flaky_first: int = 0,
    flaky_always: str | None = None,
) -> RangeHTTPServer:
    data = load_data(data_path)
    backend = RangeServer(
        data, mode=mode, flaky_first=flaky_first, flaky_always=flaky_always
    )
    httpd = RangeHTTPServer((host, port), backend)
    # 让 Handler 通过 self.server 直接访问后端业务对象
    httpd.backend = backend
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description="本地授权越权靶场 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--mode", required=True, choices=VALID_MODES)
    parser.add_argument("--data", default=None, help="数据文件路径，默认项目根 data.json")
    parser.add_argument(
        "--flaky-first",
        type=int,
        default=0,
        help="前 N 个请求返回 503（暂时性故障注入），默认 0 关闭",
    )
    parser.add_argument(
        "--flaky-always",
        default=None,
        help="指定 resource_id 的资源请求始终 503（持续不可用注入）",
    )
    args = parser.parse_args()

    httpd = make_server(
        args.host,
        args.port,
        mode=args.mode,
        data_path=args.data,
        flaky_first=args.flaky_first,
        flaky_always=args.flaky_always,
    )
    print(
        f"靶场服务已启动: http://{args.host}:{args.port} "
        f"形态={args.mode} flaky_first={args.flaky_first} "
        f"flaky_always={args.flaky_always or '无'}"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断信号，正在停止靶场服务 ...")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
