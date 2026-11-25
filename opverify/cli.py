from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .consistency import self_check_response
from .engine import run_unified
from .errors import OpVerifyError
from .ledger import CommitLock
from .samples import generate_sample_set, read_jsonl
from .service import VerificationService


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _generate(args: argparse.Namespace) -> int:
    manifest = generate_sample_set(args.out)
    _print({"ok": True, "sample": manifest})
    return 0


def _run(args: argparse.Namespace) -> int:
    service = VerificationService(args.root)
    report = service.run_manifest(
        args.manifest,
        batch_id=args.batch_id,
        crash_after_commits=args.crash_after_commits,
        crash_stage=args.crash_stage,
    )
    _print(report)
    return 0 if report.get("ok") else 2


def _check(args: argparse.Namespace) -> int:
    report = self_check_response(args.root, base_dir=args.sample)
    _print(report)
    return 0 if report.get("ok") else 3


def _validate_sample(args: argparse.Namespace) -> int:
    sample = Path(args.sample)
    cases = read_jsonl(sample / "invalid_cases.jsonl")
    results = []
    all_ok = True
    for case in cases:
        response = run_unified(
            case["record"],
            base_dir=sample,
            prior_revision=case.get("prior_revision"),
        )
        code = response.get("error", {}).get("code") if not response.get("ok") else "OK"
        passed = code == case["expect_code"]
        all_ok &= passed
        results.append(
            {
                "case": case["case"],
                "expected_code": case["expect_code"],
                "actual_code": code,
                "passed": passed,
                "job_id": response.get("error", {}).get("job_id"),
                "field": response.get("error", {}).get("field"),
            }
        )
    output = {"ok": all_ok, "results": results}
    _print(output)
    return 0 if all_ok else 4


def _race(args: argparse.Namespace) -> int:
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "race-ledger.lock"
    holder = CommitLock.acquire(lock_path)
    started = time.perf_counter()
    immediate_busy = None
    try:
        try:
            CommitLock.acquire(lock_path)
        except OpVerifyError as exc:
            immediate_busy = exc.to_dict()["error"]
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        holder.release()
    after = CommitLock.acquire(lock_path)
    after.release()
    report = {
        "ok": immediate_busy is not None and immediate_busy["code"] == "E_STAGE_BUSY",
        "first_holder": "ACQUIRED",
        "late_comer": immediate_busy,
        "late_comer_wait_ms": round(elapsed_ms, 4),
        "after_release": "ACQUIRED",
    }
    _print(report)
    return 0 if report["ok"] else 5


def _verify(args: argparse.Namespace) -> int:
    from .verification import run_verification

    report = run_verification(args.workspace)
    _print(report)
    return 0 if report.get("ok") else 6


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opverify", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate fixed-seed sample JSONL and matrices")
    generate.add_argument("--out", default="data/seed-sample")
    generate.set_defaults(func=_generate)

    run = sub.add_parser("run", help="run a task JSONL against a local service root")
    run.add_argument("--root", required=True)
    run.add_argument("--manifest", required=True)
    run.add_argument("--batch-id", default=None)
    run.add_argument("--crash-after-commits", type=int, default=None)
    run.add_argument("--crash-stage", default="chunk")
    run.set_defaults(func=_run)

    check = sub.add_parser("check", help="run cross-module consistency self-check")
    check.add_argument("--root", required=True)
    check.add_argument("--sample", default=None)
    check.set_defaults(func=_check)

    validate = sub.add_parser("validate-sample", help="verify declared validation error codes")
    validate.add_argument("--sample", default="data/seed-sample")
    validate.set_defaults(func=_validate_sample)

    race = sub.add_parser("race", help="demonstrate non-blocking ledger commit adjudication")
    race.add_argument("--root", required=True)
    race.set_defaults(func=_race)

    verify = sub.add_parser("verify", help="run full local acceptance workflow and write report")
    verify.add_argument("--workspace", default="runs/verification")
    verify.set_defaults(func=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
