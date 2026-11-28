#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from columnar_eval.acceptance import run_acceptance
from columnar_eval.generator import generate_sample
from columnar_eval.model import BatchBudget, BudgetRejected


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "确定性列式批处理求值验收器：三 schema 归一、三值逻辑、"
            "条件分支隔离、逐节点账目与独立参考核验。"
        )
    )
    parser.add_argument("--seed", type=int, default=20261128, help="固定随机种子")
    parser.add_argument("--rows", type=int, default=257, help="逻辑表行数")
    parser.add_argument("--batch-rows", type=int, default=32, help="计划分批行数")
    parser.add_argument("--max-rows", type=int, default=64, help="单批最大行数")
    parser.add_argument(
        "--max-input-columns", type=int, default=12, help="单批最大输入列宽"
    )
    parser.add_argument(
        "--max-working-columns", type=int, default=64, help="单批最大工作列宽"
    )
    parser.add_argument(
        "--max-cells",
        type=int,
        default=4096,
        help="单批最大单元格数；超出时按 reject 策略拒绝",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("artifacts/acceptance_report.json"),
        help="JSON 验收报告输出路径",
    )
    parser.add_argument(
        "--sample-out",
        type=Path,
        default=Path("artifacts/generated_sample.json"),
        help="可选：把三 schema 原始样本写入 JSON",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="报告省略逐批节点明细，仅保留表达式汇总与最终结果",
    )
    parser.add_argument("--quiet", action="store_true", help="不打印摘要")
    return parser


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        budget = BatchBudget(
            max_rows=args.max_rows,
            max_input_columns=args.max_input_columns,
            max_working_columns=args.max_working_columns,
            max_cells=args.max_cells,
            rejection_policy="reject",
        )
        report = run_acceptance(
            seed=args.seed,
            row_count=args.rows,
            batch_rows=args.batch_rows,
            budget=budget,
            include_run_details=not args.compact,
        )
    except BudgetRejected as error:
        print(json.dumps(error.to_json(), ensure_ascii=False, indent=2))
        return 2
    except ValueError as error:
        print(
            json.dumps(
                {"status": "INVALID_ARGUMENT", "message": str(error)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    _write_json(args.report, report)
    if args.sample_out is not None:
        sample = generate_sample(seed=args.seed, row_count=args.rows)
        _write_json(args.sample_out, sample.to_json())

    if not args.quiet:
        print(f"验收状态: {report['status']}")
        print(
            f"样本: seed={args.seed}, rows={args.rows}; "
            f"schema 版本={','.join(sorted(report['generator']['schemas']))}"
        )
        print(
            f"预算: rows<={args.max_rows}, input_columns<={args.max_input_columns}, "
            f"working_columns<={args.max_working_columns}, max_cells={args.max_cells}"
        )
        ref = report["reference_verification"]
        print(
            "参考核验: "
            + ", ".join(
                f"{version}={result['ok']} ({result['nodes_checked']} nodes)"
                for version, result in ref.items()
            )
        )
        consistency = report["version_consistency"]
        print(
            "三版本一致: "
            f"{consistency['ok']} "
            f"(inputs={consistency['canonical_inputs_identical']}, "
            f"main={consistency['main_results_identical']})"
        )
        print(
            "条件分支隔离: "
            f"{report['conditional_isolation_audit']['ok']}; "
            "超限拒绝: "
            f"{all(item['ok'] for item in report['budget_rejection_audit'].values())}"
        )
        print(f"报告: {args.report.resolve()}")
        if args.sample_out is not None:
            print(f"样本: {args.sample_out.resolve()}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
