"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import Planner
from .models import validation_report
from .sample import generate_project
from .validation import parse_project


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"非标准 JSON 数值: {value}")


def load_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text, parse_constant=_reject_json_constant)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exchange-storage-planner",
        description=(
            "区域医疗信息交换对象存储放置、容量水位、事件演化、"
            "增量重定位与引用对账规划器"
        ),
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="交换存储工程清单 JSON 路径；省略时使用固定种子生成样本",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20261128,
        help="确定性样本/报告种子，默认 20261128",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports"),
        help="报告输出目录，默认 reports",
    )
    parser.add_argument(
        "--generate-sample",
        action="store_true",
        help="按 --seed 生成 samples/sample_project_seed_<seed>.json（可同时运行）",
    )
    parser.add_argument(
        "--sample-only",
        action="store_true",
        help="只生成样本，不运行规划",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="将完整 JSON 报告同时打印到标准输出",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="不打印最终摘要",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    sample_path = Path("samples") / f"sample_project_seed_{args.seed}.json"
    if args.sample_only or args.generate_sample or args.project is None:
        sample = generate_project(args.seed)
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(sample_path, sample)

    if args.sample_only:
        if not args.quiet:
            print(str(sample_path))
        return 0

    project_path = args.project if args.project is not None else sample_path
    try:
        raw = load_json(project_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        error = {
            "validation": {
                "valid": False,
                "error_count": 1,
                "errors": [
                    {
                        "code": "PROJECT_LOAD_ERROR",
                        "message": str(exc),
                        "path": str(project_path),
                    }
                ],
            },
            "metadata": {"seed": args.seed, "project_path": str(project_path)},
            "processing": "rejected",
        }
        error_path = output_dir / "validation_errors.json"
        _clear_normal_reports(output_dir)
        write_json(error_path, error)
        if args.print_json:
            print(json.dumps(error, ensure_ascii=False, indent=2))
        elif not args.quiet:
            print(
                f"工程清单读取失败: {exc}; 错误清单: {error_path}",
                file=sys.stderr,
            )
        return 2

    project, errors = parse_project(raw)
    if project is None or errors:
        report = {
            "metadata": {
                "seed": args.seed,
                "project_path": str(project_path),
            },
            "validation": validation_report(errors),
            "processing": "rejected",
        }
        error_path = output_dir / "validation_errors.json"
        _clear_normal_reports(output_dir)
        write_json(error_path, report)
        if args.print_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif not args.quiet:
            print(
                f"工程清单不合法，发现 {len(errors)} 项错误；"
                f"错误清单: {error_path}",
                file=sys.stderr,
            )
        return 2

    report = Planner(project, seed=args.seed).run()
    report["metadata"]["project_path"] = str(project_path)
    report["metadata"]["sample_path"] = (
        str(sample_path) if args.generate_sample or args.project is None else None
    )
    report_path = output_dir / "report.json"
    markdown_path = output_dir / "report.md"
    (output_dir / "validation_errors.json").unlink(missing_ok=True)
    write_json(report_path, report)
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif not args.quiet:
        _print_summary(report, report_path, markdown_path, project_path)
    return 0


def _clear_normal_reports(output_dir: Path) -> None:
    (output_dir / "report.json").unlink(missing_ok=True)
    (output_dir / "report.md").unlink(missing_ok=True)


def _print_summary(
    report: dict[str, Any],
    report_path: Path,
    markdown_path: Path,
    project_path: Path,
) -> None:
    capacity = report["capacity_control"]
    progress = report["relocation_progress"]["summary"]
    reconciliation = report["reconciliation"]
    print(f"工程清单: {project_path}")
    print(f"JSON 报告: {report_path}")
    print(f"Markdown 报告: {markdown_path}")
    print(
        "容量水位: "
        f"初始越限区域={capacity['initial_over_limit_region_ids']}, "
        f"最终越限区域={capacity['final_over_limit_region_ids']}"
    )
    print(
        "重定位进度: "
        f"待迁移={progress['pending']}, "
        f"已迁移={progress['migrated']}, "
        f"失败={progress['failed']}"
    )
    print(
        "引用对账: "
        f"{'一致' if reconciliation['ok'] else '不一致'}, "
        f"不一致条目={reconciliation['inconsistency_count']}"
    )
    satisfaction = report["distribution_comparison"][
        "weighted_latency_satisfaction_comparison"
    ]["after_timeline"]
    print(
        "最终加权时延满足率: "
        f"{satisfaction['rate_percent']:.2f}%"
    )


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# 交换存储规划报告: {report['metadata']['project_id']}",
        "",
        f"- 种子: `{report['metadata']['seed']}`",
        f"- 区域数: {report['metadata']['region_count']}",
        f"- 对象数: {report['metadata']['object_count']}",
        "",
        "## 放置得分公式",
        "",
        f"`score = {report['formula']['placement_score']}`",
        "",
        "## 初始指派",
        "",
        "| 对象 | 机构 | 急缓 | 字节 | 区域 | 得分 |",
        "|---|---|---|---:|---|---:|",
    ]
    for row in report["assignment"]["objects"]:
        lines.append(
            f"| {row['object_id']} | {row['institution_id']} | "
            f"{row['urgency']} | {row['bytes']} | "
            f"{row['assigned_region_id'] or '-'} | "
            f"{row['selected_score'] if row['selected_score'] is not None else '-'} |"
        )

    lines.extend(
        [
            "",
            "## 容量水位",
            "",
            f"- 初始越限: "
            f"{report['capacity_control']['initial_over_limit_region_ids']}",
            f"- 最终越限: "
            f"{report['capacity_control']['final_over_limit_region_ids']}",
            f"- 容量修复迁移数: "
            f"{len(report['capacity_control']['repair_moves'])}",
            f"- 局部提升迁移数: "
            f"{len(report['capacity_control']['local_adjustment_moves'])}",
            "",
            "| 区域 | 对象数 | 有效已用 | 阈值上限 | 水位 | 健康 |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in report["capacity_control"]["final_regions"]:
        lines.append(
            f"| {row['region_id']} | {row['object_count']} | "
            f"{row['effective_used_bytes']} | {row['limit_bytes']} | "
            f"{row['watermark']:.4f} | {row['health']} |"
        )

    lines.extend(
        [
            "",
            "## 逐时刻演化",
            "",
            "| 时刻 | 事件数 | 活跃对象 | 越限区域 | 对账 |",
            "|---:|---:|---:|---|---|",
        ]
    )
    for row in report["timeline"]:
        lines.append(
            f"| {row['at']:g} | {len(row['events'])} | "
            f"{row['active_object_count']} | "
            f"{', '.join(row['over_limit_region_ids']) or '-'} | "
            f"{'一致' if row['reconciliation_ok'] else '不一致'} |"
        )

    lines.extend(
        [
            "",
            "## 重定位进度",
            "",
            "| 批次 | 时刻 | 对象 | 源区域 | 目标区域 | 状态 | 原因码 |",
            "|---|---:|---|---|---|---|---|",
        ]
    )
    for entry in report["relocation_progress"]["entries"]:
        lines.append(
            f"| {entry['batch_id']} | {entry['at']:g} | "
            f"{entry['object_id']} | {entry['source_region_id']} | "
            f"{entry['target_region_id'] or '-'} | {entry['status']} | "
            f"{entry['failure_reason_code'] or '-'} |"
        )

    reconciliation = report["reconciliation"]
    lines.extend(
        [
            "",
            "## 对账",
            "",
            f"- 结果: {'一致' if reconciliation['ok'] else '不一致'}",
            f"- 不一致对象数: {reconciliation['inconsistent_object_count']}",
            f"- 不一致条目数: {reconciliation['inconsistency_count']}",
            f"- 首个不一致: "
            f"{reconciliation['first_inconsistency'] or '无'}",
            "",
            "## 放置前后分布",
            "",
            "| 阶段 | 区域 | 对象数 | 承载字节 | 有效水位 | 越限 |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    for phase in (
        "before_placement",
        "preferred_unconstrained",
        "after_capacity_assignment",
        "after_timeline",
    ):
        distribution = report["distribution_comparison"][phase]
        for row in distribution["regions"]:
            lines.append(
                f"| {phase} | {row['region_id']} | {row['object_count']} | "
                f"{row['assigned_object_bytes']} | {row['watermark']:.4f} | "
                f"{'是' if row['over_limit'] else '否'} |"
            )
    comparison = report["distribution_comparison"][
        "weighted_latency_satisfaction_comparison"
    ]
    lines.extend(
        [
            "",
            "## 加权时延满足率",
            "",
            "| 阶段 | 满足权重 | 总权重 | 满足率 |",
            "|---|---:|---:|---:|",
        ]
    )
    for phase, row in comparison.items():
        lines.append(
            f"| {phase} | {row['satisfied_weight']} | "
            f"{row['total_weight']} | {row['rate_percent']:.2f}% |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
