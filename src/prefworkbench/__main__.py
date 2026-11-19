from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import read_json
from .config import validate_config
from .demo import run_demo
from .diagnostics import write_dashboard
from .engine import run_pipeline
from .errors import WorkbenchError
from .generator import (
    generate_workspace,
    mutate_samples,
    read_samples,
    write_config,
    write_jsonl,
)
from .selfcheck import run_selfcheck
from .server import serve


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _run_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": manifest["version"],
        "mode": manifest["mode"],
        "dataset_checksum": manifest["dataset_checksum"],
        "dataset_records_path": manifest["dataset_records_path"],
        "reused_nodes": manifest["reused_nodes"],
        "recomputed_nodes": manifest["recomputed_nodes"],
        "invalidated_nodes": manifest["invalidated_nodes"],
        "affected_nodes": manifest["affected_nodes"],
        "affected_pair_count": len(manifest["affected_pair_ids"]),
        "conflicts": len(manifest["conflicts"]),
        "underfilled_targets": len(manifest["underfilled_targets"]),
        "degradations": manifest["degradations"],
        "nodes": {
            node: {
                "cache_status": diag["cache_status"],
                "payload_hash": diag["payload_hash"],
                "cache_key": diag["cache_key"],
                "affected_pair_count": diag["affected_pair_count"],
            }
            for node, diag in manifest["nodes"].items()
        },
    }


def _cmd_generate(args: argparse.Namespace) -> int:
    groups = [item.strip() for item in args.groups.split(",") if item.strip()]
    result = generate_workspace(
        args.out,
        seed=args.seed,
        count=args.count,
        groups=groups,
        with_conflict=args.with_conflict,
        conflict_policy=args.conflict_policy,
    )
    _json_print(result)
    return 0


def _cmd_mutate(args: argparse.Namespace) -> int:
    samples, _ = read_samples(args.samples)
    mutated = mutate_samples(
        samples,
        pair_id=args.pair_id,
        annotator_group=args.annotator_group,
        chosen=args.chosen,
        rejected=args.rejected,
        quality_flag=args.quality_flag,
        chosen_suffix=args.chosen_suffix,
        rejected_suffix=args.rejected_suffix,
        bump_version=not args.no_version_bump,
    )
    digest = write_jsonl(args.out, mutated)
    _json_print({"out": str(args.out), "samples_sha256": digest, "count": len(mutated)})
    return 0


def _cmd_config_set(args: argparse.Namespace) -> int:
    config = read_json(args.config)
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    cursor = config
    parts = args.path.split(".")
    for part in parts[:-1]:
        cursor = cursor[part]
    cursor[parts[-1]] = value
    normalized = validate_config(config)
    digest = write_config(args.out, normalized)
    _json_print({"out": str(args.out), "config_sha256": digest, "path": args.path})
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    manifest = run_pipeline(
        samples_path=args.samples,
        config_path=args.config,
        stage_dir=args.stage,
        mode=args.mode,
        expected_base_version=args.base_version,
        requested_parallel_nodes=args.parallel_nodes,
    )
    summary = _run_summary(manifest)
    if args.json:
        _json_print(summary)
    else:
        print(
            f"v{summary['version']} {summary['mode']} | dataset {summary['dataset_checksum']} | "
            f"reused={','.join(summary['reused_nodes']) or '-'} | "
            f"recomputed={','.join(summary['recomputed_nodes']) or '-'}"
        )
        print(f"affected pairs={summary['affected_pair_count']} conflicts={summary['conflicts']}")
    return 0


def _cmd_selfcheck(args: argparse.Namespace) -> int:
    report = run_selfcheck(
        args.stage,
        version=args.version,
        raise_on_error=not args.no_raise,
    )
    if args.json:
        _json_print(report)
    else:
        print(f"selfcheck v{report['version']}: {'PASS' if report['ok'] else 'FAIL'}")
        for item in report["checks"]:
            print(f"  [{'ok' if item['ok'] else 'FAIL'}] {item['category']}: {item['message']}")
    return 0 if report["ok"] else 1


def _cmd_dashboard(args: argparse.Namespace) -> int:
    target = write_dashboard(args.stage, args.out, version=args.version)
    _json_print({"dashboard": str(target)})
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    report = run_demo(args.workspace)
    _json_print(
        {
            "ok": report["ok"],
            "checks": [
                {"name": item["name"], "ok": item["ok"]} for item in report["checks"]
            ],
            "dashboard": report["dashboard"],
            "final_stage": report["final_stage"],
        }
    )
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pref-workbench",
        description="Local deterministic preference-pair data preparation diagnostics workbench",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate deterministic samples and config")
    generate.add_argument("--out", required=True)
    generate.add_argument("--seed", type=int, default=20261120)
    generate.add_argument("--count", type=int, default=36)
    generate.add_argument("--groups", default="alpha,beta,gamma")
    generate.add_argument("--with-conflict", action="store_true")
    generate.add_argument("--conflict-policy", default="error")
    generate.set_defaults(func=_cmd_generate)

    mutate = sub.add_parser("mutate", help="apply a deterministic local annotator correction")
    mutate.add_argument("--samples", required=True)
    mutate.add_argument("--out", required=True)
    mutate.add_argument("--pair-id")
    mutate.add_argument("--annotator-group")
    mutate.add_argument("--chosen")
    mutate.add_argument("--rejected")
    mutate.add_argument("--quality-flag")
    mutate.add_argument("--chosen-suffix")
    mutate.add_argument("--rejected-suffix")
    mutate.add_argument("--no-version-bump", action="store_true")
    mutate.set_defaults(func=_cmd_mutate)

    config_set = sub.add_parser("config-set", help="set one dotted config path deterministically")
    config_set.add_argument("--config", required=True)
    config_set.add_argument("--out", required=True)
    config_set.add_argument("--path", required=True)
    config_set.add_argument("--value", required=True)
    config_set.set_defaults(func=_cmd_config_set)

    run = sub.add_parser("run", help="execute or incrementally update the DAG")
    run.add_argument("--samples", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--stage", required=True)
    run.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    run.add_argument("--base-version", type=int)
    run.add_argument("--parallel-nodes", type=int, default=1)
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=_cmd_run)

    selfcheck = sub.add_parser("selfcheck", help="run cross-module consistency checks")
    selfcheck.add_argument("--stage", required=True)
    selfcheck.add_argument("--version", type=int)
    selfcheck.add_argument("--no-raise", action="store_true")
    selfcheck.add_argument("--json", action="store_true")
    selfcheck.set_defaults(func=_cmd_selfcheck)

    dashboard = sub.add_parser("dashboard", help="write a standalone diagnostic HTML page")
    dashboard.add_argument("--stage", required=True)
    dashboard.add_argument("--out", required=True)
    dashboard.add_argument("--version", type=int)
    dashboard.set_defaults(func=_cmd_dashboard)

    server = sub.add_parser("serve", help="serve the local diagnostic dashboard")
    server.add_argument("--stage", required=True)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.set_defaults(func=lambda args: serve(args.stage, host=args.host, port=args.port))

    demo = sub.add_parser("demo", help="run the complete deterministic acceptance demo")
    demo.add_argument("--workspace", default=".")
    demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except WorkbenchError as exc:
        _json_print(exc.to_dict())
        return 2
    except FileNotFoundError as exc:
        print(json.dumps({"code": "E_INPUT", "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
