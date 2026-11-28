#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vocal_feedback.config import Config
from vocal_feedback.evaluation import evaluate_sample


def _evaluate_one(
    report_path: Path,
    annotations_path: Path | None,
    config: Config,
    output_path: Path | None,
) -> dict[str, object]:
    with report_path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    if annotations_path is None:
        value = report.get("files", {}).get("annotations")
        if not value:
            raise ValueError(f"{report_path}: no annotation path in report")
        annotations_path = Path(value)
    evaluation = evaluate_sample(report, annotations_path, config)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(evaluation, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    return evaluation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用独立手工标注核对已有分析报告"
    )
    parser.add_argument(
        "--report",
        required=True,
        help="report.json 路径；若为目录则递归查找 report.json",
    )
    parser.add_argument(
        "--annotations",
        help="手工标注 JSON；单报告模式下可省略并从报告中读取",
    )
    parser.add_argument("--config", help="分析配置 JSON")
    parser.add_argument(
        "--output",
        help="评估 JSON 输出路径；批量模式默认为各 report 同目录 evaluation.json",
    )
    args = parser.parse_args(argv)
    config = Config.from_json(args.config)
    report_path = Path(args.report)
    annotation_override = Path(args.annotations) if args.annotations else None

    paths = (
        sorted(report_path.rglob("report.json"))
        if report_path.is_dir()
        else [report_path]
    )
    if not paths:
        print(f"error: no report.json found under {report_path}", file=sys.stderr)
        return 2
    results: list[dict[str, object]] = []
    failures = 0
    for path in paths:
        output_path = (
            Path(args.output)
            if args.output and len(paths) == 1
            else (path.parent / "evaluation.json" if len(paths) > 1 else None)
        )
        try:
            evaluation = _evaluate_one(
                path, annotation_override, config, output_path
            )
            results.append(evaluation)
            passed = bool(evaluation["passed"])
            failures += int(not passed)
            print(
                f"[{'PASS' if passed else 'FAIL'}] {evaluation['sample_id']}: "
                f"pitch={evaluation['frame_metrics']['pitch_hit_rate_reference_voiced'] * 100:.1f}%, "
                f"note_mae={evaluation['note_metrics']['boundary_mae_ms']} ms, "
                f"phoneme_mae={evaluation['phoneme_metrics']['boundary_mae_ms']} ms, "
                f"mapping={evaluation['syllable_mapping']['consistent']}"
            )
        except Exception as exc:
            failures += 1
            print(f"[ERROR] {path}: {exc}", file=sys.stderr)
    if len(paths) > 1:
        summary = {
            "sample_count": len(paths),
            "passed_count": sum(bool(item["passed"]) for item in results),
            "failed_count": failures,
            "samples": results,
        }
        summary_path = (
            Path(args.output)
            if args.output
            else report_path / "evaluation_summary.json"
        )
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(
            f"evaluation: {summary['passed_count']}/{len(paths)} passed; "
            f"summary -> {summary_path}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

