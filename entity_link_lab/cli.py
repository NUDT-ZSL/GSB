"""Command line entry points for generation, execution, migration and replay."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from .cache import CacheStore
from .canonical import read_json, read_jsonl, write_json, write_jsonl
from .chains import ChainRegistry, build_chains, build_trace_records
from .errors import LinkError
from .evidence import generate_approx_candidates
from .incremental import apply_delta_to_rows, run_incremental
from .ingest import load_mentions, load_seed_candidates
from .pipeline import run_pipeline
from .replay import replay_traces
from .rules import migrate_v1_to_v2, migrate_v2_to_v1, read_rule_book
from .seed import generate_all
from .selfcheck import (
    check_invalidation_coverage,
    check_trace_coverage,
    compare_evidence_paths,
)


def _print_json(payload: Any) -> None:
    import json

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _load_updated_mentions(data_dir: Path, output_dir: Path, delta: dict[str, Any]) -> tuple[list[dict[str, Any]], Path]:
    rows = read_jsonl(data_dir / "mentions.jsonl")
    updated_rows = apply_delta_to_rows(rows, delta)
    updated_path = output_dir / "mentions_after_delta.jsonl"
    write_jsonl(updated_path, updated_rows)
    return load_mentions(updated_path), updated_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="entity-link-lab")
    sub = parser.add_subparsers(dest="command", required=True)

    seed_cmd = sub.add_parser("seed", help="generate fixed-seed local data")
    seed_cmd.add_argument("--out", default="data")

    run_cmd = sub.add_parser("run", help="run deterministic link pipeline")
    run_cmd.add_argument("--data", default="data")
    run_cmd.add_argument("--rules", default=None)
    run_cmd.add_argument("--output", default="output")
    run_cmd.add_argument("--cache-dir", default="cache")
    run_cmd.add_argument("--checkpoint-dir", default="checkpoints")
    run_cmd.add_argument("--kill-after", type=int, default=None)
    run_cmd.add_argument("--signal-after", type=int, default=None)

    migrate_cmd = sub.add_parser("migrate", help="migrate rule snapshots")
    migrate_cmd.add_argument("--direction", choices=["v1-to-v2", "v2-to-v1"], required=True)
    migrate_cmd.add_argument("--source", required=True)
    migrate_cmd.add_argument("--target", required=True)
    migrate_cmd.add_argument("--mode", choices=["fold", "discard"], default="fold")

    replay_cmd = sub.add_parser("replay", help="replay report traces")
    replay_cmd.add_argument("--data", default="data")
    replay_cmd.add_argument("--rules", default=None)
    replay_cmd.add_argument("--report", default="output/report.json")

    inc_cmd = sub.add_parser("incremental", help="apply generated incremental delta")
    inc_cmd.add_argument("--data", default="data")
    inc_cmd.add_argument("--rules", default=None)
    inc_cmd.add_argument("--report", default="output/report.json")
    inc_cmd.add_argument("--delta", default="data/incremental_delta.json")
    inc_cmd.add_argument("--output", default="output/incremental_report.json")
    inc_cmd.add_argument("--cache-dir", default="cache")

    self_cmd = sub.add_parser("selfcheck", help="show report self-check")
    self_cmd.add_argument("--report", default="output/report.json")

    args = parser.parse_args(argv)
    try:
        if args.command == "seed":
            manifest = generate_all(args.out)
            _print_json({"status": "ok", "manifest": manifest})
            return 0

        if args.command == "run":
            stop_event = threading.Event()

            def handle_signal(signum: int, frame: Any) -> None:
                stop_event.set()

            signal.signal(signal.SIGTERM, handle_signal)
            signal.signal(signal.SIGINT, handle_signal)
            report = run_pipeline(
                args.data,
                args.rules,
                output_dir=args.output,
                cache_dir=args.cache_dir,
                checkpoint_dir=args.checkpoint_dir,
                inject_kill_after=args.kill_after,
                inject_signal_after=args.signal_after,
                should_stop=stop_event.is_set,
            )
            if report.get("stopped"):
                _print_json({"status": "signal_stopped", "recovery": report})
                return 0
            _print_json(
                {
                    "status": "ok",
                    "counts": report["counts"],
                    "output": str(Path(args.output) / "report.json"),
                    "self_check": report["self_check"]["ok"],
                    "replay": report["replay"]["ok"],
                }
            )
            return 0

        if args.command == "migrate":
            if args.direction == "v1-to-v2":
                record = migrate_v1_to_v2(args.source, args.target)
            else:
                record = migrate_v2_to_v1(args.source, args.target, mode=args.mode)
            _print_json(record)
            return 0

        if args.command == "replay":
            report = read_json(args.report)
            mentions = load_mentions(Path(args.data) / "mentions.jsonl")
            rules_source = args.rules or Path(args.data) / "rules_v2" / "manifest.json"
            book = read_rule_book(rules_source)
            result = replay_traces(report["traces"], mentions, book)
            _print_json({"status": "ok", **{k: v for k, v in result.items() if k != "details"}})
            return 0

        if args.command == "incremental":
            data_dir = Path(args.data)
            output_path = Path(args.output)
            output_dir = output_path.parent
            output_dir.mkdir(parents=True, exist_ok=True)
            report = read_json(args.report)
            delta = read_json(args.delta)
            mentions_after, _ = _load_updated_mentions(data_dir, output_dir, delta)
            rules_source = args.rules or data_dir / "rules_v2" / "manifest.json"
            book = read_rule_book(rules_source)
            cache = CacheStore(args.cache_dir)
            registry = ChainRegistry()
            result = run_incremental(
                baseline_report=report,
                updated_mentions=mentions_after,
                delta=delta,
                book=book,
                cache=cache,
                registry=registry,
            )
            seed_pairs = load_seed_candidates(
                data_dir / "coref_candidates.jsonl", mentions_after
            )
            approx_evidence, approx_plan = generate_approx_candidates(
                mentions_after, book, seed_pairs
            )
            approx_bundle = build_chains(approx_evidence, mentions_after)
            result["traces"] = build_trace_records(
                result["chains"],
                {item["pair_key"]: item for item in result["exact_evidence"]},
                {item["pair_key"]: item for item in approx_evidence},
            )
            path_check = compare_evidence_paths(
                result["exact_evidence"],
                approx_evidence,
                result["chains"],
                approx_bundle["chains"],
            )
            trace_check = check_trace_coverage(
                result["chains"],
                {item["pair_key"]: item for item in result["exact_evidence"]},
                result["traces"],
            )
            invalidation_check = check_invalidation_coverage(result)
            result["self_check"] = {
                "ok": True,
                "checks": [path_check, trace_check, invalidation_check],
            }
            result["approx_plan"] = approx_plan
            write_json(output_path, result)
            _print_json(
                {
                    "status": "ok",
                    "affected": len(result["affected_mention_ids"]),
                    "recomputed_pairs": len(result["recomputed_pair_keys"]),
                    "reused_closures": len(result["reused_closures"]),
                    "output": str(output_path),
                }
            )
            return 0

        if args.command == "selfcheck":
            report = read_json(args.report)
            _print_json(report.get("self_check", {"ok": False}))
            return 0

        raise AssertionError("unreachable")
    except LinkError as exc:
        _print_json(exc.to_dict())
        return 2
    except (FileNotFoundError, ValueError, KeyError) as exc:
        _print_json({"status": "error", "code": "E_INPUT_INVALID", "message": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
