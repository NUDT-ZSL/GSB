"""旧版授权查询服务（保留任务中描述的历史缺陷，仅用于对照检测）。

已知缺陷（检测器应逐项定位）：

1. 缺失令牌或空令牌被视为“匿名内部调用”而放行；
2. 令牌使用普通 ``!=`` 比较，非恒定时间；
3. 错误令牌触发“降级模式”，仍返回完整账目数据；
4. 请求体非法 JSON 时把内部异常栈（含源码路径）返回给调用方。
"""

from __future__ import annotations

import traceback

from .base import JsonRequestHandler
from .constants import ACCOUNT_FIELD, TOKEN_FIELD
from .data import find_account


class LegacyAuthHandler(JsonRequestHandler):
    shared_token: str = ""

    def service_version_label(self) -> str:
        return "legacy"

    def handle_query_body(self, declared_length: int) -> None:
        raw_body = self._read_body(declared_length)
        try:
            payload = self._parse_json(raw_body)
        except Exception:
            # 缺陷 4：内部异常细节外泄，且以 500 而非 400 响应。
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": "internal_error",
                    "detail": traceback.format_exc(),
                },
            )
            return

        if not isinstance(payload, dict):
            payload = {}

        supplied_token = payload.get(TOKEN_FIELD)
        auth_mode = "token"
        if supplied_token is None or supplied_token == "":
            # 缺陷 1：缺失/空令牌被当成匿名内部调用。
            auth_mode = "anonymous"
        elif supplied_token != self.shared_token:
            # 缺陷 2：普通相等比较；缺陷 3：不匹配时回退到降级模式。
            auth_mode = "fallback"

        account_id = payload.get(ACCOUNT_FIELD)
        try:
            account = find_account(account_id)
        except Exception:
            # 缺陷：参数类型异常时同样把内部异常栈返回给调用方。
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": "internal_error",
                    "detail": traceback.format_exc(),
                },
            )
            return
        if account is None:
            self._send_json(404, self.not_found_body())
            return

        # 缺陷：无论鉴权结果如何都返回完整业务字段。
        self._send_json(200, {"ok": True, "auth_mode": auth_mode, **account})
