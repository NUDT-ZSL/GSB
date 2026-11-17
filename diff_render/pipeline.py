"""End-to-end orchestration: validate, render, fit, checkpoint and report."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import numpy as np

from .digest import compute_digest
from .errors import ConvergenceError, DigestError
from .optimizer import (
    fit,
    gradient_consistency,
    require_converged,
    result_from_payload,
)
from .report import build_report, build_result, write_outputs
from .scheduler import evaluate_tiled
from .scene import build_geometry
from .state import consume_interrupt_marker, load_latest_valid
from .target import initial_params, load_or_generate_target


def run_pipeline(
    config: Dict[str, Any],
    paths: Dict[str, str],
    *,
    parallel: bool = True,
    force_restart: bool = False,
    strict_digest: bool = False,
    signal_flag: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Execute the full pipeline and return the report dictionary."""
    geometry = build_geometry(config)
    target = load_or_generate_target(
        config, geometry, paths.get("target_file")
    )
    version_digest = compute_digest(config, target)
    state_dir = paths["state_dir"]
    output_dir = paths["output_dir"]

    resumed = None
    resume_diagnostics = []
    interrupt_marker = None
    if not force_restart:
        try:
            payload, resume_diagnostics = load_latest_valid(state_dir, version_digest)
            if payload is not None:
                resumed = result_from_payload(payload)
        except DigestError as exc:
            if strict_digest:
                raise
            # No state matches the current version.  Archive handling keeps the
            # old files; this run starts fresh rather than dead-locking.
            resume_diagnostics = exc.details.get("diagnostics", [])
            resume_diagnostics.append({"slot": "pipeline", "status": "fresh_start_after_digest_failure"})
        interrupt_marker = consume_interrupt_marker(state_dir)

    initial = initial_params(config)
    from .schema import learning_rates
    rates = learning_rates(config)

    def evaluate(params: np.ndarray):
        loss, gradient, _ = evaluate_tiled(
            config, geometry, params, target, parallel=parallel
        )
        return loss, gradient

    result = fit(
        config,
        geometry,
        target,
        version_digest,
        state_dir,
        evaluate,
        initial,
        rates,
        resumed=resumed,
        signal_flag=signal_flag,
    )

    # Final gradient consistency check at the fitted parameters (analytic vs FD).
    _, analytic_gradient, _ = _evaluate_full(
        config, geometry, result.params, target, parallel
    )
    grad_check = gradient_consistency(
        result.params, geometry, target, analytic_gradient, config
    )

    _, _, scheduler_stats = evaluate_tiled(
        config, geometry, result.params, target, parallel=parallel
    )
    scheduler_stats["resume_diagnostics"] = resume_diagnostics
    if interrupt_marker:
        scheduler_stats["resumed_from_interrupt"] = interrupt_marker

    report = build_report(
        config, result, target, grad_check, scheduler_stats, version_digest
    )
    fit_result = build_result(config, result, version_digest)
    expected_paths = {
        "report_path": os.path.join(output_dir, "report.json"),
        "result_path": os.path.join(output_dir, "fit_result.json"),
    }
    report["write_info"] = expected_paths
    write_info = write_outputs(output_dir, report, fit_result)
    report["write_info"] = write_info

    if result.interrupted:
        # Handled signal: report is still written for the completed iterations,
        # but convergence requirements are intentionally not enforced.
        report["exit_code"] = 143
        return report

    try:
        require_converged(result, config)
    except ConvergenceError:
        # Persist the diagnostic report even on convergence failure.
        report["converged"] = False
        write_outputs(output_dir, report, fit_result)
        raise
    report["exit_code"] = 0
    return report


def _evaluate_full(config, geometry, params, target, parallel):
    loss, gradient, stats = evaluate_tiled(
        config, geometry, params, target, parallel=parallel
    )
    return loss, gradient, stats
