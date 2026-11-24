from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json
from .codes import E_LOCK
from .generator import SEED, generate_invalid_sample, generate_sample
from .locking import LockConflict, WorkDirLock
from .parser import parse_bundle
from .pipeline import run_audit
from .store import CheckpointMismatch
from .verifier import verify_against_truth


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _lock_error(exc: LockConflict) -> int:
    _print_json(
        {
            "ok": False,
            "error": {
                "code": E_LOCK,
                "message": str(exc),
                "lock_path": str(exc.path),
            },
        }
    )
    return 3


def cmd_generate(args: argparse.Namespace) -> int:
    result = generate_sample(args.out, seed=args.seed)
    invalid_result = None
    if args.with_invalid:
        invalid_out = args.invalid_out or str(Path(args.out).parent / "invalid_sample")
        invalid_result = generate_invalid_sample(invalid_out)
    _print_json({"ok": True, "sample": result, "invalid_sample": invalid_result})
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    bundle = parse_bundle(args.sample)
    payload = {
        "ok": True,
        "counts": {
            "principals": len(bundle.principals),
            "objects": len(bundle.objects),
            "aces": len(bundle.aces),
            "requests": len(bundle.requests),
            "issues": len(bundle.issues),
        },
        "issues": [issue.to_dict() for issue in bundle.issues],
    }
    if args.out:
        atomic_write_json(args.out, payload)
    _print_json({"ok": True, **payload["counts"], "out": args.out})
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    try:
        result = run_audit(
            args.sample,
            args.workdir,
            crash_after_objects=args.crash_after_objects,
            lock_timeout_ms=args.lock_timeout_ms,
        )
    except LockConflict as exc:
        return _lock_error(exc)
    except CheckpointMismatch as exc:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                },
            }
        )
        return 5
    report = result["report"]
    _print_json(
        {
            "ok": True,
            "report_path": result["report_path"],
            "recovery": result["recovery"],
            "summary": report["summary"],
        }
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    result = verify_against_truth(args.truth, args.report, output_path=args.out)
    _print_json({"ok": result["ok"], "summary": result["summary"], "output": args.out})
    return 0 if result["ok"] else 4


def cmd_lock(args: argparse.Namespace) -> int:
    try:
        with WorkDirLock(args.workdir, timeout_ms=0):
            print(json.dumps({"ok": True, "code": "LOCK_READY", "workdir": args.workdir}), flush=True)
            time.sleep(args.hold_ms / 1000.0)
    except LockConflict as exc:
        return _lock_error(exc)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="perm_audit", description="NTFS-style permission audit pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate deterministic local samples")
    gen.add_argument("--out", default="samples/deterministic")
    gen.add_argument("--seed", type=int, default=SEED)
    gen.add_argument("--with-invalid", action="store_true")
    gen.add_argument("--invalid-out", default=None)
    gen.set_defaults(func=cmd_generate)

    parse = sub.add_parser("parse", help="strictly parse manifests and emit rejection records")
    parse.add_argument("--sample", default="samples/deterministic")
    parse.add_argument("--out", default=None)
    parse.set_defaults(func=cmd_parse)

    audit = sub.add_parser("audit", help="parse, decide, persist ledger/checkpoint, and write report")
    audit.add_argument("--sample", default="samples/deterministic")
    audit.add_argument("--workdir", default="runs/default")
    audit.add_argument("--crash-after-objects", type=int, default=None)
    audit.add_argument("--lock-timeout-ms", type=int, default=0)
    audit.set_defaults(func=cmd_audit)

    recover = sub.add_parser("recover", help="recover from last complete checkpoint and finish audit")
    recover.add_argument("--sample", default="samples/deterministic")
    recover.add_argument("--workdir", default="runs/default")
    recover.add_argument("--crash-after-objects", type=int, default=None)
    recover.add_argument("--lock-timeout-ms", type=int, default=0)
    recover.set_defaults(func=cmd_audit)

    verify = sub.add_parser("verify", help="compare report to synchronous truth by sync marker")
    verify.add_argument("--truth", default="samples/deterministic/truth.jsonl")
    verify.add_argument("--report", default="runs/default/report.json")
    verify.add_argument("--out", default="runs/default/verification.json")
    verify.set_defaults(func=cmd_verify)

    lock = sub.add_parser("lock", help="hold the workdir lock (test helper)")
    lock.add_argument("--workdir", required=True)
    lock.add_argument("--hold-ms", type=int, default=3000)
    lock.set_defaults(func=cmd_lock)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
