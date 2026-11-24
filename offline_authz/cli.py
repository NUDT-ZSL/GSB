"""Command line interface for deterministic offline authorization."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .codes import AuthzParameterError, PreconditionError
from .service import (
    exit_code_for_payload,
    run_adjudicate,
    run_import,
    run_report,
    run_revoke,
    run_verify_checkpoint,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="offline-authz",
        description="Offline deterministic MFA authorization, ledger and recovery tool",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--input-dir", type=Path, default=Path("samples/generated"))
    common.add_argument("--state-dir", type=Path, default=Path("state"))
    common.add_argument("--output", type=Path)
    common.add_argument("--workers", type=int, default=1)
    common.add_argument("--watermark", type=int)

    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import", aliases=["import-claims", "导入"], parents=[common], help="parse and merge declarations"
    )
    import_parser.set_defaults(handler="import")

    adjudicate_parser = subparsers.add_parser(
        "adjudicate",
        aliases=["decide", "login", "adjudicate-login", "login-adjudicate", "登录裁决"],
        parents=[common],
        help="adjudicate a login batch and commit the ledger",
    )
    adjudicate_parser.add_argument("--rebuild", action="store_true", help="rebuild ledger from all input batches")
    adjudicate_parser.set_defaults(handler="adjudicate")

    revoke_parser = subparsers.add_parser(
        "revoke",
        aliases=["propagate", "revocation", "closure", "revoke-propagation", "吊销传播"],
        parents=[common],
        help="compute invalidation closure and rejudge consistency",
    )
    revoke_parser.set_defaults(handler="revoke")

    report_parser = subparsers.add_parser(
        "report",
        aliases=["diagnose", "diagnostic", "export", "export-report", "export-diagnostic-report", "导出诊断报告"],
        parents=[common],
        help="export parseable JSON diagnostics",
    )
    report_parser.set_defaults(handler="report")

    verify_parser = subparsers.add_parser(
        "verify-checkpoint",
        aliases=["verify", "checkpoint", "check", "check-checkpoint", "校验检查点"],
        parents=[common],
        help="verify SQLite/file checkpoint and ledger digest",
    )
    verify_parser.add_argument("--recover", action="store_true", help="recover to last complete checkpoint")
    verify_parser.set_defaults(handler="verify")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if getattr(args, "workers", 1) < 1:
            raise AuthzParameterError("--workers must be >= 1")
        if getattr(args, "watermark", None) is not None and args.watermark < 1:
            raise AuthzParameterError("--watermark must be positive")

        if args.handler == "import":
            payload = run_import(args.input_dir, args.state_dir, args.output, args.workers)
        elif args.handler == "adjudicate":
            payload = run_adjudicate(
                args.input_dir,
                args.state_dir,
                args.watermark,
                args.workers,
                args.rebuild,
                args.output,
            )
        elif args.handler == "revoke":
            payload = run_revoke(args.input_dir, args.state_dir, args.watermark, args.workers, args.output)
        elif args.handler == "report":
            payload = run_report(args.state_dir, args.output)
        elif args.handler == "verify":
            payload = run_verify_checkpoint(args.state_dir, args.recover)
        else:  # pragma: no cover - argparse enforces this
            parser.error("unknown command")
            return 2

        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return exit_code_for_payload(payload)
    except AuthzParameterError as exc:
        error_payload = {
            "status": "parameter_error",
            "code": exc.code,
            "message": str(exc),
        }
        print(json.dumps(error_payload, ensure_ascii=False, sort_keys=True, indent=2), file=sys.stdout)
        return 2
    except PreconditionError as exc:
        error_payload = {
            "status": "partial_failure",
            "code": exc.code,
            "message": str(exc),
            "details": exc.details,
        }
        print(json.dumps(error_payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 1
    except FileNotFoundError as exc:
        error_payload = {"status": "parameter_error", "code": "E_PARAMETER", "message": str(exc)}
        print(json.dumps(error_payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 2
    except Exception as exc:  # deterministic CLI boundary
        error_payload = {"status": "error", "code": "E_INTERNAL", "message": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(error_payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
