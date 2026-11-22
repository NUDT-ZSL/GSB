"""Immutable resource and numerical policy for the component."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .canonical import sha256_json
from .codes import E_INVALID_REQUEST
from .errors import raise_business


@dataclass(frozen=True)
class ComponentConfig:
    tolerance_abs: float = 1e-11
    tolerance_rel: float = 1e-11
    row_block_size: int = 4
    max_resident_batches: int = 4
    max_row_buffers: int = 4
    max_parallel_intervals: int = 4
    resource_policy: Literal["degrade", "strict"] = "degrade"

    def __post_init__(self) -> None:
        if self.tolerance_abs < 0 or self.tolerance_rel < 0:
            raise_business(
                E_INVALID_REQUEST,
                case_id=None,
                segment="config.tolerance",
                message="tolerances must be non-negative",
            )
        integer_fields = {
            "row_block_size": self.row_block_size,
            "max_resident_batches": self.max_resident_batches,
            "max_row_buffers": self.max_row_buffers,
            "max_parallel_intervals": self.max_parallel_intervals,
        }
        for name, value in integer_fields.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise_business(
                    E_INVALID_REQUEST,
                    case_id=None,
                    segment=f"config.{name}",
                    message=f"{name} must be a positive integer",
                )
        if self.resource_policy not in {"degrade", "strict"}:
            raise_business(
                E_INVALID_REQUEST,
                case_id=None,
                segment="config.resource_policy",
                message="resource_policy must be 'degrade' or 'strict'",
            )

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def checksum(self) -> str:
        return sha256_json(self.public_dict())
