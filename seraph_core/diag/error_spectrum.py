"""Chunk-vs-whole comparison and level-wise error attribution.

The reference truth inside every snapshot is the **float64, whole-batch,
default scaling** training run on the identical dataset.  Five ablation
variants isolate one error level each, plus a float32 half-even baseline used
to compute the marginal rounding effect.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np

from ..engine.features import standardize_apply
from ..engine.training import (LinearRegressor, chunk_plan, recompute_loss)
from ..engine.training import augment

LEVEL_ORDER = ("feature_scaling", "chunk_accumulation", "fixed_quantization",
               "rounding_mode", "lr_scaling")


def _quantiles(values: np.ndarray) -> dict:
    flat = np.abs(values).reshape(-1)
    if flat.size == 0:
        return {}
    return {
        "max_abs": float(np.max(flat)),
        "mean_abs": float(np.mean(flat)),
        "p50_abs": float(np.percentile(flat, 50)),
        "p90_abs": float(np.percentile(flat, 90)),
        "p95_abs": float(np.percentile(flat, 95)),
        "p99_abs": float(np.percentile(flat, 99)),
    }


def _relative_deviation(values: np.ndarray, reference: np.ndarray) -> float:
    denom = float(np.sum(np.abs(reference)))
    if denom == 0.0:
        return 0.0 if float(np.sum(np.abs(values))) == 0.0 else float("inf")
    return float(np.sum(np.abs(values)) / denom)


@dataclass
class VariantResult:
    label: str
    level: str
    precision: str
    weights: np.ndarray
    own_history: list
    ref_ground_losses: list
    converged_epoch: int
    config_view: dict


def build_variant_config(base_config, overrides: dict):
    config = copy.deepcopy(base_config)
    for key, value in overrides.items():
        setattr(config, key, value)
    config.validate()
    return config


def variant_matrix(dataset: dict, config) -> np.ndarray:
    """Rebuild the augmented design matrix for a (possibly rescaled) config."""
    if config.feature_scale == 1.0:
        return dataset["x_aug"]
    scaled = standardize_apply(dataset["raw_x"], dataset["mean"],
                               dataset["std"], config.feature_scale)
    return augment(scaled)


def train_variant(label: str, level: str, dataset: dict, base_config,
                  overrides: dict) -> VariantResult:
    config = build_variant_config(base_config, overrides)
    x_variant = variant_matrix(dataset, config)
    plan = chunk_plan(x_variant.shape[0], config.chunk_size)
    model = LinearRegressor(x_variant.shape[1] - 1, config)
    history, converged_epoch = model.train(x_variant, dataset["y"], plan)
    ref_x = dataset["x_aug"]
    ref_losses = [recompute_loss(model.weights, ref_x, dataset["y"],
                                 base_config)]
    return VariantResult(
        label=label, level=level, precision=config.precision,
        weights=model.weights.copy(),
        own_history=[record.to_dict() if hasattr(record, "to_dict")
                     else record for record in history],
        ref_ground_losses=ref_losses,
        converged_epoch=converged_epoch,
        config_view={"precision": config.precision,
                     "chunk_size": config.chunk_size,
                     "feature_scale": config.feature_scale,
                     "lr_scale": config.lr_scale,
                     "rounding": config.rounding,
                     "fixed_scale": config.fixed_scale})


def run_all_variants(dataset: dict, config) -> dict[str, VariantResult]:
    """Execute the reference plus every isolated error-level variant."""
    variants: dict[str, VariantResult] = {}
    variants["reference"] = train_variant(
        "reference_float64_whole", "reference", dataset, config,
        {"precision": "float64", "chunk_size": 0, "feature_scale": 1.0,
         "lr_scale": 1.0, "rounding": "half_even"})
    variants["float32_baseline"] = train_variant(
        "float32_half_even_whole", "rounding_mode", dataset, config,
        {"precision": "float32", "chunk_size": 0, "rounding": "half_even"})
    variants["feature_scaling"] = train_variant(
        "float64_whole_feature_scale_2x", "feature_scaling", dataset, config,
        {"precision": "float64", "chunk_size": 0, "feature_scale": 2.0})
    variants["chunk_accumulation"] = train_variant(
        "float32_chunked_vs_float32_whole", "chunk_accumulation",
        dataset, config,
        {"precision": "float32",
         "chunk_size": max(1, config.resource_limits.min_chunk_size)})
    variants["chunk_accumulation_float64"] = train_variant(
        "float64_chunked", "chunk_accumulation", dataset, config,
        {"precision": "float64",
         "chunk_size": max(1, config.resource_limits.min_chunk_size)})
    variants["fixed_quantization"] = train_variant(
        "fixed_whole", "fixed_quantization", dataset, config,
        {"precision": "fixed", "chunk_size": 0})
    variants["rounding_mode"] = train_variant(
        "float32_truncate_whole", "rounding_mode", dataset, config,
        {"precision": "float32", "chunk_size": 0, "rounding": "truncate"})
    variants["lr_scaling"] = train_variant(
        "float64_whole_lr_scale_half", "lr_scaling", dataset, config,
        {"precision": "float64", "chunk_size": 0, "lr_scale": 0.5})
    return variants


def chunk_vs_whole_report(config, dataset: dict | None = None,
                          variants: dict[str, VariantResult] | None = None
                          ) -> dict:
    """Requirement 3: chunked vs one-shot whole-batch convergence comparison."""
    if variants is None:
        assert dataset is not None
        variants = {
            "reference": train_variant(
                "whole_float32", "reference", dataset, config,
                {"precision": "float32", "chunk_size": 0}),
            "chunk_accumulation": train_variant(
                "chunk_float32", "chunk_accumulation", dataset, config,
                {"precision": "float32",
                 "chunk_size": max(1, config.resource_limits.min_chunk_size)}),
        }
    whole = variants["reference"]
    chunked = variants["chunk_accumulation"]
    whole_losses = [item["loss"] for item in whole.own_history]
    chunk_losses = [item["loss"] for item in chunked.own_history]
    paired = min(len(whole_losses), len(chunk_losses))
    per_epoch = []
    first_divergence = None
    for epoch in range(paired):
        loss_gap = abs(chunk_losses[epoch] - whole_losses[epoch])
        status_diff = bool(chunked.own_history[epoch]["converged"]) != \
            bool(whole.own_history[epoch]["converged"])
        per_epoch.append({
            "epoch": epoch + 1,
            "whole_loss": whole_losses[epoch],
            "chunk_loss": chunk_losses[epoch],
            "max_abs_loss_deviation": loss_gap,
            "convergence_status_differs": status_diff,
        })
        if status_diff and first_divergence is None:
            first_divergence = epoch + 1
    param_diff = (chunked.weights - whole.weights).reshape(-1)
    spectrum = _quantiles(param_diff)
    return {
        "whole_converged_epoch": whole.converged_epoch,
        "chunk_converged_epoch": chunked.converged_epoch,
        "convergence_diverged": (whole.converged_epoch
                                 != chunked.converged_epoch)
        or first_divergence is not None,
        "first_divergence_epoch": first_divergence,
        "per_epoch": per_epoch,
        "final_parameter_deviation": spectrum,
        "relative_parameter_deviation": _relative_deviation(
            param_diff, whole.weights.reshape(-1)),
        "whole_epoch_count": len(whole_losses),
        "chunk_epoch_count": len(chunk_losses),
    }


def error_spectrum(variants: dict[str, VariantResult]) -> dict:
    """Requirement 4: per-level deviation metrics and dominant attribution.

    Each level is measured against its own matched-precision baseline so the
    deviation isolates exactly one mechanism:

    * feature_scaling / lr_scaling -> float64 whole-batch reference
    * chunk_accumulation           -> float32 whole-batch half-even baseline
    * fixed_quantization           -> float64 whole-batch reference
    * rounding_mode                -> float32 whole-batch half-even baseline
    """
    reference = variants["reference"]
    ref_weights = reference.weights.reshape(-1)
    baseline_for = {
        "feature_scaling": reference,
        "chunk_accumulation": variants["float32_baseline"],
        "fixed_quantization": reference,
        "rounding_mode": variants["float32_baseline"],
        "lr_scaling": reference,
    }
    entries: list[dict] = []
    for level in LEVEL_ORDER:
        variant = variants[level]
        baseline = baseline_for[level]
        base_weights = baseline.weights.reshape(-1)
        diff = (variant.weights - baseline.weights).reshape(-1)
        metrics = _quantiles(diff)
        denom = max(float(np.mean(np.abs(base_weights))),
                    np.finfo(np.float64).tiny)
        # Loss gap on the common float64 reference design matrix.
        loss_gap = abs(variant.ref_ground_losses[-1]
                       - baseline.ref_ground_losses[-1])
        entry = {
            "level": level,
            "variant_label": variant.label,
            "baseline_label": baseline.label,
            "precision": variant.precision,
            "config_view": variant.config_view,
            "absolute_deviation": metrics,
            "relative_deviation": _relative_deviation(diff, base_weights),
            "amplification_ratio": metrics["mean_abs"] / denom,
            "final_ref_ground_loss": variant.ref_ground_losses[-1],
            "baseline_final_loss": baseline.ref_ground_losses[-1],
            "loss_gap": loss_gap,
        }
        if level == "chunk_accumulation":
            entry["float64_chunk_vs_whole_max_abs"] = _quantiles(
                (variants["chunk_accumulation_float64"].weights
                 - reference.weights).reshape(-1))["max_abs"]
        entries.append(entry)
    dominant = max(entries, key=lambda item: (
        item["absolute_deviation"]["max_abs"],
        LEVEL_ORDER.index(item["level"])))
    return {
        "reference_label": reference.label,
        "reference_converged_epoch": reference.converged_epoch,
        "levels": entries,
        "dominant_source": dominant["level"],
        "dominant_evidence": {
            "entry_level": dominant["level"],
            "max_abs": dominant["absolute_deviation"]["max_abs"],
            "mean_abs": dominant["absolute_deviation"]["mean_abs"],
            "relative_deviation": dominant["relative_deviation"],
            "amplification_ratio": dominant["amplification_ratio"],
        },
        "traceability": [
            {"level": entry["level"],
             "detail_key": f"levels[{idx}].absolute_deviation",
             "variant_label": entry["variant_label"]}
            for idx, entry in enumerate(entries)],
    }
