"""Command-line entry point."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

from .acceptance import build_acceptance
from .analysis import analyze_manifest, load_manifest
from .dual import compare_runs
from .report import build_markdown_report
from .verification import DEFAULT_TOLERANCES_MS


def parse_sample_count(raw: str, sample_rate: int) -> tuple[int, dict]:
    text = str(raw).strip().lower()
    requested = {"raw": str(raw), "sample_rate": sample_rate}
    if text.endswith("ms"):
        seconds = float(text[:-2]) / 1000.0
        exact = seconds * sample_rate
        samples = int(round(exact))
        requested.update({"unit": "ms", "seconds": seconds, "fractional": abs(exact - samples) > 1e-9})
        return max(1, samples), requested
    if text.endswith("s"):
        seconds = float(text[:-1])
        exact = seconds * sample_rate
        samples = int(round(exact))
        requested.update({"unit": "s", "seconds": seconds, "fractional": abs(exact - samples) > 1e-9})
        return max(1, samples), requested
    if "." in text:
        seconds = float(text)
        exact = seconds * sample_rate
        samples = int(round(exact))
        requested.update({"unit": "s", "seconds": seconds, "fractional": abs(exact - samples) > 1e-9})
        return max(1, samples), requested
    samples = int(text)
    requested.update({"unit": "samples", "seconds": samples / float(sample_rate), "fractional": False})
    return samples, requested


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, 8)
    try:
        import numpy as np

        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            number = float(value)
            return round(number, 8) if math.isfinite(number) else None
        if isinstance(value, np.bool_):
            return bool(value)
    except ImportError:
        pass
    return value


FRAME_CSV_FIELDS = [
    "run",
    "recording_id",
    "segment_id",
    "frame_index",
    "start_sample",
    "end_sample",
    "frame_length_samples",
    "complete",
    "peak",
    "peak_dbfs",
    "peak_ratio",
    "plateau_samples",
    "plateau_ratio",
    "near_plateau_samples",
    "near_plateau_fraction",
    "dc_offset",
    "dc_dbfs",
    "frame_rms_dbfs",
    "low_quantile_rms",
    "low_quantile_dbfs",
    "noise_floor_dbfs",
    "noise_floor_updated",
    "diff_energy_peak",
    "diff_energy_median",
    "diff_peak_amplitude",
    "diff_jump_ratio",
    "impulse_flatness",
    "jump_duration_samples",
    "clipping_label",
    "impulse_candidate",
    "truth_noise_floor_dbfs",
    "noise_error_db",
]


def write_frames_csv(analysis: dict, path: Path, run_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FRAME_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for recording in analysis["recordings"]:
            for segment in recording["segments"]:
                for frame in segment["frames"]:
                    row = {
                        "run": run_name,
                        "recording_id": recording["recording_id"],
                        "segment_id": segment["segment_id"],
                        **{key: frame.get(key) for key in FRAME_CSV_FIELDS},
                    }
                    writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "对单声道录音清单执行逐帧劣化评估、真值核对与双帧长稳定性核对。"
            "帧长/帧移可用样本数（512）或秒（0.032s / 32ms）。"
        )
    )
    parser.add_argument("manifest_positional", nargs="?", default=None, help="录音清单 JSON 路径（也可用 --manifest）")
    parser.add_argument("frame_length_positional", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument("hop_positional", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--manifest", dest="manifest_option", default=None, help="生成器产出的录音清单 JSON 路径")
    parser.add_argument("--frame-length", "--frame-len", default=None, help="主评估帧长；缺省用清单 default_frame_length")
    parser.add_argument("--hop", default=None, help="主评估帧移；缺省用清单 default_hop")
    parser.add_argument(
        "--compare-frame-length",
        default=None,
        help="对照评估帧长；缺省用清单 alternate_frame_length",
    )
    parser.add_argument("--compare-hop", default=None, help="对照评估帧移；缺省用清单 alternate_hop")
    parser.add_argument(
        "--json-out", "--output", default="artifacts/assessment.json", help="完整 JSON 输出路径"
    )
    parser.add_argument(
        "--report", default="artifacts/assessment_report.md", help="Markdown 工单报告输出路径"
    )
    parser.add_argument(
        "--frames-dir", default="artifacts", help="帧级 CSV 输出目录；设置 --no-frames-csv 可关闭"
    )
    parser.add_argument("--no-frames-csv", action="store_true", help="不写出帧级 CSV")
    parser.add_argument(
        "--boundary-tolerance-ms",
        type=float,
        default=None,
        help="统一覆盖四类受损区间的段级边界容许偏差（毫秒）",
    )
    parser.add_argument("--clipping-tolerance-ms", type=float, default=None)
    parser.add_argument("--dc-tolerance-ms", type=float, default=None)
    parser.add_argument("--noise-tolerance-ms", type=float, default=None)
    parser.add_argument("--impulse-tolerance-ms", type=float, default=None)
    parser.add_argument("--noise-tolerance-db", type=float, default=6.0)
    parser.add_argument("--noise-mae-limit-db", type=float, default=4.0)
    parser.add_argument("--noise-within-min", type=float, default=0.80)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.manifest_option and args.manifest_positional is not None:
        # Support "--manifest path 512 128" as an ergonomic shorthand.
        if args.frame_length_positional is not None and args.hop_positional is None:
            args.hop_positional = args.frame_length_positional
        args.frame_length_positional = args.manifest_positional
        args.manifest_positional = None

    manifest_arg = args.manifest_option or args.manifest_positional
    if not manifest_arg:
        raise SystemExit("请用 --manifest 或第一个位置参数提供录音清单路径")
    manifest_path = Path(manifest_arg)
    manifest = load_manifest(manifest_path)
    sample_rate = int(manifest.get("sample_rate", 0))
    if not sample_rate:
        raise SystemExit("manifest is missing sample_rate")

    primary_raw = (
        args.frame_length
        or args.frame_length_positional
        or str(manifest.get("default_frame_length", 512))
    )
    primary_hop_raw = args.hop or args.hop_positional or str(manifest.get("default_hop", 128))
    comparison_raw = args.compare_frame_length or str(manifest.get("alternate_frame_length", 1024))
    comparison_hop_raw = args.compare_hop or str(manifest.get("alternate_hop", 256))

    frame_length, frame_request = parse_sample_count(primary_raw, sample_rate)
    hop, hop_request = parse_sample_count(primary_hop_raw, sample_rate)
    compare_frame_length, compare_frame_request = parse_sample_count(comparison_raw, sample_rate)
    compare_hop, compare_hop_request = parse_sample_count(comparison_hop_raw, sample_rate)

    for name, frame_value, hop_value in (
        ("primary", frame_length, hop),
        ("comparison", compare_frame_length, compare_hop),
    ):
        if frame_value < 8:
            raise SystemExit(f"{name}: frame length must be at least 8 samples")
        if hop_value < 1 or hop_value > frame_value:
            raise SystemExit(f"{name}: hop must be between 1 and frame length")

    tolerances = dict(DEFAULT_TOLERANCES_MS)
    if args.boundary_tolerance_ms is not None:
        for key in tolerances:
            tolerances[key] = float(args.boundary_tolerance_ms)
    for key, value in (
        ("clipping", args.clipping_tolerance_ms),
        ("dc_offset", args.dc_tolerance_ms),
        ("noise", args.noise_tolerance_ms),
        ("impulse", args.impulse_tolerance_ms),
    ):
        if value is not None:
            tolerances[key] = float(value)

    common = {
        "noise_tolerance_db": float(args.noise_tolerance_db),
        "noise_mae_limit_db": float(args.noise_mae_limit_db),
        "noise_within_fraction_min": float(args.noise_within_min),
        "tolerances_ms": tolerances,
    }
    primary = analyze_manifest(
        manifest_path,
        frame_length,
        hop,
        parameter_requested={"frame_length": frame_request, "hop": hop_request},
        **common,
    )
    comparison = analyze_manifest(
        manifest_path,
        compare_frame_length,
        compare_hop,
        parameter_requested={"frame_length": compare_frame_request, "hop": compare_hop_request},
        **common,
    )
    dual = compare_runs(primary, comparison, tolerances_ms=tolerances)
    acceptance = build_acceptance(primary, comparison, dual)

    frames_paths = None
    if not args.no_frames_csv:
        frames_dir = Path(args.frames_dir)
        primary_path = frames_dir / "frames_primary.csv"
        comparison_path = frames_dir / "frames_comparison.csv"
        write_frames_csv(primary, primary_path, "primary")
        write_frames_csv(comparison, comparison_path, "comparison")
        frames_paths = {"primary": str(primary_path), "comparison": str(comparison_path)}

    payload = {
        "schema_version": 1,
        "manifest": {
            "path": str(manifest_path),
            "seed": manifest.get("seed"),
            "generator": manifest.get("generator"),
            "sample_rate": sample_rate,
            "total_duration_seconds": manifest.get("total_duration_seconds"),
        },
        "grade_thresholds": manifest.get("grade_thresholds", {}),
        "primary": primary,
        "comparison": comparison,
        "dual_frame_comparison": dual,
        "acceptance": acceptance,
        "frames_csv": frames_paths,
        "io_policy": {
            "network_access": False,
            "inputs": [str(manifest_path)] + [
                str((manifest_path.parent / rec["path"]).resolve()) for rec in manifest["recordings"]
            ],
        },
    }

    json_path = Path(args.json_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(_json_safe(payload), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_markdown_report(payload, manifest_path), encoding="utf-8")

    if not args.quiet:
        print(
            f"评估完成：{primary['summary']['recording_count']} 条录音，"
            f"{primary['summary']['segment_count']} 段，"
            f"主参数 {frame_length}/{hop}，对照 {compare_frame_length}/{compare_hop}"
        )
        print(
            f"真值核验：{primary['summary']['verified_segments']}/"
            f"{primary['summary']['truth_available_segments']} 段一致，"
            f"{primary['summary']['inconsistent_segments']} 段不一致，"
            f"{primary['summary']['unverifiable_segments']} 段无法核对"
        )
        print(
            f"双帧长：{'稳定' if dual['stable'] else '存在需复核差异'} "
            f"({dual['stable_segment_count']}/{dual['segment_count']} 段)"
        )
        print(f"验收：{'通过' if acceptance['pass'] else '未通过'}")
        print(f"JSON：{json_path}")
        print(f"报告：{report_path}")
        if frames_paths:
            print(f"帧级 CSV：{frames_paths['primary']}、{frames_paths['comparison']}")
    return 0 if acceptance["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
