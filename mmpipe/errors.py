"""Structured business exceptions used across the pipeline.

Every exception carries a stable business code, the stage it originated from
(``None`` for manifest-level validation), and when possible the affected
``pair_id``. Nothing is ever swallowed silently: a failing pair is always
attributed explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class PipeError(Exception):
    """Base class for all pipeline business errors."""

    code = "E_PIPE"

    def __init__(
        self,
        message: str,
        *,
        stage: Optional[str] = None,
        pair_id: Optional[str] = None,
        version: Optional[int] = None,
        category: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.pair_id = pair_id
        self.version = version
        self.category = category

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.stage is not None:
            out["stage"] = self.stage
        if self.pair_id is not None:
            out["pair_id"] = self.pair_id
        if self.version is not None:
            out["version"] = self.version
        if self.category is not None:
            out["category"] = self.category
        return out


@dataclass
class PairFailure:
    """A worker failure attributed to a concrete pair, returned to the driver."""

    code: str
    message: str
    stage: str
    pair_id: str

    @classmethod
    def from_error(cls, err: PipeError, stage: str, pair_id: str) -> "PairFailure":
        return cls(
            code=err.code,
            message=err.message,
            stage=stage,
            pair_id=pair_id,
        )

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "pair_id": self.pair_id,
        }


class DupPairError(PipeError):
    code = "E_DUP_PAIR"


class MaskInvalidError(PipeError):
    code = "E_MASK_INVALID"


class SeedShapeError(PipeError):
    code = "E_SEED_SHAPE"


class TextEmptyError(PipeError):
    code = "E_TEXT_EMPTY"


class PairIdInvalidError(PipeError):
    code = "E_PAIR_ID_INVALID"


class HintInvalidError(PipeError):
    code = "E_HINT_INVALID"


class SnapshotCorruptError(PipeError):
    code = "E_SNAPSHOT_CORRUPT"


class ResourceLimitError(PipeError):
    code = "E_RESOURCE_LIMIT"


class InconsistentError(PipeError):
    code = "E_INCONSISTENT"

