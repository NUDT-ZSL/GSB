"""Command-line entry point: local JSONL in, deterministic JSON out."""

import argparse
import json
import sys
from pathlib import Path

from .cache import ContentCache
from .config import Config
from .engine import run_batch, run_query
from .errors import EXIT_CODES, ErrorCode, OpChainError
from .samplegen import generate_all
from .selfcheck import run_full_selfcheck
from .snapshot import read_snapshot, replace_snapshot
from .task import load_jsonl, parse_spec


def _emit(payload, stream=sys.stdout):
    stream.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    stream.write("\n")


def _fail(error):
    _emit(error.to_dict(), stream=sys.stderr)
    return EXIT_CODES.get(error.code, 1)


def _load_specs(path):
    return [parse_spec(record) for record in load_jsonl(path)]


def cmd_generate(args):
    valid_path, anomaly_path = generate_all(args.samples_dir)
    config = Config.load(args.config) if args.config else Config()
    config_path = Path(args.samples_dir) / "default_config.json"
    config.save(config_path)
    _emit(
        {
            "code": ErrorCode.OK,
            "valid_tasks": str(valid_path),
            "anomaly_tasks": str(anomaly_path),
            "config": str(config_path),
        }
    )
    return 0


def cmd_batch(args):
    config = Config.load(args.config) if args.config else Config()
    specs = _load_specs(args.input)
    envelope, _, observations = run_batch(specs, config)
    output_path = Path(args.output)
    replace_snapshot(output_path, envelope["kind"], envelope["payload"])
    observed = {item["case_id"]: item["observed_latency_ms"] for item in observations}
    summary = [
        {
            "case_id": result["case_id"],
            "final_hash": result["final_hash"],
            "observed_latency_ms": observed[result["case_id"]],
            "selected_block_size": result["execution_plan"]["block_adjudication"][
                "selected_block_size"
            ],
            "violation_count": result["adjudication"]["final_report"][
                "violation_count"
            ],
        }
        for result in envelope["payload"]["results"]
    ]
    _emit(
        {
            "code": ErrorCode.OK,
            "case_count": len(summary),
            "snapshot_version": envelope["version"],
            "snapshot_path": str(output_path),
            "cases": summary,
        }
    )
    return 0


def cmd_query(args):
    config = Config.load(args.config) if args.config else Config()
    envelope = read_snapshot(args.snapshot)
    if envelope is None:
        raise OpChainError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot {args.snapshot} does not exist",
        )
    record = next(
        (
            item
            for item in load_jsonl(args.input)
            if str(item.get("case_id")) == args.case_id
        ),
        None,
    )
    if record is None:
        raise OpChainError(
            ErrorCode.PARAM_INVALID,
            f"case_id {args.case_id} not found in {args.input}",
        )
    spec = parse_spec(record)
    result, _, _ = run_query(spec, config, envelope, cache=ContentCache())
    result.pop("_observed_latency_ms", None)
    output = {
        "code": ErrorCode.OK,
        "case_id": spec.case_id,
        "observed_latency_ms": result["query"]["observed_latency_ms"],
        "latency_budget_ms": result["query"]["latency_budget_ms"],
        "estimate": result["query"]["estimate"],
        "input_summary": result["input_record"]["merged_summary"],
        "stage_summaries": [
            {
                "stage": stage["stage"],
                "op": stage["op"],
                "semantic_axes": stage["semantic_axes"],
                "merged_summary": stage["merged_summary"],
                "content_hash": stage["content_hash"],
            }
            for stage in result["stages"]
        ],
        "final_hash": result["final_hash"],
        "final_shape": result["final_shape"],
        "query_consistency": result["query_consistency"],
    }
    _emit(output)
    return 0


def cmd_selfcheck(args):
    config = Config.load(args.config) if args.config else Config()
    envelope = read_snapshot(args.snapshot)
    if envelope is None:
        raise OpChainError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot {args.snapshot} does not exist",
        )
    specs = _load_specs(args.input)
    report = run_full_selfcheck(specs, envelope, config)
    _emit(report)
    return 0


def cmd_validate(args):
    """Parse/validate every row; report business codes without executing math."""
    reports = []
    exit_code = 0
    for record in load_jsonl(args.input):
        try:
            spec = parse_spec(record)
            reports.append({"case_id": spec.case_id, "code": ErrorCode.OK})
        except OpChainError as error:
            reports.append(
                {
                    "case_id": record.get("case_id"),
                    "code": error.code,
                    "message": error.message,
                }
            )
            exit_code = EXIT_CODES[error.code]
    _emit({"code": ErrorCode.OK if exit_code == 0 else "E_HAS_INVALID_ROWS", "reports": reports})
    return exit_code


def build_parser():
    parser = argparse.ArgumentParser(
        prog="opchain",
        description="Transformer forward-link activation/transpose operator component",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate deterministic local samples")
    gen.add_argument("--samples-dir", default="samples")
    gen.add_argument("--config", default=None)
    gen.set_defaults(func=cmd_generate)

    batch = sub.add_parser("batch", help="batch execute a JSONL task list")
    batch.add_argument("--input", required=True)
    batch.add_argument("--output", default="results/batch_snapshot.json")
    batch.add_argument("--config", default=None)
    batch.set_defaults(func=cmd_batch)

    query = sub.add_parser("query", help="interactive per-case query")
    query.add_argument("--input", required=True)
    query.add_argument("--snapshot", default="results/batch_snapshot.json")
    query.add_argument("--case-id", required=True)
    query.add_argument("--config", default=None)
    query.set_defaults(func=cmd_query)

    check = sub.add_parser("selfcheck", help="cross-module consistency self-check")
    check.add_argument("--input", required=True)
    check.add_argument("--snapshot", default="results/batch_snapshot.json")
    check.add_argument("--config", default=None)
    check.set_defaults(func=cmd_selfcheck)

    validate = sub.add_parser("validate", help="validate task rows and error codes")
    validate.add_argument("--input", required=True)
    validate.set_defaults(func=cmd_validate)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except OpChainError as error:
        return _fail(error)


if __name__ == "__main__":
    sys.exit(main())
