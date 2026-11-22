from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessError(Exception):
    """A stable, machine-readable business failure."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


ERROR_CODES = {
    "INPUT_JSON_INVALID": "输入不是合法 JSON 或 JSON 根不是对象",
    "TRAINING_PLAN_INVALID": "训练计划结构非法",
    "LAYER_NAME_DUPLICATE": "层名重复或为空",
    "LAYER_SHAPE_INVALID": "层形状维数或维度非法",
    "DTYPE_INVALID": "dtype 不在允许枚举内",
    "LAYER_OPTIMIZER_INVALID": "优化器状态标记非法",
    "LAYER_REVISION_INVALID": "层内容修订号非法",
    "SAVE_PLAN_INVALID": "保存计划结构非法",
    "SAVE_VERSION_INVALID": "检查点版本号非法",
    "CONCURRENCY_INVALID": "每轮并发上限非法",
    "BUDGET_INVALID": "每轮逻辑时间预算非法",
    "SHARD_POLICY_INVALID": "分片策略结构非法",
    "SHARD_COUNT_INVALID": "分片数非法或超过层总数",
    "SHARD_LIMIT_INVALID": "每分片字节上限非法",
    "SHARD_HASH_INVALID": "分片哈希算法不受支持",
    "SHARD_SIZE_EXCEEDED": "派生分片超过每分片字节上限",
    "CRASH_PLAN_INVALID": "崩溃注入描述非法",
    "VERSION_ROLLBACK": "检查点版本回退",
    "VERSION_CONFLICT": "同一版本存在不同内容",
    "BUILD_INCOMPLETE": "存在更早的未完成落盘轮次，需先 resume",
    "CLOCK_ROLLBACK": "逻辑时钟回拨超过容差",
    "CHECKPOINT_NOT_FOUND": "没有可恢复的完整检查点",
    "MANIFEST_MISSING": "检查点清单对象缺失",
    "MANIFEST_DIGEST_MISMATCH": "检查点清单摘要不匹配",
    "MANIFEST_INVALID": "检查点清单结构非法",
    "SHARD_MISSING": "分片对象缺失",
    "SHARD_DIGEST_MISMATCH": "分片摘要不匹配",
    "SHARD_ORDER_MISMATCH": "分片拼接顺序错误",
    "LAYER_SHAPE_MISMATCH": "层形状与清单不一致",
    "LAYER_MISSING": "清单分片中缺少层",
    "PARAMETER_DIGEST_MISMATCH": "参数摘要不匹配",
    "OPTIMIZER_DIGEST_MISMATCH": "优化器状态摘要不匹配",
    "CONSISTENCY_MISMATCH": "拼接路径与逐层独立重建路径不一致",
    "OBJECT_CORRUPT": "内容寻址对象损坏且无法安全复用",
    "FILE_LOCK_TIMEOUT": "文件锁获取超时",
    "IO_ERROR": "本地文件系统操作失败",
    "DATABASE_ERROR": "SQLite 账本操作失败",
    "WEB_HOST_NOT_LOOPBACK": "诊断工作台只允许监听回环地址",
    "CANCEL_REQUESTED": "调度被主动取消",
    "SAVE_INCOMPLETE": "本轮未形成完整检查点",
}


def fail(code: str, message: str | None = None, **details: Any) -> None:
    raise BusinessError(code, message or ERROR_CODES.get(code, code), details)
