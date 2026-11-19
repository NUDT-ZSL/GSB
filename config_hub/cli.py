"""Command-line interface for serve/apply/compute/resume/status/verify."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import CanonicalJSONError, stable_json_text
from .engine import ConfigHub
from .errors import ConfigHubError, bad_config, bad_input
from .validation import (
    load_input,
    load_patch,
    load_tolerance,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "config_hub.db"
DEFAULT_SNAPSHOT = PROJECT_ROOT / "samples" / "config_snapshot.json"
DEFAULT_INPUT = PROJECT_ROOT / "samples" / "input_records.json"
DEFAULT_TOLERANCE = PROJECT_ROOT / "samples" / "tolerance.json"


def _emit(value: Any, *, compact: bool = False) -> None:
    if compact:
        sys.stdout.write(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        )
    else:
        sys.stdout.write(stable_json_text(value))


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument(
        "--snapshot",
        default=str(DEFAULT_SNAPSHOT),
        help="initial configuration snapshot JSON",
    )
    parser.add_argument(
        "--tolerance-file",
        default=str(DEFAULT_TOLERANCE),
        help="verification tolerance JSON",
    )
    parser.add_argument(
        "--compact", action="store_true", help="emit compact stable JSON"
    )


def _ensure_initialized(args: argparse.Namespace) -> ConfigHub:
    tolerance = load_tolerance(args.tolerance_file)
    hub = ConfigHub(args.db)
    initialized = hub.initialize(args.snapshot)
    if (
        tolerance["clock_rollback_tolerance_ms"]
        != initialized["clock_rollback_tolerance_ms"]
    ):
        raise bad_config(
            "tolerance file clock_rollback_tolerance_ms differs from active config",
            {
                "file_value": tolerance["clock_rollback_tolerance_ms"],
                "config_value": initialized["clock_rollback_tolerance_ms"],
            },
        )
    return hub


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="configctl.py",
        description="Local configuration hot-update engine and deterministic pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize the database")
    _add_common(init_parser)

    serve_parser = subparsers.add_parser("serve", help="run the loopback HTTP API")
    _add_common(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    apply_parser = subparsers.add_parser("apply", help="validate and apply a patch")
    _add_common(apply_parser)
    apply_parser.add_argument("patch_file")
    apply_parser.add_argument("--now-ms", type=int, required=True)

    compute_parser = subparsers.add_parser(
        "compute", help="run one deterministic client round"
    )
    _add_common(compute_parser)
    compute_parser.add_argument("--client", required=True)
    compute_parser.add_argument("--round", type=int, required=True)
    compute_parser.add_argument("--now-ms", type=int, required=True)
    compute_parser.add_argument("--input", default=str(DEFAULT_INPUT))
    compute_parser.add_argument("--output", help="write the bound result JSON here")

    resume_parser = subparsers.add_parser(
        "resume", help="replay prepared patches and incomplete computes"
    )
    _add_common(resume_parser)

    status_parser = subparsers.add_parser(
        "status", help="print stable current state and client bindings"
    )
    _add_common(status_parser)

    verify_parser = subparsers.add_parser(
        "verify", help="verify chain, state, and replay result versions"
    )
    _add_common(verify_parser)
    verify_parser.add_argument(
        "--replay-version", type=int, help="recompute results for this config version"
    )
    verify_parser.add_argument(
        "--input",
        help="input for replay when no stored result matches the selection",
    )
    verify_parser.add_argument("--client", help="filter/synthesize replay by client")
    verify_parser.add_argument(
        "--round", type=int, dest="round_no", help="filter/synthesize replay by round"
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "serve":
        from .server import create_app, validate_loopback_host

        validate_loopback_host(args.host)
        hub = _ensure_initialized(args)
        app = create_app(hub)
        app.run(
            host=args.host,
            port=args.port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
        return 0

    if args.command == "init":
        tolerance = load_tolerance(args.tolerance_file)
        hub = ConfigHub(args.db)
        result = hub.initialize(args.snapshot)
        if (
            tolerance["clock_rollback_tolerance_ms"]
            != result["clock_rollback_tolerance_ms"]
        ):
            raise bad_config(
                "tolerance file clock_rollback_tolerance_ms differs from active config",
                {
                    "file_value": tolerance["clock_rollback_tolerance_ms"],
                    "config_value": result["clock_rollback_tolerance_ms"],
                },
            )
        _emit(result, compact=args.compact)
        return 0

    hub = _ensure_initialized(args)
    if args.command == "apply":
        patch = load_patch(args.patch_file)
        result = hub.apply_patch(patch, args.now_ms)
        _emit(result, compact=args.compact)
        return 0
    if args.command == "compute":
        normalized_input = load_input(args.input)
        result = hub.compute(args.client, args.round, args.now_ms, normalized_input)
        if args.output:
            hub.write_json_file(args.output, result["result"])
        _emit(result, compact=args.compact)
        return 0
    if args.command == "resume":
        _emit(hub.resume(), compact=args.compact)
        return 0
    if args.command == "status":
        _emit(hub.status(), compact=args.compact)
        return 0
    if args.command == "verify":
        tolerance = load_tolerance(args.tolerance_file)
        replay_input = None
        if args.input is not None:
            replay_input = load_input(args.input)
        report = hub.verify(
            replay_version=args.replay_version,
            replay_input=replay_input,
            replay_client=args.client,
            replay_round=args.round_no,
            abs_tolerance=tolerance["verify_float_abs_tolerance"],
            rel_tolerance=tolerance["verify_float_rel_tolerance"],
        )
        _emit(report, compact=args.compact)
        return 0 if report["ok"] else 1
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ConfigHubError as exc:
        _emit(exc.to_dict(), compact=getattr(args, "compact", False))
        return exc.exit_code
    except CanonicalJSONError as exc:
        error = bad_input(f"invalid JSON value: {exc}")
        _emit(error.to_dict(), compact=getattr(args, "compact", False))
        return error.exit_code
    except BrokenPipeError:
        return 0
    except Exception as exc:
        error = ConfigHubError(
            "E_INTERNAL",
            f"unexpected internal error: {exc}",
            http_status=500,
            exit_code=10,
        )
        _emit(error.to_dict(), compact=getattr(args, "compact", False))
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
