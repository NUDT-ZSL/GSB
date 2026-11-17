#!/usr/bin/env python3
"""End-to-end local demonstration without network or external services."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slpguard import (  # noqa: E402
    BatchConfig,
    InjectionSpec,
    ResourceLimits,
    analyze_path,
    inject,
    normalized_results,
    run_batch,
)
from slpguard.samples import generate_samples  # noqa: E402


def main() -> int:
    samples_dir = ROOT / "samples"
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    manifest = generate_samples(samples_dir)

    serial = run_batch(
        samples_dir / "valid_slp.slp",
        config=BatchConfig(
            workers=1,
            checkpoint_enabled=False,
            report_path=reports_dir / "demo_valid_serial.json",
        ),
        workdir=ROOT,
    )
    parallel = run_batch(
        samples_dir / "valid_slp.slp",
        config=BatchConfig(
            workers=8,
            checkpoint_enabled=False,
            report_path=reports_dir / "demo_valid_parallel.json",
        ),
        workdir=ROOT,
    )
    consistent = normalized_results(serial) == normalized_results(parallel)

    base = (samples_dir / "valid_slp.slp").read_bytes()
    injected_path = ROOT / "examples" / "injected_half_close_demo.slp"
    injection = inject(
        base,
        InjectionSpec(target_session=1, half_close_append=True),
        injected_path,
    )
    injected_result = analyze_path(
        injected_path,
        limits=ResourceLimits(max_steps_per_session=1000, max_file_seconds=15.0),
    )
    codes = sorted(
        {
            violation.code.name
            for session in injected_result.sessions
            for violation in session.violations
        }
    )
    injection_ok = (
        codes == ["E_AFTER_CLOSE"]
        and injected_result.truth.all_consistent
    )

    summary = {
        "samples_generated": len(manifest["samples"]),
        "concurrency_consistent": consistent,
        "injection": injection.to_dict(),
        "injection_codes": codes,
        "injection_truth_consistent": injected_result.truth.all_consistent,
        "serial_report": serial["report_path"],
        "parallel_report": parallel["report_path"],
        "injected_sample": str(injected_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if consistent and injection_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
