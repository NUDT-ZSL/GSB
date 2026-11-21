from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .util import canonical_json, sha256_text


@dataclass(frozen=True)
class PipelineConfig:
    seed: int = 20261122
    context_turns: int = 3
    previous_turn_decay: float = 0.5
    recall_top_k: int = 4
    vector_top_k: int = 6
    snippet_top_k: int = 4
    confidence_weight: float = 0.65
    coverage_weight: float = 0.35
    route_weights: dict[str, float] | None = None
    confidence_half_width: float = 0.08

    def __post_init__(self) -> None:
        if self.route_weights is None:
            object.__setattr__(
                self,
                "route_weights",
                {"inverted": 1.0, "category_popular": 0.35, "vector_approx": 0.65},
            )

    @property
    def content_hash(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["route_weights"] = dict(sorted(data["route_weights"].items()))
        return data
