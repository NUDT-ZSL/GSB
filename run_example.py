#!/usr/bin/env python3
"""端到端运行示例：生成三版本数据 -> 完整处理链 -> 跨版本比对 -> 真值核验。

用法：
    python run_example.py [--config config/default.json]
                          [--data-dir artifacts/data]
                          [--output-dir artifacts/output]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from piano_tempo.cross_version import compare_versions
from piano_tempo.generator import generate_dataset
from piano_tempo.injection import inject_pattern
from piano_tempo.io_utils import load_config, write_json
from piano_tempo.normalizer import load_record
from piano_tempo.pipeline import aggregate_coaching, run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="钢琴演奏速度稳定性诊断示例")
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--data-dir", default="artifacts/data")
    parser.add_argument("--output-dir", default="artifacts/output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)

    print("[1/5] 运行确定性生成器 ...")
    files = generate_dataset(
        seed=config["generation"]["seed"],
        n_students=config["generation"]["n_students"],
        pieces_per_student=config["generation"]["pieces_per_student"],
        output_dir=args.data_dir,
    )
    print(f"      生成 {len(files)} 个三版本练习记录文件 -> {args.data_dir}")

    print("[2/5] 对三版本记录运行完整处理链 ...")
    grouped: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    all_runs: list[dict] = []
    for path in sorted(files):
        record = load_record(path, float(config["grid_step_beats"]))
        result = run_pipeline(record, config)
        grouped[(record.student_id, record.piece_id)][
            record.format_version
        ] = result
        all_runs.append(result)

    print("[3/5] 跨版本一致性比对 ...")
    cross_version_reports = {}
    for (student_id, piece_id), versions in grouped.items():
        report = compare_versions(
            versions, tolerance=config["cross_version"]
        )
        cross_version_reports[f"{student_id}_{piece_id}"] = report
        status = "通过" if report["all_within_tolerance"] else "需人工复核"
        print(f"      {student_id}/{piece_id}: {status}")

    print("[4/5] 汇总段落评分与陪练分配建议 ...")
    coaching_plan = aggregate_coaching(all_runs)

    print("[5/5] 注入人工异常片段并验证判定逻辑 ...")
    injection_reports = []
    sample_path = sorted(files)[0]
    base_record = load_record(sample_path, float(config["grid_step_beats"]))
    for injection in config.get("injections", []):
        injected = inject_pattern(base_record, injection)
        injected_run = run_pipeline(injected, config)
        detected = any(
            item["kind"] in ("rhythm_offset", "robust_residual", "tempo_jump")
            and item["start_beat"] <= injection["end_beat"]
            and item["end_beat"] >= injection["start_beat"]
            for item in injected_run["anomalies"]
        )
        injection_reports.append(
            {
                "injection": injection,
                "record": injected_run["record_id"],
                "detected": bool(detected),
                "n_anomalies": len(injected_run["anomalies"]),
                "validation_summary": {
                    "anomaly_hit_rate": injected_run["validation"]["anomaly_hit_rate"],
                },
            }
        )
        print(
            f"      {injection['name']}: "
            f"{'命中' if detected else '未命中'}"
        )

    write_json(Path(args.output_dir) / "pipeline_runs.json", all_runs)
    write_json(
        Path(args.output_dir) / "cross_version_report.json",
        cross_version_reports,
    )
    write_json(
        Path(args.output_dir) / "coaching_plan.json", coaching_plan
    )
    write_json(
        Path(args.output_dir) / "injection_validation.json",
        injection_reports,
    )
    print(f"全部结果已写入 {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
