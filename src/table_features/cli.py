"""Command line interface for generation, runs, increments, and cache inspection."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, pretty_json_bytes
from .config import load_config
from .data import generate_rows, load_jsonl, write_jsonl
from .errors import BusinessError
from .injection import load_injection_config
from .pipeline import Pipeline, write_result_artifacts


def _print_json(value: Any) -> None:
    sys.stdout.buffer.write(pretty_json_bytes(value))


def _summary(result: Any) -> dict[str, Any]:
    return {
        "status": "ok" if not result.errors else "degraded",
        "matrix_shape": [int(result.matrix.shape[0]), int(result.matrix.shape[1])],
        "matrix_version": result.matrix_digest,
        "selected_features": result.selected_features,
        "scores": result.scores,
        "error_codes": list(dict.fromkeys(result.error_codes)),
        "errors": [error.to_dict() for error in result.errors],
        "degradations": [degradation.to_dict() for degradation in result.degradations],
        "cache_hits": sum(
            1 for lookup in result.diagnostics.cache_lookups if lookup.status == "hit"
        ),
        "cache_recomputed": sum(
            1 for lookup in result.diagnostics.cache_lookups if lookup.status != "hit"
        ),
        "snapshots": [write.to_dict() for write in result.snapshot_writes],
    }


def _cmd_generate(args: argparse.Namespace) -> int:
    rows = generate_rows(args.seed, args.rows)
    digest = write_jsonl(args.output, rows)
    _print_json({"output": str(args.output), "rows": len(rows), "sha256": digest})
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    rows = load_jsonl(args.samples)
    cfg = load_config(args.config)
    injection = load_injection_config(args.injection)
    pipeline = Pipeline(args.cache_dir, args.snapshot_dir, clear=args.clear)
    result = pipeline.run(rows, cfg, injection)
    hashes = write_result_artifacts(result, args.output_dir)
    summary = _summary(result)
    summary["artifacts"] = hashes
    _print_json(summary)
    return 0 if not any(error.code == "E_UNCLASSIFIED" for error in result.errors) else 1


def _cmd_incremental(args: argparse.Namespace) -> int:
    old_rows = load_jsonl(args.old_samples)
    new_rows = load_jsonl(args.new_samples)
    old_cfg = load_config(args.old_config)
    new_cfg = load_config(args.new_config)
    injection = load_injection_config(args.injection)
    pipeline = Pipeline(args.cache_dir, args.snapshot_dir, clear=args.clear)
    # Warm the old state so unaffected entries have a real opportunity to hit.
    pipeline.run(old_rows, old_cfg)
    result, incremental = pipeline.run_incremental(
        old_rows,
        old_cfg,
        new_rows,
        new_cfg,
        injection,
    )
    hashes = write_result_artifacts(result, args.output_dir)
    report = {
        "summary": _summary(result),
        "incremental": incremental.to_dict(),
        "artifacts": hashes,
    }
    atomic_write_json(Path(args.output_dir) / "incremental.json", report, pretty=True)
    _print_json(report)
    return 0


def _cmd_inspect_cache(args: argparse.Namespace) -> int:
    pipeline = Pipeline(args.cache_dir, args.snapshot_dir)
    entries = pipeline.cache.inspect_feature(args.feature)
    _print_json({"feature": args.feature, "entries": entries})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="table-features")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic JSONL samples")
    generate.add_argument("--output", required=True)
    generate.add_argument("--seed", type=int, default=20240517)
    generate.add_argument("--rows", type=int, default=24)
    generate.set_defaults(func=_cmd_generate)

    run = subparsers.add_parser("run", help="run the four-stage pipeline")
    run.add_argument("--samples", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--injection", default=None)
    run.add_argument("--cache-dir", required=True)
    run.add_argument("--snapshot-dir", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--clear", action="store_true")
    run.set_defaults(func=_cmd_run)

    incremental = subparsers.add_parser("incremental", help="compare and run an incremental update")
    incremental.add_argument("--old-samples", required=True)
    incremental.add_argument("--new-samples", required=True)
    incremental.add_argument("--old-config", required=True)
    incremental.add_argument("--new-config", required=True)
    incremental.add_argument("--injection", default=None)
    incremental.add_argument("--cache-dir", required=True)
    incremental.add_argument("--snapshot-dir", required=True)
    incremental.add_argument("--output-dir", required=True)
    incremental.add_argument("--clear", action="store_true")
    incremental.set_defaults(func=_cmd_incremental)

    inspect_cache = subparsers.add_parser("inspect-cache", help="inspect cache keys for a feature")
    inspect_cache.add_argument("--cache-dir", required=True)
    inspect_cache.add_argument("--snapshot-dir", required=True)
    inspect_cache.add_argument("--feature", required=True)
    inspect_cache.set_defaults(func=_cmd_inspect_cache)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        sys.stderr.buffer.write(pretty_json_bytes({"error": exc.record.to_dict()}))
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.buffer.write(
            pretty_json_bytes(
                {
                    "error": {
                        "stage": "cli",
                        "code": "E_CONFIG_INVALID",
                        "message": str(exc),
                        "feature": None,
                        "row_id": None,
                        "details": {"exception_type": type(exc).__name__},
                    }
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

