"""Domain models shared by the router, cache, and snapshot layers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    token_len: int
    hidden_dim: int
    num_experts: int
    top_k: int
    capacity_factor: float
    router_revision: int
    block_size: int
    dtype: str
    seed: int

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CaseSpec":
        return cls(
            case_id=payload.get("case_id"),
            token_len=payload.get("token_len"),
            hidden_dim=payload.get("hidden_dim"),
            num_experts=payload.get("num_experts"),
            top_k=payload.get("top_k"),
            capacity_factor=payload.get("capacity_factor"),
            router_revision=payload.get("router_revision"),
            block_size=payload.get("block_size"),
            dtype=payload.get("dtype"),
            seed=payload.get("seed"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "token_len": self.token_len,
            "hidden_dim": self.hidden_dim,
            "num_experts": self.num_experts,
            "top_k": self.top_k,
            "capacity_factor": self.capacity_factor,
            "router_revision": self.router_revision,
            "block_size": self.block_size,
            "dtype": self.dtype,
            "seed": self.seed,
        }

    def with_updates(self, **changes: Any) -> "CaseSpec":
        return replace(self, **changes)

    @property
    def block_count(self) -> int:
        return (self.token_len + self.block_size - 1) // self.block_size


@dataclass(frozen=True)
class RoutingPolicy:
    top_k: int
    capacity_factor: float
    block_size: int

    @classmethod
    def from_case(cls, case: CaseSpec) -> "RoutingPolicy":
        return cls(
            top_k=case.top_k,
            capacity_factor=case.capacity_factor,
            block_size=case.block_size,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_k": self.top_k,
            "capacity_factor": self.capacity_factor,
            "block_size": self.block_size,
        }


@dataclass(frozen=True)
class ResourceLimits:
    """Per-case and recomputation resource ceilings.

    A block estimate includes the retained output, gate probabilities, selected
    gate values, assignment masks, and per-expert contributions used by the
    consistency audit.
    """

    max_case_resident_bytes: int = 64 * 1024 * 1024
    max_parallel_recompute_blocks: int = 2

    def to_dict(self) -> dict[str, int]:
        return {
            "max_case_resident_bytes": self.max_case_resident_bytes,
            "max_parallel_recompute_blocks": self.max_parallel_recompute_blocks,
        }


@dataclass(frozen=True)
class ExpertBank:
    """Immutable expert parameters with per-expert revisions and a digest."""

    seed: int
    num_experts: int
    hidden_dim: int
    revisions: tuple[int, ...]
    weights: tuple[Any, ...]
    digest: str

    @property
    def revision_map(self) -> dict[str, int]:
        return {str(index): revision for index, revision in enumerate(self.revisions)}

    @property
    def revision_digest(self) -> str:
        return self.digest

    def expert_digest(self, expert_id: int) -> str:
        from .weights import array_digest

        return array_digest(self.weights[expert_id])


@dataclass(frozen=True)
class WeightBundle:
    case: CaseSpec
    tokens: Any
    gate_weights: Any
    expert_bank: ExpertBank
    token_content_hash: str
    gate_weights_digest: str

    @property
    def expert_weights(self) -> tuple[Any, ...]:
        return self.expert_bank.weights

