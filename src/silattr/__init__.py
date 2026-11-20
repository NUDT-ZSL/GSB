"""silattr: 通话静音段能量事件归因领域组件（标准库 + NumPy，确定性本地计算）。"""

from .errors import DomainError, BUSINESS_CODES
from .config import (
    NoiseConfig,
    SegmentConfig,
    AttributionConfig,
    ResourceLimits,
    PipelineConfig,
    CATEGORY_FLAT,
    CATEGORY_DEVICE,
    CATEGORY_CROSSTALK,
    CONTROLLED_CATEGORIES,
)

__all__ = [
    "DomainError",
    "BUSINESS_CODES",
    "NoiseConfig",
    "SegmentConfig",
    "AttributionConfig",
    "ResourceLimits",
    "PipelineConfig",
    "CATEGORY_FLAT",
    "CATEGORY_DEVICE",
    "CATEGORY_CROSSTALK",
    "CONTROLLED_CATEGORIES",
]

