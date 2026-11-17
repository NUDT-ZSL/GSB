"""Input and scheduling data models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import FailureRecord


@dataclass(frozen=True)
class Candidate:
    request_id: str
    candidate_id: str
    recall_scores: tuple[float, ...]
    dense_features: tuple[float, ...]
    sparse_counts: tuple[int, ...]
    source_line: int

    @property
    def vector(self) -> tuple[float, ...]:
        return self.recall_scores + self.dense_features + tuple(float(v) for v in self.sparse_counts)


@dataclass(frozen=True)
class RequestData:
    request_id: str
    candidates: tuple[Candidate, ...]
    source_lines: tuple[int, ...]

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)


@dataclass(frozen=True)
class ParamsPackage:
    path: str
    raw: dict[str, Any]
    params_hash: str
    version: str
    feature_weights: tuple[float, ...]
    bias: float
    feature_groups: dict[str, tuple[int, ...]]
    feature_names: tuple[str, ...]


@dataclass
class LoadResult:
    requests: dict[str, RequestData]
    failures: list[FailureRecord]
    candidates_hash: str
    params: ParamsPackage | None
    global_failure: FailureRecord | None = None


@dataclass(frozen=True)
class InjectionSpec:
    injection_id: str
    request_id: str
    stage: str
    fault_type: str
    candidate_id: str | None = None
    feature_index: int | None = None
    width: int | None = None
