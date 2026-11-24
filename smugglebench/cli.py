"""Command-line interface for validation, runs and incremental patches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .checkpoint import rules_basis_fingerprint
from .engine import RunConfig, Workbench
from .incremental import incremental_recompute
from .normalize import load_workspace
from .rules import merge_rule_view
from .util import BusinessError, WorkdirLock, atomic_write_json, read_json


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _run(args: argparse.Namespace) -> int:
    faults: dict[str, dict[str, Any]] = {}
    if args.echo_faults:
        loaded = read_json(args.echo_faults)
        if not isinstance(loaded, dict):
            raise BusinessError(
                "E_INPUT",
                "echo faults must be an object keyed by mutation_id",
            )
        faults = loaded
    config = RunConfig(
        concurrency=args.concurrency,
        connection_timeout_s=args.connection_timeout_s,
        max_messages_per_connection=args.max_messages,
        max_inflight_bytes=args.max_inflight_bytes,
        evaluation_ms=args.evaluation_ms,
        resume=not args.no_resume,
        echo_host=args.echo_host,
        echo_port=args.echo_port,
        start_echo_server=not args.external_echo,
        default_echo_delay_ms=args.delay_ms,
        echo_read_timeout_s=args.echo_read_timeout_s,
        te_cl_policy=args.te_cl_policy,
        duplicate_cl_policy=args.duplicate_cl_policy,
        chunk_policy=args.chunk_policy,
        trailer_policy=args.trailer_policy,
        echo_faults=faults,
    )
    report = Workbench(args.workspace, args.workdir, config).run()
    _print_json(
        {
            "report": str(Path(args.workdir).resolve() / "diagnostic_report.json"),
            "aggregate": report["deterministic_summary"]["aggregate"],
            "rejections": len(report["rejections"]),
            "checkpoint": report["checkpoint"],
        }
    )
    return 0


def _validate(args: argparse.Namespace) -> int:
    data = load_workspace(args.workspace)
    active, audit = merge_rule_view(data.rules, data.observed_clock_ms)
    _print_json(
        {
            "workspace": data.root,
            "observed_clock_ms": data.observed_clock_ms,
            "seed": data.sample_seed,
            "flows": len(data.flows),
            "mutations": len(data.mutations),
            "rules_total": len(data.rules),
            "rules_active": sorted(active),
            "rule_audit": audit,
            "rejections": [item.to_dict() for item in data.rejections],
            "source_files": data.source_files,
        }
    )
    return 0


def _patch(args: argparse.Namespace) -> int:
    data = load_workspace(args.workspace)
    patch_data = load_workspace(args.patch_workspace)
    baseline = read_json(args.baseline_report)
    if not isinstance(baseline, dict):
        raise BusinessError("E_INPUT", "baseline report must be a JSON object")
    result = incremental_recompute(
        data,
        baseline,
        patch_data.rules,
        evaluation_ms=args.evaluation_ms,
    )
    output = Path(args.output)
    with WorkdirLock(output.parent):
        atomic_write_json(output, result.report)
    _print_json(
        {
            "output": str(output.resolve()),
            "changed_rules": result.changed_rules,
            "affected_connections": result.affected_connections,
            "affected_mutations": result.affected_mutations,
            "verification": result.verification,
        }
    )
    return 0


def _basis(args: argparse.Namespace) -> int:
    data = load_workspace(args.workspace)
    _print_json(
        {
            "rules_basis": rules_basis_fingerprint(
                list(data.rules),
                observed_clock_ms=data.observed_clock_ms,
            ),
            "observed_clock_ms": data.observed_clock_ms,
        }
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="smugglebench",
        description="Offline HTTP request-smuggling state-machine workbench",
    )
    commands = root.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run the workbench")
    run.add_argument("--workspace", required=True, type=Path)
    run.add_argument("--workdir", default=Path(".workbench"), type=Path)
    run.add_argument("--concurrency", default=4, type=int)
    run.add_argument("--connection-timeout-s", default=5.0, type=float)
    run.add_argument("--max-messages", default=128, type=int)
    run.add_argument("--max-inflight-bytes", default=1024 * 1024, type=int)
    run.add_argument("--evaluation-ms", default=None, type=int)
    run.add_argument("--delay-ms", default=0, type=int)
    run.add_argument("--echo-read-timeout-s", default=5.0, type=float)
    run.add_argument("--echo-host", default="127.0.0.1")
    run.add_argument("--echo-port", default=0, type=int)
    run.add_argument("--external-echo", action="store_true")
    run.add_argument(
        "--te-cl-policy",
        choices=("content-length", "chunked"),
        default="content-length",
    )
    run.add_argument(
        "--duplicate-cl-policy",
        choices=("first", "last"),
        default="last",
    )
    run.add_argument("--chunk-policy", choices=("lenient", "strict"), default="lenient")
    run.add_argument(
        "--trailer-policy",
        choices=("preserve", "ignore"),
        default="ignore",
    )
    run.add_argument("--echo-faults", type=Path)
    run.add_argument("--no-resume", action="store_true")
    run.set_defaults(handler=_run)

    validate = commands.add_parser("validate", help="strictly parse a sample workspace")
    validate.add_argument("--workspace", required=True, type=Path)
    validate.set_defaults(handler=_validate)

    patch = commands.add_parser("patch", help="incrementally re-adjudicate a rule patch")
    patch.add_argument("--workspace", required=True, type=Path)
    patch.add_argument("--patch-workspace", required=True, type=Path)
    patch.add_argument("--baseline-report", required=True, type=Path)
    patch.add_argument("--output", default=Path("patched_report.json"), type=Path)
    patch.add_argument("--evaluation-ms", default=None, type=int)
    patch.set_defaults(handler=_patch)

    basis = commands.add_parser("rules-basis", help="print the checkpoint rule basis")
    basis.add_argument("--workspace", required=True, type=Path)
    basis.set_defaults(handler=_basis)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except BusinessError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"code": "E_INPUT", "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
