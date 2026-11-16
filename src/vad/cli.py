"""Command line entry point for the deterministic VAD component."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import VadConfig
from .errors import E_CONFIG_MISMATCH, VadError
from .injection import InjectionManifest
from .runner import _GracefulExit, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic local VAD component")
    parser.add_argument("--input", default="data/input", help="directory of WAV files")
    parser.add_argument("--output", default="data/output", help="report/snapshot directory")
    parser.add_argument("--config", default="configs/vad_config.json")
    parser.add_argument("--manifest", default="configs/injection_manifest.json")
    parser.add_argument("--force-restart", action="store_true",
                        help="ignore any existing snapshot")
    parser.add_argument("--graceful-after", type=int, default=None,
                        help="test hook: take the graceful-exit path after N samples")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    if not input_dir.is_absolute():
        input_dir = root / input_dir
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path

    try:
        config = VadConfig.load(config_path)
        manifest = InjectionManifest.load(manifest_path)
        report = run(input_dir, output_dir, config, manifest,
                     force_restart=args.force_restart,
                     graceful_after=args.graceful_after)
    except _GracefulExit:
        return 0
    except VadError as exc:
        if exc.code == E_CONFIG_MISMATCH:
            print(f"[VAD] {E_CONFIG_MISMATCH}: {exc.message}", file=sys.stderr)
            return 3
        print(f"[VAD] {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[VAD] fatal: {exc}", file=sys.stderr)
        return 1

    failures = report["failures"]
    print(f"[VAD] run_state={report['run_state']} "
          f"samples={len(report['results'])} failures={len(failures)} "
          f"concurrency_match={report['concurrency_comparison'].get('identical')}")
    for failure in failures:
        print(f"  FAIL {failure['sample']}: {failure['code']} {failure['message']}")
    for result in report["results"]:
        if result["status"] == "ok":
            print(f"  OK   {result['sample']}: segments={len(result['segments'])} "
                  f"frames={result['frame_count']} noise_floor="
                  f"{result['noise_floor_log_energy']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
