"""Command-line interface for generation, solving, and verification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence

from .boundaries import run_boundary_samples
from .generator import DEFAULT_SEED, generate_workload
from .loader import load_workload
from .reference import run_reference
from .solver import solve_workload


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKLOAD = ROOT / "artifacts" / "workload.json"
DEFAULT_REPORT = ROOT / "artifacts" / "report.json"
DEFAULT_REFERENCE = ROOT / "artifacts" / "reference.json"
DEFAULT_BOUNDARIES = ROOT / "artifacts" / "boundaries.json"
DEFAULT_SAMPLES = ROOT / "samples"


def write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(destination)


def _generate(args: argparse.Namespace) -> int:
    workload = generate_workload(seed=args.seed)
    write_json(args.out, workload)
    event_count = sum(
        len(step["writes"]) for step in workload["timeline"]
    )
    query_count = sum(
        len(step["queries"]) for step in workload["timeline"]
    )
    print(
        f"已生成 {args.out}: seed={args.seed}, "
        f"时刻={len(workload['timeline'])}, 事件={event_count}, "
        f"查询={query_count}"
    )
    return 0


def _solve(args: argparse.Namespace) -> int:
    workload = load_workload(args.workload)
    report = solve_workload(workload)
    write_json(args.out, report)
    summary = report["summary"]
    print(
        f"已求解 {args.workload} -> {args.out}: "
        f"事件={summary['write_count']}, 查询={summary['query_count']}, "
        f"合并={summary['action_counts'].get('compact', 0)}, "
        f"回收字节={summary['reclaimed_bytes_total']}"
    )
    if not summary["all_ok"]:
        verification = report["verification"]
        print(
            "核对失败: "
            f"query={verification['first_query_mismatch']}, "
            f"segment={verification['first_segment_mismatch']}",
            file=sys.stderr,
        )
        return 1
    print("核对通过: 查询顺序、段内容、失效标记与占用账目均一致")
    return 0


def _reference(args: argparse.Namespace) -> int:
    workload = load_workload(args.workload)
    result = run_reference(workload)
    write_json(args.out, result)
    query_count = sum(
        1 for answer in result["answers"] if answer["type"] == "query"
    )
    print(
        f"独立参考已输出 {args.out}: 查询={query_count}, "
        f"最终写入记录={len(result['final_records'])}"
    )
    return 0


def _boundaries(args: argparse.Namespace) -> int:
    report = run_boundary_samples(args.samples)
    write_json(args.out, report)
    print(
        f"边界样本: {report['sample_count']} 个, "
        f"全部通过={report['all_passed']} -> {args.out}"
    )
    if not report["all_passed"]:
        for result in report["results"]:
            if not result["passed"]:
                print(
                    f"失败: {result['file']} ({result['case']})",
                    file=sys.stderr,
                )
                for check in result["checks"]:
                    if not check["passed"]:
                        print(
                            f"  - {check['check']}: "
                            f"actual={check['actual']!r}, "
                            f"expected={check['expected']!r}",
                            file=sys.stderr,
                        )
        return 1
    return 0


def _selfcheck(args: argparse.Namespace) -> int:
    output_dir = Path(args.out_dir)
    workload_path = output_dir / "workload.json"
    report_path = output_dir / "report.json"
    boundaries_path = output_dir / "boundaries.json"
    print(f"[1/3] 生成确定性负载: {workload_path}")
    generate_workload_args = argparse.Namespace(
        seed=args.seed, out=workload_path
    )
    if _generate(generate_workload_args) != 0:
        return 1
    print(f"[2/3] 求解并逐查询核对: {report_path}")
    solve_args = argparse.Namespace(
        workload=workload_path, out=report_path
    )
    solve_status = _solve(solve_args)
    print(f"[3/3] 运行人工边界样本: {boundaries_path}")
    boundary_args = argparse.Namespace(
        samples=DEFAULT_SAMPLES, out=boundaries_path
    )
    boundary_status = _boundaries(boundary_args)
    return 0 if solve_status == 0 and boundary_status == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m qindex",
        description=(
            "冲压-焊装-涂装-总装质量事件分层索引离线求解器"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate", help="生成固定种子的确定性工作负载"
    )
    generate_parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED
    )
    generate_parser.add_argument(
        "--out", default=str(DEFAULT_WORKLOAD)
    )
    generate_parser.set_defaults(handler=_generate)

    solve_parser = subparsers.add_parser(
        "solve", help="求解工作负载并输出报告与独立参考核对"
    )
    solve_parser.add_argument(
        "workload", nargs="?", default=str(DEFAULT_WORKLOAD)
    )
    solve_parser.add_argument(
        "--out", default=str(DEFAULT_REPORT)
    )
    solve_parser.set_defaults(handler=_solve)

    reference_parser = subparsers.add_parser(
        "reference", help="只运行独立归并查询参考实现"
    )
    reference_parser.add_argument(
        "workload", nargs="?", default=str(DEFAULT_WORKLOAD)
    )
    reference_parser.add_argument(
        "--out", default=str(DEFAULT_REFERENCE)
    )
    reference_parser.set_defaults(handler=_reference)

    boundaries_parser = subparsers.add_parser(
        "boundaries", help="运行人工构造的判定边界样本"
    )
    boundaries_parser.add_argument(
        "--samples", default=str(DEFAULT_SAMPLES)
    )
    boundaries_parser.add_argument(
        "--out", default=str(DEFAULT_BOUNDARIES)
    )
    boundaries_parser.set_defaults(handler=_boundaries)

    selfcheck_parser = subparsers.add_parser(
        "selfcheck", help="生成、求解、核对并运行全部边界样本"
    )
    selfcheck_parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED
    )
    selfcheck_parser.add_argument(
        "--out-dir", default=str(ROOT / "artifacts")
    )
    selfcheck_parser.set_defaults(handler=_selfcheck)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))
