from __future__ import annotations

from dataclasses import dataclass
from typing import Any


E_INDEX_INVALID = "E_INDEX_INVALID"
E_TILE_SHAPE_MISMATCH = "E_TILE_SHAPE_MISMATCH"
E_PATCH_OUT_OF_RANGE = "E_PATCH_OUT_OF_RANGE"
E_RECOMPUTE_BUDGET_PENDING = "E_RECOMPUTE_BUDGET_PENDING"
E_STATE_INVALID = "E_STATE_INVALID"


@dataclass(frozen=True)
class Failure:
    code: str
    stage: str
    message: str
    row: int | None = None
    col: int | None = None

    @property
    def tile_id(self) -> str | None:
        if self.row is None or self.col is None:
            return None
        return tile_id(self.row, self.col)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "row": self.row,
            "col": self.col,
            "tile": self.tile_id,
            "message": self.message,
        }


class InputError(Exception):
    def __init__(self, code: str, stage: str, message: str, row: int | None = None, col: int | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message
        self.row = row
        self.col = col

    def as_failure(self) -> Failure:
        return Failure(self.code, self.stage, self.message, self.row, self.col)


class StagedError(Exception):
    def __init__(self, stage: str, message: str, code: str = E_TILE_SHAPE_MISMATCH):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.code = code


def tile_id(row: int, col: int) -> str:
    return f"r{row}_c{col}"


def sort_failures(failures: list[Failure]) -> list[Failure]:
    return sorted(
        failures,
        key=lambda f: (
            f.row is None,
            -1 if f.row is None else f.row,
            -1 if f.col is None else f.col,
            f.stage,
            f.code,
            f.message,
        ),
    )
