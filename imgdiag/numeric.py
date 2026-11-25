from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


LEVELS = [
    "boundary_sampling",
    "block_completion",
    "style_shading",
    "fixed_quantization",
    "boundary_fusion",
]


@dataclass(frozen=True)
class PrecisionContext:
    profile: str = "float64"
    quant_scale: float = 128.0
    rounding_mode: str = "nearest"

    def __post_init__(self) -> None:
        if self.profile not in {"float64", "float32", "fixed"}:
            raise ValueError(f"unsupported dtype_profile {self.profile}")
        if self.profile == "fixed":
            if float(self.quant_scale) <= 0:
                raise ValueError("quant_scale must be positive")
            if self.rounding_mode not in {"nearest", "half_up", "floor", "ceil", "trunc"}:
                raise ValueError(f"unsupported rounding_mode {self.rounding_mode}")

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype(np.float32 if self.profile == "float32" else np.float64)

    def transform_matrix(self, matrix: np.ndarray) -> np.ndarray:
        return np.asarray(matrix, dtype=self.numpy_dtype)

    def quantize_codes(self, values: np.ndarray) -> np.ndarray:
        values64 = np.asarray(values, dtype=np.float64)
        scaled = values64 * float(self.quant_scale)
        mode = self.rounding_mode
        if mode in {"nearest", "half_up"}:
            rounded = np.floor(scaled + 0.5)
        elif mode == "floor":
            rounded = np.floor(scaled)
        elif mode == "ceil":
            rounded = np.ceil(scaled)
        else:  # trunc
            rounded = np.trunc(scaled)
        return rounded.astype(np.int64)

    def q(self, values: np.ndarray | float | int) -> np.ndarray:
        """Apply the profile's storage/rounding operator."""
        array = np.asarray(values)
        if self.profile == "float64":
            return array.astype(np.float64)
        if self.profile == "float32":
            return array.astype(np.float32)
        codes = self.quantize_codes(array)
        return codes.astype(np.float64) / float(self.quant_scale)

    def finalize(self, values: np.ndarray) -> np.ndarray:
        """Quantize a stage and normalize diagnostics back to float64."""
        return np.asarray(self.q(values), dtype=np.float64)


def precision_from_task(task: dict[str, Any], params: dict[str, Any] | None = None) -> PrecisionContext:
    profile = task.get("dtype_profile", "float64")
    if isinstance(profile, dict):
        quant = profile.get("quant_scale", (params or {}).get("quantization", {}).get("default_quant_scale", 128))
        mode = profile.get("rounding_mode", (params or {}).get("quantization", {}).get("rounding_mode", "nearest"))
        profile_name = profile.get("kind", profile.get("profile", "fixed"))
    else:
        quant = task.get("quant_scale")
        if quant is None:
            quant = (params or {}).get("quantization", {}).get("default_quant_scale", 128)
        mode = task.get("rounding_mode")
        if mode is None:
            mode = (params or {}).get("quantization", {}).get("rounding_mode", "nearest")
        profile_name = str(profile)
    return PrecisionContext(profile_name, float(quant), str(mode))


def error_statistics(
    actual: np.ndarray,
    reference: np.ndarray,
    *,
    mask: np.ndarray | None = None,
    epsilon: float = 1e-12,
) -> dict[str, Any]:
    actual64 = np.asarray(actual, dtype=np.float64)
    reference64 = np.asarray(reference, dtype=np.float64)
    if actual64.shape != reference64.shape:
        raise ValueError("actual and reference shapes differ")
    if mask is not None:
        if mask.shape != actual64.shape[: mask.ndim] if mask.ndim <= actual64.ndim else True:
            # Supports HxW mask for HxWxC arrays.
            pass
        if mask.ndim == 2 and actual64.ndim == 3:
            selection = np.broadcast_to(mask[..., None], actual64.shape)
        else:
            selection = mask
        diff = actual64[selection] - reference64[selection]
        ref_selected = reference64[selection]
    else:
        diff = (actual64 - reference64).reshape(-1)
        ref_selected = reference64.reshape(-1)

    if diff.size == 0:
        return {
            "count": 0, "mean_abs": 0.0, "max_abs": 0.0, "rmse": 0.0,
            "relative_rmse": 0.0, "reference_rms": 0.0,
        }
    abs_diff = np.abs(diff)
    rmse = float(np.sqrt(np.mean(diff * diff)))
    ref_rms = float(np.sqrt(np.mean(ref_selected * ref_selected)))
    max_index = int(np.argmax(abs_diff))
    return {
        "count": int(diff.size),
        "mean_abs": float(np.mean(abs_diff)),
        "max_abs": float(np.max(abs_diff)),
        "rmse": rmse,
        "reference_rms": ref_rms,
        "relative_rmse": rmse / max(ref_rms, epsilon),
        "max_abs_flat_index": max_index,
    }


def first_difference(
    actual: np.ndarray,
    reference: np.ndarray,
    region_coords: np.ndarray | None = None,
) -> dict[str, Any] | None:
    actual64 = np.asarray(actual, dtype=np.float64)
    reference64 = np.asarray(reference, dtype=np.float64)
    if actual64.shape != reference64.shape:
        return {
            "coordinate": None,
            "actual_shape": list(actual64.shape),
            "reference_shape": list(reference64.shape),
            "actual_value": None,
            "reference_value": None,
        }
    unequal = actual64 != reference64
    if not np.any(unequal):
        return None
    indices = np.argwhere(unequal)
    first = indices[0]
    coordinate = [int(v) for v in first]
    if region_coords is not None and actual64.ndim == 2 and actual64.shape[1] == 3:
        global_yx = region_coords[first[0]]
        coordinate = [int(global_yx[0]), int(global_yx[1]), int(first[1])]
    return {
        "coordinate_yx_or_yxc": coordinate,
        "coordinate_xy": list(reversed(coordinate[:2])),
        "actual_value": actual64[tuple(first)].tolist(),
        "reference_value": reference64[tuple(first)].tolist(),
    }


def recompute_stage_diagnostics(details: dict[str, np.ndarray]) -> dict[str, Any]:
    """Recompute all reported error metrics from saved level arrays."""
    levels: dict[str, dict[str, Any]] = {}
    previous_rmse: float | None = None
    dominant = None
    dominant_rmse = 0.0
    for level in LEVELS:
        actual_key = f"actual_{level}"
        ref_key = f"ref_{level}"
        if actual_key not in details or ref_key not in details:
            continue
        stats = error_statistics(details[actual_key], details[ref_key])
        rmse = stats["rmse"]
        if previous_rmse is None:
            ratio = 1.0 if rmse == 0.0 else None
            step_defined = rmse == 0.0
        elif previous_rmse == 0.0:
            ratio = 1.0 if rmse == 0.0 else None
            step_defined = rmse == 0.0
        else:
            ratio = rmse / previous_rmse
            step_defined = True
        stats["amplification_ratio"] = ratio
        stats["ratio_defined"] = step_defined
        levels[level] = stats
        if rmse > dominant_rmse and rmse > 0.0:
            dominant_rmse = rmse
            dominant = level
        previous_rmse = rmse

    spectrum = []
    if "actual_block_completion" in details and "ref_block_completion" in details:
        diff = np.abs(
            np.asarray(details["actual_block_completion"], dtype=np.float64)
            - np.asarray(details["ref_block_completion"], dtype=np.float64)
        )
        flat = diff.reshape(-1)
        if flat.size:
            bins = [0.0, 0.0, 1e-12, 1e-8, 1e-5, 1e-3, 1e-1, 1.0, np.inf]
            labels = ["exact_zero", "gt_zero_to_1e-12", "1e-12_to_1e-8",
                       "1e-8_to_1e-5", "1e-5_to_1e-3", "1e-3_to_1e-1",
                       "1e-1_to_1", "gt_1"]
            counts = []
            # First two boundaries intentionally separate exact zero.
            counts.append(int(np.count_nonzero(flat == 0.0)))
            counts.append(int(np.count_nonzero((flat > 0.0) & (flat <= 1e-12))))
            for lo, hi in [(1e-12, 1e-8), (1e-8, 1e-5), (1e-5, 1e-3),
                           (1e-3, 1e-1), (1e-1, 1.0), (1.0, np.inf)]:
                counts.append(int(np.count_nonzero((flat > lo) & (flat <= hi))))
            spectrum = [{"band": label, "count": count} for label, count in zip(labels, counts)]

    return {
        "levels": levels,
        "dominant_error_level": dominant,
        "block_deviation_spectrum": spectrum,
        "recomputable": True,
    }
