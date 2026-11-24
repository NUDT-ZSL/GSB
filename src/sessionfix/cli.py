from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .adjudication import (
    AdjudicationResult,
    RevocationRequest,
    adjudicate,
    decision_digest,
)
from .errors import SessionFixError
from .ledger import Ledger
from .locking import FileLock
from .models import NormalizedSample
from .parsing import discover_samples, parse_sample
from .reporting import build_report, write_report
from .samplegen import DEFAULT_SEED, generate_sample


EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ARGUMENT = 2


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _nonnegative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return result


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _lock_for(workdir: Path) -> FileLock:
    return FileLock(workdir.resolve() / "state" / ".sessionfix.lock")


def _state_workdirs(workdir: Path) -> list[Path]:
    workdir = workdir.resolve()
    samples_dir = workdir / "samples"
    if not samples_dir.is_dir():
        return [workdir]
    children = [
        path
        for path in sorted(samples_dir.iterdir())
        if path.is_dir()
    ]
    return children or [workdir]


def _batch_mode(workdir: Path) -> bool:
    samples_dir = workdir.resolve() / "samples"
    return samples_dir.is_dir() and any(path.is_dir() for path in samples_dir.iterdir())


def _crash_after_checkpoint(batch_number: int) -> None:
    raw = os.environ.get("SESSIONFIX_TEST_CRASH_AFTER_BATCH")
    if raw is None:
        return
    try:
        target = int(raw)
    except ValueError:
        return
    if batch_number >= target:
        os._exit(137)


def _summary_for(
    sample: NormalizedSample,
    result: AdjudicationResult,
    workdir: Path,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "sample_id": sample.sample_id,
        "workdir": str(workdir),
        "processed_events": result.processed_count,
        "total_events": result.total_event_count,
        "layout_version": None,
        "sessions": len(result.sessions),
        "repair_state_counts": _count(item.repair_state for item in result.sessions),
        "business_codes": list(result.business_codes),
        "rejections": [item.to_dict() for item in sample.rejections],
        "policy_conflicts": [
            item.to_dict() for item in sample.policy_conflicts
        ],
        "cycles": [item.to_dict() for item in sample.cycles],
        "decision_digest": decision_digest(sample, result),
    }
    payload.update(extra)
    return payload


def _count(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[str(value)] = result.get(str(value), 0) + 1
    return dict(sorted(result.items()))


def _import_one(sample_dir: Path, state_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        sample = parse_sample(sample_dir)
    except Exception as exc:
        return None, {
            "sample_dir": str(sample_dir),
            "code": getattr(exc, "code", "E_SAMPLE_PARSE"),
            "message": str(exc),
        }
    try:
        with _lock_for(state_dir):
            with Ledger(state_dir) as ledger:
                result = adjudicate(sample, 0)
                initialized = ledger.initialize(sample, result, force=True)
                summary = _summary_for(
                    sample,
                    result,
                    state_dir,
                    rebuilt=initialized["rebuilt"],
                    layout_version=initialized["layout_version"],
                    checkpoint=initialized["checkpoint"],
                )
                return summary, None
    except Exception as exc:
        return None, {
            "sample_dir": str(sample_dir),
            "code": getattr(exc, "code", "E_IMPORT"),
            "message": str(exc),
        }


def cmd_generate(args: argparse.Namespace) -> int:
    summary = generate_sample(
        args.output,
        seed=args.seed,
        sample_id=args.sample_id,
    )
    _print_json({"ok": True, "command": "generate", **summary})
    return EXIT_OK


def cmd_import(args: argparse.Namespace) -> int:
    sample_path = args.sample.resolve()
    samples = discover_samples(sample_path)
    if not samples:
        _print_json(
            {
                "ok": False,
                "command": "import",
                "code": "E_SAMPLE_PATH",
                "message": f"no sample manifest found under {sample_path}",
            }
        )
        return EXIT_PARTIAL
    batch = not (sample_path / "manifest.json").is_file()
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for sample_dir in samples:
        state_dir = (
            args.workdir.resolve() / "samples" / sample_dir.name
            if batch
            else args.workdir.resolve()
        )
        summary, failure = _import_one(sample_dir, state_dir)
        if summary is not None:
            results.append(summary)
        if failure is not None:
            failures.append(failure)
    partial = bool(failures) or any(
        bool(item.get("rejections"))
        or bool(item.get("policy_conflicts"))
        or bool(item.get("cycles"))
        for item in results
    )
    _print_json(
        {
            "ok": not partial,
            "partial_failure": partial,
            "command": "import",
            "batch": batch,
            "results": results,
            "failures": failures,
        }
    )
    return EXIT_PARTIAL if partial else EXIT_OK


def _resolve_sample_for_state(
    args: argparse.Namespace,
    state_dir: Path,
) -> NormalizedSample | None:
    if args.sample is not None:
        sample_path = args.sample.resolve()
        if (sample_path / "manifest.json").is_file():
            return parse_sample(sample_path)
        candidates = {
            path.name: path for path in discover_samples(sample_path)
        }
        sample_dir = candidates.get(state_dir.name)
        if sample_dir is None:
            raise SessionFixError(
                "E_SAMPLE_PATH",
                f"cannot map state directory to a sample: {state_dir.name}",
            )
        return parse_sample(sample_dir)
    return None


def _adjudicate_one(
    state_dir: Path,
    *,
    sample: NormalizedSample | None,
    batch_size: int,
    restart: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        if sample is None and not (state_dir / "state" / "normalized.json").exists():
            raise SessionFixError(
                "E_STATE_MISSING",
                "normalized sample is not present; run import first",
                str(state_dir),
            )
        with _lock_for(state_dir):
            with Ledger(state_dir) as ledger:
                parsed = sample
                if parsed is None:
                    parsed = ledger.load_normalized()
                preparation = ledger.prepare_for_sample(parsed)
                requests = ledger.load_revocation_requests()
                if restart:
                    zero_result = adjudicate(parsed, 0, requests)
                    initialized = ledger.initialize(
                        parsed,
                        zero_result,
                        force=True,
                    )
                    batch_index = 0
                    processed = 0
                    recovery = {
                        "used_checkpoint_id": initialized["checkpoint"][
                            "checkpoint_id"
                        ],
                        "restored": False,
                        "half_write_detected": False,
                        "issues": [],
                        "temp_residue": [],
                        "start_processed_count": 0,
                        "start_batch_index": 0,
                    }
                else:
                    recovery = ledger.recover(parsed)
                    requests = ledger.load_revocation_requests()
                    processed = int(recovery["start_processed_count"])
                    batch_index = int(recovery["start_batch_index"])
                    if recovery["used_checkpoint_id"] is None:
                        ledger.apply_result(
                            parsed,
                            adjudicate(parsed, 0, requests),
                            batch_index=0,
                        )

                total = len(parsed.events)
                completed_batch = 0
                while processed < total:
                    next_processed = min(total, processed + batch_size)
                    batch_index += 1
                    result = adjudicate(parsed, next_processed, requests)
                    ledger.apply_result(parsed, result, batch_index=batch_index)
                    checkpoint = ledger.write_checkpoint(
                        sample_id=parsed.sample_id,
                        kind="adjudicate",
                        batch_index=batch_index,
                        processed_count=next_processed,
                        decision_watermark=result.decision_watermark,
                    )
                    processed = next_processed
                    completed_batch += 1
                    _crash_after_checkpoint(completed_batch)

                result = adjudicate(parsed, total, requests)
                if processed < total or ledger._meta().get(
                    "result_digest"
                ) != _result_digest(result):
                    ledger.apply_result(parsed, result, batch_index=batch_index)
                    checkpoint = ledger.write_checkpoint(
                        sample_id=parsed.sample_id,
                        kind="final",
                        batch_index=batch_index,
                        processed_count=total,
                        decision_watermark=result.decision_watermark,
                    )
                    _crash_after_checkpoint(completed_batch + 1)
                else:
                    rows = ledger.checkpoint_rows()
                    checkpoint = rows[0] if rows else None
                report = build_report(
                    parsed,
                    result,
                    ledger,
                    operation="adjudicate",
                    batch_size=batch_size,
                    recovery=recovery,
                    checkpoint=checkpoint,
                )
                write_report(ledger.report_path, report)
                summary = _summary_for(
                    parsed,
                    result,
                    state_dir,
                    layout_version=preparation["layout_version"],
                    rebuilt=preparation["rebuilt"],
                    recovery=recovery,
                    checkpoint=checkpoint,
                )
                return summary, None
    except Exception as exc:
        return None, {
            "workdir": str(state_dir),
            "code": getattr(exc, "code", "E_ADJUDICATE"),
            "message": str(exc),
        }


def _result_digest(result: AdjudicationResult) -> str:
    from .util import digest_json

    return digest_json(result.to_dict())


def cmd_adjudicate(args: argparse.Namespace) -> int:
    state_dirs = _state_workdirs(args.workdir)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    parsed_before: dict[str, NormalizedSample] = {}
    if args.sample is not None:
        sample_path = args.sample.resolve()
        if (sample_path / "manifest.json").is_file():
            try:
                parsed_before["__single__"] = parse_sample(sample_path)
            except Exception as exc:
                failures.append(
                    {
                        "sample_dir": str(sample_path),
                        "code": getattr(exc, "code", "E_SAMPLE_PARSE"),
                        "message": str(exc),
                    }
                )
        else:
            for sample_dir in discover_samples(sample_path):
                try:
                    parsed_before[sample_dir.name] = parse_sample(sample_dir)
                except Exception as exc:
                    failures.append(
                        {
                            "sample_dir": str(sample_dir),
                            "code": getattr(exc, "code", "E_SAMPLE_PARSE"),
                            "message": str(exc),
                        }
                    )
    for state_dir in state_dirs:
        sample = None
        if args.sample is not None:
            sample = parsed_before.get("__single__") or parsed_before.get(
                state_dir.name
            )
            if sample is None:
                failures.append(
                    {
                        "workdir": str(state_dir),
                        "code": "E_SAMPLE_PARSE",
                        "message": "sample parse failed; state was left unchanged",
                    }
                )
                continue
        summary, failure = _adjudicate_one(
            state_dir,
            sample=sample,
            batch_size=args.batch_size,
            restart=args.restart,
        )
        if summary is not None:
            results.append(summary)
        if failure is not None:
            failures.append(failure)
    partial = bool(failures) or any(
        bool(item.get("business_codes"))
        and (
            "E_POLICY_CONFLICT" in item["business_codes"]
            or "E_SESSION_CYCLE" in item["business_codes"]
        )
        or bool(item.get("rejections"))
        for item in results
    )
    _print_json(
        {
            "ok": not partial,
            "partial_failure": partial,
            "command": "adjudicate",
            "results": results,
            "failures": failures,
        }
    )
    return EXIT_PARTIAL if partial else EXIT_OK


def _revoke_one(
    state_dir: Path,
    request: RevocationRequest,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        if not (state_dir / "state" / "normalized.json").exists():
            raise SessionFixError(
                "E_STATE_MISSING",
                "normalized sample is not present; run import first",
                str(state_dir),
            )
        with _lock_for(state_dir):
            with Ledger(state_dir) as ledger:
                sample = ledger.load_normalized()
                meta = ledger._meta()
                processed = int(meta.get("processed_count", "0"))
                batch_index = int(meta.get("batch_index", "0"))
                requests = ledger.add_revocation_requests([request])
                result = adjudicate(sample, processed, requests)
                ledger.apply_result(sample, result, batch_index=batch_index)
                checkpoint = ledger.write_checkpoint(
                    sample_id=sample.sample_id,
                    kind="revoke",
                    batch_index=batch_index,
                    processed_count=processed,
                    decision_watermark=result.decision_watermark,
                )
                report = build_report(
                    sample,
                    result,
                    ledger,
                    operation="revoke",
                    checkpoint=checkpoint,
                )
                write_report(ledger.report_path, report)
                outcome = next(
                    item
                    for item in result.revocations
                    if item.request == request
                )
                return {
                    "sample_id": sample.sample_id,
                    "workdir": str(state_dir),
                    "revocation": outcome.to_dict(),
                    "checkpoint": checkpoint,
                }, None
    except Exception as exc:
        return None, {
            "workdir": str(state_dir),
            "code": getattr(exc, "code", "E_REVOKE"),
            "message": str(exc),
        }


def cmd_revoke(args: argparse.Namespace) -> int:
    if (args.session is None) == (args.login_seq is None):
        raise SessionFixError(
            "E_ARGUMENT",
            "exactly one of --session or --login-seq is required",
        )
    request = (
        RevocationRequest("session", args.session)
        if args.session is not None
        else RevocationRequest("login", str(args.login_seq))
    )
    state_dirs = _state_workdirs(args.workdir)
    if args.sample_id is not None:
        state_dirs = [
            path
            for path in state_dirs
            if path.name == args.sample_id
        ]
        if not state_dirs:
            _print_json(
                {
                    "ok": False,
                    "command": "revoke",
                    "code": "E_SAMPLE_ID",
                    "message": f"sample state not found: {args.sample_id}",
                }
            )
            return EXIT_PARTIAL
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for state_dir in state_dirs:
        summary, failure = _revoke_one(state_dir, request)
        if summary is not None:
            results.append(summary)
        if failure is not None:
            failures.append(failure)
    partial = bool(failures) or any(
        bool(item["revocation"]["codes"]) for item in results
    )
    _print_json(
        {
            "ok": not partial,
            "partial_failure": partial,
            "command": "revoke",
            "results": results,
            "failures": failures,
        }
    )
    return EXIT_PARTIAL if partial else EXIT_OK


def _report_one(
    state_dir: Path,
    *,
    output: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        if not (state_dir / "state" / "normalized.json").exists():
            raise SessionFixError(
                "E_STATE_MISSING",
                "normalized sample is not present; run import first",
                str(state_dir),
            )
        with _lock_for(state_dir):
            with Ledger(state_dir) as ledger:
                sample = ledger.load_normalized()
                processed = int(ledger._meta().get("processed_count", "0"))
                requests = ledger.load_revocation_requests()
                result = adjudicate(sample, processed, requests)
                validation = ledger.verify_checkpoints(sample)
                rows = ledger.checkpoint_rows()
                checkpoint = rows[0] if rows else None
                report = build_report(
                    sample,
                    result,
                    ledger,
                    operation="report",
                    checkpoint=checkpoint,
                    checkpoint_validation=validation,
                )
                if output is not None:
                    write_report(output, report)
                return report, None
    except Exception as exc:
        return None, {
            "workdir": str(state_dir),
            "code": getattr(exc, "code", "E_REPORT"),
            "message": str(exc),
        }


def cmd_report(args: argparse.Namespace) -> int:
    state_dirs = _state_workdirs(args.workdir)
    batch = _batch_mode(args.workdir)
    reports: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    output_base = args.output.resolve() if args.output is not None else None
    for state_dir in state_dirs:
        output = None
        if output_base is not None:
            output = (
                output_base / f"{state_dir.name}.report.json"
                if batch
                else output_base
            )
        report, failure = _report_one(state_dir, output=output)
        if report is not None:
            reports.append(report)
        if failure is not None:
            failures.append(failure)
    partial = bool(failures) or any(
        bool(report.get("rejections"))
        or "E_POLICY_CONFLICT" in report.get("business_codes", [])
        or "E_SESSION_CYCLE" in report.get("business_codes", [])
        or not report.get("ledger", {}).get("validation", {}).get("ok", True)
        for report in reports
    )
    if batch:
        payload = {
            "ok": not partial,
            "partial_failure": partial,
            "command": "report",
            "reports": reports,
            "failures": failures,
        }
    else:
        payload = reports[0] if reports else {
            "report_version": 1,
            "operation": "report",
            "sessions": [],
            "business_codes": [],
        }
        payload["ok"] = not partial
        payload["partial_failure"] = partial
        payload["failures"] = failures
    if output_base is not None:
        _print_json(
            {
                "ok": not partial,
                "partial_failure": partial,
                "command": "report",
                "output": str(output_base),
                "samples": len(reports),
                "failures": failures,
            }
        )
    else:
        _print_json(payload)
    return EXIT_PARTIAL if partial else EXIT_OK


def cmd_verify_checkpoint(args: argparse.Namespace) -> int:
    state_dirs = _state_workdirs(args.workdir)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for state_dir in state_dirs:
        try:
            if not (state_dir / "state" / "ledger.sqlite3").exists():
                raise SessionFixError(
                    "E_STATE_MISSING",
                    "ledger database not found",
                    str(state_dir),
                )
            with _lock_for(state_dir):
                with Ledger(state_dir) as ledger:
                    sample = ledger.load_normalized()
                    result = ledger.verify_checkpoints(sample)
                    result["workdir"] = str(state_dir)
                    results.append(result)
        except Exception as exc:
            failures.append(
                {
                    "workdir": str(state_dir),
                    "code": getattr(exc, "code", "E_VERIFY"),
                    "message": str(exc),
                }
            )
    ok = not failures and all(item["ok"] for item in results)
    _print_json(
        {
            "ok": ok,
            "command": "verify-checkpoint",
            "results": results,
            "failures": failures,
        }
    )
    return EXIT_OK if ok else EXIT_PARTIAL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sessionfix",
        description="离线会话固定缺陷裁决与检查点验证工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate",
        help="生成固定种子离线样本",
    )
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--sample-id")
    generate.set_defaults(func=cmd_generate)

    import_parser = subparsers.add_parser(
        "import",
        help="导入并严格解析样本，初始化账本",
    )
    import_parser.add_argument("--sample", type=Path, required=True)
    import_parser.add_argument("--workdir", type=Path, default=Path("."))
    import_parser.set_defaults(func=cmd_import)

    adjudicate_parser = subparsers.add_parser(
        "adjudicate",
        help="执行固定缺陷裁决与批次检查点",
    )
    adjudicate_parser.add_argument("--workdir", type=Path, default=Path("."))
    adjudicate_parser.add_argument("--sample", type=Path)
    adjudicate_parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=1000,
    )
    adjudicate_parser.add_argument("--restart", action="store_true")
    adjudicate_parser.set_defaults(func=cmd_adjudicate)

    revoke_parser = subparsers.add_parser(
        "revoke",
        help="撤销登录事件或会话并计算传递失效闭包",
    )
    revoke_parser.add_argument("--workdir", type=Path, default=Path("."))
    revoke_parser.add_argument("--session")
    revoke_parser.add_argument("--login-seq", type=_nonnegative_int)
    revoke_parser.add_argument("--sample-id")
    revoke_parser.set_defaults(func=cmd_revoke)

    report_parser = subparsers.add_parser(
        "report",
        help="导出确定性 JSON 诊断报告",
    )
    report_parser.add_argument("--workdir", type=Path, default=Path("."))
    report_parser.add_argument("--output", type=Path)
    report_parser.set_defaults(func=cmd_report)

    verify_parser = subparsers.add_parser(
        "verify-checkpoint",
        help="校验最后一个完整检查点与半写残留",
    )
    verify_parser.add_argument("--workdir", type=Path, default=Path("."))
    verify_parser.set_defaults(func=cmd_verify_checkpoint)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SessionFixError as exc:
        _print_json(
            {
                "ok": False,
                "command": getattr(args, "command", ""),
                "code": exc.code,
                "message": exc.message,
                "source": exc.source,
            }
        )
        if exc.code == "E_ARGUMENT":
            return EXIT_ARGUMENT
        return EXIT_PARTIAL
    except BrokenPipeError:
        return EXIT_PARTIAL
    except Exception as exc:
        _print_json(
            {
                "ok": False,
                "command": getattr(args, "command", ""),
                "code": "E_INTERNAL",
                "message": f"{type(exc).__name__}: {exc}",
                "source": None,
            }
        )
        return EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
