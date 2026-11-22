from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import digest_json
from .errors import DiagnosticError
from .samples import generate_samples
from .service import DiagnosticService
from .validation import load_json


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _service(args: argparse.Namespace) -> DiagnosticService:
    return DiagnosticService(args.db)


def _connection_id(args: argparse.Namespace, patch: dict[str, Any] | None = None) -> str:
    connection_id = getattr(args, "connection_id", None)
    if connection_id:
        return connection_id
    if patch and patch.get("connection_id"):
        return str(patch["connection_id"])
    raise ValueError("--connection-id is required")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="h2diag",
        description="Deterministic HTTP/2 multiplexing state and recovery diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    samples_parser = subparsers.add_parser("generate-samples")
    samples_parser.add_argument("--out-dir", default="samples")

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--db", required=True)
    init_parser.add_argument("--scenario", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--db", required=True)
    run_parser.add_argument("--scenario")
    run_parser.add_argument("--connection-id")
    run_parser.add_argument("--max-frames", type=int)
    run_parser.add_argument("--until-clock", type=int)
    run_parser.add_argument("--patch-version", type=int)

    patch_parser = subparsers.add_parser("patch")
    patch_parser.add_argument("--db", required=True)
    patch_parser.add_argument("--patch", required=True)
    patch_parser.add_argument("--connection-id")

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--db", required=True)
    resume_parser.add_argument("--connection-id", required=True)
    resume_parser.add_argument("--max-frames", type=int)
    resume_parser.add_argument("--until-clock", type=int)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--db", required=True)
    status_parser.add_argument("--connection-id", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--db", required=True)
    verify_parser.add_argument("--connection-id", required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--db", required=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    demo_parser = subparsers.add_parser("demo")
    demo_parser.add_argument("--db", default="artifacts/demo.db")
    demo_parser.add_argument("--samples", default="samples")
    demo_parser.add_argument("--report", default="artifacts/consistency-report.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-samples":
            _print(generate_samples(args.out_dir))
            return 0
        if args.command == "init":
            service = _service(args)
            result = service.initialize_from_file(args.scenario)
            _print({"ok": True, **result})
            return 0
        if args.command == "run":
            service = _service(args)
            if args.scenario:
                initialization = service.initialize_from_file(args.scenario)
                connection_id = initialization["connection_id"]
            elif args.connection_id:
                connection_id = args.connection_id
            else:
                raise ValueError("--scenario or --connection-id is required")
            result = service.run(
                connection_id,
                max_frames=args.max_frames,
                until_clock=args.until_clock,
                expected_patch_version=args.patch_version,
            )
            _print({"ok": True, **result})
            return 0
        if args.command == "patch":
            service = _service(args)
            patch = load_json(args.patch, kind="patch")
            connection_id = _connection_id(args, patch)
            result = service.apply_patch(connection_id, patch)
            _print({"ok": True, **result})
            return 0
        if args.command == "resume":
            service = _service(args)
            result = service.resume(
                args.connection_id,
                max_frames=args.max_frames,
                until_clock=args.until_clock,
            )
            _print({"ok": True, **result})
            return 0
        if args.command == "status":
            _print(_service(args).status(args.connection_id))
            return 0
        if args.command == "verify":
            report = _service(args).verify(args.connection_id)
            _print(report)
            return 0 if report["ok"] else 1
        if args.command == "serve":
            if args.host not in {"127.0.0.1", "::1", "localhost"}:
                raise ValueError("FastAPI diagnostics must bind to a loopback address")
            import uvicorn

            from .api import create_app

            uvicorn.run(create_app(args.db), host=args.host, port=args.port)
            return 0
        if args.command == "demo":
            return _run_demo(args)
        parser.error("unknown command")
        return 2
    except DiagnosticError as exc:
        _print({"ok": False, "error": exc.as_dict()})
        return 2
    except (OSError, ValueError, KeyError) as exc:
        _print({"ok": False, "error": {"code": "CLI_001", "message": str(exc)}})
        return 2


def _run_demo(args: argparse.Namespace) -> int:
    samples = Path(args.samples)
    manifest = generate_samples(samples)
    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()
    lock_path = db_path.with_name(db_path.name + ".lock")
    if lock_path.exists():
        lock_path.unlink()
    service = DiagnosticService(db_path)
    scenario = load_json(samples / "scenario.json", kind="scenario")
    initialized = service.initialize(scenario)
    connection_id = initialized["connection_id"]
    first = service.run(connection_id, max_frames=12)
    patch = load_json(samples / "patches" / "settings-weight-v2.json", kind="patch")
    patched = service.apply_patch(connection_id, patch)
    resumed = service.resume(connection_id)
    report = service.verify(connection_id)
    end_scenario = load_json(samples / "end_stream_scenario.json", kind="scenario")
    end_initialized = service.initialize(end_scenario)
    end_connection_id = end_initialized["connection_id"]
    service.run(end_connection_id)
    end_patch = load_json(samples / "patches" / "end-stream-v2.json", kind="patch")
    end_patch_result = service.apply_patch(end_connection_id, end_patch)
    end_report = service.verify(end_connection_id)
    result = {
        "ok": report["ok"] and end_report["ok"],
        "samples_manifest_digest": manifest["manifest_digest"],
        "main": {
            "connection_id": connection_id,
            "frames_before_patch": first["processed"],
            "patch": {
                "version": patched["version"],
                "kind": patched["kind"],
                "invalidated": patched["manifest"]["invalidated"],
            },
            "resumed_frames": resumed["processed"],
            "status": service.status(connection_id),
            "verify": report,
        },
        "end_stream": {
            "connection_id": end_connection_id,
            "patch": {
                "version": end_patch_result["version"],
                "kind": end_patch_result["kind"],
                "invalidated": end_patch_result["manifest"]["invalidated"],
            },
            "status": service.status(end_connection_id),
            "verify": end_report,
        },
    }
    result["report_digest"] = digest_json(result)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
