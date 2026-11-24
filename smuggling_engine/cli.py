"""Command-line entry points for sample generation and engine execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointLocked, CheckpointManager
from .engine import Engine, EngineConfig
from .generator import generate_sample, verify_report_against_truth
from .parsing import group_connections, parse_messages
from .rules import load_rule_view


def _write_json(path: str | Path | None, value: dict[str, Any]) -> None:
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    if path is None:
        sys.stdout.write(data)
    else:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(data, encoding="utf-8")


def _sample_paths(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        paths = manifest["paths"]
        return {
            "manifest": manifest,
            "messages": directory / paths["messages"],
            "v1": directory / paths["rules_v1"],
            "shards": [directory / item for item in paths["v2_shards"]],
            "database": directory / paths["v2_database"],
            "truth": directory / paths["sync_truth"],
        }
    return {
        "manifest": {},
        "messages": directory / "messages.jsonl",
        "v1": directory / "rules_v1.json",
        "shards": sorted((directory / "rules_v2").glob("*.json")),
        "database": directory / "rules_v2.sqlite3",
        "truth": directory / "sync_truth.json",
    }


def _run_identity(paths: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for key in ("messages", "v1", "database"):
        path = Path(paths[key])
        digest.update(key.encode("utf-8"))
        if path.exists():
            digest.update(path.read_bytes())
    for path in sorted(Path(item) for item in paths["shards"]):
        digest.update(str(path.name).encode("utf-8"))
        if path.exists():
            digest.update(path.read_bytes())
    manifest = paths.get("manifest", {})
    digest.update(str(manifest.get("seed", "")).encode("utf-8"))
    return digest.hexdigest()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smuggling-engine",
        description="Deterministic offline HTTP request-smuggling engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed fixtures")
    generate.add_argument("--out-dir", required=True)
    generate.add_argument("--seed", type=int, default=20261125)
    generate.add_argument(
        "--scenario", choices=("standard", "cycle"), default="standard"
    )

    run = subparsers.add_parser("run", help="analyze a generated sample")
    run.add_argument("--sample-dir", required=True)
    run.add_argument("--report", default=None)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument(
        "--timeout-ms",
        type=int,
        default=5000,
        help="real per-connection wall-clock budget; use -1 to disable",
    )
    run.add_argument("--max-messages", type=int, default=10000)
    run.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    run.add_argument("--checkpoint-dir", default=None)
    run.add_argument(
        "--rebuild",
        action="store_true",
        help="discard an existing checkpoint before running",
    )
    verify = subparsers.add_parser(
        "verify", help="compare a report with generator synchronization truth"
    )
    verify.add_argument("--sample-dir", required=True)
    verify.add_argument("--report", required=True)
    verify.add_argument("--out", default=None)
    return parser


def command_generate(arguments: argparse.Namespace) -> int:
    manifest = generate_sample(
        arguments.out_dir,
        seed=arguments.seed,
        scenario=arguments.scenario,
    )
    _write_json(None, manifest)
    return 0


def command_run(arguments: argparse.Namespace) -> int:
    paths = _sample_paths(Path(arguments.sample_dir))
    config = EngineConfig(
        concurrency=arguments.workers,
        per_connection_timeout_ms=(
            None if arguments.timeout_ms < 0 else arguments.timeout_ms
        ),
        max_messages_per_connection=(
            None if arguments.max_messages < 0 else arguments.max_messages
        ),
        max_bytes_per_connection=(
            None if arguments.max_bytes < 0 else arguments.max_bytes
        ),
    )
    messages, parse_rejects = parse_messages(paths["messages"])
    groups, _ = group_connections(
        messages, first_sequence=config.first_sequence
    )
    view = load_rule_view(
        v1_path=paths["v1"],
        v2_shards=paths["shards"],
        v2_database=paths["database"],
    )
    engine = Engine(config)
    try:
        if arguments.checkpoint_dir:
            manager = CheckpointManager(arguments.checkpoint_dir)
            if arguments.rebuild:
                manager.reset()
            report = engine.run_with_checkpoint(
                groups,
                view,
                manager,
                run_identity=_run_identity(paths),
                rejected=parse_rejects,
            )
        else:
            report = engine.run(groups, view, rejected=parse_rejects)
    except CheckpointLocked as exc:
        _write_json(
            arguments.report,
            {
                "schema_version": "1.0",
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                },
            },
        )
        return 4
    _write_json(arguments.report, report.to_dict())
    return 0


def command_verify(arguments: argparse.Namespace) -> int:
    paths = _sample_paths(Path(arguments.sample_dir))
    report = json.loads(Path(arguments.report).read_text(encoding="utf-8"))
    truth = json.loads(Path(paths["truth"]).read_text(encoding="utf-8"))
    result = verify_report_against_truth(report, truth)
    _write_json(arguments.out, result)
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "generate":
        return command_generate(arguments)
    if arguments.command == "run":
        return command_run(arguments)
    if arguments.command == "verify":
        return command_verify(arguments)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

