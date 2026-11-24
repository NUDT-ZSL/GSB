"""业务错误码定义。

所有可由输入参数/样本内容触发的可处理问题都映射到稳定的 ``E_xxx`` 字符串码，
命令行与报告只依赖这些字符串，不依赖异常类型名称。
"""

from __future__ import annotations

from dataclasses import dataclass


class ErrorCode:
    """稳定业务码集合，新增只能追加，不得复用旧码语义。"""

    E_BASE_COLOR_RANGE = "E_BASE_COLOR_RANGE"
    E_ROUGHNESS_INVALID = "E_ROUGHNESS_INVALID"
    E_NORMAL_STRENGTH_INVALID = "E_NORMAL_STRENGTH_INVALID"
    E_OCTAVES_INVALID = "E_OCTAVES_INVALID"
    E_FREQUENCY_ORDER = "E_FREQUENCY_ORDER"
    E_AMPLITUDE_INVALID = "E_AMPLITUDE_INVALID"
    E_GRID_DIVISIBILITY = "E_GRID_DIVISIBILITY"
    E_DIMENSION_INVALID = "E_DIMENSION_INVALID"
    E_CHANNEL_MISSING = "E_CHANNEL_MISSING"
    E_CONFIG_INVALID = "E_CONFIG_INVALID"
    E_CHUNK_DIGEST_MISMATCH = "E_CHUNK_DIGEST_MISMATCH"
    E_CHUNK_DATA_MISSING = "E_CHUNK_DATA_MISSING"
    E_INTERRUPTED = "E_INTERRUPTED"
    E_UNKNOWN = "E_UNKNOWN"


@dataclass(frozen=True)
class BusinessError:
    """单条可区分业务错误，始终绑定材质标识。"""

    material_id: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return {
            "material_id": self.material_id,
            "code": self.code,
            "message": self.message,
        }
