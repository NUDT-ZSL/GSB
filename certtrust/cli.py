"""Interactive command line interface.

Subcommands: import-snapshot, validate-chain, query, verify-checkpoint.
Exit codes: 0 = full success, 1 = partial failure (rejects, non-trusted
conclusions or operational errors such as E_LOCK), 2 = parameter error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import errors
from .anchors import parse_snapshot_files
from .checkpoint import atomic_write_json
from .engine import Engine
from .parsing import parse_cert_files, parse_chain_files
from .report import build_report, human_readable
from .validation import TRUSTED

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ARGS = 2


def _emit(obj):
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


def _error(code, message, **extra):
    record = {"ok": False, "code": code, "message": message}
    record.update(extra)
    _emit(record)


def _parse_inputs(args):
    certs, rejects = parse_cert_files(args.certs or [])
    claims, claim_rejects = parse_chain_files(args.chains or [], certs)
    rejects.extend(claim_rejects)
    snapshots, snap_rejects = parse_snapshot_files(args.snapshots or [])
    rejects.extend(snap_rejects)
    return certs, claims, snapshots, rejects


def _run_with_engine(args, action):
    engine = Engine(args.workdir)
    if getattr(args, "break_lock", False):
        try:
            os.unlink(engine.lock_path)
        except FileNotFoundError:
            pass
    try:
        with engine.lock():
            engine.load()
            return action(engine)
    except errors.CertTrustError as exc:
        _error(exc.code, exc.message, **exc.context)
        return EXIT_PARTIAL


def cmd_import_snapshot(args):
    certs, claims, snapshots, rejects = _parse_inputs(args)

    def action(engine):
        engine.import_certs(certs)
        engine.import_claims(claims)
        engine.import_snapshots(snapshots)
        engine.add_rejects(rejects)
        conclusions = []
        if engine.store().effective is not None and engine.state["claims"]:
            conclusions = engine.recompute(jobs=args.jobs)
        engine.save()
        effective = engine.store().effective
        _emit({
            "ok": not rejects,
            "imported": {
                "certs": len(certs),
                "claims": len(claims),
                "snapshots": len(snapshots),
            },
            "effective_anchor": None if effective is None else {
                "snapshot_version": effective.snapshot_version,
                "stable_key": effective.stable_key,
                "policy_version": effective.policy_version,
            },
            "rejects": rejects,
            "counters": engine.state["counters"],
            "conclusions": len(conclusions),
        })
        return EXIT_OK if not rejects else EXIT_PARTIAL

    return _run_with_engine(args, action)


def cmd_validate_chain(args):
    certs, claims, snapshots, rejects = _parse_inputs(args)

    def action(engine):
        engine.import_certs(certs)
        engine.import_claims(claims)
        engine.import_snapshots(snapshots)
        engine.add_rejects(rejects)
        conclusions = engine.recompute(jobs=args.jobs)
        engine.save()
        report = build_report(engine, conclusions)
        report_path = args.report or os.path.join(args.workdir, "report.json")
        atomic_write_json(report_path, report)
        print(human_readable(report))
        all_trusted = all(c.verdict == TRUSTED for c in conclusions)
        ok = not engine.state["rejects"] and all_trusted
        return EXIT_OK if ok else EXIT_PARTIAL

    return _run_with_engine(args, action)


def cmd_query(args):
    engine = Engine(args.workdir)
    if not engine.load():
        _error(errors.E_NO_CHECKPOINT,
               "no usable checkpoint; run import-snapshot first")
        return EXIT_PARTIAL
    conclusions = engine.conclusions()
    if args.claim:
        concl = conclusions.get(args.claim)
        if concl is None:
            _error(errors.E_ARGS, f"unknown claim id {args.claim!r}")
            return EXIT_ARGS
        _emit(concl.to_dict())
    else:
        _emit({
            "applied": engine.state["applied"],
            "policy_version": engine.state["policy_version"],
            "counters": engine.state["counters"],
            "conclusions": [conclusions[cid].to_dict() for cid in sorted(conclusions)],
        })
    return EXIT_OK


def cmd_verify_checkpoint(args):
    engine = Engine(args.workdir)
    status = engine.checkpoint_status()
    _emit(status)
    return EXIT_OK if status["ok"] else EXIT_PARTIAL


def build_parser():
    parser = argparse.ArgumentParser(
        prog="certtrust",
        description="local certificate chain & trust-anchor management tool")
    parser.add_argument("--workdir", default=".",
                        help="working directory holding the state/ folder")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_inputs(p):
        p.add_argument("--certs", nargs="*", default=[],
                       help="certificate description JSON files")
        p.add_argument("--chains", nargs="*", default=[],
                       help="chain claim JSON files")
        p.add_argument("--snapshots", nargs="*", default=[],
                       help="trust-anchor snapshot JSON files")
        p.add_argument("--jobs", type=int, default=1,
                       help="parallel validation workers (deterministic)")
        p.add_argument("--break-lock", action="store_true",
                       help="remove a stale lock left by a killed process")

    add_inputs(sub.add_parser("import-snapshot",
                              help="import snapshots/certs/chains into state"))
    validate = sub.add_parser("validate-chain",
                              help="validate all chain claims and write a report")
    add_inputs(validate)
    validate.add_argument("--report", default=None,
                          help="report output path (default <workdir>/report.json)")
    query = sub.add_parser("query", help="query stored conclusions")
    query.add_argument("--claim", default=None, help="claim id to query")
    sub.add_parser("verify-checkpoint", help="verify checkpoint integrity")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "jobs", 1) < 1:
        _error(errors.E_ARGS, "--jobs must be >= 1")
        return EXIT_ARGS
    handlers = {
        "import-snapshot": cmd_import_snapshot,
        "validate-chain": cmd_validate_chain,
        "query": cmd_query,
        "verify-checkpoint": cmd_verify_checkpoint,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
