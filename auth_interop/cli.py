from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from .adjudicate import adjudicate_attempts, load_attempts
from .checkpoint import CheckpointManager, CheckpointLockError
from .codes import EXIT_PARTIAL, EXIT_OK, EXIT_USAGE
from .graph import load_revocations, propagate_requests
from .io_utils import atomic_write_json, json_dumps, read_json_file
from .merge import MergedView
from .parser import load_clock
from .service import import_sample, run_batch_import, write_report


JsonDict = dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auth-interop",
        description="Offline two-generation auth-record merge and adjudication workbench.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", aliases=["import-samples"], help="parse, merge and checkpoint samples")
    imp.add_argument("--sample-dir", default="samples")
    imp.add_argument("--clock", default=None)
    imp.add_argument("--state-dir", default="state")
    imp.add_argument("--report", default="artifacts/reports/import-report.json")
    imp.add_argument("--force-rebuild", action="store_true")
    imp.add_argument("--batch-dir", default=None)
    imp.add_argument("--state-root", default="state/batch")
    imp.add_argument("--report-dir", default="artifacts/reports/batch")
    imp.set_defaults(handler=cmd_import)

    redeem = sub.add_parser("redeem", aliases=["adjudicate", "redeem-code"])
    redeem.add_argument("--state-dir", default="state")
    redeem.add_argument("--sample-dir", default="samples")
    redeem.add_argument("--clock", default="samples/clock.json")
    redeem.add_argument("--attempts", default="samples/redemption_attempts.json")
    redeem.add_argument("--report", default="artifacts/reports/redeem-report.json")
    redeem.set_defaults(handler=cmd_redeem)

    revoke = sub.add_parser("revoke", aliases=["revocation", "propagate-revocation"])
    revoke.add_argument("--state-dir", default="state")
    revoke.add_argument("--sample-dir", default="samples")
    revoke.add_argument("--clock", default="samples/clock.json")
    revoke.add_argument("--requests", default="samples/revocation_requests.json")
    revoke.add_argument("--report", default="artifacts/reports/revoke-report.json")
    revoke.set_defaults(handler=cmd_revoke)

    cp = sub.add_parser(
        "checkpoint",
        aliases=["checkpoint-verify", "verify-checkpoint"],
        help="verify SQLite and snapshot checkpoint integrity/versions",
    )
    cp.add_argument("action", nargs="?", choices=["verify"], default="verify")
    cp.add_argument("--state-dir", default="state")
    cp.add_argument("--report", default="artifacts/reports/checkpoint-verify.json")
    cp.set_defaults(handler=cmd_checkpoint)

    view = sub.add_parser("view", help="display unified token state, closures and conflicts")
    view.add_argument("--state-dir", default="state")
    view.add_argument("--sample-dir", default="samples")
    view.add_argument("--revocations", default=None)
    view.add_argument("--format", choices=["table", "json"], default="table")
    view.add_argument("--output", default=None)
    view.set_defaults(handler=cmd_view)

    generate = sub.add_parser("generate-samples", aliases=["samplegen"])
    generate.add_argument("--output", default="samples")
    generate.add_argument("--seed", type=int, default=20261125)
    generate.set_defaults(handler=cmd_generate)
    return parser


def _print_json(payload: JsonDict) -> None:
    print(json_dumps(payload), end="")


def _usage_error(message: str) -> int:
    _print_json({"code": "E_PARAMETER_OR_INPUT", "ok": False, "error": message})
    return EXIT_USAGE


def _load_checkpoint_view(
    state_dir: str, sample_dir: Optional[str] = None
) -> tuple[Optional[MergedView], JsonDict]:
    manager = CheckpointManager(state_dir)
    recovery = manager.recover()
    if recovery.get("checkpoint"):
        return MergedView.from_dict(recovery["checkpoint"]["view"]), recovery

    if sample_dir and Path(sample_dir).exists():
        report = import_sample(sample_dir, state_dir)
        recovery = report["checkpoint"]["verify"]
        recovered = manager.recover()
        if recovered.get("checkpoint"):
            return MergedView.from_dict(recovered["checkpoint"]["view"]), recovered
    return None, recovery


def cmd_generate(args: argparse.Namespace) -> int:
    from .samplegen import generate_samples

    path = generate_samples(args.output, args.seed)
    print(f"generated deterministic samples in {path.as_posix()}")
    return EXIT_OK


def cmd_import(args: argparse.Namespace) -> int:
    try:
        if args.batch_dir:
            report = run_batch_import(
                args.batch_dir,
                args.state_root,
                args.report_dir,
                force_rebuild=args.force_rebuild,
            )
            report_path = Path(args.report_dir) / "batch-summary.json"
            print(
                f"batch import: {report['succeeded']}/{report['total']} samples succeeded; "
                f"report={report_path.as_posix()}"
            )
            return EXIT_OK if report["all_succeeded"] else EXIT_PARTIAL

        report = import_sample(
            args.sample_dir,
            args.state_dir,
            clock_path=args.clock,
            force_rebuild=args.force_rebuild,
        )
        path = write_report(report, args.report)
        summary = report["summary"]
        print(
            "import complete: "
            f"entities={summary['entity_total']} rejections={summary['rejection_total']} "
            f"conflicts={summary['conflict_total']} pkce_findings={summary['pkce_finding_total']} "
            f"report={path.as_posix()}"
        )
        return EXIT_OK if report["ok"] else EXIT_PARTIAL
    except CheckpointLockError as exc:
        error_report = {"ok": False, "code": "E_LOCK", "error": str(exc)}
        try:
            write_report(error_report, args.report)
        except OSError:
            pass
        _print_json(error_report)
        return EXIT_PARTIAL
    except (OSError, ValueError) as exc:
        return _usage_error(str(exc))


def cmd_redeem(args: argparse.Namespace) -> int:
    try:
        clock_ms, _ = load_clock(args.clock)
        view, recovery = _load_checkpoint_view(args.state_dir, args.sample_dir)
        if view is None:
            return _usage_error(f"no usable checkpoint in {args.state_dir}: {recovery}")
        attempts = load_attempts(args.attempts)
        report = adjudicate_attempts(view, attempts, clock_ms)
        report["report_type"] = "redemption_report"
        report["state_dir"] = args.state_dir
        path = write_report(report, args.report)
        summary = report["summary"]
        print(
            f"redemption: allowed={summary['allowed']} denied={summary['denied']} "
            f"codes={json.dumps(summary['codes'], ensure_ascii=False)} report={path.as_posix()}"
        )
        return EXIT_OK if summary["all_succeeded"] else EXIT_PARTIAL
    except CheckpointLockError as exc:
        _print_json({"code": "E_LOCK", "ok": False, "error": str(exc)})
        return EXIT_PARTIAL
    except (OSError, ValueError) as exc:
        return _usage_error(str(exc))


def cmd_revoke(args: argparse.Namespace) -> int:
    try:
        _clock_ms, _ = load_clock(args.clock)
        view, recovery = _load_checkpoint_view(args.state_dir, args.sample_dir)
        if view is None:
            return _usage_error(f"no usable checkpoint in {args.state_dir}: {recovery}")
        requests = load_revocations(args.requests)
        report = propagate_requests(view, requests)
        report["report_type"] = "revocation_report"
        report["state_dir"] = args.state_dir
        path = write_report(report, args.report)
        summary = report["summary"]
        print(
            f"revocation: closure_size={summary['closure_size']} cycles={summary['cycle_count']} "
            f"report={path.as_posix()}"
        )
        return EXIT_OK if summary["all_succeeded"] else EXIT_PARTIAL
    except (OSError, ValueError) as exc:
        return _usage_error(str(exc))


def cmd_checkpoint(args: argparse.Namespace) -> int:
    try:
        manager = CheckpointManager(args.state_dir)
        result = manager.verify()
        if args.report:
            write_report(result, args.report)
        _print_json(result)
        if result.get("status") == "ok":
            return EXIT_OK
        if result.get("status") == "missing":
            return _usage_error(f"no checkpoint found under {args.state_dir}")
        return EXIT_PARTIAL
    except (OSError, ValueError) as exc:
        return _usage_error(str(exc))


def cmd_view(args: argparse.Namespace) -> int:
    try:
        view, recovery = _load_checkpoint_view(args.state_dir, args.sample_dir)
        if view is None:
            return _usage_error(f"no usable checkpoint in {args.state_dir}: {recovery}")

        payload: JsonDict = {
            "stats": view.stats(),
            "entities": [
                {
                    key: {
                        "status": entity.status,
                        "type": entity.entity_type,
                        "generation": entity.authoritative.generation
                        if entity.authoritative
                        else None,
                        "client_id": entity.authoritative.client_id
                        if entity.authoritative
                        else None,
                        "scope": entity.authoritative.scope if entity.authoritative else [],
                        "expires_at_ms": entity.authoritative.expires_at_ms
                        if entity.authoritative
                        else None,
                        "source": entity.authoritative.source_dict()
                        if entity.authoritative
                        else None,
                    }
                }
                for key, entity in sorted(view.entities.items())
            ],
            "conflicts": view.conflicts,
            "pkce_findings": view.pkce_findings,
        }
        if args.revocations:
            requests = load_revocations(args.revocations)
            payload["revocation_propagation"] = propagate_requests(view, requests)

        if args.output:
            write_report(payload, args.output)
        if args.format == "json":
            _print_json(payload)
        else:
            _print_table_view(payload)
        return EXIT_OK
    except (OSError, ValueError) as exc:
        return _usage_error(str(exc))


def _print_table_view(payload: JsonDict) -> None:
    stats = payload["stats"]
    print("Unified token-state view")
    print(
        f"  entities={stats['entity_total']} statuses={stats['statuses']} "
        f"conflicts={stats['conflict_total']} pkce_findings={stats['pkce_finding_total']}"
    )
    print("\nEntities:")
    for item in payload["entities"]:
        ((key, details),) = item.items()
        source = details.get("source") or {}
        print(
            f"  [{details['status']:<11}] {key} "
            f"gen={details['generation']} source={source.get('source_version')}#{source.get('record_no')}"
        )

    if payload["conflicts"]:
        print("\nConflicts:")
        for conflict in payload["conflicts"]:
            print(
                f"  {conflict['code']} {conflict['entity_key']} field={conflict['field']} "
                f"left={conflict['left']!r} right={conflict['right']!r}"
            )

    if payload["pkce_findings"]:
        print("\nPKCE lineage findings:")
        for finding in payload["pkce_findings"]:
            print(f"  {finding['code']} {finding['entity_key']} reason={finding['reason']}")

    revocation = payload.get("revocation_propagation")
    if revocation:
        print("\nInvalidation closure:")
        print(f"  keys={revocation['combined_closure_keys']}")
        if revocation["cycles"]:
            print(f"  cycles={revocation['cycles']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
