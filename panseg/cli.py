"""Runnable entry point with signal handling and integer exit code contract."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from .errors import (
    EXIT_BUSINESS_FAILURES,
    EXIT_FATAL,
    EXIT_HANDLED_SIGNAL,
    EXIT_HARD_CRASH,
    E_HARD_CRASH_SIMULATED,
    PipelineError,
)
from .models import PipelineConfig
from .pipeline import PanopticPipeline, run_conversion_roundtrip
from .report import build_report, write_report


def _install_signal_handlers(pipeline: PanopticPipeline) -> None:
    def handler(signum, _frame):
        pipeline.request_shutdown()

    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            signal.signal(sig, handler)
    sig_break = getattr(signal, "SIGBREAK", None)
    if sig_break is not None:
        signal.signal(sig_break, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic panoptic mask pipeline")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--fault-config")
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)

    config = PipelineConfig.load(args.config)
    if args.fault_config:
        fault_document = json.loads(Path(args.fault_config).read_text(encoding="utf-8"))
        fault_injection = dict(config.fault_injection)
        fault_injection.update(fault_document)
        config = PipelineConfig(**{**config.to_dict(), "fault_injection": fault_injection})

    pipeline = PanopticPipeline(args.sample_dir, config, args.work_dir)
    _install_signal_handlers(pipeline)
    report_path = Path(args.report or (Path(args.work_dir) / "validation_report.json"))
    report_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        upgrade_losses, downgrade_losses, _artifacts = run_conversion_roundtrip(
            Path(args.sample_dir), Path(args.work_dir), config
        )
        result = pipeline.run()
        result.conversion_losses.extend(upgrade_losses)
        result.conversion_losses.extend(downgrade_losses)
    except PipelineError as exc:
        if exc.code == E_HARD_CRASH_SIMULATED:
            # A hard crash never promises an exit report; emit only a minimal
            # crash marker and use the 137 convention.
            marker = Path(args.work_dir) / "hard_crash_marker.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(exc.to_record(), indent=2, sort_keys=True), encoding="utf-8")
            return EXIT_HARD_CRASH
        report = {
            "report_version": 1,
            "fatal": exc.to_record(),
            "exit_code": EXIT_FATAL,
        }
        write_report(report_path, report)
        return EXIT_FATAL

    exit_code = EXIT_HANDLED_SIGNAL if result.signaled else (
        EXIT_BUSINESS_FAILURES if result.failures else 0
    )
    report = build_report(result, pipeline.current_config_hash, exit_code)
    write_report(report_path, report)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
