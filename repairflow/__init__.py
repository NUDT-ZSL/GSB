"""Deterministic, region-incremental image repair and style transfer."""

from .engine import RepairEngine, UpdateResult
from .errors import BusinessError, ErrorCode
from .mask import MaskState, Region, coerce_mask_state

__all__ = [
    "BusinessError",
    "ErrorCode",
    "MaskState",
    "Region",
    "coerce_mask_state",
    "RepairEngine",
    "UpdateResult",
]

__version__ = "1.0.0"
