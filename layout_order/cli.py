"""Command-line entry points for generation, runs, migration and self-check."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .annotations import (
    load_annotations,
    migrate_annotations,
    render_annotations,
)
from .cache import ContentAddressedCache
from .canonical import atomic_write_json, read_json, report_hash
from .errors import LayoutError
from .pipeline import LayoutRunner
from .samples import DEFAULT_SEED, generate_dataset, read_samples
from .selfcheck import run_self_check
from .snapshot import build_snapshot


def _seal(report: dict[str, Any]) -> dict[str, Any]:
    report["report_hash"] = report_hash(report)
    return report


def _emit_error(exc: LayoutError, out: str | None = None) -> int:
    payload = {
        "schema_version": 1,
        "business_codes": [exc.code],
        "error": exc.to_dict(),
    }
    payload["report_hash"] = report_hash(payload)
    if out:
        atomic_write_json(out, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2


def _cmd_generate(args: argparse.Namespace) -> int:
    result = generate_dataset(
        args.out,
        seed=args.seed,
        page_count=args.pages,
        snapshot_dir=args.snapshot_dir,
    )
    print(
        json.dumps(
            {
                "business_codes": ["OK"],
                "samples_path": result["samples_path"],
                "config_path": result["config_path"],
                "annotations_dir": result["annotations_dir"],
                "anomalies": result["anomalies"],
                "snapshot": result["snapshot"].metadata(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _build_snapshot_from_config(
    pages: list[dict[str, Any]], config: dict[str, Any]
):
    block_annotations = {page["page_id"]: page for page in pages}
    return build_snapshot(
        block_annotations,
        dict(config["cost_config"]["type_order"]),
        config["edge_weights"],
        config.get("initial_versions"),
    )


def _cmd_run(args: argparse.Namespace) -> int:
    pages = read_samples(args.samples)
    config = read_json(args.config)
    snapshot = _build_snapshot_from_config(pages, config)
    runner = LayoutRunner(
        ContentAddressedCache(args.cache_dir),
        geometry_config=config.get("geometry_config"),
        limits=config.get("limits"),
    )
    report = runner.run(snapshot, mode=args.mode)
    report["business_codes"] = ["OK"]
    report = _seal(report)
    atomic_write_json(args.out, report)
    print(
        json.dumps(
            {
                "business_codes": report["business_codes"],
                "report": args.out,
                "report_hash": report["report_hash"],
                "summary": report["summary"],
                "execution": {
                    "recomputed_nodes": report["execution"]["recomputed_nodes"],
                    "reused_nodes": report["execution"]["reused_nodes"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_self_check(args: argparse.Namespace) -> int:
    report = run_self_check(
        args.out,
        workdir=args.workdir,
        seed=args.seed,
        page_count=args.pages,
    )
    summary = {
        "passed": report["passed"],
        "business_codes": report["business_codes"],
        "report": args.out,
        "report_hash": report["report_hash"],
        "check_count": len(report["checks"]),
        "failed_checks": [
            check for check in report["checks"] if not check["passed"]
        ],
        "reproducibility": report["reproducibility"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def _cmd_migrate(args: argparse.Namespace) -> int:
    document = load_annotations(args.input)
    migrated = migrate_annotations(
        document, args.to, allow_loss=args.allow_loss
    )
    text = render_annotations(migrated, args.to)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text, encoding="utf-8")
    result = {
        "business_codes": ["OK"],
        "source_version": document.version,
        "target_version": args.to,
        "output": args.out,
        "preview": text.splitlines()[:4],
        "allow_loss": args.allow_loss,
        "losses": migrated.losses,
        "completion_count": len(migrated.completions),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="layout-order",
        description="Deterministic layout reading-order analysis",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="generate deterministic samples and snapshots"
    )
    generate.add_argument("--out", default="artifacts/data")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--pages", type=int, default=5)
    generate.add_argument("--snapshot-dir", default="artifacts/snapshots")
    generate.set_defaults(func=_cmd_generate)

    run = subparsers.add_parser("run", help="run a full analysis report")
    run.add_argument("--samples", default="artifacts/data/samples.jsonl")
    run.add_argument(
        "--config", default="artifacts/data/dataset_config.json"
    )
    run.add_argument("--cache-dir", default="artifacts/cache")
    run.add_argument("--out", default="artifacts/report.json")
    run.add_argument("--mode", default="baseline")
    run.set_defaults(func=_cmd_run)

    self_check = subparsers.add_parser(
        "self-check", help="run baseline, incremental and invariant checks"
    )
    self_check.add_argument("--out", default="artifacts/selfcheck_report.json")
    self_check.add_argument("--workdir", default="artifacts")
    self_check.add_argument("--seed", type=int, default=DEFAULT_SEED)
    self_check.add_argument("--pages", type=int, default=5)
    self_check.set_defaults(func=_cmd_self_check)

    migrate = subparsers.add_parser(
        "migrate", help="migrate one annotation document"
    )
    migrate.add_argument("--input", required=True)
    migrate.add_argument("--to", required=True, choices=["v1", "v2", "v3"])
    migrate.add_argument("--out", required=True)
    migrate.add_argument("--allow-loss", action="store_true")
    migrate.set_defaults(func=_cmd_migrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LayoutError as exc:
        out = getattr(args, "out", None)
        return _emit_error(exc, out)


if __name__ == "__main__":
    sys.exit(main())

