"""Gradient fitting loop with projection, convergence and checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .errors import ConvergenceError
from .schema import DOMAIN
from .shading import finite_difference_grad
from .state import save_checkpoint, write_interrupt_marker

PARAMETER_NAMES = ("albedo", "roughness")


@dataclass
class FitResult:
    params: np.ndarray
    losses: List[float] = field(default_factory=list)
    grad_norms: List[float] = field(default_factory=list)
    projection_events: List[Dict[str, Any]] = field(default_factory=list)
    converged: bool = False
    iterations_completed: int = 0
    start_iteration: int = 0
    interrupted: bool = False
    interrupt_signal: Optional[str] = None

    def projection_count(self, name: str) -> int:
        return sum(1 for event in self.projection_events if event["parameter"] == name)


def project_to_domain(
    params: np.ndarray, iteration: int
) -> (np.ndarray, List[Dict[str, Any]]):
    """Clamp parameters back into the domain and record per-parameter events."""
    events: List[Dict[str, Any]] = []
    projected = params.copy()
    for index, name in enumerate(PARAMETER_NAMES):
        low, high = DOMAIN[name]
        if projected[index] < low:
            events.append(
                {"parameter": name, "iteration": iteration,
                 "bound": "lower", "requested": float(projected[index]),
                 "projected_to": low}
            )
            projected[index] = low
        elif projected[index] > high:
            events.append(
                {"parameter": name, "iteration": iteration,
                 "bound": "upper", "requested": float(projected[index]),
                 "projected_to": high}
            )
            projected[index] = high
    return projected, events


def check_convergence(
    losses: List[float],
    grad_norms: List[float],
    loss_rel_tol: float,
    grad_norm_tol: float,
    window: int,
    loss_abs_tol: float,
) -> bool:
    """Dual-threshold convergence: small relative loss change AND small gradient."""
    if len(losses) < window + 1 or not grad_norms:
        return False
    recent = losses[-(window + 1):]
    # Normalize by the largest loss magnitude in the window.  An absolute floor
    # keeps the relative-change test meaningful once the loss itself is tiny,
    # where geometric convergence still produces non-vanishing ratios.
    baseline = max(1.0e-12, max(abs(value) for value in recent))
    relative_change = abs(recent[-1] - recent[0]) / baseline
    gradient_ok = all(norm <= grad_norm_tol for norm in grad_norms[-window:])
    loss_ok = relative_change <= loss_rel_tol or abs(recent[-1]) <= loss_abs_tol
    return loss_ok and gradient_ok


def gradient_consistency(
    params: np.ndarray,
    geometry: Dict[str, Any],
    target: np.ndarray,
    analytic: np.ndarray,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare the analytic gradient against the finite-difference reference."""
    check = config["gradient_check"]
    step = float(check["step"])
    tolerance = float(check["tolerance"])
    reference = finite_difference_grad(params, geometry, target, step)

    abs_diff = np.abs(analytic - reference)
    if check["compare"] == "relative":
        scale = np.maximum(np.abs(reference), 1.0e-8)
        deviation = abs_diff / scale
    elif check["compare"] == "absolute":
        deviation = abs_diff
    else:
        # Pass when either the absolute difference or the relative difference
        # is within tolerance; robust where the true gradient is near zero.
        relative = abs_diff / np.maximum(np.abs(reference), 1.0e-8)
        deviation = np.minimum(abs_diff, relative)
    max_index = int(np.argmax(deviation))
    return {
        "analytic": [float(x) for x in analytic],
        "finite_difference": [float(x) for x in reference],
        "abs_diff": [float(x) for x in abs_diff],
        "max_abs_diff": float(np.max(abs_diff)),
        "max_deviation": float(np.max(deviation)),
        "max_deviation_parameter": PARAMETER_NAMES[max_index],
        "tolerance": tolerance,
        "compare": check["compare"],
        "step": step,
        "passed": bool(np.max(deviation) <= tolerance),
    }


def fit(
    config: Dict[str, Any],
    geometry: Dict[str, Any],
    target: np.ndarray,
    version_digest: str,
    state_dir: str,
    evaluate: Callable[[np.ndarray], (float, np.ndarray)],
    initial: np.ndarray,
    learning_rates: (float, float),
    *,
    resumed: Optional[FitResult] = None,
    signal_flag: Optional[Dict[str, str]] = None,
) -> FitResult:
    """Run the deterministic gradient-descent loop.

    ``evaluate`` returns ``(loss, gradient)`` for the chosen serial or tiled
    execution path.  Checkpoints are written only after a complete iteration,
    and handled signals are observed only at iteration boundaries.
    """
    optimization = config["optimization"]
    total_iterations = int(optimization["iterations"])
    window = int(optimization["convergence_window"])
    checkpoint_every = int(optimization["checkpoint_every"])
    loss_rel_tol = float(optimization["loss_rel_tol"])
    loss_abs_tol = float(optimization.get("loss_abs_tol", 1.0e-8))
    grad_norm_tol = float(optimization["grad_norm_tol"])
    lr_albedo, lr_roughness = learning_rates
    learning_rate = np.array([lr_albedo, lr_roughness], dtype=np.float64)

    if resumed is not None:
        result = resumed
        params = result.params.copy()
    else:
        result = FitResult(params=initial.copy())
        params = initial.copy()

    while result.iterations_completed < total_iterations:
        iteration = result.iterations_completed + 1
        loss, gradient = evaluate(params)
        grad_norm = float(np.sqrt(np.sum(gradient * gradient)))

        # Deterministic plain SGD update followed by domain projection.
        updated = params - learning_rate * gradient
        updated, events = project_to_domain(updated, iteration)

        params = updated
        result.params = params.copy()
        result.losses.append(float(loss))
        result.grad_norms.append(grad_norm)
        result.projection_events.extend(events)
        result.iterations_completed = iteration

        if iteration % checkpoint_every == 0 or iteration == total_iterations:
            save_checkpoint(state_dir, version_digest, _checkpoint_payload(result))

        if check_convergence(
            result.losses, result.grad_norms,
            loss_rel_tol, grad_norm_tol, window, loss_abs_tol,
        ):
            result.converged = True
            break

        # Handled termination signals take effect only at iteration boundaries.
        if signal_flag is not None and signal_flag.get("requested"):
            signal_name = signal_flag["requested"]
            write_interrupt_marker(state_dir, signal_name, iteration)
            result.interrupted = True
            result.interrupt_signal = signal_name
            break

    return result


def _checkpoint_payload(result: FitResult) -> Dict[str, Any]:
    return {
        "iteration": result.iterations_completed,
        "params": {
            name: float(value)
            for name, value in zip(PARAMETER_NAMES, result.params)
        },
        "losses": list(result.losses),
        "grad_norms": list(result.grad_norms),
        "projection_events": list(result.projection_events),
    }


def result_from_payload(payload: Dict[str, Any]) -> FitResult:
    """Rebuild an in-memory fit result from a validated checkpoint payload."""
    result = FitResult(
        params=np.array(
            [float(payload["params"][name]) for name in PARAMETER_NAMES],
            dtype=np.float64,
        )
    )
    result.losses = [float(x) for x in payload["losses"]]
    result.grad_norms = [float(x) for x in payload["grad_norms"]]
    result.projection_events = list(payload.get("projection_events", []))
    result.iterations_completed = int(payload["iteration"])
    result.start_iteration = result.iterations_completed
    return result


def require_converged(result: FitResult, config: Dict[str, Any]) -> None:
    """Raise :class:`ConvergenceError` with the final diagnostic tail."""
    if result.converged or result.interrupted:
        return
    tail_size = int(config["optimization"]["tail_size"])
    tail = min(tail_size, len(result.losses))
    raise ConvergenceError(
        "optimizer reached the iteration budget without satisfying both "
        "convergence thresholds",
        {
            "iterations_completed": result.iterations_completed,
            "tail_losses": result.losses[-tail:],
            "tail_grad_norms": result.grad_norms[-tail:],
            "projection_counts": {
                name: result.projection_count(name) for name in PARAMETER_NAMES
            },
        },
    )
