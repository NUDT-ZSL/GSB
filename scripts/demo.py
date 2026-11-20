"""End-to-end demonstration entry point.

Scenarios:
  full       one uninterrupted training run with JSON report
  recovery   forced termination after an epoch, then restart and resume,
             followed by a byte-level comparison against the full run
  invalid    inject an illegal configuration and show the business error code

All artefacts land under ``reports/`` and ``snapshots/``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil

import numpy as np

from seraph_core.config.config import EngineConfig
from seraph_core.diag.app import DiagnosticApp, write_report, _json_default
from seraph_core.errors import EngineError


def load_config(path: str) -> EngineConfig:
    with open(path, "r", encoding="utf-8") as handle:
        return EngineConfig.from_dict(json.load(handle))


def scenario_full(config: EngineConfig, args) -> dict:
    snapshot_dir = os.path.join(args.snapshot_root, "full")
    shutil.rmtree(snapshot_dir, ignore_errors=True)
    app = DiagnosticApp(config, args.data, snapshot_dir)
    result = app.run_full()
    report = build_report("full", config, result, app)
    write_report(os.path.join(args.report_dir, "full_run_report.json"),
                 report)
    print(app.render_status_text())
    return result


def scenario_recovery(config: EngineConfig, args, full_result: dict) -> None:
    snapshot_dir = os.path.join(args.snapshot_root, "recovery")
    shutil.rmtree(snapshot_dir, ignore_errors=True)
    app = DiagnosticApp(config, args.data, snapshot_dir)
    interrupted = app.run_interrupted(args.kill_epoch)
    resumed = app.run_resumed()

    full_weights = full_result["training"]["weights"]
    resumed_weights = resumed["training"]["weights"]
    weight_match = bool(np.array_equal(full_weights, resumed_weights))
    full_losses = [row["loss"] for row in
                   full_result["training"]["history"]]
    resumed_losses = [row["loss"] for row in
                      resumed["training"]["history"]]
    loss_match = full_losses == resumed_losses
    report = {
        "scenario": "recovery",
        "interruption": interrupted,
        "resume_events": resumed["events"],
        "weights_byte_identical": weight_match,
        "loss_trajectory_identical": loss_match,
        "full_loss_trajectory": full_losses,
        "resumed_loss_trajectory": resumed_losses,
        "protected_epochs": resumed["training"]["protected_epochs"],
        "skipped_epochs": resumed["training"]["skipped_epochs"],
        "diagnostics": resumed["diagnostics"],
    }
    write_report(os.path.join(args.report_dir, "recovery_report.json"),
                 report)
    print(f"recovery byte-identical weights: {weight_match}; "
          f"loss trajectories identical: {loss_match}")


def scenario_invalid(args) -> None:
    with open(args.config, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["sample_rate"] = 8000
    captured = None
    try:
        EngineConfig.from_dict(raw)
    except EngineError as exc:
        captured = exc.to_dict()
    report = {"scenario": "invalid_config", "expected_code": "E_CONFIG_INVALID",
              "error": captured}
    write_report(os.path.join(args.report_dir,
                              "invalid_config_report.json"), report)
    print(json.dumps(report, indent=2))


def build_report(scenario: str, config: EngineConfig, result: dict,
                 app: DiagnosticApp) -> dict:
    diagnostics = result["diagnostics"]
    return {
        "scenario": scenario,
        "config_fingerprint": app.runner.store.config_checksum,
        "stage_status": app.current_status()["stages"],
        "training": {
            "chunk_size": result["training"]["chunk_size"],
            "plan": [list(span) for span in result["training"]["plan"]],
            "converged_epoch": result["training"]["converged_epoch"],
            "loss_history": result["training"]["history"],
            "protected_epochs": result["training"]["protected_epochs"],
            "skipped_epochs": result["training"]["skipped_epochs"],
            "degradations": result["training"]["degradations"],
        },
        "dialogue_scores": result["summary"]["dialogue_scores"],
        "chunk_vs_whole": diagnostics["chunk_vs_whole"],
        "error_spectrum": diagnostics["error_spectrum"],
        "self_check": diagnostics["self_check"],
        "events": result["events"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario",
                        choices=["full", "recovery", "invalid", "all"],
                        default="all")
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--data", default="data/corpus")
    parser.add_argument("--snapshot-root", default="snapshots")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--kill-epoch", type=int, default=3)
    args = parser.parse_args()
    os.makedirs(args.report_dir, exist_ok=True)

    full_result = None
    if args.scenario in ("full", "recovery", "all"):
        config = load_config(args.config)
        full_result = scenario_full(config, args)
    if args.scenario in ("recovery", "all"):
        config = load_config(args.config)
        scenario_recovery(config, args, full_result)
    if args.scenario in ("invalid", "all"):
        scenario_invalid(args)


if __name__ == "__main__":
    main()
