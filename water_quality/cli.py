"""Command-line interface for the water-quality pushdown kernel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .db import DEFAULT_DB_PATH
from .engine import PartitionVersionMismatchError, PushdownEngine
from .generator import (
    GeneratorConfig,
    generate_database,
    verify_reproducible,
)
from .rules import DEFAULT_RULES, PartitionRules
from .snapshots import (
    create_snapshot,
    get_snapshot,
    resume_snapshot,
    verify_snapshot_digest,
)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _load_json(path: str | None) -> Any:
    if path is None:
        return None
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _query_changes(path: str | None) -> dict[str, dict[str, Any]] | None:
    data = _load_json(path)
    if data is None:
        return None
    if "query_changes" in data:
        return data["query_changes"]
    return data


def _rules_from_args(args: argparse.Namespace) -> PartitionRules:
    return PartitionRules(
        version=args.rule_version,
        pipe_strategy=args.pipe_strategy,
        point_strategy=args.point_strategy,
        pipe_hash_buckets=args.pipe_hash_buckets,
        point_hash_buckets=args.point_hash_buckets,
    )


def _config_from_args(args: argparse.Namespace) -> GeneratorConfig:
    return GeneratorConfig(
        seed=args.seed,
        pipe_count=args.pipe_count,
        point_count=args.point_count,
        time_steps=args.time_steps,
        sample_interval_minutes=args.sample_interval_minutes,
        null_rate=args.null_rate,
        null_burst_rate=args.null_burst_rate,
        null_burst_max=args.null_burst_max,
        start_time=args.start_time,
    )


def _compact_run(result: dict[str, Any]) -> dict[str, Any]:
    output = {
        "run_id": result["run_id"],
        "mode": result["mode"],
        "partition_rules_version": result["partition_rules_version"],
        "query_count": result["query_count"],
        "all_consistent": result["all_consistent"],
        "all_truth_matched": result["all_truth_matched"],
        "query_summary": [
            {
                "query_id": item["query_id"],
                "is_consistent": item["is_consistent"],
                "downpush_count": item["downpush_count"],
                "central_count": item["central_count"],
                "divergence_partition": item["divergence_partition"],
                "divergence_predicate": item["divergence_predicate"],
            }
            for item in result["query_reports"]
        ],
        "audit": {
            key: value
            for key, value in result["audit"].items()
            if not isinstance(value, list)
        },
    }
    if "all_incremental_consistent" in result:
        output["all_incremental_consistent"] = result[
            "all_incremental_consistent"
        ]
        output["incremental_checks"] = result["incremental_checks"]
    if "resumed_from_snapshot" in result:
        output["resumed_from_snapshot"] = result["resumed_from_snapshot"]
    return output


def cmd_init(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    rules = _rules_from_args(args)
    result = generate_database(args.db, config, rules=rules, reset=True)
    _print(
        {
            "database": result.db_path,
            "seed": result.seed,
            "rows": {
                "pipes": result.pipe_rows,
                "monitoring_points": result.point_rows,
                "readings": result.reading_rows,
                "query_requests": result.query_rows,
                "truth_violations": result.violation_rows,
                "truth_manual_reviews": result.review_rows,
            },
            "partition_rules_version": rules.version,
            "data_fingerprint": result.data_fingerprint,
            "truth_signature": result.truth_signature,
        }
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    with PushdownEngine(args.db) as engine:
        result = engine.run_all(run_id=args.run_id)
    _print(result if args.full else _compact_run(result))
    return (
        0
        if result["all_consistent"] and result["all_truth_matched"]
        else 2
    )


def cmd_incremental(args: argparse.Namespace) -> int:
    rules = None
    rules_data = _load_json(args.new_rules_file)
    if rules_data is not None:
        rules = PartitionRules.from_dict(rules_data)
    changes = _query_changes(args.query_change_file)
    with PushdownEngine(args.db) as engine:
        result = engine.run_incremental(
            args.previous_run_id,
            query_changes=changes,
            new_rules=rules,
            run_id=args.run_id,
        )
    _print(result if args.full else _compact_run(result))
    return (
        0
        if result["all_consistent"]
        and result["all_incremental_consistent"]
        and result["all_truth_matched"]
        else 2
    )


def cmd_snapshot(args: argparse.Namespace) -> int:
    from .db import connect

    conn = connect(args.db)
    try:
        result = create_snapshot(
            conn, args.run_id, snapshot_id=args.snapshot_id
        )
    finally:
        conn.close()
    _print(result)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    changes = _query_changes(args.query_change_file)
    try:
        with PushdownEngine(args.db) as engine:
            result = resume_snapshot(
                engine,
                args.snapshot_id,
                query_changes=changes,
                run_id=args.run_id,
            )
    except PartitionVersionMismatchError as exc:
        _print(
            {
                "error": "PARTITION_RULE_VERSION_MISMATCH",
                "message": str(exc),
                "snapshot_id": args.snapshot_id,
            }
        )
        return 3
    _print(result if args.full else _compact_run(result))
    return (
        0
        if result["all_consistent"]
        and result["all_incremental_consistent"]
        and result["all_truth_matched"]
        else 2
    )


def cmd_verify_generator(args: argparse.Namespace) -> int:
    result = verify_reproducible(args.db)
    _print(result)
    return 0 if result["reproducible"] else 2


def cmd_verify_snapshot(args: argparse.Namespace) -> int:
    from .db import connect

    conn = connect(args.db)
    try:
        result = verify_snapshot_digest(conn, args.snapshot_id)
    finally:
        conn.close()
    _print(result)
    return 0 if result["valid"] else 2


def cmd_show_snapshot(args: argparse.Namespace) -> int:
    from .db import connect

    conn = connect(args.db)
    try:
        _print(get_snapshot(conn, args.snapshot_id))
    finally:
        conn.close()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    db_path = args.db
    config = _config_from_args(args)
    generated = generate_database(
        db_path, config, rules=_rules_from_args(args), reset=True
    )
    with PushdownEngine(db_path) as engine:
        original_queries = [
            dict(row)
            for row in engine.conn.execute(
                """
                SELECT query_id, description, join_type, request_json, created_at
                FROM query_requests
                ORDER BY query_id
                """
            )
        ]
        try:
            full = engine.run_all(run_id="demo-full")
            snapshot = create_snapshot(
                engine.conn, full["run_id"], snapshot_id="demo-snapshot"
            )
            query = json.loads(
                engine.conn.execute(
                    "SELECT request_json FROM query_requests WHERE query_id = 'Q001'"
                ).fetchone()["request_json"]
            )
            query["reading_predicate_groups"][0][0]["value"] = 0.5
            incremental = engine.run_incremental(
                full["run_id"],
                query_changes={"Q001": query},
                run_id="demo-incremental",
            )
            query2 = json.loads(
                engine.conn.execute(
                    "SELECT request_json FROM query_requests WHERE query_id = 'Q002'"
                ).fetchone()["request_json"]
            )
            query2["reading_predicate_groups"][0][0]["value"] = 1.2
            resumed = resume_snapshot(
                engine,
                snapshot["snapshot_id"],
                query_changes={"Q002": query2},
                run_id="demo-resumed",
            )
        finally:
            engine.conn.executemany(
                """
                INSERT INTO query_requests(
                    query_id, description, join_type, request_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(query_id) DO UPDATE SET
                    description = excluded.description,
                    join_type = excluded.join_type,
                    request_json = excluded.request_json,
                    created_at = excluded.created_at
                """,
                [
                    (
                        item["query_id"],
                        item["description"],
                        item["join_type"],
                        item["request_json"],
                        item["created_at"],
                    )
                    for item in original_queries
                ],
            )
            engine.conn.commit()
    _print(
        {
            "database": str(db_path),
            "seed": generated.seed,
            "generated_rows": {
                "pipes": generated.pipe_rows,
                "monitoring_points": generated.point_rows,
                "readings": generated.reading_rows,
                "queries": generated.query_rows,
            },
            "full_run": _compact_run(full),
            "snapshot": snapshot,
            "incremental_run": _compact_run(incremental),
            "resumed_run": _compact_run(resumed),
            "truth": {
                "violation_rows": generated.violation_rows,
                "manual_review_rows": generated.review_rows,
                "signature": generated.truth_signature,
            },
        }
    )
    return 0 if (
        full["all_consistent"]
        and full["all_truth_matched"]
        and incremental["all_incremental_consistent"]
        and incremental["all_truth_matched"]
        and resumed["all_incremental_consistent"]
        and resumed["all_truth_matched"]
    ) else 2


def _add_init_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--seed", type=int, default=20261128)
    parser.add_argument("--pipe-count", type=int, default=20)
    parser.add_argument("--point-count", type=int, default=32)
    parser.add_argument("--time-steps", type=int, default=18)
    parser.add_argument("--sample-interval-minutes", type=int, default=120)
    parser.add_argument("--null-rate", type=float, default=0.12)
    parser.add_argument("--null-burst-rate", type=float, default=0.025)
    parser.add_argument("--null-burst-max", type=int, default=4)
    parser.add_argument("--start-time", default="2026-11-01T00:00:00Z")
    parser.add_argument("--rule-version", default=DEFAULT_RULES.version)
    parser.add_argument("--pipe-strategy", default=DEFAULT_RULES.pipe_strategy)
    parser.add_argument("--point-strategy", default=DEFAULT_RULES.point_strategy)
    parser.add_argument("--pipe-hash-buckets", type=int, default=2)
    parser.add_argument("--point-hash-buckets", type=int, default=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="water-quality-pushdown",
        description="SQLite 水质监控数据分区谓词下推执行内核",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="生成确定性数据、查询与真值")
    _add_init_arguments(init)
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="执行全量分区下推并逐查询核对")
    run.add_argument("--db", default=str(DEFAULT_DB_PATH))
    run.add_argument("--run-id")
    run.add_argument("--full", action="store_true")
    run.set_defaults(func=cmd_run)

    incremental = sub.add_parser("incremental", help="仅重算受影响分区")
    incremental.add_argument("--db", default=str(DEFAULT_DB_PATH))
    incremental.add_argument("--previous-run-id", required=True)
    incremental.add_argument("--query-change-file")
    incremental.add_argument("--new-rules-file")
    incremental.add_argument("--run-id")
    incremental.add_argument("--full", action="store_true")
    incremental.set_defaults(func=cmd_incremental)

    snapshot = sub.add_parser("snapshot", help="持久化运行快照")
    snapshot.add_argument("--db", default=str(DEFAULT_DB_PATH))
    snapshot.add_argument("--run-id", required=True)
    snapshot.add_argument("--snapshot-id")
    snapshot.set_defaults(func=cmd_snapshot)

    resume = sub.add_parser("resume", help="从快照续算")
    resume.add_argument("--db", default=str(DEFAULT_DB_PATH))
    resume.add_argument("--snapshot-id", required=True)
    resume.add_argument("--query-change-file")
    resume.add_argument("--run-id")
    resume.add_argument("--full", action="store_true")
    resume.set_defaults(func=cmd_resume)

    verify_gen = sub.add_parser("verify-generator", help="复核固定种子可重复性")
    verify_gen.add_argument("--db", default=str(DEFAULT_DB_PATH))
    verify_gen.set_defaults(func=cmd_verify_generator)

    verify_snap = sub.add_parser("verify-snapshot", help="校验快照摘要")
    verify_snap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    verify_snap.add_argument("--snapshot-id", required=True)
    verify_snap.set_defaults(func=cmd_verify_snapshot)

    show = sub.add_parser("show-snapshot", help="显示快照中的结果类别与计数")
    show.add_argument("--db", default=str(DEFAULT_DB_PATH))
    show.add_argument("--snapshot-id", required=True)
    show.set_defaults(func=cmd_show_snapshot)

    demo = sub.add_parser("demo", help="一键生成、全量、增量、快照与续算")
    _add_init_arguments(demo)
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

