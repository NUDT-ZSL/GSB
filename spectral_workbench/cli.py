"""End-to-end command-line workflow."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .config import config_fingerprint, load_config
from .embedding import build_token_features
from .formats import load_all_versions, save_normalized_datasets
from .generator import generate_and_write
from .reference import verify_training_records
from .reporting import build_cross_version_report, build_specialization_report
from .training import Trainer, save_model_snapshot
from .utils import append_jsonl, ensure_dir, file_sha256, write_json


def _prepare_output_dir(path: Path, overwrite: bool) -> None:
    if path.exists() and any(path.iterdir()):
        if not overwrite:
            raise FileExistsError(
                f"output directory is not empty: {path}. "
                "Pass --overwrite to replace it."
            )
        if path.resolve() == Path("/"):
            raise ValueError("refusing to overwrite filesystem root")
        shutil.rmtree(path)
    ensure_dir(path)


def _normalization_alignment_report(
    datasets: dict[str, Any], tolerance: float
) -> dict[str, Any]:
    reference = datasets["A"]
    values_a, masks_a = reference.stacked()
    versions: dict[str, Any] = {}
    maximum_difference = 0.0
    masks_equal = True
    for version in ("A", "B", "C"):
        values, masks = datasets[version].stacked()
        difference = float(np.max(np.abs(values - values_a)))
        version_masks_equal = bool(np.array_equal(masks, masks_a))
        maximum_difference = max(maximum_difference, difference)
        masks_equal = masks_equal and version_masks_equal
        versions[version] = {
            "max_abs_token_value_difference_to_A": difference,
            "masks_equal_A": version_masks_equal,
            "values_within_tolerance": bool(difference <= tolerance),
        }
    return {
        "index_mapping": (
            "positional acquisition-segment index; token index 0..N-1 is shared "
            "by all three versions"
        ),
        "token_length": reference.token_length,
        "sample_count": len(reference.samples),
        "maximum_abs_token_value_difference": maximum_difference,
        "all_masks_equal": masks_equal,
        "all_values_within_tolerance": bool(maximum_difference <= tolerance),
        "tolerance": tolerance,
        "versions": versions,
    }


def run_workbench(
    *,
    seed: int,
    config_path: str | Path,
    output_dir: str | Path,
    overwrite: bool = False,
    quiet: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    output_root = Path(output_dir)
    _prepare_output_dir(output_root, overwrite)
    fingerprint = config_fingerprint(config)
    config["_runtime_seed"] = int(seed)

    def progress(message: str) -> None:
        if not quiet:
            print(message, file=sys.stderr)

    progress(f"[1/7] Generating three deterministic format versions in {output_root}")
    generated_root = ensure_dir(output_root / "generated_data")
    generate_and_write(seed, config, generated_root)

    progress("[2/7] Parsing and normalizing A/B/C spectra")
    datasets = load_all_versions(generated_root, config)
    normalization_dir = ensure_dir(output_root / "normalization")
    log_path = normalization_dir / "normalization_log.jsonl"
    log_path.write_text("", encoding="utf-8")
    for version in ("A", "B", "C"):
        for sample in datasets[version].samples:
            append_jsonl(log_path, sample.normalization_log)
    save_normalized_datasets(datasets, normalization_dir)
    alignment_report = _normalization_alignment_report(
        datasets, float(config["evaluation"]["value_tolerance"])
    )
    write_json(normalization_dir / "index_alignment.json", alignment_report)

    progress("[3/7] Training capacity-constrained top-k gated mixture of experts")
    training_dir = ensure_dir(output_root / "training")
    scalar_values, masks = datasets["A"].stacked()
    values = build_token_features(scalar_values, masks, config)
    trainer = Trainer(
        seed=seed,
        config=config,
        tokens=values,
        masks=masks,
        sample_ids=datasets["A"].sample_ids,
        training_output_dir=training_dir,
        token_values=scalar_values,
    )
    training_result = trainer.run()

    progress("[4/7] Exporting downstream fine-tuning snapshot")
    snapshot_dir = ensure_dir(output_root / "snapshot")
    snapshot_path = save_model_snapshot(
        training_result.model,
        output_dir=snapshot_dir,
        seed=seed,
        config_fingerprint=fingerprint,
        config=config,
    )
    snapshot_manifest = {
        "format": "numpy_npz",
        "path": "model_snapshot.npz",
        "seed": int(seed),
        "config_fingerprint": fingerprint,
        "token_dim": int(training_result.model.gate.weight.shape[0]),
        "num_experts": int(config["model"]["num_experts"]),
        "top_k": int(config["model"]["top_k"]),
        "final_temperature": float(training_result.model.temperature),
        "arrays": {
            "gate_weight": "[token_dim, num_experts]",
            "gate_bias": "[num_experts]",
            "expert_weight": "[num_experts, token_dim, token_dim]",
            "expert_bias": "[num_experts, token_dim]",
            "temperature": "scalar",
        },
        "sha256": file_sha256(snapshot_path),
    }
    write_json(snapshot_dir / "snapshot_manifest.json", snapshot_manifest)

    progress("[5/7] Auditing the same batch across A/B/C normalization")
    reports_dir = ensure_dir(output_root / "reports")
    cross_version_report = build_cross_version_report(
        training_result.model,
        datasets,
        config,
        output_dir=reports_dir,
    )

    progress("[6/7] Building material/instrument expert specialization report")
    specialization_report = build_specialization_report(
        training_result.model,
        datasets["A"],
        config,
        output_dir=reports_dir,
    )

    progress("[7/7] Running independent routing reference audit")
    reference_report = verify_training_records(
        training_dir / "step_arrays.npz",
        config,
    )
    write_json(reports_dir / "reference_check.json", reference_report)

    acceptance_checks = {
        "normalization_index_alignment": bool(
            alignment_report["all_masks_equal"]
            and alignment_report["all_values_within_tolerance"]
        ),
        "cross_version_routing_consistency": bool(
            cross_version_report["consistent"]
        ),
        "reference_router_verified": bool(reference_report["verified"]),
        "snapshot_exported": snapshot_path.exists(),
        "adjustment_events_recorded": len(training_result.adjustments),
        "collapse_alerts_recorded": len(training_result.alerts),
        "rare_concentration_warning_count": int(
            specialization_report["rare_concentration_warning_count"]
        ),
    }
    write_json(reports_dir / "acceptance_checks.json", acceptance_checks)
    write_json(output_root / "resolved_config.json", config)

    summary = {
        "seed": int(seed),
        "config_fingerprint": fingerprint,
        "config_path": str(Path(config_path)),
        "output_dir": str(output_root),
        "token_length": int(config["generator"]["token_length"]),
        "sample_count": int(config["generator"]["sample_count"]),
        "steps_completed": int(training_result.steps_completed),
        "final_temperature": float(training_result.model.temperature),
        "adjustment_count": len(training_result.adjustments),
        "alert_count": len(training_result.alerts),
        "reference_verified": bool(reference_report["verified"]),
        "reference_first_mismatch_step": reference_report[
            "first_mismatch_step"
        ],
        "cross_version_consistent": bool(cross_version_report["consistent"]),
        "cross_version_difference_count": int(
            cross_version_report["difference_count"]
        ),
        "rare_concentration_warning_count": int(
            specialization_report["rare_concentration_warning_count"]
        ),
        "artifacts": {
            "generated_data": "generated_data/manifest.json",
            "normalization_log": "normalization/normalization_log.jsonl",
            "index_alignment": "normalization/index_alignment.json",
            "step_records": "training/step_records.jsonl",
            "assignment_records": "training/assignment_records.jsonl",
            "step_arrays": "training/step_arrays.npz",
            "adjustments": "training/adjustments.json",
            "reference_check": "reports/reference_check.json",
            "cross_version_check": "reports/cross_version_check.json",
            "specialization_report": "reports/specialization_report.json",
            "acceptance_checks": "reports/acceptance_checks.json",
            "snapshot": "snapshot/model_snapshot.npz",
        },
        "acceptance_checks": acceptance_checks,
    }
    write_json(output_root / "run_summary.json", summary)
    progress(
        "[done] reference_verified={} cross_version_consistent={} "
        "adjustments={} alerts={}".format(
            summary["reference_verified"],
            summary["cross_version_consistent"],
            summary["adjustment_count"],
            summary["alert_count"],
        )
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline deterministic spectral mixture-of-experts workbench for "
            "Raman/XRD format normalization, routing audits, and snapshots."
        )
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20261128,
        help="deterministic spectrum generator and model initialization seed",
    )
    parser.add_argument(
        "--config",
        default="configs/default.json",
        help="training/generator JSON configuration path",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/default",
        help="directory for generated data, logs, reports, and snapshot",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing non-empty output directory",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress progress messages",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = run_workbench(
            seed=int(args.seed),
            config_path=args.config,
            output_dir=args.output_dir,
            overwrite=bool(args.overwrite),
            quiet=bool(args.quiet),
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not summary["reference_verified"]:
        print(
            "warning: independent reference router found a mismatch; "
            "see reports/reference_check.json",
            file=sys.stderr,
        )
    if not summary["cross_version_consistent"]:
        print(
            "warning: cross-version routing differences were found; "
            "see reports/cross_version_check.json",
            file=sys.stderr,
        )
    return 0
