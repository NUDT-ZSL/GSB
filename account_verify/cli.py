from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .codes import E_LOCK, E_PARAM, OK
from .engine import Engine
from .errors import CrashInjected, LockError, VerificationError
from .integrity import verify_samples_against_truth
from .samplegen import generate_sample_tree


def _print(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _error_payload(exc: VerificationError) -> Dict[str, Any]:
    return {"ok": False, **exc.to_dict()}


def _cmd_generate(args: argparse.Namespace) -> int:
    hashes = generate_sample_tree(Path(args.output))
    _print({"ok": True, "code": OK, "output": args.output, "files": len(hashes)})
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    with Engine(args.state, lock_timeout_seconds=args.lock_timeout) as engine:
        result = engine.import_samples(Path(args.samples))
        integrity = verify_samples_against_truth(Path(args.samples))
        result["input_integrity"] = integrity
        result["ok"] = not result["rejections"] and integrity.get("unchanged", True)
        result["code"] = OK if result["ok"] else "E_PARTIAL_IMPORT"
        _print(result)
        return 0 if result["ok"] else 1


def _cmd_migrate(args: argparse.Namespace) -> int:
    with Engine(args.state, lock_timeout_seconds=args.lock_timeout) as engine:
        if args.upgrade_policy:
            if not engine.ledger.latest_checkpoint():
                manifest = engine.require_bundle().get("manifest", {})
                engine.adjudicate(batch_size=int(manifest.get("default_batch_size", args.batch_size)))
            result = engine.apply_policy_upgrade(Path(args.upgrade_policy))
            _print({"ok": True, "code": OK, **result})
            return 0

        result = engine.adjudicate(
            batch_size=args.batch_size,
            crash_after_batch=args.crash_after_batch,
            crash_phase=args.crash_phase,
        )
        bundle = engine.require_bundle()
        view = engine.policy_view(bundle)
        conflict_count = len(view.conflict_report())
        result["policy_conflicts"] = conflict_count
        result["ok"] = conflict_count == 0 and len(bundle.get("rejections", [])) == 0
        result["code"] = OK if result["ok"] else "E_PARTIAL_ADJUDICATION"
        _print(result)
        return 0 if result["ok"] else 1


def _cmd_use_token(args: argparse.Namespace) -> int:
    with Engine(args.state, lock_timeout_seconds=args.lock_timeout) as engine:
        result = engine.consume_token(args.token_id)
        result["ok"] = result.get("code") == OK
        _print(result)
        return 0 if result["ok"] else 1


def _cmd_export_report(args: argparse.Namespace) -> int:
    with Engine(args.state, lock_timeout_seconds=args.lock_timeout) as engine:
        samples_dir = Path(args.samples) if args.samples else Path(engine.require_bundle().get("samples_dir", "samples"))
        integrity = verify_samples_against_truth(samples_dir)
        report = engine.write_report(Path(args.output), input_integrity=integrity)
        denied_tokens = [token for token in report["tokens"] if token.get("code") != OK]
        partial = (
            report["summary"]["rejection_count"] > 0
            or report["summary"]["policy_conflict_count"] > 0
            or bool(denied_tokens)
            or not integrity.get("unchanged", False)
        )
        output = {
            "ok": not partial,
            "code": OK if not partial else "E_PARTIAL_REPORT_FINDINGS",
            "report_path": args.output,
            "summary": report["summary"],
            "input_integrity": integrity,
            "denied_token_count": len(denied_tokens),
        }
        _print(output)
        return 0 if not partial else 1


def _cmd_verify_checkpoint(args: argparse.Namespace) -> int:
    with Engine(args.state, lock_timeout_seconds=args.lock_timeout) as engine:
        checkpoint = engine.ledger.latest_checkpoint()
        verification = engine.ledger.verify_digest(checkpoint["ledger_digest"] if checkpoint else None)
        snapshot = None
        snapshot_error = None
        try:
            snapshot = engine.checkpoints.read_snapshot()
        except Exception as exc:  # recovery normally restores before command handler
            snapshot_error = str(exc)
        valid = bool(checkpoint and verification["valid"] and snapshot and snapshot["ledger_digest"] == checkpoint["ledger_digest"])
        payload = {
            "ok": valid,
            "code": OK if valid else "E_CHECKPOINT_CORRUPT",
            "checkpoint": checkpoint,
            "snapshot": snapshot,
            "snapshot_error": snapshot_error,
            "verification": verification,
            "recovery": engine.recovery,
        }
        _print(payload)
        return 0 if valid else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="account-verify",
        description="Deterministic offline credential migration and reset-token adjudication",
    )
    parser.add_argument("--state", default="state", help="local state directory")
    parser.add_argument("--lock-timeout", type=float, default=0.0, help="seconds to wait for a state lock")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=argparse.SUPPRESS, help="local state directory")
    common.add_argument("--lock-timeout", type=float, default=argparse.SUPPRESS, help="seconds to wait for a state lock")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-samples", parents=[common], help="generate deterministic local samples")
    generate.add_argument("--output", default="samples")
    generate.set_defaults(handler=_cmd_generate)

    imp = sub.add_parser("import", parents=[common], help="parse and import sample inputs")
    imp.add_argument("--samples", default="samples")
    imp.set_defaults(handler=_cmd_import)

    migrate = sub.add_parser("migrate", parents=[common], help="run migration and token adjudication batches")
    migrate.add_argument("--batch-size", type=int, default=2)
    migrate.add_argument("--upgrade-policy", default=None)
    migrate.add_argument("--crash-after-batch", type=int, help=argparse.SUPPRESS)
    migrate.add_argument("--crash-phase", default="before_snapshot", choices=("before_snapshot", "temp_residue", "after_snapshot"), help=argparse.SUPPRESS)
    migrate.set_defaults(handler=_cmd_migrate)

    use_token = sub.add_parser("use-token", parents=[common], help="consume one reset token exactly once")
    use_token.add_argument("token_id")
    use_token.set_defaults(handler=_cmd_use_token)

    report = sub.add_parser("export-report", parents=[common], help="export atomic JSON diagnostic report")
    report.add_argument("--output", default="reports/diagnostic-report.json")
    report.add_argument("--samples", default=None)
    report.set_defaults(handler=_cmd_export_report)

    verify = sub.add_parser("verify-checkpoint", parents=[common], help="verify SQLite and snapshot checkpoints")
    verify.set_defaults(handler=_cmd_verify_checkpoint)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except LockError as exc:
        _print({"ok": False, **exc.to_dict()})
        return 3
    except CrashInjected as exc:
        _print({"ok": False, "code": "E_CRASH_INJECTED", "message": str(exc)})
        return 137
    except VerificationError as exc:
        _print(_error_payload(exc))
        return 2 if exc.code == E_PARAM else 1
    except FileNotFoundError as exc:
        error = VerificationError("required file was not found", code=E_PARAM, details={"error": str(exc)})
        _print(_error_payload(error))
        return 2
    except Exception as exc:
        error = VerificationError("unexpected verification failure", code="E_STATE_CORRUPT", details={"type": type(exc).__name__, "error": str(exc)})
        _print(_error_payload(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
