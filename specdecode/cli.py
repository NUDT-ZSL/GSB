"""Command-line interface for generation, loading, replay and comparison."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from .compare import compare_policies, comparison_markdown, grid_policies
from .generator import (
    DEFAULT_REQUEST_COUNT,
    DEFAULT_SEED,
    DEFAULT_TICK_MS,
    generate_dataset_files,
    make_policy,
)
from .loader import LoadValidationError, Policy, load_dataset, load_dataset_file
from .reference import verify_policy
from .runner import SimulatedCrash, run_policy, summarize_run
from .schema import SchemaError, dump_json, load_json


def _write_json(path: str, value: Any) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    dump_json(path, value)


def _source_schema(document: Any) -> int | None:
    if not isinstance(document, dict):
        return None
    if "schema_version" in document:
        return int(document["schema_version"])
    if "schemaVersion" in document:
        return int(document["schemaVersion"])
    return None


def _cmd_generate(args: argparse.Namespace) -> int:
    manifest = generate_dataset_files(
        args.out_dir,
        seed=args.seed,
        request_count=args.requests,
        tick_ms=args.tick_ms,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_load(args: argparse.Namespace) -> int:
    document = load_json(args.dataset)
    dataset = load_dataset(document)
    summary = dataset.load_summary(_source_schema(document))
    if not summary["all_truth_match"]:
        print("normalization truth mismatch", file=sys.stderr)
        return 2
    if args.summary:
        _write_json(args.summary, summary)
        print(
            json.dumps(
                {
                    "dataset_fingerprint": summary["dataset_fingerprint"],
                    "request_count": summary["request_count"],
                    "mapping_incomplete_requests": summary[
                        "mapping_incomplete_requests"
                    ],
                    "all_truth_match": True,
                    "summary": args.summary,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _selected_policy(dataset: Any, args: argparse.Namespace) -> Policy:
    base = dataset.policy(args.policy) if args.policy else dataset.policies[0]
    if (
        args.width is None
        and args.threshold_bps is None
        and args.budget_ticks is None
        and args.policy_id is None
    ):
        return base
    width = base.candidate_length if args.width is None else int(args.width)
    threshold = (
        base.acceptance_threshold_bps
        if args.threshold_bps is None
        else int(args.threshold_bps)
    )
    budget = (
        base.latency_budget_ticks
        if args.budget_ticks is None
        else int(args.budget_ticks)
    )
    policy_id = args.policy_id or f"custom-w{width}-t{threshold}"
    raw = make_policy(policy_id, width, threshold, budget)
    return Policy.from_dict(raw)


def _cmd_run(args: argparse.Namespace) -> int:
    document = load_json(args.dataset)
    dataset = load_dataset(document)
    policy = _selected_policy(dataset, args)
    if args.simulate_crash_after is not None and not args.db:
        raise ValueError("--simulate-crash-after requires --db for resume semantics")
    try:
        result = run_policy(
            dataset,
            policy,
            database_path=args.db,
            source_schema=_source_schema(document),
            crash_after_new_commits=args.simulate_crash_after,
        )
    except SimulatedCrash as exc:
        print(
            json.dumps(
                {
                    "simulated_crash": True,
                    "message": str(exc),
                    "database": args.db,
                    "policy_id": policy.policy_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 75

    if args.out:
        _write_json(args.out, result)
    if args.full and not args.out:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                summarize_run(result), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    return 0


def _parse_int_list(value: str, name: str) -> list[int]:
    try:
        result = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError(f"{name} must be a comma-separated integer list") from exc
    if not result:
        raise ValueError(f"{name} cannot be empty")
    return result


def _cmd_compare(args: argparse.Namespace) -> int:
    document = load_json(args.dataset)
    dataset = load_dataset(document)
    if args.use_dataset_policies:
        policies = list(dataset.policies)
    else:
        widths = _parse_int_list(args.widths, "--widths")
        thresholds = _parse_int_list(args.thresholds, "--thresholds")
        budget = (
            args.budget_ticks
            if args.budget_ticks is not None
            else dataset.policies[0].latency_budget_ticks
        )
        policies = grid_policies(
            widths, thresholds, budget_ticks=int(budget)
        )
    report = compare_policies(
        dataset,
        policies,
        database_path=args.db,
        source_schema=_source_schema(document),
        independently_verify=not args.no_verify,
    )
    if args.out:
        _write_json(args.out, report)
    if args.markdown:
        markdown = comparison_markdown(report)
        parent = os.path.dirname(os.path.abspath(args.markdown))
        os.makedirs(parent, exist_ok=True)
        with open(args.markdown, "w", encoding="utf-8") as handle:
            handle.write(markdown)
            handle.write("\n")
    if args.json_stdout:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(comparison_markdown(report), end="")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    document = load_json(args.dataset)
    dataset = load_dataset(document)
    if args.policy:
        if args.db:
            from .storage import open_database, stored_run_policies

            connection = open_database(args.db)
            try:
                stored_policies = stored_run_policies(
                    connection, dataset_fingerprint=dataset.fingerprint
                )
            finally:
                connection.close()
            stored_by_id = {
                policy.policy_id: policy for policy in stored_policies
            }
            policies = [
                stored_by_id.get(policy_id) or dataset.policy(policy_id)
                for policy_id in args.policy
            ]
        else:
            policies = [dataset.policy(policy_id) for policy_id in args.policy]
    elif args.db:
        from .storage import open_database, stored_run_policies

        connection = open_database(args.db)
        try:
            policies = stored_run_policies(
                connection, dataset_fingerprint=dataset.fingerprint
            )
        finally:
            connection.close()
        if not policies:
            raise ValueError(
                "database contains no stored runs for this dataset fingerprint"
            )
    else:
        policies = list(dataset.policies)
    errors: list[str] = []
    reports: list[dict[str, Any]] = []
    for policy in policies:
        report = verify_policy(
            dataset, policy, database_path=args.db
        )
        reports.append(report)
        errors.extend(
            f"{policy.policy_id}: {error}" for error in report["errors"]
        )

    summary = {
        "ok": not errors,
        "dataset_fingerprint": dataset.fingerprint,
        "database": args.db,
        "policies": [
            {
                "policy_id": report["policy_id"],
                "ok": report["ok"],
                "request_count": report["request_count"],
                "database_audit_ok": (
                    report["database_audit"]["ok"]
                    if report["database_audit"] is not None
                    else None
                ),
            }
            for report in reports
        ],
        "errors": errors,
    }
    if args.out:
        _write_json(
            args.out,
            {
                "summary": summary,
                "reports": reports,
            },
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="specdecode",
        description=(
            "Deterministic local speculative-decoding replay, verification "
            "and SQLite resume toolkit."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="write identical workloads in schema versions 1..3"
    )
    generate.add_argument("--out-dir", default="data")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument(
        "--requests", type=int, default=DEFAULT_REQUEST_COUNT
    )
    generate.add_argument("--tick-ms", type=int, default=DEFAULT_TICK_MS)
    generate.set_defaults(func=_cmd_generate)

    load = subparsers.add_parser(
        "load", help="normalize prompts and emit per-request parameter summaries"
    )
    load.add_argument("--dataset", default="data/dataset_v1.json")
    load.add_argument("--summary", help="optional summary JSON output path")
    load.set_defaults(func=_cmd_load)

    run = subparsers.add_parser(
        "run", help="decode one policy, optionally with SQLite persistence/resume"
    )
    run.add_argument("--dataset", default="data/dataset_v1.json")
    run.add_argument("--policy", help="policy ID from the dataset")
    run.add_argument("--width", type=int, help="override candidate length")
    run.add_argument(
        "--threshold-bps",
        type=int,
        help="override acceptance threshold in basis points (0..10000)",
    )
    run.add_argument(
        "--budget-ticks", type=int, help="override per-request latency budget"
    )
    run.add_argument("--policy-id", help="ID used for an overridden policy")
    run.add_argument("--db", help="SQLite database path; reusing it resumes")
    run.add_argument("--out", help="write the complete run JSON")
    run.add_argument(
        "--full", action="store_true", help="print complete run JSON to stdout"
    )
    run.add_argument(
        "--simulate-crash-after",
        type=int,
        metavar="N",
        help="commit N new requests, then stop before committing the next",
    )
    run.set_defaults(func=_cmd_run)

    compare = subparsers.add_parser(
        "compare", help="compare widths and acceptance thresholds on one stream"
    )
    compare.add_argument("--dataset", default="data/dataset_v1.json")
    compare.add_argument(
        "--widths",
        default="4,6,8",
        help="comma-separated fixed draft widths",
    )
    compare.add_argument(
        "--thresholds",
        default="5000,8000,9500",
        help="comma-separated acceptance thresholds in basis points",
    )
    compare.add_argument("--budget-ticks", type=int)
    compare.add_argument(
        "--use-dataset-policies",
        action="store_true",
        help="compare the three generator-provided policies instead of the grid",
    )
    compare.add_argument("--db", help="persist each policy run in this database")
    compare.add_argument("--out", help="write complete comparison JSON")
    compare.add_argument("--markdown", help="write a compact Markdown report")
    compare.add_argument(
        "--json-stdout", action="store_true", help="print JSON instead of Markdown"
    )
    compare.add_argument(
        "--no-verify",
        action="store_true",
        help="skip independent reference recomputation (not recommended)",
    )
    compare.set_defaults(func=_cmd_compare)

    verify = subparsers.add_parser(
        "verify",
        help="compare production, two replays, reference decoder and stored rows",
    )
    verify.add_argument("--dataset", default="data/dataset_v1.json")
    verify.add_argument(
        "--policy", action="append", help="policy ID to verify; repeatable"
    )
    verify.add_argument("--db", help="verify persisted runs from this database")
    verify.add_argument("--out", help="write full verification reports")
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (SchemaError, LoadValidationError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
