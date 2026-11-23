"""Stable business error codes used by the CLI and HTTP API."""

from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        exit_code: int = 2,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code
        self.http_status = http_status

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
    "INVALID_JSON": "输入不是合法 JSON",
    "INVALID_FORMAT": "输入格式或版本字段不合法",
    "CHANNEL_DUPLICATE": "通道名重复",
    "SEQUENCE_DUPLICATE": "通道内事件序号重复",
    "PARTITIONS_INVALID": "分区数必须为正整数",
    "BRIDGE_CHANNEL_MISSING": "桥接规则引用了不存在的通道",
    "BRIDGE_CYCLE": "桥接图存在环",
    "FIELD_COUNT_MISMATCH": "v1 扁平字段数组长度与声明不一致",
    "V2_CHECKSUM_MISMATCH": "v2 具名字段信封校验和不匹配",
    "SHARD_HASH_MISMATCH": "v2 分片哈希不匹配",
    "V2_CLOCK_INVALID": "逻辑时钟字段类型或取值非法",
    "V1_TIME_INVALID": "v1 相对产生秒数必须为非负整数",
    "SUBSCRIPTION_RANGE": "订阅起始偏移超出合法区间",
    "MALFORMED_EVENT": "事件结构畸形",
    "METADATA_INVALID": "v2 SQLite 元数据库不合法",
    "DATA_CONFLICT": "同一事件序号对应不同载荷",
    "OFFSET_CONFLICT": "日志偏移冲突",
    "BRIDGE_POSITION_REWIND": "桥接位点不能回退",
    "BRIDGE_UPSTREAM_WATERMARK": "上游偏移水位不足或回退",
    "BRIDGE_DIGEST_MISMATCH": "变换结果或载荷摘要校验失败",
    "GENERATION_MISMATCH": "上下游通道代数不一致",
    "CONFIG_CHANGED": "转发期间配置版本发生变化",
    "CONFIG_VERSION_INVALID": "配置版本不存在或不可切换",
    "CLOCK_REWIND": "逻辑时钟回拨超过容差",
    "SUBSCRIPTION_OFFSET_OUT_OF_RANGE": "订阅偏移越界",
    "SUBSCRIPTION_REWIND": "订阅偏移不能回退",
    "SUBSCRIPTION_CONFLICT": "同一订阅位点回报冲突",
    "EVENT_LOCKED": "事件已被消费或桥接位点越过，不能编辑",
    "PATCH_INVALID": "编辑补丁结构不合法",
    "DATASET_CONFLICT": "现有数据库来自不同样本",
    "VERIFY_FAILED": "持久水位与重放结果不一致",
    "NOT_FOUND": "目标对象不存在",
    "CONCURRENT_MODIFICATION": "并发修改冲突，请重试",
    "INTERNAL_ERROR": "内部错误",
}


def err(code: str, message: str | None = None, **details: Any) -> BusinessError:
    return BusinessError(code, message or ERROR_CODES.get(code, code), details)
