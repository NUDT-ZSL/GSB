"""Scenario configuration construction and validation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .errors import DiagnosticError, cross_def, intermediate_dim, precision_invalid
from .precision import Precision


VARIABLES = [f"x{i:02d}" for i in range(10)]


def default_config(seed: int = 440) -> dict[str, Any]:
    """Return the deterministic scenario configuration used by fixtures."""
    return {
        "schema": "uid-diagnostic-scenario/v1",
        "config_version": "scenario-fixed-v1",
        "seed": seed,
        "variables": VARIABLES,
        "time": {
            "origin_ms": 0,
            "grid_ms": 100,
            "window_ms": 3200,
            "hop_ms": 3200,
            "grid_rows": 32,
            "values_width": 4,
            "subsample_ms": 25,
            "windows": 8,
        },
        "completion": {
            "method": "ffill",
            "constant": 0.0,
            "noise_decay": 0.5,
            "variance_floor": 1e-12,
        },
        "cross": {
            "column_pattern": "{slot}#{combo}#{op}",
            "encoding_rules": {
                "product": "signed_log1p",
                "triple_product": "signed_log1p",
                "difference": "raw",
            },
            "condition_method": "zscore",
            "condition_bins": 2,
            "max_cross_columns": 128,
            "max_resident_bytes": 50_000_000,
            "slots": [
                {
                    "name": "pair_core",
                    "arity": 2,
                    "variables": ["x00", "x01", "x02", "x03", "x04"],
                    "ops": ["product", "difference"],
                },
                {
                    "name": "pair_edge",
                    "arity": 2,
                    "variables": ["x05", "x06", "x07", "x08", "x09"],
                    "ops": ["product", "difference"],
                },
                {
                    "name": "triple_core",
                    "arity": 3,
                    "variables": ["x00", "x01", "x02", "x03"],
                    "ops": ["triple_product"],
                },
            ],
        },
        "reduction": {
            "active": "nested",
            "candidates": [
                {"name": "pca3", "method": "pca", "dim": 3, "seed": 17},
                {
                    "name": "random3",
                    "method": "random_projection",
                    "dim": 3,
                    "density": 0.5,
                    "seed": 29,
                },
                {
                    "name": "nested",
                    "method": "pca_then_random_projection",
                    "intermediate_dim": 6,
                    "dim": 3,
                    "density": 0.5,
                    "seed": 41,
                },
            ],
        },
        "covariance": {"decay": 0.82, "ddof": 0},
        "predictor": {
            "target_var": "x00",
            "horizon_rows": 1,
            "quantiles": [0.1, 0.5, 0.9],
            "coefficients": [
                [0.32, -0.18, 0.11],
                [0.30, -0.16, 0.10],
                [0.28, -0.14, 0.09],
            ],
            "intercepts": [-0.72, 0.02, 0.76],
            "learning_rate": 0.0,
        },
        "drift": {
            "residual_decay": 0.85,
            "feature_decay": 0.8,
            "score_threshold": 2.5,
            "min_residuals": 4,
        },
        "gating": {
            "strategy": "tolerance",
            "error_threshold": 2.0,
            "tolerance": 0.25,
            "near_band": 0.2,
        },
        "precision": {
            "default": "float64",
            "rounding": "nearest_even",
            "profiles": {
                "float64": {"kind": "float64", "rounding": "nearest_even"},
                "float32": {"kind": "float32", "rounding": "nearest_even"},
                "fixed": {
                    "kind": "fixed",
                    "scale": 1000,
                    "rounding": "half_up",
                },
            },
        },
        "cache": {"directory": ".diagnostic/cache"},
        "state": {"directory": ".diagnostic/state"},
        "sqlite": {"path": ".diagnostic/index.sqlite3"},
    }


def clone_config(config: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(config)


def validate_config(config: dict[str, Any], profile: str | None = None) -> Precision:
    """Validate cross/reduction/precision declarations and return precision."""
    variables = config.get("variables")
    if not isinstance(variables, list) or not variables:
        raise DiagnosticError("E_BAD_INPUT", "config.variables must be a non-empty list")
    if len(set(variables)) != len(variables):
        raise DiagnosticError("E_BAD_INPUT", "config.variables must be unique")

    cross_cfg = config.get("cross", {})
    known = set(variables)
    for slot in cross_cfg.get("slots", []):
        slot_vars = slot.get("variables", [])
        if slot.get("arity") not in (2, 3):
            raise cross_def("slot arity must be 2 or 3", slot=slot.get("name"), arity=slot.get("arity"))
        if len(slot_vars) < slot["arity"]:
            raise cross_def(
                "slot must contain at least arity variables",
                slot=slot.get("name"),
                arity=slot["arity"],
                variables=slot_vars,
            )
        missing = [name for name in slot_vars if name not in known]
        if missing:
            raise cross_def(
                "cross slot references unknown variable(s)",
                slot=slot.get("name"),
                missing=missing,
            )

    reduction_cfg = config.get("reduction", {})
    active_name = reduction_cfg.get("active")
    candidates = {c.get("name"): c for c in reduction_cfg.get("candidates", [])}
    if active_name not in candidates:
        raise DiagnosticError("E_BAD_INPUT", "active reduction candidate is not declared", active=active_name)
    candidate = candidates[active_name]
    method = candidate.get("method")
    dim = int(candidate.get("dim", 0))
    grid_rows = int(config.get("time", {}).get("grid_rows", 0))
    if dim <= 0:
        raise intermediate_dim("reduction dim must be positive", candidate=active_name, dim=dim)
    if method == "pca_then_random_projection":
        middle = candidate.get("intermediate_dim")
        if not isinstance(middle, int) or middle <= 0 or middle < dim or middle > grid_rows:
            raise intermediate_dim(
                "nested projection intermediate dimension is illegal",
                candidate=active_name,
                intermediate_dim=middle,
                output_dim=dim,
                grid_rows=grid_rows,
            )
    if dim > grid_rows:
        raise intermediate_dim(
            "output dimension cannot exceed deterministic grid rows",
            candidate=active_name,
            dim=dim,
            grid_rows=grid_rows,
        )

    precision = Precision.from_config(config, profile)
    if precision.kind == "fixed" and (precision.scale is None or precision.scale <= 0):
        raise precision_invalid("fixed precision requires positive scale")
    return precision
