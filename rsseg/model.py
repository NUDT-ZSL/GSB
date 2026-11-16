from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class TileMeta:
    row: int
    col: int
    path: Path
    bounds: tuple[int, int, int, int]
    neighbors: tuple[tuple[int, int], ...]
    band_version: str
    band_order: tuple[str, ...]
    expected_shape: tuple[int, int]
    adjacency_digest: str

    @property
    def id(self) -> str:
        return f"r{self.row}_c{self.col}"

    @property
    def width(self) -> int:
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]

    @property
    def stable_key(self) -> tuple[int, int]:
        return self.row, self.col


@dataclass
class TileData:
    meta: TileMeta
    bands: np.ndarray
    array_digest: str
    overlay_applied: bool


@dataclass
class TileResult:
    row: int
    col: int
    cache_key: str
    classes: np.ndarray
    scores: np.ndarray
    band_digest: str
    window_count: int
    result_version: int = 0

    @property
    def id(self) -> str:
        return f"r{self.row}_c{self.col}"

    @property
    def stable_key(self) -> tuple[int, int]:
        return self.row, self.col


@dataclass
class FusionResult:
    row: int
    col: int
    dep_fingerprint: str
    classes: np.ndarray
    participant_keys: tuple[tuple[str, str], ...]
    result_version: int = 0

    @property
    def id(self) -> str:
        return f"r{self.row}_c{self.col}"
