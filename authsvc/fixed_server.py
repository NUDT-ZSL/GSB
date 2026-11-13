"""修复版授权查询服务。

修复点：

* 缺失、空、错误令牌统一返回 401，响应体只有通用错误信封；
* 令牌比较只经由 :func:`authsvc.security.constant_time_token_match`，
  本模块中不存在对令牌的普通相等/不等比较；
* 请求体超过上限返回 413，非法 JSON / 非对象 / 超长字段返回 400，
  错误响应不回显异常栈、路径或任何业务数据；
* 所有拒绝路径在返回前都不读取账目样本。
"""

from __future__ import annotations

from .base import JsonRequestHandler
from .constants import (
    ACCOUNT_FIELD,
    MAX_ACCOUNT_ID_LENGTH,
    MAX_BODY_BYTES,
    TOKEN_FIELD,
)
from .data import find_account
from .security import constant_time_token_match


class FixedAuthHandler(JsonRequestHandler):
    shared_token: str = ""
    max_body_bytes = MAX_BODY_BYTES
    max_account_id_length = MAX_ACCOUNT_ID_LENGTH

    def service_version_label(self) -> str:
        return "fixed"

    def handle_query_body(self, declared_length: int) -> None:
        if declared_length > self.max_body_bytes:
            # 不解析请求体，但先把入站数据读掉，避免 Windows 在关闭连接时
            # 发送 RST 导致客户端收不到 413 响应。
            self._discard_body(declared_length)
            self._send_json(413, self.too_large_body())
            return

        raw_body = self._read_body(declared_length)
        if len(raw_body) != declared_length:
            self._send_json(400, self.bad_request_body())
            return

        try:
            payload = self._parse_json(raw_body)
        except Exception:
            self._send_json(400, self.bad_request_body())
            return

        if not isinstance(payload, dict):
            self._send_json(400, self.bad_request_body())
            return

        supplied_token = payload.get(TOKEN_FIELD)
        supplied_text = supplied_token if isinstance(supplied_token, str) else ""
        token_present = len(supplied_text) > 0
        # 恒定时间比较：无论令牌缺失、为空还是错误，都执行同样成本的比较。
        token_ok = constant_time_token_match(supplied_text, self.shared_token)
        if not (token_present and token_ok):
            self._send_json(401, self.unauthorized_body())
            return

        account_id = payload.get(ACCOUNT_FIELD)
        if not isinstance(account_id, str) or len(account_id) == 0:
            self._send_json(400, self.bad_request_body())
            return
        if len(account_id) > self.max_account_id_length:
            self._send_json(400, self.bad_request_body())
            return

        account = find_account(account_id)
        if account is None:
            self._send_json(404, self.not_found_body())
            return
        self._send_json(200, {"ok": True, **account})
