"""靶场账号与资源数据加载。

数据文件 data.json 为确定性本地样本，不包含任何真实凭据。
服务端与判定工具共用同一份数据，保证“资源归属”判定口径一致。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# data.json 位于本包的上一级目录（项目根目录）
DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data.json"


class RangeData:
    """封装账号、资源以及敏感字段清单的只读视图。"""

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.users: list[dict[str, Any]] = list(raw.get("users", []))
        self.resources: list[dict[str, Any]] = list(raw.get("resources", []))
        self.sensitive_account_fields: list[str] = list(
            raw.get("sensitive_account_fields", [])
        )
        self.sensitive_resource_fields: list[str] = list(
            raw.get("sensitive_resource_fields", [])
        )
        self._users_by_id = {u["id"]: u for u in self.users}
        self._users_by_token = {u["token"]: u for u in self.users}
        self._resources_by_id = {r["id"]: r for r in self.resources}

    def user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self._users_by_id.get(user_id)

    def user_by_token(self, token: str) -> dict[str, Any] | None:
        return self._users_by_token.get(token)

    def resource_by_id(self, resource_id: str) -> dict[str, Any] | None:
        return self._resources_by_id.get(resource_id)

    def sensitive_pairs(self, kind: str, target: dict[str, Any]) -> list[tuple[str, str]]:
        """返回目标对象上 (敏感字段名, 字符串形式的值) 列表，用于泄露比对。"""
        fields = (
            self.sensitive_account_fields
            if kind == "account"
            else self.sensitive_resource_fields
        )
        pairs: list[tuple[str, str]] = []
        for field in fields:
            if field in target and target[field] is not None:
                pairs.append((field, str(target[field])))
        return pairs


def load_data(path: str | Path | None = None) -> RangeData:
    data_path = Path(path) if path else DEFAULT_DATA_PATH
    with data_path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return RangeData(raw)
