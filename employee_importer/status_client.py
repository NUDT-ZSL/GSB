# -*- coding: utf-8 -*-
"""状态回读客户端：按记录标识查询本地状态接口。

语义：
- 首次查询返回 pending（无结论）时保留再次确认机会，继续轮询直到拿到最终状态
- 查询遇到暂时故障（连接错误/5xx）时重试
- 重试耗尽仍无法取得结论 -> status="unavailable"，由报告归入需跟进区
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

# 暂时无结论或暂时故障时的最大查询次数（含首次）
MAX_STATUS_ATTEMPTS = 5
# 两次查询之间的等待秒数（本地服务，保持较短以便样例快速跑完）
RETRY_INTERVAL_SECONDS = 0.8
# pending 与故障共用同一预算：总尝试不超过 MAX_STATUS_ATTEMPTS
FINAL_STATUSES = {"confirmed", "rejected"}


@dataclass(frozen=True)
class StatusResult:
    status: str          # "ok" | "unavailable"
    final_status: str | None  # confirmed / rejected，未取得时为 None
    message: str
    attempts: int


def _query_once(url: str, timeout: float) -> tuple[int | None, dict | None, str]:
    """执行一次 HTTP 查询，返回 (http状态码, JSON体, 错误描述)。"""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            code = resp.getcode()
            try:
                body = json.loads(resp.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                return code, None, f"状态响应不是合法 JSON: {exc}"
            return code, body, ""
    except urllib.error.HTTPError as exc:
        # 5xx/404 等：读出错误体（若有），交给上层按状态码决策
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            body = None
        return exc.code, body, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, f"状态接口连接失败: {exc}"


def query_final_status(base_url: str, record_id: str,
                       max_attempts: int = MAX_STATUS_ATTEMPTS,
                       timeout: float = 5.0,
                       sleep=__import__("time").sleep) -> StatusResult:
    """按记录标识轮询最终状态。

    base_url: 模拟服务根地址，如 http://127.0.0.1:8765
    sleep 可注入，便于测试时不真实等待
    """
    url = f"{base_url.rstrip('/')}/api/status/{record_id}"
    last_message = "尚未查询"
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        code, body, error = _query_once(url, timeout)
        if code is not None and 200 <= code < 300 and isinstance(body, dict):
            remote_status = str(body.get("status", ""))
            if remote_status in FINAL_STATUSES:
                return StatusResult(
                    status="ok",
                    final_status=remote_status,
                    message=str(body.get("message") or f"最终状态：{remote_status}"),
                    attempts=attempts,
                )
            if remote_status == "pending":
                # 接口暂无结论：保留再次确认机会
                last_message = str(body.get("message") or "接口暂无结论")
            else:
                last_message = f"状态接口返回未知状态值: {remote_status!r}"
        else:
            # 连接失败或 4xx/5xx：视为暂时故障，继续重试
            detail = ""
            if isinstance(body, dict) and body.get("message"):
                detail = str(body["message"])
            last_message = error or detail or "状态接口暂时故障"

        if attempts < max_attempts:
            sleep(RETRY_INTERVAL_SECONDS)

    return StatusResult(
        status="unavailable",
        final_status=None,
        message=f"重试 {max_attempts} 次后仍无法取得最终状态（{last_message}）",
        attempts=attempts,
    )
