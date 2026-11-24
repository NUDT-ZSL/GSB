"""Command line entry points for generation, demo, and local verification."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .engine import EngineConfig, MFAEngine, load_engine_from_samples
from .errors import Codes, MFAError
from .locking import FileLock
from .reporting import write_report
from .samplegen import generate_samples


def _root(value: str) -> Path:
    return Path(value).resolve()


def _clean_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _normalized_results(report: Any) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for result in report.results:
        value = result.to_dict()
        for key in ("latency_ms", "ledger_watermark"):
            value.pop(key, None)
        comparisons.append(value)
    return comparisons


def _status_reason_ok(
    result: dict[str, Any], expected: dict[str, str]
) -> bool:
    if result["status"] != expected["status"]:
        return False
    reason = expected.get("reason")
    if reason and reason not in result["reasons"]:
        return False
    return True


def _config(
    *,
    concurrency: int = 4,
    timeout_ms: int | None = 5_000,
    queue_limit: int = 64,
    auto_recover: bool = True,
) -> EngineConfig:
    return EngineConfig(
        canonical_scale=6,
        fingerprint_threshold_micro=5_000,
        concurrency=concurrency,
        timeout_ms=timeout_ms,
        queue_limit=queue_limit,
        default_timestamp_error_us=500,
        auto_recover=auto_recover,
    )


def command_generate(args: argparse.Namespace) -> int:
    result = generate_samples(_root(args.root), seed=args.seed)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_demo(args: argparse.Namespace) -> int:
    root = _root(args.root)
    generate_samples(root, seed=args.seed)
    demo_work = root / "work" / "demo"
    _clean_directory(demo_work)
    engine = load_engine_from_samples(
        root / "samples",
        ledger_path=demo_work / "mfa_ledger.sqlite",
        work_dir=demo_work,
        config=_config(
            concurrency=args.concurrency,
            timeout_ms=args.timeout_ms,
            queue_limit=args.queue_limit,
        ),
    )
    try:
        preview_attempt = next(
            item for item in engine.initial_attempts if item.attempt_no == 1001
        )
        preview = engine.preview(
            {
                "attempt_no": preview_attempt.attempt_no,
                "user_id": preview_attempt.user_id,
                "factor_id": preview_attempt.factor_id,
                "totp_code": preview_attempt.totp_code,
                "device_components": list(preview_attempt.device_components),
                "fingerprint_scale": preview_attempt.fingerprint_scale,
                "risk_score_millis": preview_attempt.risk_score_millis,
                "submitted_at_ms": preview_attempt.submitted_at_ms,
                "timestamp_error_ms": preview_attempt.timestamp_error_us / 1000,
            }
        )
        batch = engine.process_batch()
        report = engine.write_report(batch, root / "reports" / "decision_report.json")
        batch_1001 = next(
            item for item in report["results"] if item["attempt_no"] == 1001
        )
    finally:
        engine.close()
    summary = {
        "decisions": report["counts"],
        "policy_version": report["policy"]["used_policy_version"],
        "policy_generation": report["policy"]["policy_generation"],
        "snapshot_replaced": report["policy"]["snapshot_replaced"],
        "preview_status": preview.status,
        "preview_matches_batch": (
            preview.status == batch_1001["status"]
            and preview.reasons == batch_1001["reasons"]
            and preview.policy_version == batch_1001["policy_version"]
        ),
        "report": str(root / "reports" / "decision_report.json"),
        "checkpoint": report.get("checkpoint"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    root = _root(args.root)
    generated = generate_samples(root, seed=args.seed)
    expected = generated["expectations"]
    verify_root = root / "work" / "verify"
    _clean_directory(verify_root)
    checks: list[dict[str, Any]] = []
    failed = False

    # A: baseline deterministic batch.
    a_dir = verify_root / "a"
    a_dir.mkdir(parents=True)
    engine_a = load_engine_from_samples(
        root / "samples",
        ledger_path=a_dir / "mfa_ledger.sqlite",
        work_dir=a_dir,
        config=_config(concurrency=4, timeout_ms=None),
    )
    try:
        batch_a = engine_a.process_batch()
        report_a = engine_a.report_dict(batch_a)
        digest_a = engine_a.ledger.ledger_digest()
    finally:
        engine_a.close()

    scenario_failures = []
    by_attempt = {
        str(item["attempt_no"]): item
        for item in report_a["results"]
        if item["attempt_no"] is not None
    }
    for attempt_no, expectation in expected.items():
        if not isinstance(expectation, dict) or "status" not in expectation:
            continue
        actual = by_attempt.get(attempt_no)
        if actual is None or not _status_reason_ok(actual, expectation):
            scenario_failures.append(
                {
                    "attempt_no": attempt_no,
                    "expected": expectation,
                    "actual": actual,
                }
            )
    expected_parse_codes = {
        (item["record_index"], item["code"])
        for item in expected.get("parse_rejections", [])
    }
    actual_parse_codes = {
        (item["record_index"], item["code"])
        for item in report_a["rejects"]
        if item["source"] == "attempts"
    }
    if not expected_parse_codes.issubset(actual_parse_codes):
        scenario_failures.append(
            {
                "expected_parse_rejections": sorted(expected_parse_codes),
                "actual_parse_rejections": sorted(actual_parse_codes),
            }
        )
    replay = by_attempt.get("1006", {})
    if not (
        replay.get("replay", {}).get("first_attempt_no") == 1001
        and replay.get("replay", {}).get("second_attempt_no") == 1006
    ):
        scenario_failures.append({"replay_trace": replay.get("replay")})
    checks.append(
        {
            "name": "scenario_expectations",
            "passed": not scenario_failures,
            "failures": scenario_failures,
        }
    )
    failed = failed or bool(scenario_failures)

    # B: concurrency / arrival-order consistency.
    b_dir = verify_root / "b"
    b_dir.mkdir(parents=True)
    raw_attempts = list(engine_a.initial_attempts)
    raw_attempts.reverse()
    engine_b = load_engine_from_samples(
        root / "samples",
        ledger_path=b_dir / "mfa_ledger.sqlite",
        work_dir=b_dir,
        config=_config(concurrency=1, timeout_ms=None),
    )
    try:
        batch_b = engine_b.process_batch(raw_attempts)
        report_b = engine_b.report_dict(batch_b)
        normalized_a = _normalized_results(batch_a)
        normalized_b = _normalized_results(batch_b)
        consistency = (
            normalized_a == normalized_b
            and report_a["rejects"] == report_b["rejects"]
            and report_a["policy"]["used_policy_version"]
            == report_b["policy"]["used_policy_version"]
        )
        if not consistency:
            differences = []
            for index, (left, right) in enumerate(zip(normalized_a, normalized_b)):
                if left != right:
                    differences.append({"index": index, "a": left, "b": right})
            if len(normalized_a) != len(normalized_b):
                differences.append(
                    {
                        "length_a": len(normalized_a),
                        "length_b": len(normalized_b),
                    }
                )
            if report_a["rejects"] != report_b["rejects"]:
                differences.append(
                    {
                        "rejects_a": report_a["rejects"],
                        "rejects_b": report_b["rejects"],
                    }
                )
        else:
            differences = []
    finally:
        engine_b.close()
    checks.append(
        {
            "name": "batch_order_and_concurrency_consistency",
            "passed": consistency,
            "differences": differences,
        }
    )
    failed = failed or not consistency

    # C: forced-interruption recovery from the last complete checkpoint.
    c_dir = verify_root / "c"
    c_dir.mkdir(parents=True)
    engine_c = load_engine_from_samples(
        root / "samples",
        ledger_path=c_dir / "mfa_ledger.sqlite",
        work_dir=c_dir,
        config=_config(concurrency=2, timeout_ms=None),
    )
    try:
        sorted_attempts = sorted(
            engine_c.initial_attempts,
            key=lambda item: (
                item.submitted_at_ms,
                item.attempt_no,
                item.user_id,
                item.factor_id,
            ),
        )
        first_part = sorted_attempts[:5]
        second_part = sorted_attempts[5:]
        first_batch = engine_c.process_batch(first_part, checkpoint=True)
        # This simulates a process that committed the next batch and vanished
        # before writing its checkpoint.
        engine_c.process_batch(second_part, checkpoint=False)
        recovery = engine_c.recover()
        restored_batch = engine_c.process_batch(second_part, checkpoint=True)
        final_digest = engine_c.ledger.ledger_digest()
        recovered_results = (
            [item.to_dict() for item in first_batch.results]
            + [item.to_dict() for item in restored_batch.results]
        )
    finally:
        engine_c.close()
    for item in recovered_results:
        item.pop("latency_ms", None)
        item.pop("ledger_watermark", None)
    recovery_consistent = (
        recovery.get("status") == "RECOVERED_HALF_WRITE"
        and recovery.get("half_write_detected") is True
        and final_digest == digest_a
        and recovered_results == _normalized_results(batch_a)
    )
    checks.append(
        {
            "name": "interruption_recovery",
            "passed": recovery_consistent,
            "recovery": recovery,
            "final_digest": final_digest,
            "baseline_digest": digest_a,
        }
    )
    failed = failed or not recovery_consistent

    # D: lock conflict must return E_LOCK without touching the ledger.
    lock_path = c_dir / ".checkpoint.lock"
    lock_error: dict[str, Any] | None = None
    try:
        with FileLock(lock_path):
            try:
                engine_d = load_engine_from_samples(
                    root / "samples",
                    ledger_path=c_dir / "mfa_ledger.sqlite",
                    work_dir=c_dir,
                    config=_config(auto_recover=False),
                )
                try:
                    engine_d.checkpoints.write_checkpoint(
                        batch_seq=999_999,
                        watermark=0,
                        policy_version=engine_d.policy.policy_version,
                    )
                finally:
                    engine_d.close()
            except MFAError as exc:
                lock_error = exc.to_dict()
    except OSError as exc:
        lock_error = {
            "code": Codes.ATOMIC_WRITE,
            "message": str(exc),
        }
    lock_ok = bool(lock_error and lock_error.get("code") == Codes.LOCK)
    checks.append(
        {
            "name": "checkpoint_lock_conflict",
            "passed": lock_ok,
            "error": lock_error,
        }
    )
    failed = failed or not lock_ok

    summary = {
        "format": "mfa-verification-summary-v1",
        "seed": args.seed,
        "generated_at_wall_ms": time.time_ns() // 1_000_000,
        "passed": not failed,
        "checks": checks,
        "baseline_counts": report_a["counts"],
        "baseline_policy_version": report_a["policy"]["used_policy_version"],
        "baseline_report": str(root / "reports" / "decision_report.json"),
    }
    write_report(root / "reports" / "verification_report.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mfa-guard",
        description="Offline deterministic multi-factor authentication adjudicator",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="workspace root containing samples, work, and reports",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic samples")
    generate.add_argument("--seed", type=int, default=20_261_125)
    generate.set_defaults(func=command_generate)

    demo = subparsers.add_parser("demo", help="run the reproducible sample batch")
    demo.add_argument("--seed", type=int, default=20_261_125)
    demo.add_argument("--concurrency", type=int, default=4)
    demo.add_argument("--timeout-ms", type=int, default=5_000)
    demo.add_argument("--queue-limit", type=int, default=64)
    demo.set_defaults(func=command_demo)

    verify = subparsers.add_parser(
        "verify", help="run consistency, recovery, and lock self-checks"
    )
    verify.add_argument("--seed", type=int, default=20_261_125)
    verify.set_defaults(func=command_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MFAError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

