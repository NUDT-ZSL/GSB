from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    INVALID_INPUT = "HG1001"
    VERSION_NOT_MONOTONIC = "HG1002"
    MISSING_SHARD = "HG1003"
    SHARD_DIGEST_MISMATCH = "HG1004"
    MISSING_LAYER_REFERENCE = "HG1005"
    CONFIG_REFERENCE_CYCLE = "HG1006"
    INVALID_GRAY_RATIO = "HG1007"
    CLOCK_REGRESSION = "HG1008"
    UNRESOLVED_REFERENCE = "HG1009"
    DUPLICATE_LAYER = "HG1010"
    INVALID_PARAM_COUNT = "HG1011"
    REPLICA_INVALID = "HG1012"
    PATCH_INVALID = "HG1013"
    TARGET_NOT_READY = "HG1014"
    ILLEGAL_STATE_TRANSITION = "HG1015"
    ROLLBACK_NOT_ACHIEVED = "HG1016"
    NOT_FOUND = "HG1017"
    CONFLICT = "HG1018"
    PERSISTENCE_ERROR = "HG1019"
    INVALID_CLOCK = "HG1020"


_DIAGNOSTIC_KEYS = (
    "version",
    "baseline_version",
    "target_version",
    "layer",
    "shard_index",
    "replica_id",
    "round_id",
    "resource_id",
    "request_id",
    "expected",
    "actual",
    "path",
)

ERROR_CATALOG: dict[str, dict[str, Any]] = {
    "HG1001": {
        "name": "INVALID_INPUT",
        "title": "输入结构或字段非法",
        "diagnostic_fields": [],
    },
    "HG1002": {
        "name": "VERSION_NOT_MONOTONIC",
        "title": "模型版本重复或非严格递增",
        "diagnostic_fields": ["version", "expected", "actual"],
    },
    "HG1003": {
        "name": "MISSING_SHARD",
        "title": "分片缺失或未完整覆盖层字节区间",
        "diagnostic_fields": ["version", "layer", "shard_index", "path"],
    },
    "HG1004": {
        "name": "SHARD_DIGEST_MISMATCH",
        "title": "分片摘要或增量重组结果不一致",
        "diagnostic_fields": [
            "version",
            "layer",
            "shard_index",
            "expected",
            "actual",
        ],
    },
    "HG1005": {
        "name": "MISSING_LAYER_REFERENCE",
        "title": "补丁或模型引用了不存在的层",
        "diagnostic_fields": ["version", "layer"],
    },
    "HG1006": {
        "name": "CONFIG_REFERENCE_CYCLE",
        "title": "预处理/后处理/特征表/上游模型引用成环",
        "diagnostic_fields": ["resource_id", "path"],
    },
    "HG1007": {
        "name": "INVALID_GRAY_RATIO",
        "title": "灰度档位比例非法或未以 1.0 收尾",
        "diagnostic_fields": ["expected", "actual"],
    },
    "HG1008": {
        "name": "CLOCK_REGRESSION",
        "title": "逻辑时钟回拨超过容差",
        "diagnostic_fields": ["expected", "actual", "tolerance"],
    },
    "HG1009": {
        "name": "UNRESOLVED_REFERENCE",
        "title": "模型版本或配置引用无法解析",
        "diagnostic_fields": ["version", "resource_id"],
    },
    "HG1010": {
        "name": "DUPLICATE_LAYER",
        "title": "同一模型版本存在重复层名",
        "diagnostic_fields": ["version", "layer"],
    },
    "HG1011": {
        "name": "INVALID_PARAM_COUNT",
        "title": "层参数量不是正整数",
        "diagnostic_fields": ["version", "layer", "actual"],
    },
    "HG1012": {
        "name": "REPLICA_INVALID",
        "title": "副本清单或加载分片集合非法",
        "diagnostic_fields": ["replica_id", "layer", "shard_index"],
    },
    "HG1013": {
        "name": "PATCH_INVALID",
        "title": "补丁结构与计划或目标版本不一致",
        "diagnostic_fields": ["version", "layer", "shard_index", "expected", "actual"],
    },
    "HG1014": {
        "name": "TARGET_NOT_READY",
        "title": "目标版本组合或预热状态尚未就绪",
        "diagnostic_fields": ["version", "replica_id", "not_ready"],
    },
    "HG1015": {
        "name": "ILLEGAL_STATE_TRANSITION",
        "title": "副本阶段迁移非法",
        "diagnostic_fields": ["replica_id", "expected", "actual"],
    },
    "HG1016": {
        "name": "ROLLBACK_NOT_ACHIEVED",
        "title": "回退窗口耗尽仍未恢复基线",
        "diagnostic_fields": ["round_id", "expected", "actual"],
    },
    "HG1017": {
        "name": "NOT_FOUND",
        "title": "轮次、请求或资源不存在",
        "diagnostic_fields": ["round_id", "request_id", "resource_id"],
    },
    "HG1018": {
        "name": "CONFLICT",
        "title": "状态或幂等键冲突",
        "diagnostic_fields": ["round_id", "request_id", "actual"],
    },
    "HG1019": {
        "name": "PERSISTENCE_ERROR",
        "title": "SQLite 持久化或事务错误",
        "diagnostic_fields": ["path"],
    },
    "HG1020": {
        "name": "INVALID_CLOCK",
        "title": "逻辑时钟脚本或计划时间非法",
        "diagnostic_fields": ["expected", "actual"],
    },
}


def error_catalog() -> list[dict[str, Any]]:
    return [
        {"code": code, **ERROR_CATALOG[code]}
        for code in sorted(ERROR_CATALOG)
    ]


@dataclass
class BizError(Exception):
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        diagnostics = {
            key: self.details[key]
            for key in _DIAGNOSTIC_KEYS
            if key in self.details and self.details[key] is not None
        }
        extra = {
            key: value
            for key, value in self.details.items()
            if key not in diagnostics and value is not None
        }
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "name": self.code.name,
                "message": self.message,
                "diagnostics": diagnostics,
                "context": extra,
            },
        }


def fail(
    code: ErrorCode,
    message: str,
    **details: Any,
) -> "NoReturn":
    raise BizError(code, message, details)


from typing import NoReturn  # noqa: E402  (kept after runtime imports for clarity)
