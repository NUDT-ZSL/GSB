"""Subprocess helper used by the OS-level signal integration test.

Installs the real CLI signal handlers, then optionally parks the process so
the test can deliver SIGTERM while a pipeline run is in progress.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from panseg.cli import _install_signal_handlers
from panseg.models import PipelineConfig
from panseg.pipeline import PanopticPipeline


def _run(argv):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--report", default=None)
    parser.add_argument("--fault-config", default=None)
    args = parser.parse_args(argv)

    config = PipelineConfig.load(args.config)
    pipeline = PanopticPipeline(args.sample_dir, config, args.work_dir)
    _install_signal_handlers(pipeline)

    if os.environ.get("PANSEG_PARK_BEFORE_RUN"):
        deadline = time.time() + 10
        while time.time() < deadline and not pipeline.signaled:
            time.sleep(0.02)

    result = pipeline.run()
    from panseg.cli import EXIT_BUSINESS_FAILURES, EXIT_HANDLED_SIGNAL
    from panseg.report import build_report, write_report

    exit_code = EXIT_HANDLED_SIGNAL if result.signaled else (
        EXIT_BUSINESS_FAILURES if result.failures else 0
    )
    report_path = Path(args.report or (Path(args.work_dir) / "validation_report.json"))
    write_report(report_path, build_report(result, pipeline.current_config_hash, exit_code))
    return exit_code


if __name__ == "__main__":
    sys.exit(_run(sys.argv[1:]))
