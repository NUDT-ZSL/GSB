from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .config import migrate_config, read_config
from .errors import WorkbenchError
from .pipeline import PipelineOptions, SimulatedCrash, run_pipeline
from .report import write_diagnostic_report
from .sample import generate_sample
from .util import atomic_write_json, atomic_write_text, canonical_json_text


LOGGER = logging.getLogger("dsn_workbench")


def _json_stdout(value: Any) -> None:
    sys.stdout.write(canonical_json_text(value) + "\n")


def _run_command(args: argparse.Namespace) -> int:
    options = PipelineOptions(
        verify_parallel=not args.no_verify_parallel,
        crash_stage=args.crash_stage,
        crash_after_batches=args.crash_after_batches,
        workers_override=args.workers,
        batch_size_override=args.batch_size,
    )
    try:
        run = run_pipeline(
            args.config,
            args.tasks,
            cache_dir=args.cache_dir,
            state_dir=args.state_dir,
            options=options,
        )
    except SimulatedCrash as exc:
        _json_stdout(
            {
                "ok": False,
                "interrupted": True,
                "stage": exc.stage,
                "batches_completed": exc.batches_completed,
                "state_dir": str(args.state_dir),
            }
        )
        return 3

    output_path = Path(args.output)
    atomic_write_text(
        output_path,
        "".join(
            json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n"
            for output in run.outputs
        ),
    )
    report_path = Path(args.report)
    atomic_write_json(report_path, run.report)
    html_path = None
    if args.html:
        html_path = write_diagnostic_report(
            args.state_dir,
            args.html,
            report=run.report,
            outputs=run.outputs,
            title="Domain Segmentation Diagnostics",
        )
    summary = {
        "ok": True,
        "sentence_count": run.report["sentence_count"],
        "output_sha256": run.report["output_sha256"],
        "parallel_verification": run.report["parallel_verification"],
        "reference_metrics": run.report["reference_metrics"],
        "cache_plan": {
            "affected_sentences": run.report["cache_plan"][
                "affected_sentences"
            ],
            "reuse_sentences": run.report["cache_plan"]["reuse_sentences"],
        },
        "stage_reports": run.report["stage_reports"],
        "output": str(output_path),
        "report": str(report_path),
        "html": str(html_path) if html_path else None,
    }
    _json_stdout(summary)
    return 0


def _generate_sample_command(args: argparse.Namespace) -> int:
    metadata = generate_sample(
        args.output,
        seed=args.seed,
        count=args.count,
    )
    _json_stdout({"ok": True, **metadata})
    return 0


def _migrate_command(args: argparse.Namespace) -> int:
    report = migrate_config(
        args.source,
        args.target,
        args.to_version,
        overwrite=args.overwrite,
    )
    payload = {"ok": True, **report.to_dict()}
    if args.report:
        atomic_write_json(args.report, payload)
    _json_stdout(payload)
    return 0


def _diagnose_command(args: argparse.Namespace) -> int:
    path = write_diagnostic_report(
        args.state_dir,
        args.output,
        title=args.title,
    )
    _json_stdout({"ok": True, "html": str(path)})
    return 0


def _inspect_config_command(args: argparse.Namespace) -> int:
    bundle = read_config(args.source)
    _json_stdout(
        {
            "ok": True,
            "source": bundle.source,
            "format_version": bundle.format_version,
            "effective_hash": bundle.effective_hash,
            "semantic_hashes": bundle.semantic_hashes,
            "dictionary_entries": len(bundle.data["dictionary"]),
            "rules": len(bundle.data["rules"]),
            "generation": bundle.data["generation"],
            "workers": bundle.data["workers"],
            "batch_size": bundle.data["batch_size"],
            "logical_clock_ms": bundle.data["logical_clock_ms"],
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsn-workbench",
        description="Deterministic domain segmentation and POS-tagging workbench",
    )
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser("generate-sample")
    sample.add_argument("--output", required=True)
    sample.add_argument("--seed", type=int, default=20261127)
    sample.add_argument("--count", type=int, default=24)
    sample.set_defaults(func=_generate_sample_command)

    run = subparsers.add_parser("run")
    run.add_argument("--config", required=True)
    run.add_argument("--tasks", required=True)
    run.add_argument("--cache-dir", default=".dsn/cache")
    run.add_argument("--state-dir", default=".dsn/state")
    run.add_argument("--output", default=".dsn/output.jsonl")
    run.add_argument("--report", default=".dsn/run_report.json")
    run.add_argument("--html")
    run.add_argument("--workers", type=int)
    run.add_argument("--batch-size", type=int)
    run.add_argument("--no-verify-parallel", action="store_true")
    run.add_argument("--crash-stage")
    run.add_argument("--crash-after-batches", type=int)
    run.set_defaults(func=_run_command)

    migrate = subparsers.add_parser("migrate")
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--target", required=True)
    migrate.add_argument(
        "--to-version", type=int, choices=(1, 2), required=True
    )
    migrate.add_argument("--report")
    migrate.add_argument("--overwrite", action="store_true")
    migrate.set_defaults(func=_migrate_command)

    diagnose = subparsers.add_parser("diagnose")
    diagnose.add_argument("--state-dir", default=".dsn/state")
    diagnose.add_argument("--output", default=".dsn/diagnostic.html")
    diagnose.add_argument("--title", default="Domain Segmentation Diagnostics")
    diagnose.set_defaults(func=_diagnose_command)

    inspect_config = subparsers.add_parser("inspect-config")
    inspect_config.add_argument("--source", required=True)
    inspect_config.set_defaults(func=_inspect_config_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return int(args.func(args))
    except WorkbenchError as exc:
        sys.stderr.write(canonical_json_text(exc.to_dict()) + "\n")
        return 2
    except FileNotFoundError as exc:
        sys.stderr.write(
            canonical_json_text(
                {
                    "ok": False,
                    "error": {
                        "code": "E_FILE_NOT_FOUND",
                        "message": str(exc),
                        "details": {},
                    },
                }
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

