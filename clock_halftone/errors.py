"""Business errors with stable machine-readable codes."""

from __future__ import annotations

from typing import Any


ERROR_HINTS: dict[str, str] = {
    "E_SAMPLE_NOT_FOUND": "样本目录或 sample.json 不存在",
    "E_GRAY_NOT_FOUND": "缺少灰阶矩阵 gray.npy",
    "E_MASK_NOT_FOUND": "缺少零件掩码 mask.npy",
    "E_DETAILS_NOT_FOUND": "缺少细节标注 details.json",
    "E_METADATA_INVALID": "sample.json 或 details.json 字段不完整",
    "E_INPUT_HASH": "输入矩阵或细节文件哈希与样本清单不一致",
    "E_IMAGE_SIZE": "灰阶图像尺寸与元数据或打印配置不一致",
    "E_GRAY_DEPTH": "灰阶图像不是受支持的 8 位灰度矩阵",
    "E_MASK_SIZE": "掩码尺寸与灰阶图像不一致",
    "E_MASK_UNKNOWN_LABEL": "掩码中出现未登记的零件标签",
    "E_MASK_LABEL_MISSING": "声明的零件标签在掩码中没有像素",
    "E_MASK_MISSING_PIXELS": "掩码存在缺失像素，或细节区域未落在所属零件上",
    "E_PRINT_LPI": "打印线数无效",
    "E_PRINT_DOT_SIZE": "墨点尺寸与网点栅格节距不相容",
    "E_PRINT_GEOMETRY": "展板物理尺寸、像素纵横比或打印栅格无效",
    "E_DETAIL_REGION": "重点细节区域越界或元数据无效",
    "E_DENSITY_BOUNDS": "目标网点密度或修正量越界",
    "E_SNAPSHOT_NOT_FOUND": "快照目录或必要状态文件不存在",
    "E_RECOMPUTE_PART": "局部重算指定的零件无效",
    "E_RECOMPUTE_VERIFY": "局部重算与全量重算的一致性核验失败",
    "E_CONFIG": "配置文件字段或类型无效",
    "E_INTERNAL": "内部计算状态异常",
}


class BusinessError(RuntimeError):
    """An expected input/business failure, suitable for CLI JSON output."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_HINTS:
            raise ValueError(f"unknown business error code: {code}")
        self.code = code
        self.hint = ERROR_HINTS[code]
        self.message = message or self.hint
        self.details = details or {}
        super().__init__(f"{self.code}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "hint": self.hint,
            "message": self.message,
            "details": self.details,
        }


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise BusinessError(code, message, details=details)
