from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import EngineConfig
from .errors import DiagnosticError
from .injection import InjectionController
from .utils import sha256_json


@dataclass(frozen=True)
class BlockPlan:
    block_size: int
    blocks: tuple[tuple[int, int], ...]
    reduction_order: str
    nominal_block_size: int
    budget_bytes: int
    bytes_per_sample: int
    estimated_peak_bytes: int
    estimated_gradient_passes: int
    small_block_degrade: bool
    decision_basis: tuple[str, ...]
    coverage_sha256: str
    decision_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_size": self.block_size,
            "blocks": [list(block) for block in self.blocks],
            "reduction_order": self.reduction_order,
            "nominal_block_size": self.nominal_block_size,
            "budget_bytes": self.budget_bytes,
            "bytes_per_sample": self.bytes_per_sample,
            "estimated_peak_bytes": self.estimated_peak_bytes,
            "estimated_gradient_passes": self.estimated_gradient_passes,
            "small_block_degrade": self.small_block_degrade,
            "decision_basis": list(self.decision_basis),
            "coverage_sha256": self.coverage_sha256,
            "decision_sha256": self.decision_sha256,
        }


def _make_plan(
    *,
    n: int,
    selected_size: int,
    nominal_size: int,
    budget_bytes: int,
    bytes_per_sample: int,
    small_block_degrade: bool,
    decision_basis: list[str],
    reduction_order: str = "ascending_block_index_then_feature_index",
) -> BlockPlan:
    blocks = tuple(
        (start, min(start + selected_size, n))
        for start in range(0, n, selected_size)
    )
    coverage_payload = {
        "sample_count": n,
        "blocks": [list(block) for block in blocks],
        "reduction_order": reduction_order,
    }
    coverage_sha256 = sha256_json(coverage_payload)
    decision_payload = {
        "sample_count": n,
        "block_size": selected_size,
        "nominal_block_size": nominal_size,
        "blocks": [list(block) for block in blocks],
        "reduction_order": reduction_order,
        "budget_bytes": budget_bytes,
        "bytes_per_sample": bytes_per_sample,
        "small_block_degrade": small_block_degrade,
        "decision_basis": decision_basis,
        "coverage_sha256": coverage_sha256,
    }
    return BlockPlan(
        block_size=selected_size,
        blocks=blocks,
        reduction_order=reduction_order,
        nominal_block_size=nominal_size,
        budget_bytes=budget_bytes,
        bytes_per_sample=bytes_per_sample,
        estimated_peak_bytes=selected_size * bytes_per_sample,
        estimated_gradient_passes=max(1, (n + selected_size - 1) // selected_size),
        small_block_degrade=small_block_degrade,
        decision_basis=tuple(decision_basis),
        coverage_sha256=coverage_sha256,
        decision_sha256=sha256_json(decision_payload),
    )


def decide_block_plan(
    *,
    sample_count: int,
    feature_count: int,
    config: EngineConfig,
    injector: InjectionController | None = None,
) -> BlockPlan:
    controller = injector or InjectionController()
    if sample_count <= 0:
        raise DiagnosticError("E_DATA_EMPTY", "block_reduce", "no samples to block")
    bytes_per_sample = 8 * (feature_count + 3) + 16
    budget_bytes = max(1, int(config.block_budget_bytes))
    budget_capacity = max(1, budget_bytes // bytes_per_sample)
    nominal = int(config.block_size) if config.block_size is not None else 64
    injected_size, injected = controller.inject_block_size(nominal)
    if (injected and int(injected_size) <= 0) or (not injected and nominal <= 0):
        invalid_size = int(injected_size) if injected else nominal
        reason = [
            f"invalid_block_size={invalid_size}",
            f"budget_capacity={budget_capacity}",
            f"recovered_block_size={min(64, budget_capacity, sample_count)}",
        ]
        selected = min(64, budget_capacity, sample_count)
        controller.record_block_fallback(
            injected_value=invalid_size, fallback_size=selected
        )
        return _make_plan(
            n=sample_count,
            selected_size=selected,
            nominal_size=nominal,
            budget_bytes=budget_bytes,
            bytes_per_sample=bytes_per_sample,
            small_block_degrade=True,
            decision_basis=reason + ["degrade_category=auto_block_plan"],
        )
    requested = int(injected_size) if injected else nominal
    selected = min(requested, budget_capacity, sample_count)
    if selected <= 0:
        raise DiagnosticError(
            "E_BLOCK_BUDGET_EXHAUSTED",
            "block_reduce",
            "remaining budget cannot hold one sample",
            degrade_category="whole_block",
        )
    small_block_degrade = selected < requested
    basis = [
        f"sample_count={sample_count}",
        f"feature_count={feature_count}",
        f"requested_block_size={requested}",
        f"budget_bytes={budget_bytes}",
        f"bytes_per_sample={bytes_per_sample}",
        f"budget_capacity={budget_capacity}",
        f"selected_block_size={selected}",
        f"last_block={[(start, min(start + selected, sample_count)) for start in range(0, sample_count, selected)][-1]}",
    ]
    if small_block_degrade:
        basis.append("small_block_degrade=true")
    return _make_plan(
        n=sample_count,
        selected_size=selected,
        nominal_size=nominal,
        budget_bytes=budget_bytes,
        bytes_per_sample=bytes_per_sample,
        small_block_degrade=small_block_degrade,
        decision_basis=basis,
    )


def whole_block_plan(
    *, sample_count: int, feature_count: int, config: EngineConfig
) -> BlockPlan:
    bytes_per_sample = 8 * (feature_count + 3) + 16
    return _make_plan(
        n=sample_count,
        selected_size=sample_count,
        nominal_size=sample_count,
        budget_bytes=int(config.block_budget_bytes),
        bytes_per_sample=bytes_per_sample,
        small_block_degrade=False,
        decision_basis=[
            "comparison_path=whole",
            f"sample_count={sample_count}",
            "selected_block_size=sample_count",
        ],
        reduction_order="whole_matrix_feature_order",
    )

