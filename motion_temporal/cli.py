"""Command-line interface for sample generation, pipeline runs and verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, canonical_bytes
from .codes import E_INCONSISTENT, E_INPUT_INVALID, STAGES
from .config import PipelineConfig
from .errors import BusinessError
from .generator import default_specs, generate_manifest, load_manifest
from .injections import INJECTION_CODES, InjectionSpec
from .metrics import recompute_metrics_from_details
from .pipeline import run_batch


def _parse_stop_at(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    parts = value.split(":")
    if len(parts) != 2 or parts[0] not in STAGES:
        raise argparse.ArgumentTypeError("stop-at must be STAGE:FRAME")
    try:
        frame = int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("stop-at frame must be an integer") from exc
    if frame < 0:
        raise argparse.ArgumentTypeError("stop-at frame must be >= 0")
    return {"stage": parts[0], "frame": frame}


def _write_report(path: Path, report: dict[str, Any]) -> None:
    atomic_write_bytes(path, canonical_bytes(report) + b"\n")


def _config_from_args(args: argparse.Namespace) -> PipelineConfig:
    return PipelineConfig(
        window_length=args.window_length,
        decay=args.decay,
        acceleration_threshold=args.acceleration_threshold,
        hit_iou_threshold=args.hit_iou_threshold,
        max_sequence_frames=args.max_sequence_frames,
        max_history_windows=args.max_history_windows,
        max_parallel_clips=args.max_parallel_clips,
        resource_policy=args.resource_policy,
        extrapolation_margin=args.extrapolation_margin,
    )


def command_generate(args: argparse.Namespace) -> int:
    specs = default_specs(frame_count=args.frame_count, frame_interval_ms=args.frame_interval_ms, seed=args.seed)
    summary = generate_manifest(args.out, specs=specs, missing_frames=args.missing_frame)
    print(canonical_bytes(summary).decode("utf-8"))
    return 0


def command_run(args: argparse.Namespace) -> int:
    try:
        _, clips = load_manifest(args.manifest)
        if args.clip:
            selected = set(args.clip)
            clips = [clip for clip in clips if clip.spec.clip_id in selected]
            if not clips:
                raise BusinessError(E_INPUT_INVALID, f"no clips matched {sorted(selected)}")
        injections = [InjectionSpec.from_token(token) for token in args.inject]
        config = _config_from_args(args)
        stop_at = _parse_stop_at(args.stop_at)
        report = run_batch(
            clips,
            args.cache_dir,
            config=config,
            injections=injections,
            stop_at=stop_at,
            delay_ms=args.delay_ms,
            snapshot_dir=args.snapshot_dir,
        )
        _write_report(Path(args.output), report)
        summary = {
            "status": report["status"],
            "business_code": report["business_code"],
            "output": str(Path(args.output)),
            "report_checksum": report.get("report_checksum"),
            "clips": [
                {
                    "clip_id": item["clip_id"],
                    "status": item["status"],
                    "business_code": item["business_code"],
                    "predictions_checksum": item.get("predictions_checksum"),
                    "attribution_count": len(item.get("attribution", [])),
                    "injection_count": len(item.get("injection_records", [])),
                }
                for item in report.get("clip_reports", [])
            ],
        }
        print(canonical_bytes(summary).decode("utf-8"))
        return 0 if report["status"] in {"ok", "paused"} else 2
    except BusinessError as error:
        payload = {"status": "error", "business_code": error.code, "error": error.to_dict()}
        _write_report(Path(args.output), payload)
        print(canonical_bytes(payload).decode("utf-8"))
        return 2
    except argparse.ArgumentTypeError as error:
        print(str(error), file=sys.stderr)
        return 2


def _reports(report: dict[str, Any]) -> list[dict[str, Any]]:
    if "clip_reports" in report:
        return list(report["clip_reports"])
    return [report]


def _verify_one(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("status") != "ok":
        errors.append(f"{report.get('clip_id')}: status is {report.get('status')}")
        return errors
    selfcheck = report.get("selfcheck") or {}
    if not selfcheck.get("ok"):
        errors.append(f"{report.get('clip_id')}: selfcheck failed {selfcheck.get('categories')}")
    metrics = report.get("metrics")
    if metrics is None:
        errors.append(f"{report.get('clip_id')}: metrics missing")
        return errors
    recomputed = recompute_metrics_from_details(metrics.get("details", []))
    for key, value in recomputed.items():
        if abs(float(metrics[key]) - float(value)) > 1e-12:
            errors.append(f"{report.get('clip_id')}: metric {key} not reproducible")
    predictions = canonical_bytes(report.get("frames", []))
    from .canonical import sha256_bytes

    if sha256_bytes(predictions) != report.get("predictions_checksum"):
        errors.append(f"{report.get('clip_id')}: prediction checksum mismatch")
    return errors


def command_verify(args: argparse.Namespace) -> int:
    try:
        candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
        errors: list[str] = []
        for report in _reports(candidate):
            errors.extend(_verify_one(report))
        if args.baseline:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            baseline_by_id = {item["clip_id"]: item for item in _reports(baseline)}
            candidate_by_id = {item["clip_id"]: item for item in _reports(candidate)}
            for clip_id, item in candidate_by_id.items():
                base = baseline_by_id.get(clip_id)
                if base is None:
                    errors.append(f"{clip_id}: missing from baseline")
                    continue
                if base.get("predictions_checksum") != item.get("predictions_checksum"):
                    errors.append(f"{clip_id}: predictions differ from baseline")
        result = {"status": "ok" if not errors else "error", "business_code": "E_OK" if not errors else E_INCONSISTENT, "errors": errors}
        print(canonical_bytes(result).decode("utf-8"))
        return 0 if not errors else 2
    except (OSError, json.JSONDecodeError) as error:
        print(f"verify failed: {error}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m motion_temporal")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="write deterministic local manifest and truth JSONL")
    generate.add_argument("--out", required=True, type=Path)
    generate.add_argument("--frame-count", type=int, default=18)
    generate.add_argument("--frame-interval-ms", type=int, default=100)
    generate.add_argument("--seed", type=int, default=20261121)
    generate.add_argument("--missing-frame", action="append", type=int, default=[])
    generate.set_defaults(func=command_generate)

    run = subparsers.add_parser("run", help="run or resume the four-stage pipeline")
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--cache-dir", required=True, type=Path)
    run.add_argument("--snapshot-dir", type=Path)
    run.add_argument("--clip", action="append", default=[])
    run.add_argument("--inject", action="append", default=[], help="stage:kind:frame[:key=value...]")
    run.add_argument("--stop-at", help="STAGE:FRAME graceful frame-boundary stop")
    run.add_argument("--delay-ms", type=float, default=0.0, help="per-frame delay used by termination demos")
    run.add_argument("--window-length", type=int, default=4)
    run.add_argument("--decay", type=float, default=0.85)
    run.add_argument("--acceleration-threshold", type=float, default=800.0)
    run.add_argument("--hit-iou-threshold", type=float, default=0.35)
    run.add_argument("--max-sequence-frames", type=int, default=512)
    run.add_argument("--max-history-windows", type=int, default=32)
    run.add_argument("--max-parallel-clips", type=int, default=8)
    run.add_argument("--resource-policy", choices=["error", "degrade"], default="error")
    run.add_argument("--extrapolation-margin", type=float, default=96.0)
    run.set_defaults(func=command_run)

    verify = subparsers.add_parser("verify", help="recompute metrics/checksums and optionally compare two reports")
    verify.add_argument("--candidate", required=True, type=Path)
    verify.add_argument("--baseline", type=Path)
    verify.set_defaults(func=command_verify)

    injections = subparsers.add_parser("injections", help="list controlled injection points")
    injections.set_defaults(
        func=lambda _: (
            print(
                canonical_bytes(
                    [
                        {"stage": stage, "kind": kind, "business_code": code}
                        for (stage, kind), code in sorted(INJECTION_CODES.items())
                    ]
                ).decode("utf-8")
            )
            or 0
        )
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

