"""Generate a deterministic large NDJSON source and benchmark adaptive evaluation."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np

from sre_burnrate import AdaptivePolicy, WindowSpec, evaluate_file, load_config


START_EPOCH = 1_767_225_600  # 2026-01-01T00:00:00Z


def generate(path: Path, rows: int, step_minutes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunk_size = 100_000
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for start in range(0, rows, chunk_size):
            stop = min(rows, start + chunk_size)
            indices = np.arange(start, stop, dtype=np.int64)
            timestamps = START_EPOCH + indices * 60
            requests = 100 + (indices * 7) % 21
            errors = np.zeros(stop - start, dtype=np.int64)
            last_step_start = ((rows - 1) // step_minutes) * step_minutes
            burst_start = max(0, last_step_start)
            burst_end = min(rows, last_step_start + min(step_minutes, 10))
            errors[(indices >= burst_start) & (indices < burst_end)] = 20
            lines = [
                f'{{"minute":{int(ts)},"requests":{int(total)},"errors":{int(failed)}}}\n'
                for ts, total, failed in zip(timestamps, requests, errors, strict=True)
            ]
            handle.writelines(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the adaptive SRE burn-rate path")
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--output-dir", default="artifacts/benchmark")
    parser.add_argument("--config", default="examples/config/slo.json")
    parser.add_argument("--step-minutes", type=int, default=60)
    parser.add_argument("--threshold", type=int, default=50_000)
    parser.add_argument("--regenerate", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    data_path = output_dir / f"sli_{args.rows}.ndjson"
    if args.regenerate or not data_path.exists():
        generate(data_path, args.rows, args.step_minutes)

    config = load_config(args.config)
    config = replace(
        config,
        windows=WindowSpec(
            long_minutes=config.windows.long_minutes,
            short_minutes=config.windows.short_minutes,
            step_minutes=args.step_minutes,
        ),
        adaptive=AdaptivePolicy(row_threshold=args.threshold),
    )
    session = evaluate_file(data_path, config, data_version="benchmark-v1")
    result = {
        "rows": session.row_count,
        "file_size_bytes": data_path.stat().st_size,
        "adaptive_path": session.resource.adaptive_path,
        "threshold": session.resource.auto_row_threshold,
        "elapsed_ms": session.resource.elapsed_ms,
        "elapsed_us": session.resource.elapsed_us,
        "rows_per_second": (
            session.row_count * 1_000_000 // max(1, session.resource.elapsed_us)
            if session.resource.elapsed_us
            else 0
        ),
        "peak_memory_estimate_bytes": session.resource.peak_memory_estimate_bytes,
        "peak_memory_observed_bytes": session.resource.peak_memory_observed_bytes,
        "window_count": {
            "long": len(session.long_windows),
            "short": len(session.short_windows),
            "total": session.window_count,
            "steps": len(session.steps),
        },
        "final_alert_state": session.final_alert_state,
        "final_burn_rate": {
            "long": session.steps[-1].long_burn_rate.to_dict() if session.steps else None,
            "short": session.steps[-1].short_burn_rate.to_dict() if session.steps else None,
        },
        "memory_accounting": session.resource.memory_accounting,
        "estimator": session.resource.estimator,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
