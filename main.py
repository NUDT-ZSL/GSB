#!/usr/bin/env python3
"""Command-line entry point for the underwater teaching-footage pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from underwater_pipeline.config import DEFAULT_CONFIG
from underwater_pipeline.errors import PipelineError
from underwater_pipeline.generator import generate_sample
from underwater_pipeline.pipeline import analyze_frames, canonical_hash
from underwater_pipeline.report import (
    write_json,
    write_result_bundle,
    write_sample_file,
)


def _core_sections(result: dict) -> dict[str, Any]:
    return {
        "frame_metrics": result["frame_metrics"],
        "boundaries": result["boundaries"],
        "visibility": result["visibility"],
        "teaching_segments": result["teaching_segments"],
        "turbidity_intervals": result["turbidity_intervals"],
        "turbidity_change_points": result["turbidity_change_points"],
        "events": result["events"],
        "detector_collisions": result["detector_collisions"],
    }


def _reproducibility_check(
    seed: int,
    n_frames: int,
) -> tuple[Any, dict, dict]:
    first = generate_sample(seed, n_frames=n_frames)
    first_result = analyze_frames(
        first.frames,
        first.timestamps,
        config=DEFAULT_CONFIG,
        source="deterministic_generator",
        seed=seed,
        truth=first.truth,
    )
    second = generate_sample(seed, n_frames=n_frames)
    second_result = analyze_frames(
        second.frames,
        second.timestamps,
        config=DEFAULT_CONFIG,
        source="deterministic_generator",
        seed=seed,
        truth=second.truth,
    )
    checks = {
        "frames_identical": bool(np.array_equal(first.frames, second.frames)),
        "timestamps_identical": bool(
            np.array_equal(first.timestamps, second.timestamps)
        ),
        "boundaries_identical": first_result["boundaries"]
        == second_result["boundaries"],
        "visibility_identical": first_result["visibility"]
        == second_result["visibility"],
        "segments_identical": first_result["teaching_segments"]
        == second_result["teaching_segments"],
        "turbidity_events_identical": first_result["events"]
        == second_result["events"],
        "detector_collisions_identical": first_result[
            "detector_collisions"
        ]
        == second_result["detector_collisions"],
        "frame_metrics_identical": first_result["frame_metrics"]
        == second_result["frame_metrics"],
        "content_hash_identical": first_result["content_hash"]
        == second_result["content_hash"],
    }
    replay = {
        "seed": seed,
        "checks": checks,
        "all_reproducible": all(checks.values()),
        "content_hash": first_result["content_hash"],
        "replay_content_hash": second_result["content_hash"],
        "first_core_hash": canonical_hash(_core_sections(first_result)),
        "second_core_hash": canonical_hash(_core_sections(second_result)),
    }
    return first, first_result, replay


def _load_npz(path: Path) -> tuple[np.ndarray, np.ndarray, dict | None]:
    try:
        with np.load(path, allow_pickle=False) as archive:
            if "frames" not in archive:
                raise PipelineError(
                    "E_INVALID_INPUT",
                    "输入 NPZ 缺少 frames 字段。",
                    {"path": str(path)},
                )
            if "timestamps" not in archive:
                raise PipelineError(
                    "E_INVALID_INPUT",
                    "输入 NPZ 缺少 timestamps 字段。",
                    {"path": str(path)},
                )
            frames = archive["frames"]
            timestamps = archive["timestamps"]
            truth = None
            if "truth_json" in archive:
                raw_truth = archive["truth_json"].item()
                if isinstance(raw_truth, bytes):
                    raw_truth = raw_truth.decode("utf-8")
                truth = json.loads(str(raw_truth))
            return frames, timestamps, truth
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(
            "E_INVALID_INPUT",
            "无法读取输入 NPZ。",
            {"path": str(path), "reason": str(exc)},
        ) from exc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "从确定性的水下缩略帧序列检测镜头、评估能见度、"
            "生成教学段落并输出可复核报告。"
        )
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20240517,
        help="主样本的固定随机种子（默认 20240517）。",
    )
    parser.add_argument(
        "--compare-seed",
        type=int,
        default=20240518,
        help="用于不同浑浊分布验证的第二个固定种子（默认 20240518）。",
    )
    parser.add_argument(
        "--n-frames",
        type=int,
        default=480,
        help="生成样本帧数（默认 480）。",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("outputs"),
        help="输出根目录（默认 outputs）。",
    )
    parser.add_argument(
        "--input-npz",
        type=Path,
        default=None,
        help=(
            "改为分析已有 NPZ（必须含 frames、timestamps；"
            "可选 truth_json）。"
        ),
    )
    parser.add_argument(
        "--skip-replay",
        action="store_true",
        help="跳过同种子二次生成复现校验。",
    )
    return parser.parse_args(argv)


def _write_error(out_dir: Path, error: PipelineError) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        write_json(out_dir / "error.json", error.to_dict())
    except Exception:
        pass


def _run_input(
    input_path: Path,
    out_dir: Path,
) -> dict:
    frames, timestamps, truth = _load_npz(input_path)
    result = analyze_frames(
        frames,
        timestamps,
        config=DEFAULT_CONFIG,
        source=f"npz:{input_path}",
        seed=None,
        truth=truth,
    )
    directory = out_dir / "input"
    artifacts = write_result_bundle(directory, result)
    write_sample_file(directory, frames, timestamps, truth or {})
    return {
        "ok": True,
        "mode": "input_npz",
        "input": str(input_path),
        "content_hash": result["content_hash"],
        "artifacts": artifacts,
    }


def _run_generated(
    seed: int,
    compare_seed: int,
    n_frames: int,
    out_dir: Path,
    skip_replay: bool,
) -> dict:
    runs: list[dict] = []
    all_results: list[dict] = []
    reproducibility: list[dict] = []

    for current_seed in (seed, compare_seed):
        if skip_replay:
            sample = generate_sample(current_seed, n_frames=n_frames)
            result = analyze_frames(
                sample.frames,
                sample.timestamps,
                config=DEFAULT_CONFIG,
                source="deterministic_generator",
                seed=current_seed,
                truth=sample.truth,
            )
            replay = {
                "seed": current_seed,
                "skipped": True,
                "all_reproducible": None,
                "content_hash": result["content_hash"],
            }
        else:
            sample, result, replay = _reproducibility_check(
                current_seed, n_frames
            )
        directory = out_dir / f"seed_{current_seed}"
        artifacts = write_result_bundle(directory, result)
        sample_path = write_sample_file(
            directory, sample.frames, sample.timestamps, sample.truth
        )
        artifacts["sample"] = str(sample_path)
        run = {
            "seed": current_seed,
            "content_hash": result["content_hash"],
            "truth_comparison": result["truth_comparison"],
            "consistency_all_passed": result["consistency"]["all_passed"],
            "artifacts": artifacts,
        }
        runs.append(run)
        all_results.append(result)
        reproducibility.append(replay)

    structure_signature = {
        "analysis_keys": sorted(all_results[0].keys()),
        "shot_score_keys": sorted(
            all_results[0]["visibility"]["shots"][0].keys()
        ),
        "segment_keys": sorted(
            all_results[0]["teaching_segments"][0].keys()
        ),
        "event_keys": sorted(all_results[0]["events"][0].keys()),
    }
    other_signature = {
        "analysis_keys": sorted(all_results[1].keys()),
        "shot_score_keys": sorted(
            all_results[1]["visibility"]["shots"][0].keys()
        ),
        "segment_keys": sorted(
            all_results[1]["teaching_segments"][0].keys()
        ),
        "event_keys": sorted(all_results[1]["events"][0].keys()),
    }
    comparison = {
        "main_seed": seed,
        "compare_seed": compare_seed,
        "same_structure": structure_signature == other_signature,
        "both_consistency_passed": all(
            result["consistency"]["all_passed"] for result in all_results
        ),
        "results_differ": (
            all_results[0]["content_hash"]
            != all_results[1]["content_hash"]
        ),
        "main_boundary_count": all_results[0]["boundaries"]["summary"][
            "stable_count"
        ],
        "compare_boundary_count": all_results[1]["boundaries"]["summary"][
            "stable_count"
        ],
        "main_segment_count": len(
            all_results[0]["teaching_segments"]
        ),
        "compare_segment_count": len(
            all_results[1]["teaching_segments"]
        ),
        "main_turbidity_intervals": len(
            all_results[0]["turbidity_intervals"]
        ),
        "compare_turbidity_intervals": len(
            all_results[1]["turbidity_intervals"]
        ),
    }
    return {
        "ok": True,
        "mode": "generated",
        "runs": runs,
        "reproducibility": reproducibility,
        "seed_comparison": comparison,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.input_npz is not None:
            summary = _run_input(args.input_npz, args.out_dir)
        else:
            summary = _run_generated(
                args.seed,
                args.compare_seed,
                args.n_frames,
                args.out_dir,
                args.skip_replay,
            )
        write_json(args.out_dir / "run_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        reproducible = [
            item.get("all_reproducible")
            for item in summary.get("reproducibility", [])
            if item.get("all_reproducible") is not None
        ]
        if reproducible and not all(reproducible):
            return 30
        return 0
    except PipelineError as error:
        _write_error(args.out_dir, error)
        print(
            json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return error.exit_code
    except (OSError, ValueError, TypeError) as error:
        wrapped = PipelineError(
            "E_INVALID_INPUT",
            str(error),
            {"exception_type": type(error).__name__},
        )
        _write_error(args.out_dir, wrapped)
        print(
            json.dumps(wrapped.to_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return wrapped.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
