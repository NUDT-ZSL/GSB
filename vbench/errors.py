"""业务错误码与失败清单条目。"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional


class ErrorCode:
    E_TIMESERIES_INVALID = "E_TIMESERIES_INVALID"
    E_CONFIG_INVALID = "E_CONFIG_INVALID"
    E_PATH_DIVERGENCE = "E_PATH_DIVERGENCE"
    E_DENOISE_BUDGET_PENDING = "E_DENOISE_BUDGET_PENDING"
    E_PREVIEW_BUDGET_PENDING = "E_PREVIEW_BUDGET_PENDING"
    E_STAGE_FAILED = "E_STAGE_FAILED"
    E_SNAPSHOT_INVALID = "E_SNAPSHOT_INVALID"


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    clip_id: Optional[str] = None
    tier: Optional[str] = None
    path: Optional[str] = None
    step: Optional[int] = None
    frame: Optional[int] = None
    exception: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return {key: value for key, value in data.items() if value is not None}
