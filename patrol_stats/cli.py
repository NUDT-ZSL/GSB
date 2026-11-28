from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .advisor import (
    apply_update_plan,
    generate_update_plan,
    list_advisor_suggestions,
)
from .db import connect, initialize_schema
from .evaluation import (
    compare_versions,
    evaluate_workload,
    list_snapshots,
    snapshot,
)
from .generator import generate_dataset
from .statistics import (
    build_statistics,
    statistics_summary,
    validate_summary,
)


DEFAULT_STORAGE = Path("storage")
DEFAULT_DB = DEFAULT_STORAGE / "patrol_stats.db"
DEFAULT_GENERATED = DEFAULT_STORAGE / "generated"
DEFAULT_REPORTS = DEFAULT_STORAGE / "reports"


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _open_db(args: argparse.Namespace):
    conn = connect(args.db)
    initialize_schema(conn)
    return conn


def command_generate(args: argparse.Namespace) -> int:
    manifest = generate_dataset(
        args.db,
        args.out_dir,
        seed=args.seed,
        log_count=args.logs,
        query_count=args.queries,
        overwrite=args.force,
    )
    _print_json(manifest)
    return 0


def command_stats(args: argparse.Namespace) -> int:
    conn = _open_db(args)
    try:
        build_statistics(
            conn,
            args.version,
            sample_size=args.sample_size,
            combo_sample_size=args.combo_sample_size,
            bucket_count=args.buckets,
            combo_source=args.combo_source,
            note=args.note,
            replace=args.replace,
        )
        summary = statistics_summary(conn, args.version)
        _print_json(summary)
    finally:
        conn.close()
    return 0


def command_validate(args: argparse.Namespace) -> int:
    conn = _open_db(args)
    try:
        issues = validate_summary(conn, args.version)
    finally:
        conn.close()
    payload = {
        "version": args.version,
        "valid": not issues,
        "issue_count": len(issues),
        "issues": [issue.as_dict() for issue in issues],
    }
    _print_json(payload)
    return 0 if not issues else 2


def command_evaluate(args: argparse.Namespace) -> int:
    conn = _open_db(args)
    try:
        result = evaluate_workload(
            conn,
            args.version,
            query_ids=args.query,
            note=args.note,
        )
        if args.output:
            _write_json(Path(args.output), snapshot(conn, run_id=result["run_id"]))
    finally:
        conn.close()
    _print_json(result)
    return 0


def command_advise(args: argparse.Namespace) -> int:
    conn = _open_db(args)
    try:
        run_id = args.run_id
        if run_id is None:
            run = conn.execute(
                """
                SELECT run_id FROM evaluation_runs
                WHERE version = ? AND completed_at IS NOT NULL
                ORDER BY run_id DESC LIMIT 1
                """,
                (args.version,),
            ).fetchone()
            if run is None:
                raise LookupError(f"no completed evaluation for {args.version}")
            run_id = int(run["run_id"])
        plan = generate_update_plan(conn, run_id)
        payload: dict[str, Any] = plan
        if args.apply:
            applied = apply_update_plan(
                conn,
                plan["plan_id"],
                target_version=args.target_version,
                sample_size=args.update_sample_size,
            )
            payload = {"plan": plan, "applied": applied}
        if args.output:
            _write_json(Path(args.output), payload)
    finally:
        conn.close()
    _print_json(payload)
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    conn = _open_db(args)
    try:
        payload = snapshot(conn, run_id=args.run_id, version=args.version)
    finally:
        conn.close()
    if args.output:
        _write_json(Path(args.output), payload)
    else:
        _print_json(payload)
    return 0


def command_snapshots(args: argparse.Namespace) -> int:
    conn = _open_db(args)
    try:
        payload = list_snapshots(conn)
    finally:
        conn.close()
    _print_json(payload)
    return 0


def command_compare(args: argparse.Namespace) -> int:
    conn = _open_db(args)
    try:
        payload = compare_versions(
            conn, args.query_id, args.from_version, args.to_version
        )
    finally:
        conn.close()
    if args.output:
        _write_json(Path(args.output), payload)
    else:
        _print_json(payload)
    return 0


def command_suggestions(args: argparse.Namespace) -> int:
    conn = _open_db(args)
    try:
        payload = list_advisor_suggestions(conn, args.run_id)
    finally:
        conn.close()
    _print_json(payload)
    return 0


def command_demo(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    out_dir = Path(args.out_dir)
    reports = Path(args.reports_dir)
    manifest = generate_dataset(
        db_path,
        out_dir,
        seed=args.seed,
        log_count=args.logs,
        query_count=args.queries,
        overwrite=True,
    )
    conn = connect(db_path)
    initialize_schema(conn)
    try:
        initial_version = args.initial_version
        build_statistics(
            conn,
            initial_version,
            sample_size=args.sample_size,
            combo_sample_size=args.combo_sample_size,
            bucket_count=args.buckets,
            combo_source="sample",
            note="初始摘要：全量单列直方图 + 小样本区域频段组合统计",
        )
        validation = {
            "version": initial_version,
            "valid": True,
            "issue_count": 0,
            "issues": [],
        }
        issues = validate_summary(conn, initial_version)
        validation = {
            "version": initial_version,
            "valid": not issues,
            "issue_count": len(issues),
            "issues": [issue.as_dict() for issue in issues],
        }
        if issues:
            raise RuntimeError(f"initial summary is invalid: {validation}")
        initial_eval = evaluate_workload(
            conn,
            initial_version,
            note="初始摘要下的同批查询评估",
        )
        plan = generate_update_plan(conn, initial_eval["run_id"])
        applied = apply_update_plan(
            conn,
            plan["plan_id"],
            sample_size=args.update_sample_size,
        )
        target_version = applied["target_version"]
        comparisons = {
            query["query_id"]: compare_versions(
                conn, query["query_id"], initial_version, target_version
            )
            for query in initial_eval["queries"]
        }
        initial_snapshot = snapshot(conn, run_id=initial_eval["run_id"])
        target_snapshot = snapshot(conn, run_id=applied["target_run_id"])
        _write_json(reports / f"validation_{initial_version}.json", validation)
        _write_json(
            reports / f"evaluation_{initial_version}.json", initial_snapshot
        )
        _write_json(reports / "update_plan.json", plan)
        _write_json(
            reports / f"evaluation_{target_version}.json", target_snapshot
        )
        _write_json(reports / "comparison_versions.json", comparisons)
        summary_payload = {
            "seed": args.seed,
            "manifest": manifest,
            "initial_version": initial_version,
            "target_version": target_version,
            "initial_run_id": initial_eval["run_id"],
            "target_run_id": applied["target_run_id"],
            "initial_mean_error": initial_eval["mean_abs_rel_error"],
            "target_mean_error": applied["mean_after"],
            "improvement_ratio": applied["improvement_ratio"],
            "converged": applied["converged"],
            "validation": validation,
            "suggestions": plan["suggestions"],
            "reports_dir": str(reports),
        }
        _write_json(reports / "demo_summary.json", summary_payload)
    finally:
        conn.close()

    print("林区巡护基数估计演示完成")
    print(f"  数据哈希: {manifest['data_sha256']}")
    print(
        f"  初始版本: {initial_version}  run={initial_eval['run_id']}  "
        f"平均相对偏差={initial_eval['mean_abs_rel_error']:.4f}"
    )
    print(
        f"  更新版本: {target_version}  run={applied['target_run_id']}  "
        f"平均相对偏差={applied['mean_after']:.4f}"
    )
    print(f"  偏差收敛: {'是' if applied['converged'] else '否'}")
    print(f"  报告目录: {reports}")
    return 0 if applied["converged"] else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m patrol_stats",
        description="林区巡护日志基数估计、连接建议与摘要更新内核",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 文件路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="确定性生成巡护数据与查询")
    generate.add_argument("--seed", type=int, default=20261128)
    generate.add_argument("--logs", type=int, default=5000)
    generate.add_argument("--queries", type=int, default=16)
    generate.add_argument(
        "--out-dir", default=str(DEFAULT_GENERATED), help="CSV/manifest 输出目录"
    )
    generate.add_argument("--force", action="store_true", help="覆盖已有数据库")
    generate.set_defaults(func=command_generate)

    stats = subparsers.add_parser("stats", help="构建一个统计摘要版本")
    stats.add_argument("--version", default="v1_initial")
    stats.add_argument("--sample-size", type=int, default=512)
    stats.add_argument("--combo-sample-size", type=int, default=160)
    stats.add_argument("--buckets", type=int, default=8)
    stats.add_argument(
        "--combo-source", choices=("sample", "full"), default="sample"
    )
    stats.add_argument("--note", default="")
    stats.add_argument("--replace", action="store_true")
    stats.set_defaults(func=command_stats)

    validate = subparsers.add_parser("validate", help="校验摘要与基表是否一致")
    validate.add_argument("--version", default="v1_initial")
    validate.set_defaults(func=command_validate)

    evaluate = subparsers.add_parser("evaluate", help="评估查询并写入 SQLite 快照")
    evaluate.add_argument("--version", default="v1_initial")
    evaluate.add_argument("--query", action="append", help="仅评估指定 query_id，可重复")
    evaluate.add_argument("--note", default="")
    evaluate.add_argument("--output", help="额外写出 JSON 快照")
    evaluate.set_defaults(func=command_evaluate)

    advise = subparsers.add_parser("advise", help="生成/应用摘要更新建议")
    advise.add_argument("--run-id", type=int)
    advise.add_argument("--version", default="v1_initial")
    advise.add_argument("--apply", action="store_true")
    advise.add_argument("--target-version")
    advise.add_argument("--update-sample-size", type=int, default=1024)
    advise.add_argument("--output")
    advise.set_defaults(func=command_advise)

    snapshots = subparsers.add_parser("snapshots", help="列出评估快照")
    snapshots.set_defaults(func=command_snapshots)

    snapshot_cmd = subparsers.add_parser("snapshot", help="回看一次评估快照")
    snapshot_cmd.add_argument("--run-id", type=int)
    snapshot_cmd.add_argument("--version")
    snapshot_cmd.add_argument("--output")
    snapshot_cmd.set_defaults(func=command_snapshot)

    compare = subparsers.add_parser("compare", help="比较同一查询在两个摘要版本下的估计")
    compare.add_argument("--query-id", required=True)
    compare.add_argument("--from-version", required=True)
    compare.add_argument("--to-version", required=True)
    compare.add_argument("--output")
    compare.set_defaults(func=command_compare)

    suggestions = subparsers.add_parser("suggestions", help="查看某次运行的更新建议")
    suggestions.add_argument("--run-id", type=int, required=True)
    suggestions.set_defaults(func=command_suggestions)

    demo = subparsers.add_parser("demo", help="执行生成、评估、更新与收敛核验全流程")
    demo.add_argument("--seed", type=int, default=20261128)
    demo.add_argument("--logs", type=int, default=5000)
    demo.add_argument("--queries", type=int, default=16)
    demo.add_argument("--buckets", type=int, default=8)
    demo.add_argument("--sample-size", type=int, default=512)
    demo.add_argument("--combo-sample-size", type=int, default=160)
    demo.add_argument("--update-sample-size", type=int, default=1024)
    demo.add_argument("--initial-version", default="v1_initial")
    demo.add_argument("--out-dir", default=str(DEFAULT_GENERATED))
    demo.add_argument("--reports-dir", default=str(DEFAULT_REPORTS))
    demo.set_defaults(func=command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, LookupError, FileExistsError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
