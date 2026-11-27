"""Matrix-free conjugate-gradient solver for the discrete Poisson equation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import FusionError
from .field_builder import FieldBundle


@dataclass
class ChannelSolve:
    iterations: int
    residual: float
    residual_limit: float
    b_scale: float
    converged: bool
    unknown_pixels: int


@dataclass
class SolveResult:
    image: np.ndarray
    solver: dict[str, Any]


def _apply_operator(values: np.ndarray, unknown: np.ndarray) -> np.ndarray:
    output = np.zeros_like(values)
    center = 4.0 * values
    center[1:, :] -= values[:-1, :]
    center[:-1, :] -= values[1:, :]
    center[:, 1:] -= values[:, :-1]
    center[:, :-1] -= values[:, 1:]
    output[unknown] = center[unknown]
    return output


def build_rhs(base: np.ndarray, bundle: FieldBundle) -> np.ndarray:
    unknown = bundle.unknown_mask
    boundary = bundle.boundary_mask
    divergence = np.zeros_like(base)
    divergence[:, :-1, :] += bundle.gradient_x[:, :-1, :]
    divergence[:, 1:, :] -= bundle.gradient_x[:, :-1, :]
    divergence[:-1, :, :] += bundle.gradient_y[:-1, :, :]
    divergence[1:, :, :] -= bundle.gradient_y[:-1, :, :]

    rhs = -divergence
    # Add the known inner-boundary values to equations of their adjacent unknown pixels.
    rhs[1:, :, :] += np.where(boundary[:-1, :, None], base[:-1, :, :], 0.0)
    rhs[:-1, :, :] += np.where(boundary[1:, :, None], base[1:, :, :], 0.0)
    rhs[:, 1:, :] += np.where(boundary[:, :-1, None], base[:, :-1, :], 0.0)
    rhs[:, :-1, :] += np.where(boundary[:, 1:, None], base[:, 1:, :], 0.0)
    rhs[~unknown] = 0.0
    return rhs


def _solve_channel(
    rhs: np.ndarray,
    unknown: np.ndarray,
    *,
    channel: int,
    tolerance: float,
    max_iterations: int,
) -> tuple[np.ndarray, ChannelSolve]:
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    shape = rhs.shape
    values = np.zeros(shape, dtype=np.float64)
    residual = rhs.copy()
    scale = float(np.max(np.abs(rhs[unknown]))) if np.any(unknown) else 0.0
    residual_limit = float(tolerance) * max(1.0, scale)
    if scale <= residual_limit:
        return values, ChannelSolve(0, float(np.max(np.abs(residual[unknown]))) if np.any(unknown) else 0.0, residual_limit, scale, True, int(np.count_nonzero(unknown)))

    # Jacobi preconditioner: all unknown diagonal entries equal four.
    preconditioned = residual / 4.0
    direction = preconditioned.copy()
    rz = float(np.sum(residual[unknown] * preconditioned[unknown]))
    converged = False
    final_residual = float(np.max(np.abs(residual[unknown])))

    for iteration in range(1, max_iterations + 1):
        operator_direction = _apply_operator(direction, unknown)
        if not np.all(np.isfinite(operator_direction)):
            location = np.argwhere(~np.isfinite(operator_direction))
            y, x = int(location[0, 0]), int(location[0, 1])
            raise FusionError(
                "E_SOLVER_NONFINITE",
                "CG 迭代中出现非有限算子值",
                {
                    "channel": channel,
                    "iteration": iteration,
                    "pixel": [y, x],
                    "finite": False,
                },
            )
        denominator = float(np.sum(direction[unknown] * operator_direction[unknown]))
        if not np.isfinite(denominator) or denominator <= 0:
            if np.any(residual[unknown]):
                location = np.argwhere(unknown & (np.abs(residual) == np.max(np.abs(residual[unknown]))))
                y, x = int(location[0, 0]), int(location[0, 1])
                raise FusionError(
                    "E_SOLVER_NOT_CONVERGED",
                    "CG 方向退化，无法继续求解",
                    {
                        "channel": channel,
                        "iteration": iteration,
                        "denominator": denominator,
                        "pixel": [y, x],
                        "residual": float(np.max(np.abs(residual[unknown]))),
                        "tolerance": float(tolerance),
                    },
                )
            converged = True
            break
        alpha = rz / denominator
        if not np.isfinite(alpha):
            location = np.argwhere(unknown)
            y, x = int(location[0, 0]), int(location[0, 1])
            raise FusionError(
                "E_SOLVER_NONFINITE",
                "CG 步长出现非有限值",
                {"channel": channel, "iteration": iteration, "pixel": [y, x]},
            )
        values[unknown] += alpha * direction[unknown]
        residual[unknown] -= alpha * operator_direction[unknown]
        if not np.all(np.isfinite(values[unknown])) or not np.all(np.isfinite(residual[unknown])):
            bad = np.argwhere(unknown & (~np.isfinite(values) | ~np.isfinite(residual)))
            y, x = int(bad[0, 0]), int(bad[0, 1])
            raise FusionError(
                "E_SOLVER_NONFINITE",
                "CG 迭代产生非有限像素值",
                {
                    "channel": channel,
                    "iteration": iteration,
                    "pixel": [y, x],
                    "values_finite": bool(np.all(np.isfinite(values[unknown]))),
                    "residual_finite": bool(np.all(np.isfinite(residual[unknown]))),
                },
            )
        final_residual = float(np.max(np.abs(residual[unknown])))
        if final_residual <= residual_limit:
            converged = True
            break

        preconditioned[unknown] = residual[unknown] / 4.0
        rz_new = float(np.sum(residual[unknown] * preconditioned[unknown]))
        if not np.isfinite(rz_new):
            location = np.argwhere(unknown)
            y, x = int(location[0, 0]), int(location[0, 1])
            raise FusionError(
                "E_SOLVER_NONFINITE",
                "CG 内积出现非有限值",
                {"channel": channel, "iteration": iteration, "pixel": [y, x]},
            )
        beta = rz_new / rz if rz != 0 else 0.0
        direction[unknown] = preconditioned[unknown] + beta * direction[unknown]
        rz = rz_new

    iterations = iteration if "iteration" in locals() else 0
    if not converged:
        candidates = np.argwhere(unknown & (np.abs(residual) == final_residual))
        y, x = int(candidates[0, 0]), int(candidates[0, 1])
        raise FusionError(
            "E_SOLVER_NOT_CONVERGED",
            "泊松求解未达到给定容差",
            {
                "channel": channel,
                "iteration": iterations,
                "max_iterations": max_iterations,
                "pixel": [y, x],
                "residual": final_residual,
                "residual_limit": residual_limit,
                "tolerance": float(tolerance),
            },
        )
    stats = ChannelSolve(
        iterations=iterations,
        residual=final_residual,
        residual_limit=residual_limit,
        b_scale=scale,
        converged=converged,
        unknown_pixels=int(np.count_nonzero(unknown)),
    )
    return values, stats


def solve_poisson(
    base: np.ndarray,
    bundle: FieldBundle,
    *,
    tolerance: float = 1e-8,
    max_iterations: int = 8000,
) -> SolveResult:
    base = np.asarray(base, dtype=np.float64)
    if base.ndim != 3 or base.shape[2] != 3:
        raise ValueError("base must be HxWx3")
    rhs_all = build_rhs(base, bundle)
    fused = base.copy()
    channel_stats: list[ChannelSolve] = []
    for channel in range(3):
        values, stats = _solve_channel(
            rhs_all[:, :, channel],
            bundle.unknown_mask,
            channel=channel,
            tolerance=tolerance,
            max_iterations=max_iterations,
        )
        fused[:, :, channel] = base[:, :, channel]
        fused[:, :, channel][bundle.unknown_mask] = values[bundle.unknown_mask]
        channel_stats.append(stats)
    if not np.all(np.isfinite(fused)):
        location = np.argwhere(~np.isfinite(fused))
        y, x = int(location[0, 0]), int(location[0, 1])
        raise FusionError(
            "E_SOLVER_NONFINITE",
            "融合结果包含非有限值，已禁止输出",
            {"pixel": [y, x]},
        )
    solver_info = {
        "method": "preconditioned conjugate gradient",
        "operator": "5-point negative Laplacian",
        "boundary_condition": "Dirichlet: base RGB on inner mask boundary",
        "convergence_metric": "max_abs_residual <= tolerance * max(1, max_abs_rhs)",
        "tolerance": float(tolerance),
        "max_iterations": int(max_iterations),
        "converged": all(item.converged for item in channel_stats),
        "iterations_total": int(sum(item.iterations for item in channel_stats)),
        "iterations_max": int(max(item.iterations for item in channel_stats)),
        "channels": [
            {
                "channel": index,
                "iterations": item.iterations,
                "residual": item.residual,
                "residual_limit": item.residual_limit,
                "rhs_scale": item.b_scale,
                "unknown_pixels": item.unknown_pixels,
                "converged": item.converged,
            }
            for index, item in enumerate(channel_stats)
        ],
    }
    return SolveResult(image=fused, solver=solver_info)
