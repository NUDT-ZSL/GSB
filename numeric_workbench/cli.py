"""Command-line entry points for the local diagnostic workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import TrainingConfig
from .data import (
    clean_generated_state,
    generate_deterministic_samples,
    prepare_directories,
    write_config,
)
from .defaults import default_config_dict
from .errors import WorkbenchError
from .reporting import record_business_error, resume_job, run_job
from .snapshots import SnapshotStore
from .verification import build_verification_summary
from .workbench import Workbench


def _load_config(path: str | Path) -> TrainingConfig:
    target = Path(path)
    if not target.is_file():
        raise WorkbenchError(
            "E_CONFIG_INVALID",
            "configuration file does not exist",
            {"field": "$", "path": str(target)},
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(
            "E_CONFIG_INVALID",
            "configuration file cannot be decoded as JSON",
            {"field": "$", "reason": str(exc)},
        ) from exc
    return TrainingConfig.from_dict(raw)


def _ensure_default_config(root: Path, path: Path) -> TrainingConfig:
    if not path.is_file():
        config = TrainingConfig.from_dict(default_config_dict())
        write_config(path, config)
        return config
    return _load_config(path)


def _generate(root: Path, config: TrainingConfig, samples: Path, rows: int, seed: int) -> dict[str, Any]:
    return generate_deterministic_samples(
        samples,
        row_count=rows,
        seed=seed,
        numeric_columns=config.numeric_columns,
        categorical_columns=config.categorical_columns,
        label_column=config.label_column,
        positive_label=config.positive_label,
    )


def _print_error(root: Path, error: WorkbenchError) -> int:
    record_business_error(root, error)
    print(json.dumps(error.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 2


def command_prepare(args: argparse.Namespace) -> int:
    root = Path(args.root)
    removed = clean_generated_state(root, include_samples=args.clean_samples)
    paths = prepare_directories(root)
    print(json.dumps({"removed": removed, "paths": paths}, indent=2, sort_keys=True))
    return 0


def command_generate(args: argparse.Namespace) -> int:
    root = Path(args.root)
    prepare_directories(root)
    try:
        config = _load_config(args.config)
        samples = Path(args.samples)
        if not samples.is_absolute():
            samples = root / samples
        manifest = _generate(root, config, samples, args.rows, args.seed)
    except WorkbenchError as error:
        return _print_error(root, error)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    prepare_directories(root)
    try:
        config = _load_config(args.config)
        samples = Path(args.samples)
        if not samples.is_absolute():
            samples = root / samples
        report = run_job(
            root=root,
            config=config,
            samples_path=samples,
            stop_after_rounds=args.stop_after,
            interrupt_round=args.interrupt_round,
            interrupt_after_chunk=args.interrupt_after_chunk,
            verify_recompute=not args.no_verify,
        )
    except WorkbenchError as error:
        return _print_error(root, error)
    print(Workbench(root=root, config=config, samples_path=samples).render_all())
    if report["status"] == "ERROR":
        return 2
    return 0


def command_resume(args: argparse.Namespace) -> int:
    root = Path(args.root)
    prepare_directories(root)
    try:
        config = _load_config(args.config)
        samples = Path(args.samples)
        if not samples.is_absolute():
            samples = root / samples
        report = resume_job(
            root=root,
            config=config,
            samples_path=samples,
            verify_recompute=not args.no_verify,
        )
    except WorkbenchError as error:
        return _print_error(root, error)
    print(Workbench(root=root, config=config, samples_path=samples).render_all())
    if report["status"] == "ERROR":
        return 2
    return 0


def command_demo(args: argparse.Namespace) -> int:
    root = Path(args.root)
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    sample_path = root / "samples" / "samples.jsonl"
    try:
        if args.clean:
            clean_generated_state(root, include_samples=True)
        prepare_directories(root)
        config = _ensure_default_config(root, config_path)
        manifest = _generate(root, config, sample_path, args.rows, args.seed)
        print(
            f"Generated {manifest['row_count']} deterministic rows at {manifest['path']} "
            f"(sha256={manifest['sha256']})"
        )
        report = run_job(
            root=root,
            config=config,
            samples_path=sample_path,
            verify_recompute=not args.no_verify,
        )
    except WorkbenchError as error:
        return _print_error(root, error)
    workbench = Workbench(root=root, config=config, samples_path=sample_path)
    summary_path = root / "artifacts" / "verification-summary.json"
    summary = build_verification_summary(report)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print()
    print(workbench.render_all())
    print()
    print(f"Full JSON report: {report['outputs']['report']}")
    print(f"Verification summary: {summary_path}")
    return 2 if report["status"] == "ERROR" else 0


def command_interactive(args: argparse.Namespace) -> int:
    root = Path(args.root)
    prepare_directories(root)
    try:
        config = _load_config(args.config)
    except WorkbenchError as error:
        return _print_error(root, error)
    samples = Path(args.samples)
    if not samples.is_absolute():
        samples = root / samples
    Workbench(root=root, config=config, samples_path=samples).interactive()
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    root = Path(args.root)
    try:
        state = SnapshotStore(root / "snapshots").load(args.job_id)
    except WorkbenchError as error:
        return _print_error(root, error)
    manifest = SnapshotStore(root / "snapshots").manifest(state)
    manifest["job_id"] = args.job_id
    manifest["load_changes"] = state.get("load_changes", [])
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_config_check(args: argparse.Namespace) -> int:
    root = Path(args.root)
    try:
        config = _load_config(args.config)
    except WorkbenchError as error:
        return _print_error(root, error)
    print(json.dumps({"valid": True, "config": config.to_dict()}, indent=2, sort_keys=True))
    return 0


def command_summary(args: argparse.Namespace) -> int:
    root = Path(args.root)
    report_path = root / "runs" / args.job_id / "report.json"
    if not report_path.is_file():
        return _print_error(
            root,
            WorkbenchError(
                "E_JOB_NOT_FOUND",
                "report does not exist",
                {"job_id": args.job_id, "path": str(report_path)},
            ),
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = build_verification_summary(report)
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="numeric-workbench",
        description="Local numerical consistency diagnostic workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser, *, config: bool = True) -> None:
        sub.add_argument("--root", default=".", help="local workbench root directory")
        if config:
            sub.add_argument("--config", default="configs/default.json")

    prepare = subparsers.add_parser("prepare", help="create/clean local state directories")
    prepare.add_argument("--root", default=".")
    prepare.add_argument("--clean-samples", action="store_true")
    prepare.set_defaults(func=command_prepare)

    generate = subparsers.add_parser("generate", help="generate deterministic JSONL samples")
    add_common(generate)
    generate.add_argument("--samples", default="samples/samples.jsonl")
    generate.add_argument("--rows", type=int, default=240)
    generate.add_argument("--seed", type=int, default=20261120)
    generate.set_defaults(func=command_generate)

    run = subparsers.add_parser("run", help="execute one complete diagnostic job")
    add_common(run)
    run.add_argument("--samples", default="samples/samples.jsonl")
    run.add_argument("--stop-after", type=int, default=None)
    run.add_argument("--interrupt-round", type=int, default=None)
    run.add_argument("--interrupt-after-chunk", type=int, default=None)
    run.add_argument("--no-verify", action="store_true")
    run.set_defaults(func=command_run)

    resume = subparsers.add_parser("resume", help="resume an interrupted job")
    add_common(resume)
    resume.add_argument("--samples", default="samples/samples.jsonl")
    resume.add_argument("--no-verify", action="store_true")
    resume.set_defaults(func=command_resume)

    demo = subparsers.add_parser("demo", help="clean, generate, run and render a local demo")
    add_common(demo)
    demo.add_argument("--clean", action="store_true", default=True)
    demo.add_argument("--rows", type=int, default=240)
    demo.add_argument("--seed", type=int, default=20261120)
    demo.add_argument("--no-verify", action="store_true")
    demo.set_defaults(func=command_demo)

    interactive = subparsers.add_parser("interactive", help="open the local text workbench")
    add_common(interactive)
    interactive.add_argument("--samples", default="samples/samples.jsonl")
    interactive.set_defaults(func=command_interactive)

    snapshot = subparsers.add_parser("snapshot-show", help="load and verify a versioned snapshot")
    snapshot.add_argument("--root", default=".")
    snapshot.add_argument("--job-id", required=True)
    snapshot.set_defaults(func=command_snapshot)

    config_check = subparsers.add_parser("config-check", help="validate configuration fields")
    add_common(config_check)
    config_check.set_defaults(func=command_config_check)

    summary = subparsers.add_parser(
        "summary", help="write a compact verification summary from a full report"
    )
    summary.add_argument("--root", default=".")
    summary.add_argument("--job-id", required=True)
    summary.add_argument("--output", default="artifacts/verification-summary.json")
    summary.set_defaults(func=command_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
