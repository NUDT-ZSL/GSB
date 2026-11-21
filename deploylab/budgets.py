from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .bundle import LoadedBundle
from .config import BudgetConfig
from .errors import DeployError, E_RESOURCE_LIMIT


@dataclass
class BudgetLedger:
    stage: str
    limits: dict[str, int]
    usage: dict[str, int]
    strategy: str
    degradations: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "limits": self.limits,
            "usage": self.usage,
            "strategy": self.strategy,
            "degradations": sorted(set(self.degradations)),
            "details": self.details,
        }


def _resource_error(
    stage: str,
    message: str,
    *,
    limit: str,
    actual: int,
    maximum: int,
    layer_id: str | None = None,
) -> DeployError:
    return DeployError(
        E_RESOURCE_LIMIT,
        stage,
        message,
        layer_id=layer_id,
        details={"limit": limit, "actual": actual, "max": maximum},
    )


def check_layer_count(
    stage: str,
    layer_count: int,
    budget: BudgetConfig,
) -> None:
    if layer_count > budget.max_layers_per_stage:
        raise _resource_error(
            stage,
            f"stage requires {layer_count} layers, limit is "
            f"{budget.max_layers_per_stage}",
            limit="max_layers_per_stage",
            actual=layer_count,
            maximum=budget.max_layers_per_stage,
        )


def quant_macs(bundle: LoadedBundle) -> int:
    return sum(layer.out_features * layer.in_features * 2 for layer in bundle.layers)


def check_quant_macs(bundle: LoadedBundle, budget: BudgetConfig) -> int:
    actual = quant_macs(bundle)
    if actual > budget.max_quant_macs:
        raise _resource_error(
            "quantization",
            f"quantization MAC budget exceeded: {actual} > {budget.max_quant_macs}",
            limit="max_quant_macs",
            actual=actual,
            maximum=budget.max_quant_macs,
        )
    return actual


def choose_quant_group_cap(
    bundle: LoadedBundle,
    budget: BudgetConfig,
) -> tuple[int | None, BudgetLedger]:
    max_layer_elements = max(
        layer.out_features * layer.in_features for layer in bundle.layers
    )
    peak_layer_bytes = max_layer_elements * 8 * 3
    total_dequant_bytes = sum(
        layer.out_features * layer.in_features * 8 for layer in bundle.layers
    )
    resident_estimate = total_dequant_bytes * 2 + peak_layer_bytes
    degradations: list[str] = []
    group_cap: int | None = None
    strategy = "layer_streaming"
    if resident_estimate > budget.max_resident_bytes:
        if not budget.allow_degrade:
            raise _resource_error(
                "quantization",
                "quantization resident memory budget exceeded and degradation is disabled",
                limit="max_resident_bytes",
                actual=resident_estimate,
                maximum=budget.max_resident_bytes,
            )
        group_cap = budget.degraded_group_size
        degradations.append("DEGRADED_GROUP_SIZE")
        strategy = "degraded_group_layer_streaming"
        reduced_estimate = peak_layer_bytes
        resident_estimate = reduced_estimate
        if reduced_estimate > budget.max_resident_bytes:
            raise _resource_error(
                "quantization",
                "single-layer quantization working set exceeds resident memory budget",
                limit="max_resident_bytes",
                actual=reduced_estimate,
                maximum=budget.max_resident_bytes,
            )
    ledger = BudgetLedger(
        stage="quantization",
        limits={
            "max_layers_per_stage": budget.max_layers_per_stage,
            "max_resident_bytes": budget.max_resident_bytes,
            "max_quant_macs": budget.max_quant_macs,
        },
        usage={
            "layers": len(bundle.layers),
            "resident_bytes": resident_estimate,
            "dequant_bytes": total_dequant_bytes,
            "quant_macs": quant_macs(bundle),
        },
        strategy=strategy,
        degradations=degradations,
        details={"group_size_cap": group_cap},
    )
    return group_cap, ledger


def plan_inference(
    bundle: LoadedBundle,
    budget: BudgetConfig,
) -> tuple[dict[str, Any], BudgetLedger]:
    weight_bytes = sum(
        layer.out_features * layer.in_features * 8 for layer in bundle.layers
    )
    activation_bytes = sum(layer.out_features * 8 for layer in bundle.layers)
    input_bytes = bundle.layers[0].in_features * 8
    total_macs = sum(
        layer.out_features * layer.in_features for layer in bundle.layers
    )
    if total_macs > budget.max_inference_macs:
        raise _resource_error(
            "inference",
            f"inference MAC budget exceeded: {total_macs} > "
            f"{budget.max_inference_macs}",
            limit="max_inference_macs",
            actual=total_macs,
            maximum=budget.max_inference_macs,
        )
    baseline = weight_bytes * 2 + activation_bytes + input_bytes
    max_layer = max(
        (
            layer.out_features * layer.in_features * 8
            + layer.in_features * 8
            + layer.out_features * 8
            + layer.out_features * 8
            for layer in bundle.layers
        ),
        default=0,
    )
    strategy: dict[str, Any]
    degradations: list[str] = []
    resident_estimate = baseline
    if baseline <= budget.max_resident_bytes:
        strategy = {"name": "resident", "tile_rows": None}
    elif budget.allow_degrade and max_layer <= budget.max_resident_bytes:
        strategy = {"name": "streaming", "tile_rows": None}
        resident_estimate = max_layer
        degradations.append("DEGRADED_STREAMING")
    elif budget.allow_degrade:
        largest = max(bundle.layers, key=lambda item: item.out_features)
        available = (
            budget.max_resident_bytes
            - largest.in_features * 8
            - largest.out_features * 8
            - largest.out_features * 8
        )
        tile_rows = int(available // (largest.in_features * 8)) if available > 0 else 0
        if tile_rows < budget.inference_tile_min_rows:
            raise _resource_error(
                "inference",
                "resident memory budget is below one output row plus activation working set",
                limit="max_resident_bytes",
                actual=max_layer,
                maximum=budget.max_resident_bytes,
            )
        strategy = {"name": "tiled", "tile_rows": tile_rows}
        resident_estimate = (
            tile_rows * largest.in_features * 8
            + largest.in_features * 8
            + largest.out_features * 8 * 2
        )
        degradations.append("DEGRADED_TILED")
    else:
        raise _resource_error(
            "inference",
            "inference resident memory budget exceeded and degradation is disabled",
            limit="max_resident_bytes",
            actual=baseline,
            maximum=budget.max_resident_bytes,
        )
    ledger = BudgetLedger(
        stage="inference",
        limits={
            "max_layers_per_stage": budget.max_layers_per_stage,
            "max_resident_bytes": budget.max_resident_bytes,
            "max_inference_macs": budget.max_inference_macs,
        },
        usage={
            "layers": len(bundle.layers),
            "resident_bytes": resident_estimate,
            "weight_bytes": weight_bytes,
            "activation_bytes": activation_bytes,
            "inference_macs": total_macs,
        },
        strategy=str(strategy["name"]),
        degradations=degradations,
        details=strategy,
    )
    return strategy, ledger


def simple_ledger(stage: str, bundle: LoadedBundle, budget: BudgetConfig) -> BudgetLedger:
    return BudgetLedger(
        stage=stage,
        limits={
            "max_layers_per_stage": budget.max_layers_per_stage,
            "max_resident_bytes": budget.max_resident_bytes,
        },
        usage={
            "layers": len(bundle.layers),
            "resident_bytes": max(
                layer.out_features * layer.in_features * 8 + layer.out_features * 8
                for layer in bundle.layers
            ),
        },
        strategy="sequential_layers",
    )

