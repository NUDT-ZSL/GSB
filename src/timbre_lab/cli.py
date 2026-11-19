"""Command-line examples for local deterministic acceptance runs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import FrameConfig, PipelineConfig, SimilarityConfig
from .deterministic import atomic_write_bytes, canonical_json_text
from .diagnostics import build_fault_injected_inputs, diagnose_dataset, diagnose_records
from .errors import DomainError
from .pipeline import run_pipeline
from .selfcheck import run_selfcheck
from .snapshots import SnapshotManager
from .synthesis import generate_dataset, load_dataset


def _add_generation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workdir", type=Path, default=Path("artifacts/demo"))
    parser.add_argument("--seed", type=int, default=20261120)
    parser.add_argument("--speakers", type=int, default=3)
    parser.add_argument("--utterances-per-speaker", type=int, default=2)
    parser.add_argument("--fixed-scale", type=int, default=1 << 20)
    parser.add_argument(
        "--rounding-mode",
        default="half_even",
        choices=("half_even", "half_away", "toward_zero", "floor", "ceil"),
    )
    parser.add_argument("--feature-scale", type=float, default=0.75)
    parser.add_argument("--threshold", type=float, default=0.70)
    parser.add_argument("--tolerance", type=float, default=1e-6)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument(
        "--compare-strategy",
        default="tolerance",
        choices=("strict", "tolerance"),
    )
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--output", type=Path)


def _add_dataset_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", type=Path, default=Path("artifacts/demo"))
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=20261120)
    parser.add_argument("--speakers", type=int, default=3)
    parser.add_argument("--utterances-per-speaker", type=int, default=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="timbre-lab",
        description="Deterministic timbre-cloning and pronunciation error laboratory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser(
        "demo",
        help="generate a local dataset, run all precision tiers, self-check and publish snapshots",
    )
    _add_generation_arguments(demo)
    demo.add_argument("--snapshot-dir", type=Path)
    demo.add_argument("--inject-errors", action="store_true")

    generate = subparsers.add_parser("generate", help="generate only the local PCM/truth bundle")
    _add_generation_arguments(generate)

    verify = subparsers.add_parser("verify", help="run a dataset and print the full JSON report")
    _add_dataset_arguments(verify)
    verify.add_argument("--inject-errors", action="store_true")
    verify.add_argument("--fixed-scale", type=int, default=1 << 20)
    verify.add_argument(
        "--rounding-mode",
        default="half_even",
        choices=("half_even", "half_away", "toward_zero", "floor", "ceil"),
    )
    verify.add_argument("--feature-scale", type=float, default=0.75)
    verify.add_argument("--threshold", type=float, default=0.70)
    verify.add_argument("--tolerance", type=float, default=1e-6)
    verify.add_argument("--top-k", type=int, default=3)
    verify.add_argument(
        "--compare-strategy",
        default="tolerance",
        choices=("strict", "tolerance"),
    )

    diagnose = subparsers.add_parser(
        "diagnose",
        help="inject input faults and print structured attribution",
    )
    _add_dataset_arguments(diagnose)
    diagnose.add_argument("--unattributed", action="store_true")

    return parser


def _pipeline_config(args: argparse.Namespace) -> PipelineConfig:
    config = PipelineConfig(
        frame=FrameConfig(),
        tiers=("float64", "float32", "fixed"),
        fixed_scale=args.fixed_scale,
        rounding_mode=args.rounding_mode,
        feature_scale=args.feature_scale,
        similarity=SimilarityConfig(
            threshold=args.threshold,
            tolerance=args.tolerance,
            top_k=args.top_k,
            compare_strategy=args.compare_strategy,
        ),
    )
    config.validate()
    return config


def _emit(payload: dict[str, Any], output: Path | None) -> None:
    text = canonical_json_text(payload, indent=2) + "\n"
    if output is not None:
        atomic_write_bytes(output, text.encode("utf-8"))
    sys.stdout.write(text)
    sys.stdout.flush()


def _error_payload(command: str, exc: DomainError) -> dict[str, Any]:
    return {
        "ok": False,
        "command": command,
        "reference_tier": "float64",
        "error": exc.to_dict(),
        "unattributed_exceptions": [],
        "business_codes": [exc.code],
        "degraded": True,
        "degradation_flags": {
            "business_error": True,
            "selfcheck_failed": False,
            "input_faults_injected": False,
            "precision_loss_observed": False,
        },
    }


def _run_demo(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = _pipeline_config(args)
    dataset = generate_dataset(
        args.workdir,
        seed=args.seed,
        num_speakers=args.speakers,
        utterances_per_speaker=args.utterances_per_speaker,
        config=config.frame,
    )
    result = run_pipeline(dataset, config)
    selfcheck = run_selfcheck(result)
    snapshot_dir = args.snapshot_dir or (args.workdir / "snapshots")
    manager = SnapshotManager(snapshot_dir)
    published = result.publish_snapshots(manager)
    reloaded = SnapshotManager(snapshot_dir).reload()
    injection: dict[str, Any] | None = None
    if args.inject_errors:
        entries, audios, truths = build_fault_injected_inputs(
            dataset,
            include_unattributed=True,
        )
        injection = diagnose_records(
            entries,
            audios,
            truths,
            dataset.references,
            config=config.frame,
        )
    report = result.report(include_error_details=not args.compact)
    selfcheck_codes = [selfcheck["business_code"]] if selfcheck["business_code"] else []
    injection_codes = injection["business_codes"] if injection else []
    business_codes = sorted(
        set(report.get("business_codes", [])) | set(selfcheck_codes) | set(injection_codes)
    )
    payload = {
        "ok": bool(report["ok"] and selfcheck["ok"]),
        "command": "demo",
        "reference_tier": "float64",
        "dataset": report["dataset"],
        "tiers": report["tiers"],
        "error_spectrum": report["error_spectrum"],
        "truth_observation": report["truth_observation"],
        "clone_decisions": report["clone_decisions"],
        "self_check": selfcheck,
        "snapshots": {
            "directory": str(snapshot_dir),
            "publish": published,
            "reload": reloaded,
        },
        "injection_diagnostics": injection,
        "business_codes": business_codes,
        "degraded": bool(business_codes or not selfcheck["ok"]),
        "degradation_flags": {
            "business_error": bool(business_codes),
            "selfcheck_failed": not selfcheck["ok"],
            "input_faults_injected": bool(args.inject_errors),
            "precision_loss_observed": any(
                report["error_spectrum"]["tiers"][tier].get("degraded", False)
                for tier in report["error_spectrum"]["tiers"]
            ),
            "unattributed_exception_count": len(
                injection["unattributed_exceptions"] if injection else []
            ),
        },
    }
    return payload, 0


def _run_generate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    dataset = generate_dataset(
        args.workdir,
        seed=args.seed,
        num_speakers=args.speakers,
        utterances_per_speaker=args.utterances_per_speaker,
        config=FrameConfig(),
    )
    payload = {
        "ok": True,
        "command": "generate",
        "dataset": {
            "root": str(dataset.root),
            "seed": dataset.seed,
            "speaker_count": len(dataset.speakers),
            "utterance_count": len(dataset.utterances),
            "reference_count": len(dataset.references),
            "manifest": str(dataset.manifest_path),
            "references": str(dataset.references_path),
        },
        "business_codes": [],
        "degraded": False,
        "degradation_flags": {
            "business_error": False,
            "selfcheck_failed": False,
            "input_faults_injected": False,
            "precision_loss_observed": False,
        },
    }
    return payload, 0


def _run_verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = _pipeline_config(args)
    dataset = load_dataset(args.dataset, config.frame)
    result = run_pipeline(dataset, config)
    selfcheck = run_selfcheck(result)
    report = result.report(include_error_details=not args.compact)
    snapshots: dict[str, Any] | None = None
    if args.snapshot_dir is not None:
        manager = SnapshotManager(args.snapshot_dir)
        published = result.publish_snapshots(manager)
        snapshots = {
            "directory": str(args.snapshot_dir),
            "publish": published,
            "reload": SnapshotManager(args.snapshot_dir).reload(),
        }
    injection: dict[str, Any] | None = None
    if args.inject_errors:
        entries, audios, truths = build_fault_injected_inputs(
            dataset,
            include_unattributed=True,
        )
        injection = diagnose_records(
            entries,
            audios,
            truths,
            dataset.references,
            config=config.frame,
        )
    selfcheck_codes = [selfcheck["business_code"]] if selfcheck["business_code"] else []
    injection_codes = injection["business_codes"] if injection else []
    business_codes = sorted(
        set(report.get("business_codes", [])) | set(selfcheck_codes) | set(injection_codes)
    )
    payload = {
        "ok": bool(report["ok"] and selfcheck["ok"]),
        "command": "verify",
        "reference_tier": "float64",
        "dataset": report["dataset"],
        "tiers": report["tiers"],
        "error_spectrum": report["error_spectrum"],
        "truth_observation": report["truth_observation"],
        "clone_decisions": report["clone_decisions"],
        "self_check": selfcheck,
        "snapshots": snapshots,
        "injection_diagnostics": injection,
        "business_codes": business_codes,
        "degraded": bool(business_codes or not selfcheck["ok"]),
        "degradation_flags": {
            "business_error": bool(business_codes),
            "selfcheck_failed": not selfcheck["ok"],
            "input_faults_injected": bool(args.inject_errors),
            "precision_loss_observed": any(
                report["error_spectrum"]["tiers"][tier].get("degraded", False)
                for tier in report["error_spectrum"]["tiers"]
            ),
            "unattributed_exception_count": len(
                injection["unattributed_exceptions"] if injection else []
            ),
        },
    }
    return payload, 0


def _run_diagnose(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    dataset = load_dataset(args.dataset, FrameConfig())
    clean = diagnose_dataset(dataset)
    entries, audios, truths = build_fault_injected_inputs(
        dataset,
        include_unattributed=args.unattributed,
    )
    injected = diagnose_records(
        entries,
        audios,
        truths,
        dataset.references,
        config=dataset.config,
    )
    payload = {
        "ok": True,
        "command": "diagnose",
        "dataset": {
            "root": str(dataset.root),
            "utterance_count": len(dataset.utterances),
        },
        "clean_diagnostics": clean,
        "injected_diagnostics": injected,
        "business_codes": injected["business_codes"],
        "degraded": injected["degraded"],
        "degradation_flags": {
            "business_error": False,
            "selfcheck_failed": False,
            "input_faults_injected": True,
            "precision_loss_observed": False,
            "unattributed_exception_count": len(injected["unattributed_exceptions"]),
        },
    }
    return payload, 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    try:
        if command == "demo":
            payload, exit_code = _run_demo(args)
        elif command == "generate":
            payload, exit_code = _run_generate(args)
        elif command == "verify":
            payload, exit_code = _run_verify(args)
        elif command == "diagnose":
            payload, exit_code = _run_diagnose(args)
        else:
            parser.error(f"unknown command: {command}")
            return 2
        _emit(payload, getattr(args, "output", None))
        return exit_code
    except DomainError as exc:
        _emit(_error_payload(command, exc), getattr(args, "output", None))
        return 2
    except Exception as exc:
        payload = {
            "ok": False,
            "command": command,
            "reference_tier": "float64",
            "error": {
                "code": None,
                "message": str(exc),
                "stage": "unattributed",
                "utterance_id": None,
                "field": None,
                "details": {"exception_type": type(exc).__name__},
            },
            "unattributed_exceptions": [
                {
                    "stage": "unattributed",
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
            ],
            "business_codes": [],
            "degraded": True,
            "degradation_flags": {
                "business_error": False,
                "selfcheck_failed": False,
                "input_faults_injected": False,
                "precision_loss_observed": False,
            },
        }
        _emit(payload, getattr(args, "output", None))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

