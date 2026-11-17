"""Final fit report and atomic result output."""

from __future__ import annotations

import os
from typing import Any, Dict

from .errors import WriteError
from .io_utils import replace_if_different
from .optimizer import PARAMETER_NAMES, FitResult

REPORT_NAME = "report.json"
RESULT_NAME = "fit_result.json"


def build_report(
    config: Dict[str, Any],
    result: FitResult,
    target: np.ndarray,
    gradient_check: Dict[str, Any],
    scheduler_stats: Dict[str, Any],
    version_digest: str,
) -> Dict[str, Any]:
    truth = config["material"]["truth"]
    final_params = {
        name: float(value) for name, value in zip(PARAMETER_NAMES, result.params)
    }
    deviations = {
        name: abs(final_params[name] - float(truth[name])) for name in PARAMETER_NAMES
    }
    tail_size = int(config["optimization"]["tail_size"])
    tail = min(tail_size, len(result.losses))

    return {
        "ok": True,
        "format": "diff-render-report-v1",
        "version_digest": version_digest,
        "converged": bool(result.converged),
        "interrupted": bool(result.interrupted),
        "interrupt_signal": result.interrupt_signal,
        "iterations_completed": result.iterations_completed,
        "iterations_requested": int(config["optimization"]["iterations"]),
        "final_params": final_params,
        "truth": {name: float(truth[name]) for name in PARAMETER_NAMES},
        "param_error": deviations,
        "losses": list(result.losses),
        "grad_norms": list(result.grad_norms),
        "final_loss": result.losses[-1] if result.losses else None,
        "tail_losses": result.losses[-tail:],
        "tail_grad_norms": result.grad_norms[-tail:],
        "projection_counts": {
            name: result.projection_count(name) for name in PARAMETER_NAMES
        },
        "projection_events": result.projection_events,
        "gradient_check": gradient_check,
        "scheduler": scheduler_stats,
        "memory_budget_bytes": int(config["memory"]["buffer_byte_budget"]),
    }


def build_result(
    config: Dict[str, Any],
    result: FitResult,
    version_digest: str,
) -> Dict[str, Any]:
    return {
        "format": "diff-render-result-v1",
        "version_digest": version_digest,
        "final_params": {
            name: float(value) for name, value in zip(PARAMETER_NAMES, result.params)
        },
        "losses": list(result.losses),
        "grad_norms": list(result.grad_norms),
        "iterations_completed": result.iterations_completed,
        "converged": bool(result.converged),
    }


def write_outputs(
    output_dir: str,
    report: Dict[str, Any],
    fit_result: Dict[str, Any],
) -> Dict[str, str]:
    """Atomically write report and fit result; old files survive any failure."""
    try:
        os.makedirs(output_dir, exist_ok=True)
        report_path = os.path.join(output_dir, REPORT_NAME)
        result_path = os.path.join(output_dir, RESULT_NAME)

        from .io_utils import canonical_json_bytes

        replaced_report = replace_if_different(
            report_path, canonical_json_bytes(report)
        )
        replaced_result = replace_if_different(
            result_path, canonical_json_bytes(fit_result)
        )
        return {
            "report_path": report_path,
            "result_path": result_path,
            "report_replaced": str(replaced_report),
            "result_replaced": str(replaced_result),
        }
    except WriteError:
        raise
    except OSError as exc:
        raise WriteError("failed while writing final outputs: %s" % exc,
                         {"output_dir": output_dir})
